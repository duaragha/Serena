"""Exercise real manifest/IPA validation and retry boundaries without publishing."""

import base64
import copy
import hashlib
import io
import json
import plistlib
import sqlite3
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import agent_checkouts
from core import sidestore_publish as publish


def _ipa(bundle: str, version: str, build_number: str) -> bytes:
    info = {"CFBundleIdentifier": bundle, "CFBundleShortVersionString": version,
            "CFBundleVersion": build_number, "MinimumOSVersion": "16.4"}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Payload/App.app/Info.plist", plistlib.dumps(info))
    return out.getvalue()


def _build(app_id: str, bundle: str, version: str, build_number: str, ipa_name: str,
           commit: str, workflow: str = "ios-sidestore") -> tuple[dict, dict, bytes]:
    ipa = _ipa(bundle, version, build_number)
    manifest = {"bundleIdentifier": bundle, "version": version, "buildVersion": build_number,
                "commit": commit, "ipa": ipa_name, "size": len(ipa),
                "sha256": hashlib.sha256(ipa).hexdigest()}
    artifact = {"name": ipa_name, "version": version, "size": len(ipa),
                "url": f"https://api.codemagic.io/artifacts/{app_id}/ipa"}
    build = {"_id": f"build-{app_id}-{build_number}", "appId": app_id,
             "fileWorkflowId": workflow, "workflowId": None, "branch": "main",
             "status": "finished", "finishedAt": "2026-09-21T20:00:00Z",
             "commit": {"hash": commit},
             "artefacts": [artifact, {"name": f"{app_id}_{build_number}_artifacts.zip",
                                      "url": f"https://api.codemagic.io/artifacts/{app_id}/manifest"}]}
    return build, manifest, ipa


def _install_fakes(w, monkeypatch, repo: str = "owner/releases"):
    """Codemagic and GitHub doubles that keep state in ``w``."""

    feed_path = f"repos/{repo}/contents/sidestore-source.json"

    def download(url, **kwargs):
        if "/builds?" in url:
            app_id = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["appId"][0]
            return json.dumps({"builds": w.builds.get(app_id, [])}).encode()
        if url.startswith("https://api.codemagic.io/artifacts/"):
            app_id, kind = url.rsplit("/", 2)[-2:]
            if kind == "manifest":
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w") as z:
                    z.writestr("build/sidestore-build.json", json.dumps(w.manifests[app_id]))
                return out.getvalue()
            return w.ipas[app_id]
        if "releases/download" in url:
            asset = url.rsplit("/", 1)[-1]
            return w.public_ipa or w.uploaded[asset]
        if "raw.githubusercontent.com" in url:
            doc = copy.deepcopy(w.feed)
            if w.stale_public:
                for app in doc["apps"]:
                    app["versions"] = []
            return json.dumps(doc).encode()
        raise AssertionError(url)

    def gh(*args, **kwargs):
        w.calls.append(args)
        if args[:2] == ("api", feed_path):
            return SimpleNamespace(stdout=json.dumps({"sha": str(w.puts), "content":
                base64.b64encode(json.dumps(w.feed).encode()).decode()}), returncode=0)
        if args[:2] == ("release", "view"):
            tag = args[2]
            return SimpleNamespace(returncode=0 if tag in w.releases else 1,
                                   stdout=json.dumps({"assets": w.releases.get(tag, []),
                                                      "isDraft": False}))
        if args[:2] in (("release", "create"), ("release", "upload")):
            tag = args[2]
            assert "--clobber" not in args
            if args[1] == "create":
                assert "--latest=false" in args
                w.notes[tag] = Path(args[args.index("--notes-file") + 1]).read_text()
                w.titles[tag] = args[args.index("--title") + 1]
            ipa = Path(next(a for a in args if str(a).endswith(".ipa")))
            w.uploaded[ipa.name] = ipa.read_bytes()
            w.releases.setdefault(tag, []).append({"name": ipa.name, "size": ipa.stat().st_size})
            return SimpleNamespace(returncode=0, stdout="")
        if args[:3] == ("api", "--method", "PUT"):
            if w.fail_put:
                raise agent_checkouts.CheckoutError("simulated contents conflict")
            payload = json.loads(Path(args[args.index("--input") + 1]).read_text())
            assert payload["sha"] == str(w.puts)
            w.messages.append(payload["message"])
            w.feed = json.loads(base64.b64decode(payload["content"]))
            w.puts += 1
            return SimpleNamespace(returncode=0, stdout="{}")
        raise AssertionError(args)

    monkeypatch.setattr(publish, "_download", download)
    monkeypatch.setattr(publish, "_gh", gh)


class _World(SimpleNamespace):
    @property
    def release(self) -> bool:
        return bool(self.releases)


