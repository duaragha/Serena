"""Finish configured Codemagic releases during the dispatcher's reconcile tick.

Codemagic's build history is the durable pending queue. Only successful builds
of the configured branch/workflow qualify, including builds started before a
dispatcher restart. The local ledger records verified publications, not starts.
GitHub assets are immutable here; contents-API SHA checks protect concurrent
feed edits. Nothing from the private source archive is published.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import plistlib
import re
import sqlite3
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path

from core import agent_checkouts

API = "https://api.codemagic.io"
FEED = "sidestore-source.json"


class PublishError(RuntimeError):
    pass


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            if urllib.parse.urlsplit(newurl).scheme != "https":
                raise PublishError("artifact redirected away from HTTPS")
            redirected.remove_header("X-auth-token")
        return redirected


def _download(url: str, *, authenticated: bool = False, limit: int = 512 << 20) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise PublishError("download requires HTTPS")
    headers = {}
    if authenticated:
        if parsed.hostname != "api.codemagic.io":
            raise PublishError("unexpected Codemagic artifact host")
        token = agent_checkouts._codemagic_token()
        if not token:
            raise PublishError("Codemagic API token is missing")
        headers["x-auth-token"] = token
    try:
        import certifi

        https = urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
        with urllib.request.build_opener(_Redirect(), https).open(
            urllib.request.Request(url, headers=headers), timeout=60
        ) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise PublishError(f"download returned HTTP {error.code}") from None
    except OSError as error:
        # Artifact URLs contain credentials. Never put them in the receipt.
        raise PublishError(f"download failed: {type(error).__name__}") from None
    if len(data) > limit:
        raise PublishError("download exceeds the configured size limit")
    return data


def _gh(*args: str, check: bool = True):
    return agent_checkouts._run(["gh", *args], check=check, timeout=180)


def _version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise PublishError("SideStore requires a numeric three-part app version")
    return tuple(map(int, value.split(".")))


def _feed(repo: str) -> tuple[dict, str]:
    data = json.loads(_gh("api", f"repos/{repo}/contents/{FEED}").stdout)
    return json.loads(base64.b64decode(data["content"])), data["sha"]


def _app(feed: dict, bundle: str) -> dict:
    apps = [app for app in feed.get("apps", []) if app.get("bundleIdentifier") == bundle]
    if len(apps) != 1:
        raise PublishError("feed must contain exactly one matching app bundle")
    return apps[0]


def _manifest(build: dict) -> dict:
    artifacts = build.get("artefacts", [])
    for artifact in artifacts:
        if Path(artifact["name"]).name == "sidestore-build.json":
            return json.loads(_download(artifact["url"], authenticated=True, limit=1 << 20))
    # Codemagic groups small artifacts into <app>_<build>_artifacts.zip.
    for artifact in artifacts:
        if artifact["name"].endswith("_artifacts.zip"):
            data = _download(artifact["url"], authenticated=True, limit=16 << 20)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                matches = [i for i in archive.infolist()
                           if Path(i.filename).name == "sidestore-build.json"]
                if len(matches) == 1 and matches[0].file_size <= 1 << 20:
                    return json.loads(archive.read(matches[0]))
    raise PublishError("successful build has no SideStore manifest")


def _verified_ipa(build: dict, artifact: dict, bundle: str) -> tuple[bytes, dict, str]:
    manifest = _manifest(build)
    version = str(artifact.get("version") or "")
    commit = (build.get("commit") or {}).get("hash")
    if (not commit or manifest.get("commit") != commit
            or manifest.get("version") != version
            or manifest.get("bundleIdentifier") != bundle
            or manifest.get("ipa") != artifact["name"]):
        raise PublishError("build manifest does not match the build, version, or app")
    data = _download(artifact["url"], authenticated=True)
    if (manifest.get("size") != len(data) or artifact.get("size") != len(data)
            or manifest.get("sha256") != hashlib.sha256(data).hexdigest()):
        raise PublishError("IPA size or checksum does not match its manifest")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [i for i in archive.infolist()
                 if re.fullmatch(r"Payload/[^/]+\.app/Info.plist", i.filename)]
        if len(names) != 1 or names[0].file_size > 1 << 20:
            raise PublishError("IPA must contain one application Info.plist")
        info = plistlib.loads(archive.read(names[0]))
    if (info.get("CFBundleIdentifier") != bundle
            or info.get("CFBundleShortVersionString") != version
            or str(info.get("CFBundleVersion")) != str(manifest.get("buildVersion"))):
        raise PublishError("IPA bundle, version, or build number differs from its manifest")
    minimum_os = str(info.get("MinimumOSVersion") or "")
    if not re.fullmatch(r"\d+(?:\.\d+){1,2}", minimum_os):
        raise PublishError("IPA has no valid minimum iOS version")
    return data, manifest, minimum_os


def _publish(build: dict, artifact: dict, rule: dict) -> dict:
    repo, bundle = rule["repo"], rule["bundle_id"]
    version = artifact["version"]
    feed, _ = _feed(repo)
    versions = _app(feed, bundle).get("versions", [])
    if any(_version(v["version"]) > _version(version) for v in versions):
        return {"status": "superseded", "version": version}

    data, manifest, minimum_os = _verified_ipa(build, artifact, bundle)
    tag = f"mobile-v{version}"
    name = f"Unified-{version}-ios-unsigned.ipa"
    url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    # Public notes are deliberately generic: private task briefs and source
    # commit messages can contain his contacts, infrastructure, or messages.
    notes = f"Unified iOS {version}."
    with tempfile.TemporaryDirectory(prefix="sidestore-publish-") as directory:
        root = Path(directory)
        ipa = root / name
        ipa.write_bytes(data)
        note_file = root / "notes.txt"
        note_file.write_text(notes + "\n", encoding="utf-8")
        release = _gh("release", "view", tag, "--repo", repo, "--json", "assets,isDraft",
                      check=False)
        if release.returncode:
            _gh("release", "create", tag, "--repo", repo, "--title", f"Unified iOS {version}",
                "--notes-file", str(note_file), "--latest=false", str(ipa))
        else:
            existing = json.loads(release.stdout)
            if existing.get("isDraft"):
                raise PublishError("release tag already belongs to a draft")
            assets = [a for a in existing["assets"] if a["name"] == name]
            if not assets:
                _gh("release", "upload", tag, str(ipa), "--repo", repo)
            elif len(assets) != 1 or assets[0]["size"] != len(data):
                raise PublishError("existing release asset conflicts with this build")
        # Also detects same-size but different content, without clobbering it.
        if hashlib.sha256(_download(url)).hexdigest() != manifest["sha256"]:
            raise PublishError("public release IPA differs from the verified build")

        # Re-read immediately before the compare-and-swap update. Another
        # publisher may have released a newer version during the upload.
        feed, sha = _feed(repo)
        app = _app(feed, bundle)
        versions = app.get("versions", [])
        if any(_version(v["version"]) > _version(version) for v in versions):
            return {"status": "superseded", "version": version}
        matching = [v for v in versions if v["version"] == version]
        if matching:
            if (len(matching) != 1 or matching[0].get("downloadURL") != url
                    or matching[0].get("size") != len(data)):
                raise PublishError("existing feed version conflicts with this build")
        else:
            app["versions"] = [{
                "version": version, "date": build["finishedAt"],
                "localizedDescription": notes, "downloadURL": url,
                "size": len(data), "minOSVersion": minimum_os,
            }, *versions]
            payload = root / "feed-update.json"
            payload.write_text(json.dumps({
                "message": f"chore(release): publish Unified iOS {version}", "sha": sha,
                "content": base64.b64encode(
                    (json.dumps(feed, indent=2) + "\n").encode()).decode(),
            }), encoding="utf-8")
            _gh("api", "--method", "PUT", f"repos/{repo}/contents/{FEED}",
                "--input", str(payload))
        public = json.loads(_download(
            f"https://raw.githubusercontent.com/{repo}/main/{FEED}", limit=8 << 20))
        entries = _app(public, bundle).get("versions", [])
        if not any(v.get("version") == version and v.get("downloadURL") == url
                   and v.get("size") == len(data) for v in entries):
            raise PublishError("public feed has not caught up with the release yet")
    return {"status": "published", "version": version, "url": url}


def reconcile() -> list[dict]:
    """Poll once per configured app; retry incomplete publications next tick."""
    rules = []
    for source, ship in (agent_checkouts.dispatch_config().get("ship") or {}).items():
        if isinstance(ship, dict) and isinstance(ship.get("sidestore"), dict):
            rules.append((source, ship, ship["sidestore"]))
    if not rules:
        return []
    path = Path(os.environ.get("SERENA_SIDESTORE_PUBLISH_DB", str(
        Path.home() / ".config" / "serena" / "sidestore-publish.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    reports = []
    with closing(sqlite3.connect(path, timeout=0.1)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS published "
                   "(destination TEXT, build_id TEXT, receipt TEXT, "
                   "PRIMARY KEY (destination, build_id))")
        try:
            # A separate DB, so slow artifact uploads do not lock task dispatch.
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return [{"status": "busy"}]
        for source, ship, rule in rules[:4]:
            try:
                repo = rule.get("repo", "")
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not rule.get("bundle_id"):
                    raise PublishError("SideStore destination repo and bundle_id are required")
                query = urllib.parse.urlencode({"appId": ship["codemagic_app_id"]})
                builds = json.loads(_download(f"{API}/builds?{query}", authenticated=True,
                                              limit=16 << 20)).get("builds", [])
                candidates = []
                for build in builds:
                    workflow = build.get("fileWorkflowId") or build.get("workflowId")
                    if (build.get("status") != "finished"
                            or build.get("appId") != ship["codemagic_app_id"]
                            or workflow != ship["codemagic_workflow"]
                            or build.get("branch") != rule.get("branch", "main")
                            or build.get("pullRequest")):
                        continue
                    ipas = [a for a in build.get("artefacts", []) if a["name"].endswith(".ipa")]
                    if len(ipas) == 1:
                        candidates.append((_version(ipas[0].get("version", "")), build, ipas[0]))
                if not candidates:
                    reports.append({"source": source, "status": "waiting"})
                    continue
                _, build, artifact = max(candidates, key=lambda item: (item[0], item[1]["finishedAt"]))
                if db.execute("SELECT 1 FROM published WHERE destination = ? AND build_id = ?",
                              (repo, build["_id"])).fetchone():
                    continue
                receipt = _publish(build, artifact, rule)
                db.execute("INSERT INTO published VALUES (?, ?, ?)",
                           (repo, build["_id"], json.dumps(receipt)))
                reports.append({"source": source, "build_id": build["_id"], **receipt})
            except (PublishError, agent_checkouts.CheckoutError, OSError, ValueError,
                    KeyError, TypeError, zipfile.BadZipFile) as error:
                detail = str(error) if isinstance(error, PublishError) else type(error).__name__
                reports.append({"source": source, "status": "retry", "error": detail})
        db.commit()
    return reports
