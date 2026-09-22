"""Exercise real manifest/IPA validation and retry boundaries without publishing."""

import base64
import copy
import hashlib
import io
import json
import plistlib
import sqlite3
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import agent_checkouts
from core import sidestore_publish as publish


@pytest.fixture
def world(tmp_path, monkeypatch):
    bundle = "dev.unifiedinbox.mobile"
    info = {"CFBundleIdentifier": bundle, "CFBundleShortVersionString": "0.1.92",
            "CFBundleVersion": "82", "MinimumOSVersion": "16.4"}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Payload/Unified.app/Info.plist", plistlib.dumps(info))
    ipa = out.getvalue()
    manifest = {"bundleIdentifier": bundle, "version": "0.1.92", "buildVersion": "82",
                "commit": "a" * 40, "ipa": "unified-ios-unsigned.ipa", "size": len(ipa),
                "sha256": hashlib.sha256(ipa).hexdigest()}
    artifact = {"name": manifest["ipa"], "version": "0.1.92", "size": len(ipa),
                "url": "https://api.codemagic.io/artifacts/ipa"}
    build = {"_id": "build-92", "appId": "app", "fileWorkflowId": "ios-sidestore",
             "workflowId": None, "branch": "main", "status": "finished",
             "finishedAt": "2026-09-21T20:00:00Z", "commit": {"hash": "a" * 40},
             "artefacts": [artifact, {"name": "app_82_artifacts.zip",
                                     "url": "https://api.codemagic.io/artifacts/manifest"}]}
    rule = {"repo": "owner/releases", "bundle_id": bundle, "branch": "main"}
    w = SimpleNamespace(builds=[build], build=build, manifest=manifest, ipa=ipa,
                        rule=rule, assets=[], release=False, calls=[], puts=0,
                        fail_put=False, stale_public=False, public_ipa=None,
                        feed={"apps": [{"bundleIdentifier": bundle, "versions": [
                            {"version": "0.1.91", "downloadURL": "https://old", "size": 9}
                        ]}]})
    monkeypatch.setenv("SERENA_SIDESTORE_PUBLISH_DB", str(tmp_path / "receipts.db"))
    monkeypatch.setattr(agent_checkouts, "dispatch_config", lambda: {"ship": {"owner/source": {
        "codemagic_app_id": "app", "codemagic_workflow": "ios-sidestore", "sidestore": rule}}})

    def download(url, **kwargs):
        if "/builds?" in url:
            return json.dumps({"builds": w.builds}).encode()
        if url.endswith("/manifest"):
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as z:
                z.writestr("build/sidestore-build.json", json.dumps(w.manifest))
            return out.getvalue()
        if url.endswith("/ipa"):
            return w.ipa
        if "releases/download" in url:
            return w.public_ipa or w.ipa
        if "raw.githubusercontent.com" in url:
            doc = copy.deepcopy(w.feed)
            if w.stale_public:
                doc["apps"][0]["versions"] = []
            return json.dumps(doc).encode()
        raise AssertionError(url)

    def gh(*args, **kwargs):
        w.calls.append(args)
        if args[:2] == ("api", "repos/owner/releases/contents/sidestore-source.json"):
            return SimpleNamespace(stdout=json.dumps({"sha": str(w.puts), "content":
                base64.b64encode(json.dumps(w.feed).encode()).decode()}), returncode=0)
        if args[:2] == ("release", "view"):
            return SimpleNamespace(returncode=0 if w.release else 1,
                                   stdout=json.dumps({"assets": w.assets, "isDraft": False}))
        if args[:2] in (("release", "create"), ("release", "upload")):
            w.release = True
            assert "--clobber" not in args
            if args[1] == "create":
                assert "--latest=false" in args
                assert Path(args[args.index("--notes-file") + 1]).read_text() == "Unified iOS 0.1.92.\n"
            w.assets = [{"name": "Unified-0.1.92-ios-unsigned.ipa", "size": len(w.ipa)}]
            return SimpleNamespace(returncode=0, stdout="")
        if args[:3] == ("api", "--method", "PUT"):
            if w.fail_put:
                raise agent_checkouts.CheckoutError("simulated contents conflict")
            payload = json.loads(Path(args[args.index("--input") + 1]).read_text())
            assert payload["sha"] == str(w.puts)
            w.feed = json.loads(base64.b64decode(payload["content"]))
            w.puts += 1
            return SimpleNamespace(returncode=0, stdout="{}")
        raise AssertionError(args)

    monkeypatch.setattr(publish, "_download", download)
    monkeypatch.setattr(publish, "_gh", gh)
    return w