def _world(**fields) -> _World:
    base = dict(builds={}, manifests={}, ipas={}, releases={}, uploaded={}, notes={},
                titles={}, messages=[], calls=[], puts=0, fail_put=False,
                stale_public=False, public_ipa=None)
    base.update(fields)
    return _World(**base)


@pytest.fixture
def world(tmp_path, monkeypatch):
    bundle = "dev.unifiedinbox.mobile"
    build, manifest, ipa = _build("app", bundle, "0.1.92", "82",
                                  "unified-ios-unsigned.ipa", "a" * 40)
    build["_id"] = "build-92"
    rule = {"repo": "owner/releases", "bundle_id": bundle, "branch": "main"}
    w = _world(builds={"app": [build]}, build=build, manifest=manifest, ipa=ipa,
               manifests={"app": manifest}, ipas={"app": ipa}, rule=rule,
               feed={"apps": [{"name": "Unified", "bundleIdentifier": bundle, "versions": [
                   {"version": "0.1.91", "downloadURL": "https://old", "size": 9}
               ]}]})
    monkeypatch.setenv("SERENA_SIDESTORE_PUBLISH_DB", str(tmp_path / "receipts.db"))
    monkeypatch.setattr(agent_checkouts, "dispatch_config", lambda: {"ship": {"owner/source": {
        "codemagic_app_id": "app", "codemagic_workflow": "ios-sidestore", "sidestore": rule}}})
    _install_fakes(w, monkeypatch)
    return w


def test_finished_build_publishes_once_and_preserves_feed_history(world):
    receipt = publish.reconcile()[0]
    assert receipt["status"] == "published"
    # A single-app releases repo keeps exactly the names Unified always had.
    assert receipt["url"] == ("https://github.com/owner/releases/releases/download/"
                              "mobile-v0.1.92/Unified-0.1.92-ios-unsigned.ipa")
    assert world.notes == {"mobile-v0.1.92": "Unified iOS 0.1.92.\n"}
    assert world.titles == {"mobile-v0.1.92": "Unified iOS 0.1.92"}
    assert world.messages == ["chore(release): publish Unified iOS 0.1.92"]
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


SHARED_APPS = [
    # (source repo, Codemagic app, bundle, feed name, IPA version, build, artifact)
    ("duaragha/atrium", "atrium", "com.atrium.mobile", "Atrium", "14.22.33.7", "7",
     "Atrium-unsigned.ipa"),
    ("duaragha/vantage", "vantage", "com.raghav.vantage", "Vantage", "1.0.0.3", "3",
     "Vantage.ipa"),
    ("duaragha/openwhispr", "openwhispr", "sh.owhispr.app", "OpenWhispr", "2.8.1.12", "12",
     "OpenWhispr-unsigned.ipa"),
    ("duaragha/locket", "locket", "com.ragha.locket", "Locket", "1.27.0.90", "90",
     "Locket-unsigned.ipa"),
    ("duaragha/extra", "extra", "dev.example.extra", "Extra App", "3.1.4", "5",
     "extra.ipa"),
]


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """Five apps, one releases repo: more rules than the old cap of four."""
    w = _world(feed={"name": "Sideload", "apps": []})
    ship = {}
    for source, app_id, bundle, name, version, number, ipa_name in SHARED_APPS:
        build, manifest, ipa = _build(app_id, bundle, version, number, ipa_name,
                                      app_id[0] * 40, workflow=f"{app_id}-ios")
        w.builds[app_id], w.manifests[app_id], w.ipas[app_id] = [build], manifest, ipa
        w.feed["apps"].append({"name": name, "bundleIdentifier": bundle, "versions": []})
        ship[source] = {"codemagic_app_id": app_id, "codemagic_workflow": f"{app_id}-ios",
                        "sidestore": {"repo": "duaragha/sideload-releases",
                                      "bundle_id": bundle, "branch": "main"}}
    w.ship = ship
    monkeypatch.setenv("SERENA_SIDESTORE_PUBLISH_DB", str(tmp_path / "receipts.db"))
    monkeypatch.setattr(agent_checkouts, "dispatch_config", lambda: {"ship": w.ship})
    _install_fakes(w, monkeypatch, repo="duaragha/sideload-releases")
    return w


def _versions(feed: dict, bundle: str) -> list[str]:
    app = next(a for a in feed["apps"] if a["bundleIdentifier"] == bundle)
    return [v["version"] for v in app["versions"]]


