"""Saved browser auth profiles: enroll once (user types), reuse sealed sessions.

Security shape: the agent NEVER sees credentials. Enrollment launches an
isolated Chromium profile where Raghav logs in himself; only a seal marker
(URL prefix + title regex) is recorded. Every attach re-verifies the marker
before any task proceeds. Audit is metadata-only (slug, timestamps, signals).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.computer_browser import BrowserError, Check
from core.file_lock import exclusive_lock

STORE_DIR = Path("~/.config/serena/browser-profiles").expanduser()
AUDIT_NAME = "audit.jsonl"
MANIFEST_NAME = "manifest.json"
PROFILE_DIR_NAME = "profile"
LOCK_NAME = "attach.lock"

CHROMIUM_BINARIES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")

# Purpose-bound: no sync, no sign-in promos, no default-browser nags.
LAUNCH_FLAGS = (
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    "--disable-sync",
    "--disable-features=Translate,Signin",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-default-apps",
    "--disable-background-networking",
)

# Seconds a just-launched profile browser gets to publish its CDP listener.
LAUNCH_STARTUP_TIMEOUT = 10.0

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_NEEDS_RE_ENROLL = "profile session expired or logged out; run browser enroll again"


class BrowserProfileError(ValueError):
    """Raised for any profile lifecycle failure. Messages are metadata-only."""


@dataclass
class SealMarker:
    url_prefix: str = ""
    title_regex: str = ""
    url_verified: bool = False


@dataclass
class Manifest:
    slug: str
    purpose: str = ""
    start_url: str = ""
    enrolled_at: float = 0.0
    sealed: bool = False
    marker: SealMarker | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Manifest:
        marker = data.get("marker") or {}
        return Manifest(
            slug=str(data.get("slug") or ""),
            purpose=str(data.get("purpose") or ""),
            start_url=str(data.get("start_url") or ""),
            enrolled_at=float(data.get("enrolled_at") or 0.0),
            sealed=bool(data.get("sealed")),
            marker=SealMarker(
                url_prefix=str(marker.get("url_prefix") or ""),
                title_regex=str(marker.get("title_regex") or ""),
                url_verified=bool(marker.get("url_verified")),
            ),
        )


def _store_root() -> Path:
    override = os.environ.get("SERENA_BROWSER_PROFILE_DIR", "").strip()
    root = Path(override).expanduser() if override else STORE_DIR
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _slug_dir(slug: str) -> Path:
    if not _SLUG_RE.match(slug or ""):
        raise BrowserProfileError(f"invalid profile slug: {slug!r}")
    return _store_root() / slug


def _audit(slug: str, action: str, **fields: Any) -> None:
    """Append a metadata-only audit row. Never logs page content or cookies."""
    path = _slug_dir(slug) / AUDIT_NAME
    payload = {
        "ts": time.time(),
        "slug": slug,
        "action": action,
        **{k: v for k, v in fields.items() if k in {"ok", "method", "signal", "pid", "marker_hint"}},
    }
    if "marker_hint" in payload and payload["marker_hint"]:
        digest = hashlib.sha256(str(payload["marker_hint"]).encode()).hexdigest()[:16]
        payload["marker_hint"] = f"sha256:{digest}"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_manifest(slug: str) -> Manifest:
    path = _slug_dir(slug) / MANIFEST_NAME
    if not path.exists():
        raise BrowserProfileError(f"unknown browser profile: {slug}")
    return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _write_manifest(manifest: Manifest) -> None:
    path = _slug_dir(manifest.slug) / MANIFEST_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _chromium() -> str:
    for name in CHROMIUM_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    raise BrowserProfileError("no chromium binary on PATH (need one of: " + ", ".join(CHROMIUM_BINARIES) + ")")


def _launch_args(user_data_dir: Path, start_url: str = "") -> list[str]:
    args = [_chromium(), f"--user-data-dir={user_data_dir}", *LAUNCH_FLAGS]
    if start_url:
        args.append(start_url)
    return args


def _focused_window_title() -> str:
    """Best-effort title of the focused window via the desktop service."""
    from core.computer_client import ComputerClient

    try:
        status = ComputerClient().call("status")
    except Exception as exc:
        raise BrowserProfileError(f"desktop service unreachable: {exc}") from exc
    session = (status or {}).get("session") or {}
    focused = session.get("focused_window") or {}
    return str(focused.get("title") or "")


def _cdp_marker(
    url_prefix: str,
    title_regex: str,
    user_data_dir: Path,
    startup_timeout: float | None = None,
) -> tuple[bool, str]:
    """Verify marker over CDP when the scripted-browser module is available."""
    try:
        from core import computer_browser  # type: ignore
    except Exception:
        return False, "cdp-unavailable"
    extra = {} if startup_timeout is None else {"startup_timeout": startup_timeout}
    try:
        checks = computer_browser.check_session(
            user_data_dir, url_prefix=url_prefix, title_regex=title_regex, **extra
        )
    except Exception:
        return False, "cdp-error"
    ok = bool(checks.get("url_ok", True)) and bool(checks.get("title_ok", False))
    return ok, "cdp"


def _verify_marker(
    manifest: Manifest, *, cdp_dir: Path | None = None, startup_timeout: float | None = None
) -> tuple[bool, str]:
    marker = manifest.marker or SealMarker()
    if cdp_dir is not None and (marker.url_prefix or marker.title_regex):
        return _cdp_marker(marker.url_prefix, marker.title_regex, cdp_dir, startup_timeout)
    if not marker.title_regex:
        return False, "no-marker"
    try:
        title = _focused_window_title()
    except BrowserProfileError:
        return False, "service-unreachable"
    try:
        matched = re.search(marker.title_regex, title or "") is not None
    except re.error:
        return False, "bad-regex"
    if not matched:
        return False, "title-mismatch"
    return True, "service-title"


def enroll(slug: str, start_url: str, *, purpose: str = "") -> dict[str, Any]:
    """Launch an isolated profile for user-typed login. Agent watches only."""
    slug_dir = _slug_dir(slug)
    slug_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(slug_dir, 0o700)
    profile_dir = slug_dir / PROFILE_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = Manifest(
        slug=slug, purpose=purpose, start_url=start_url, enrolled_at=time.time()
    )
    _write_manifest(manifest)
    proc = subprocess.Popen(  # noqa: S603 — argv list, resolved binary
        _launch_args(profile_dir, start_url),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _audit(slug, "enroll-started", ok=True, pid=proc.pid)
    return {
        "slug": slug,
        "pid": proc.pid,
        "next": (
            f"log in inside the opened browser window, focus the logged-in tab, then run: "
            f"chats browser seal {slug} --url-prefix <prefix> --title-regex <regex>"
        ),
    }


def seal(slug: str, *, url_prefix: str = "", title_regex: str = "") -> dict[str, Any]:
    """Verify the logged-in marker and seal the profile. Quits the browser."""
    if not url_prefix and not title_regex:
        raise BrowserProfileError("seal needs --url-prefix and/or --title-regex")
    manifest = _read_manifest(slug)
    manifest.marker = SealMarker(url_prefix=url_prefix, title_regex=title_regex)
    profile_dir = _slug_dir(slug) / PROFILE_DIR_NAME
    ok, method = _verify_marker(manifest, cdp_dir=profile_dir)
    if not ok:
        _audit(slug, "seal-failed", ok=False, signal=method)
        raise BrowserProfileError(
            f"marker did not verify ({method}); focus the logged-in tab and retry, "
            "or re-enroll if the login did not stick"
        )
    if method == "cdp":
        manifest.marker.url_verified = True
    manifest.sealed = True
    _write_manifest(manifest)
    _quit_profile(slug)
    _audit(slug, "sealed", ok=True, method=method, marker_hint=url_prefix + "|" + title_regex)
    return {"slug": slug, "sealed": True, "method": method}


def _quit_profile(slug: str) -> None:
    """Best-effort quit of the enrolled browser (pid recorded at enroll)."""
    path = _slug_dir(slug) / AUDIT_NAME
    pid: int | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("action") == "enroll-started" and row.get("pid"):
                pid = int(row["pid"])
    except OSError:
        pid = None
    if pid is None:
        return
    with contextlib.suppress(OSError):
        os.kill(pid, 15)


def _authorize_attach(slug: str, *, authority_path=None, confirm_fn=None) -> None:
    """Tier-2 gate: fail closed on lock/deny. No credential handling here.

    Interactive callers pass a confirm_fn(prompt)->bool (CLI prompt);
    non-interactive callers without a grant are denied with guidance.
    """
    from core.action_authority import (
        BASIS_CONFIRMATION,
        ActionAuthority,
        build_request,
    )

    auth = ActionAuthority(path=authority_path) if authority_path else ActionAuthority()

    def _request(basis=None, confirmation_id=None):
        return build_request(
            capability="browser.attach_profile",
            intent=f"attach sealed browser profile: {slug}",
            source="cli",
            effect="external",
            target=slug,
            **({"authorization_basis": basis} if basis else {}),
            **({"confirmation_id": confirmation_id} if confirmation_id else {}),
        )

    decision = auth.authorize(_request())
    if decision.allowed and not decision.requires_confirmation:
        return
    if not decision.requires_confirmation:
        raise BrowserProfileError(f"attach denied by action authority: {decision.reason}")
    if confirm_fn is None:
        raise BrowserProfileError(
            "attach needs a live confirmation or grant; run from an interactive "
            "terminal or pre-issue a grant for browser.attach_profile"
        )
    record = auth.request_confirmation(
        capability="browser.attach_profile",
        target=slug,
        tier=decision.tier,
        prompt=f"attach sealed browser profile '{slug}'?",
    )
    resolved = auth.resolve_confirmation(
        record.confirmation_id, approved=bool(confirm_fn(f"attach sealed browser profile '{slug}'?"))
    )
    if resolved.state != "approved":
        raise BrowserProfileError("attach not approved")
    final = auth.authorize(
        _request(basis=BASIS_CONFIRMATION, confirmation_id=record.confirmation_id)
    )
    if not final.allowed:
        raise BrowserProfileError(f"attach denied by action authority: {final.reason}")


def attach(
    slug: str,
    *,
    authority_path=None,
    confirm_fn=None,
    _launcher=None,
    _verifier=None,
) -> dict[str, Any]:
    """Launch a sealed profile and re-verify its marker before handing over."""
    manifest = _read_manifest(slug)
    if not manifest.sealed:
        raise BrowserProfileError(f"profile is not sealed: {slug} (run browser seal first)")
    _authorize_attach(slug, authority_path=authority_path, confirm_fn=confirm_fn)
    lock_path = _slug_dir(slug) / LOCK_NAME
    with open(lock_path, "a+") as handle:
        try:
            with exclusive_lock(handle, timeout=0):
                return _attach_locked(slug, manifest, _launcher=_launcher, _verifier=_verifier)
        except TimeoutError as exc:
            raise BrowserProfileError(
                f"profile {slug} is already attached elsewhere; refusing concurrent attach"
            ) from exc


def _attach_locked(slug: str, manifest: Manifest, *, _launcher=None, _verifier=None):
    profile_dir = _slug_dir(slug) / PROFILE_DIR_NAME
    launcher = _launcher or (
        lambda: subprocess.Popen(  # noqa: S603 — argv list, resolved binary
            _launch_args(profile_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ).pid
    )
    pid = launcher()
    # Verification runs straight after the launch, so it has to outwait a cold
    # browser start instead of fast-failing into a bogus re-enrollment.
    verifier = _verifier or (
        lambda: _verify_marker(
            manifest, cdp_dir=profile_dir, startup_timeout=LAUNCH_STARTUP_TIMEOUT
        )
    )
    ok, method = verifier()
    if not ok:
        _audit(slug, "attach-unverified", ok=False, signal=method, pid=pid)
        raise BrowserProfileError(f"{_NEEDS_RE_ENROLL} (signal: {method})")
    _audit(slug, "attached", ok=True, method=method, pid=pid)
    return {"slug": slug, "pid": pid, "verified": True, "method": method}


def status(slug: str | None = None) -> dict[str, Any]:
    """Metadata-only status for one profile or the whole store."""
    root = _store_root()
    slugs = [slug] if slug else sorted(p.name for p in root.iterdir() if p.is_dir())
    profiles: dict[str, Any] = {}
    for name in slugs:
        try:
            manifest = _read_manifest(name)
        except BrowserProfileError:
            continue
        profiles[name] = {
            "sealed": manifest.sealed,
            "purpose": manifest.purpose,
            "enrolled_at": manifest.enrolled_at,
            "marker_set": bool((manifest.marker or SealMarker()).title_regex or (manifest.marker or SealMarker()).url_prefix),
            "url_verified": bool((manifest.marker or SealMarker()).url_verified),
        }
    return {"profiles": profiles}


def remove(slug: str) -> dict[str, Any]:
    """Wipe a profile completely (cookies included). Refuses while attached."""
    slug_dir = _slug_dir(slug)
    if not slug_dir.exists():
        raise BrowserProfileError(f"unknown browser profile: {slug}")
    lock_path = slug_dir / LOCK_NAME
    with open(lock_path, "a+") as handle:
        try:
            with exclusive_lock(handle, timeout=0):
                pass
        except TimeoutError as exc:
            raise BrowserProfileError(
                f"profile {slug} is attached; detach before removing"
            ) from exc
    _audit(slug, "removed", ok=True)
    shutil.rmtree(slug_dir, ignore_errors=False)
    return {"slug": slug, "removed": True}


class ReenrollRequired(BrowserError):
    pass


def validate_seal(seal):
    if not isinstance(seal, dict) or any(
        not isinstance(seal.get(k), str) or not seal[k]
        for k in ("slug", "enrolled_at", "url_prefix", "title_regex")
    ):
        raise ReenrollRequired("profile is not sealed; re-enroll required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", seal["slug"]):
        raise ReenrollRequired("invalid profile seal; re-enroll required")
    if len(seal["title_regex"]) > 2000 or len(seal["url_prefix"]) > 16000:
        raise ReenrollRequired("profile marker is too large; re-enroll required")


async def verify_seal(browser, seal):
    """Verify every use; return metadata only, never cookies or page content."""
    validate_seal(seal)
    url = await browser.check(Check("url", expected=seal["url_prefix"], match_mode="prefix"))
    title = await browser.check(Check("title", expected=seal["title_regex"], match_mode="regex"))
    if not url["match"] or not title["match"]:
        raise ReenrollRequired("profile marker no longer matches; re-enroll required")
    return {"slug": seal["slug"], "enrolled_at": seal["enrolled_at"], "verified": True}
