"""SideStore/AltStore source feed for the Serena mobile iOS app.

Add ONE url to SideStore or LiveContainer (Sources -> +):

    https://<daemon-host>/sidestore/source

and every finished Codemagic build of the `ios-unsigned-ipa` workflow shows
up as an install/update. Unauthenticated by design: the store clients can't
send auth headers, the tailnet is the boundary, and the unsigned shell IPA
contains no secrets (the daemon token lives in the app's saved settings,
never in the bundle). The Codemagic API token stays server-side; the store
downloads the IPA through /sidestore/ipa which proxies the artefact.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file

sidestore_bp = Blueprint("sidestore", __name__)

CODEMAGIC_API = "https://api.codemagic.io"
BUNDLE_ID = "sh.serena.app"
_ENV_FILE = Path.home() / ".config" / "serena" / "codemagic.env"

# The build list barely changes; don't hammer Codemagic on every store refresh.
_CACHE_TTL = 300
_cache: dict = {"at": 0.0, "build": None}


def _credentials() -> tuple[str | None, str | None]:
    """Codemagic token + the SERENA app id (not Locket's CODEMAGIC_APP_ID)."""
    token = os.environ.get("CODEMAGIC_API_TOKEN")
    app_id = os.environ.get("SERENA_CODEMAGIC_APP_ID")
    if token and app_id:
        return token, app_id
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key == "CODEMAGIC_API_TOKEN":
                token = token or value
            elif key == "SERENA_CODEMAGIC_APP_ID":
                app_id = app_id or value
    except OSError:
        pass
    return token, app_id


def _latest_ipa_build() -> dict | None:
    """Latest finished build carrying an .ipa artefact, cached for 5 minutes."""
    if _cache["build"] and time.time() - _cache["at"] < _CACHE_TTL:
        return _cache["build"]
    token, app_id = _credentials()
    if not token or not app_id:
        return None
    req = urllib.request.Request(
        f"{CODEMAGIC_API}/builds?appId={app_id}",
        headers={"x-auth-token": token},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    for build in data.get("builds", []):
        if build.get("status") != "finished":
            continue
        ipa = next(
            (a for a in build.get("artefacts", []) if a.get("name", "").endswith(".ipa")),
            None,
        )
        if ipa:
            found = {"build": build, "ipa": ipa}
            _cache.update(at=time.time(), build=found)
            return found
    return None


def _release_notes(build: dict) -> str:
    """Commit message as the store's 'what's new', minus the bot trailer."""
    message = (build.get("commit") or {}).get("commitMessage") or ""
    for marker in ("Co-Authored-By:", "🤖", "Generated with"):
        message = message.split(marker)[0]
    return message.strip() or f"Codemagic build {build.get('_id', '')[-6:]}"


@sidestore_bp.route("/sidestore/source")
def sidestore_source():
    try:
        found = _latest_ipa_build()
    except OSError:
        return jsonify({"error": "source unavailable"}), 503
    if not found:
        return jsonify({"error": "no builds"}), 404

    build, ipa = found["build"], found["ipa"]
    # Behind tailscale serve the backend sees plain http; trust the proxy's
    # scheme or the store gets http:// URLs pointing at a TLS port.
    origin = request.url_root.rstrip("/")
    proto = request.headers.get("X-Forwarded-Proto")
    if proto and origin.startswith("http://"):
        origin = proto + "://" + origin[len("http://"):]
    version = build.get("version") or ipa.get("version") or "1.0.0"
    finished_at = build.get("finishedAt") or "1970-01-01T00:00:00Z"

    return jsonify(
        {
            "name": "Serena",
            "identifier": f"{BUNDLE_ID}.source",
            "subtitle": "she runs things",
            "iconURL": f"{origin}/sidestore/icon.png",
            "apps": [
                {
                    "name": "Serena",
                    "bundleIdentifier": BUNDLE_ID,
                    "developerName": "Raghav Dua",
                    # Must equal the IPA's real CFBundleShortVersionString or
                    # SideStore throws verification error 4. Codemagic stamps
                    # package.json version plus BUILD_NUMBER, e.g. 1.0.0.3.
                    "version": version,
                    "versionDate": finished_at,
                    "versionDescription": _release_notes(build),
                    "downloadURL": f"{origin}/sidestore/ipa",
                    "localizedDescription": "Serena mobile — chat client for the daemon.",
                    "iconURL": f"{origin}/sidestore/icon.png",
                    "size": ipa.get("size") or 0,
                }
            ],
        }
    )


@sidestore_bp.route("/sidestore/ipa")
def sidestore_ipa():
    try:
        found = _latest_ipa_build()
    except OSError:
        return jsonify({"error": "source unavailable"}), 503
    if not found or not found["ipa"].get("url"):
        return jsonify({"error": "no builds"}), 404

    token, _ = _credentials()
    req = urllib.request.Request(
        found["ipa"]["url"], headers={"x-auth-token": token or ""}
    )
    upstream = urllib.request.urlopen(req, timeout=60)

    def stream():
        try:
            while chunk := upstream.read(1 << 16):
                yield chunk
        finally:
            upstream.close()

    headers = {"Content-Disposition": 'attachment; filename="Serena.ipa"'}
    length = upstream.headers.get("Content-Length")
    if length:
        headers["Content-Length"] = length
    return Response(stream(), mimetype="application/octet-stream", headers=headers)


@sidestore_bp.route("/sidestore/icon.png")
def sidestore_icon():
    icon = Path(__file__).resolve().parent.parent / "static" / "serena-icon.png"
    if not icon.is_file():
        return jsonify({"error": "no icon"}), 404
    return send_file(str(icon), mimetype="image/png")