def test_shared_feed_publishes_every_app_under_its_own_name_and_tag(shared):
    reports = publish.reconcile()
    assert [r["status"] for r in reports] == ["published"] * len(SHARED_APPS)
    assert set(shared.notes) == {"atrium-v14.22.33.7", "vantage-v1.0.0.3",
                                 "openwhispr-v2.8.1.12", "locket-v1.27.0.90",
                                 "extra-app-v3.1.4"}
    assert shared.notes["locket-v1.27.0.90"] == "Locket iOS 1.27.0.90.\n"
    assert shared.titles["openwhispr-v2.8.1.12"] == "OpenWhispr iOS 2.8.1.12"
    assert "chore(release): publish Atrium iOS 14.22.33.7" in shared.messages
    assert set(shared.uploaded) == {"Atrium-14.22.33.7-ios-unsigned.ipa",
                                    "Vantage-1.0.0.3-ios-unsigned.ipa",
                                    "OpenWhispr-2.8.1.12-ios-unsigned.ipa",
                                    "Locket-1.27.0.90-ios-unsigned.ipa",
                                    "Extra-App-3.1.4-ios-unsigned.ipa"}
    # Four-part stamps are published verbatim: SideStore compares them exactly.
    assert _versions(shared.feed, "com.ragha.locket") == ["1.27.0.90"]
    locket = next(a for a in shared.feed["apps"] if a["bundleIdentifier"] == "com.ragha.locket")
    assert locket["versions"][0]["downloadURL"] == (
        "https://github.com/duaragha/sideload-releases/releases/download/"
        "locket-v1.27.0.90/Locket-1.27.0.90-ios-unsigned.ipa")
    assert publish.reconcile() == []


def test_same_version_of_two_apps_never_shares_a_release(shared):
    for app_id in ("atrium", "vantage"):
        bundle = next(a[2] for a in SHARED_APPS if a[1] == app_id)
        name = next(a[6] for a in SHARED_APPS if a[1] == app_id)
        build, manifest, ipa = _build(app_id, bundle, "2.0.0.1", "1", name, app_id[0] * 40,
                                      workflow=f"{app_id}-ios")
        shared.builds[app_id], shared.manifests[app_id], shared.ipas[app_id] = (
            [build], manifest, ipa)
    publish.reconcile()
    assert shared.releases["atrium-v2.0.0.1"] == [
        {"name": "Atrium-2.0.0.1-ios-unsigned.ipa", "size": len(shared.ipas["atrium"])}]
    assert shared.releases["vantage-v2.0.0.1"] == [
        {"name": "Vantage-2.0.0.1-ios-unsigned.ipa", "size": len(shared.ipas["vantage"])}]


def test_rule_can_name_the_app_and_its_tag_prefix(shared):
    rule = shared.ship["duaragha/locket"]["sidestore"]
    rule.update(name="Locket Beta", tag_prefix="locket-ios-v")
    publish.reconcile()
    assert shared.notes["locket-ios-v1.27.0.90"] == "Locket Beta iOS 1.27.0.90.\n"
    assert "Locket-Beta-1.27.0.90-ios-unsigned.ipa" in shared.uploaded


@pytest.mark.parametrize("field,value", [("name", "../../etc"), ("tag_prefix", "Bad Tag"),
                                         ("tag_prefix", "-v")])
def test_unsafe_name_or_tag_prefix_is_refused(shared, field, value):
    shared.ship = {"duaragha/locket": shared.ship["duaragha/locket"]}
    shared.ship["duaragha/locket"]["sidestore"][field] = value
    assert publish.reconcile() == [{"source": "duaragha/locket", "status": "retry",
                                    "error": "SideStore app needs a plain name in the feed or rule"
                                    if field == "name" else
                                    "SideStore tag prefix must be lowercase letters, digits, or ._-"}]
    assert not shared.releases


@pytest.mark.parametrize("existing,expected", [
    ("1.27.0", "published"),      # a four-part build is newer than its three-part product
    ("1.27.0.89", "published"),
    ("1.27.0.91", "superseded"),
    ("1.28.0", "superseded"),
])
def test_three_and_four_part_versions_order_numerically(shared, existing, expected):
    shared.ship = {"duaragha/locket": shared.ship["duaragha/locket"]}
    app = next(a for a in shared.feed["apps"] if a["bundleIdentifier"] == "com.ragha.locket")
    app["versions"] = [{"version": existing, "downloadURL": "https://old", "size": 1}]
    assert publish.reconcile()[0]["status"] == expected


@pytest.mark.parametrize("value", ["1.2", "1.2.3.4.5", "1.2.x", ""])
def test_version_shapes_sidestore_cannot_verify_are_refused(value):
    with pytest.raises(publish.PublishError):
        publish._version(value)


def test_four_part_version_pads_three_part_for_ordering():
    assert publish._version("1.2.3") == publish._version("1.2.3.0") < publish._version("1.2.3.4")