def test_finished_build_publishes_once_and_preserves_feed_history(world):
    assert publish.reconcile()[0]["status"] == "published"
    entries = world.feed["apps"][0]["versions"]
    assert [v["version"] for v in entries] == ["0.1.92", "0.1.91"]
    assert entries[0]["minOSVersion"] == "16.4"
    assert entries[0]["size"] == len(world.ipa)
    assert publish.reconcile() == []
    assert world.puts == 1


@pytest.mark.parametrize("field,value", [("status", "failed"), ("status", "building"),
    ("branch", "feature"), ("fileWorkflowId", "other"), ("pullRequest", {"id": 4}),
    ("appId", "other")])
def test_unqualified_builds_never_publish(world, field, value):
    world.build[field] = value
    assert publish.reconcile()[0]["status"] == "waiting"
    assert not world.calls


@pytest.mark.parametrize("field,value", [("sha256", "bad"), ("size", 1),
    ("commit", "b" * 40), ("version", "0.1.90"), ("bundleIdentifier", "wrong"),
    ("buildVersion", "1")])
def test_manifest_or_ipa_mismatch_cannot_upload(world, field, value):
    world.manifest[field] = value
    assert publish.reconcile()[0]["status"] == "retry"
    assert not world.release


def test_feed_write_failure_retries_without_creating_another_release(world):
    world.fail_put = True
    assert publish.reconcile()[0]["status"] == "retry"
    world.fail_put = False
    assert publish.reconcile()[0]["status"] == "published"
    assert sum(c[:2] == ("release", "create") for c in world.calls) == 1
    assert world.puts == 1


def test_public_feed_must_be_visible_before_receipt_is_recorded(world):
    world.stale_public = True
    assert publish.reconcile()[0]["status"] == "retry"
    world.stale_public = False
    assert publish.reconcile()[0]["status"] == "published"
    assert world.puts == 1


def test_newer_feed_version_is_never_displaced(world):
    world.feed["apps"][0]["versions"].insert(0, {"version": "0.1.93"})
    assert publish.reconcile()[0]["status"] == "superseded"
    assert not world.release
    assert world.puts == 0


def test_same_size_conflicting_release_asset_is_not_overwritten(world):
    world.public_ipa = b"x" * len(world.ipa)
    assert publish.reconcile()[0]["status"] == "retry"
    assert world.puts == 0
    assert not any("--clobber" in c for c in world.calls)


def test_publication_runs_even_when_no_tasks_remain(world, monkeypatch):
    from core import scheduler_actions
    from memory import store
    monkeypatch.setattr(store, "tasks_in_state", lambda state: [])
    result = scheduler_actions.reconcile_fleet_tasks({})
    assert result.output["publications"][0]["status"] == "published"


def test_another_reconciler_cannot_publish_concurrently(world, tmp_path):
    assert publish.reconcile()[0]["status"] == "published"
    with sqlite3.connect(tmp_path / "receipts.db") as db:
        db.execute("BEGIN IMMEDIATE")
        assert publish.reconcile() == [{"status": "busy"}]


def test_newer_version_published_during_upload_is_preserved(world, monkeypatch):
    original = publish._gh

    def gh(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("release", "create"):
            world.feed["apps"][0]["versions"].insert(0, {"version": "0.1.93"})
        return result

    monkeypatch.setattr(publish, "_gh", gh)
    assert publish.reconcile()[0]["status"] == "superseded"
    assert world.puts == 0


def test_missing_manifest_never_uploads(world):
    world.build["artefacts"] = world.build["artefacts"][:1]
    assert publish.reconcile()[0]["status"] == "retry"
    assert not world.release


def test_artifact_redirect_drops_codemagic_credentials():
    req = urllib.request.Request("https://api.codemagic.io/artifacts/one",
                                 headers={"x-auth-token": "private"})
    redirect = publish._Redirect().redirect_request(
        req, None, 302, "Found", {}, "https://storage.example/file")
    assert not redirect.has_header("X-auth-token")


def test_download_rejects_foreign_host_before_attaching_credentials():
    with pytest.raises(publish.PublishError, match="unexpected Codemagic artifact host"):
        publish._download("https://other.example/file", authenticated=True)
