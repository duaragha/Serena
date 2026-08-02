"""Small, audited capability broker for Serena's laptop voice surface.

Only reversible local controls live here.  Messaging, typing, clicking,
deleting, purchasing, account changes, deployment, and arbitrary commands are
not representable, so an unattended model cannot smuggle them through.
"""

from __future__ import annotations

import hashlib
import json
import os
import contextlib
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_AUDIT_PATH = (
    Path.home() / ".local" / "state" / "serena" / "laptop-actions.jsonl"
)

LOW_RISK_ACTIONS = {
    "volume_up",
    "volume_down",
    "mute",
    "unmute",
    "toggle_mute",
    "media_play_pause",
    "media_next",
    "media_previous",
    "open_app",
    "open_url",
}

_APP_IDS = {
    "browser": "microsoft-edge.desktop",
    "edge": "microsoft-edge.desktop",
    "files": "nemo.desktop",
    "file manager": "nemo.desktop",
    "terminal": "org.gnome.Terminal.desktop",
    "code": "code.desktop",
    "vs code": "code.desktop",
    "serena": "chats.desktop",
    "settings": "cinnamon-settings.desktop",
}
# "can you open spotify" is an instruction in every household on earth. Strip
# the politeness before deciding anything, instead of refusing him for
# phrasing a command the way people actually speak.
_POLITE_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay)[,!]?\s+)?(?:serena[,!]?\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+)?",
    re.IGNORECASE,
)
_POLITE_TAIL = re.compile(r"[\s,]*(?:for me|please|real quick|right now)[.!?]?$", re.IGNORECASE)
_QUESTION_PREFIX = re.compile(
    r"^(can|could|would|will|do|does|did|are|is|should|may|might)\b"
)


def _instruction_text(text: str) -> str:
    stripped = _POLITE_LEAD.sub("", text, count=1).strip()
    return _POLITE_TAIL.sub("", stripped).strip()


def _desktop_entry_for(name: str) -> str | None:
    """Resolve a spoken app name against everything actually installed.

    A hardcoded map of nine apps means "open spotify" fails on a machine
    where Spotify is sitting right there. The desktop database is the real
    allowlist: if it is installed and has a launcher, she may open it, which
    is the same authority the applications menu gives any user.
    """
    from pathlib import Path as _Path

    wanted = _clean(name)
    if not wanted:
        return None
    if wanted in _APP_IDS:
        return _APP_IDS[wanted]
    best = None
    directories = [
        _Path.home() / ".local/share/applications",
        _Path("/usr/share/applications"),
        _Path("/var/lib/flatpak/exports/share/applications"),
        _Path.home() / ".local/share/flatpak/exports/share/applications",
    ]
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.glob("*.desktop")):
            try:
                body = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "NoDisplay=true" in body:
                continue
            display = ""
            for line in body.splitlines():
                if line.startswith("Name="):
                    display = line[5:].strip().lower()
                    break
            stem = entry.stem.lower()
            if wanted == display or wanted == stem:
                return entry.name
            if best is None and (wanted in display or wanted in stem.replace("-", " ")):
                best = entry.name
    return best
_NEGATION = re.compile(r"\b(don't|do not|not|never|without|stop before)\b")
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)

_AUTHORITY_PATTERNS = {
    "volume_up": re.compile(r"\b(volume up|raise|increase|turn .*volume up)\b"),
    "volume_down": re.compile(r"\b(volume down|lower|decrease|turn .*volume down)\b"),
    "mute": re.compile(r"\b(mute|silence) (the )?(laptop|computer|audio|sound)\b"),
    "unmute": re.compile(r"\bunmute (the )?(laptop|computer|audio|sound)\b"),
    "toggle_mute": re.compile(r"\btoggle (the )?(laptop |computer )?(mute|sound)\b"),
    "media_play_pause": re.compile(r"\b(play|pause|play pause|resume) (the )?(media|music|video)\b"),
    "media_next": re.compile(r"\b(next|skip) (track|song|video)\b"),
    "media_previous": re.compile(r"\b(previous|last) (track|song|video)\b"),
    "open_app": re.compile(r"^(open|launch|start) .+"),
    "open_url": re.compile(r"^(open|visit|go to) .+"),
}


@dataclass(frozen=True, slots=True)
class LaptopActionResult:
    ok: bool
    status: str
    action: str
    target: str
    message: str
    receipt_id: str


def _clean(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _trusted_executable(name: str) -> str | None:
    found = shutil.which(name)
    if not found:
        return None
    try:
        path = Path(found).resolve(strict=True)
        metadata = path.stat()
    except OSError:
        return None
    if os.name != "nt" and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
        return None
    return str(path)


def _authority_denial(action: str, target: str, origin: Mapping[str, object]) -> str | None:
    if str(origin.get("protocol") or "") != "voice":
        return "laptop actions are available only from a live voice turn"
    if not str(origin.get("call_id") or "").startswith("desk-"):
        return "laptop actions require the local desk voice surface"
    text = _instruction_text(_clean(origin.get("text")))
    if not text:
        return "the originating voice text is unavailable"
    if _QUESTION_PREFIX.search(text):
        return "a capability question is not an instruction"
    if _NEGATION.search(text):
        return "negated or ambiguous voice instructions never execute"
    pattern = _AUTHORITY_PATTERNS.get(action)
    if pattern is None or not pattern.search(text):
        return "a fresh direct instruction for this exact action is required"
    if action == "open_app":
        canonical = _clean(target)
        if canonical not in text:
            return "the requested app must be named in the spoken instruction"
        if _desktop_entry_for(canonical) is None:
            return f"no installed application matches {canonical!r}"
    if action == "open_url":
        urls = [match.group(0).rstrip(".,!?") for match in _URL.finditer(text)]
        if target not in urls:
            return "the exact http or https URL must be spoken in this turn"
    return None


def _command_for(action: str, target: str) -> list[str]:
    if action in {"volume_up", "volume_down", "mute", "unmute", "toggle_mute"}:
        pactl = _trusted_executable("pactl")
        if not pactl:
            raise RuntimeError("trusted pactl is unavailable")
        if action == "volume_up":
            return [pactl, "set-sink-volume", "@DEFAULT_SINK@", "+5%"]
        if action == "volume_down":
            return [pactl, "set-sink-volume", "@DEFAULT_SINK@", "-5%"]
        if action == "mute":
            return [pactl, "set-sink-mute", "@DEFAULT_SINK@", "1"]
        if action == "unmute":
            return [pactl, "set-sink-mute", "@DEFAULT_SINK@", "0"]
        return [pactl, "set-sink-mute", "@DEFAULT_SINK@", "toggle"]
    if action in {"media_play_pause", "media_next", "media_previous"}:
        xdotool = _trusted_executable("xdotool")
        if not xdotool:
            raise RuntimeError("trusted xdotool is unavailable")
        key = {
            "media_play_pause": "XF86AudioPlay",
            "media_next": "XF86AudioNext",
            "media_previous": "XF86AudioPrev",
        }[action]
        return [xdotool, "key", key]
    if action == "open_app":
        launcher = _trusted_executable("gtk-launch")
        if not launcher:
            raise RuntimeError("trusted gtk-launch is unavailable")
        entry = _desktop_entry_for(_clean(target))
        if entry is None:
            raise ValueError(f"no installed application matches {target!r}")
        return [launcher, entry.removesuffix(".desktop")]
    if action == "open_url":
        launcher = _trusted_executable("xdg-open")
        if not launcher:
            raise RuntimeError("trusted xdg-open is unavailable")
        return [launcher, target]
    raise ValueError(f"unsupported laptop action {action!r}")


def _append_audit(
    result: LaptopActionResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    audit_path = audit_path.expanduser().resolve()
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        **asdict(result),
        "created_at": time.time(),
        "protocol": str(origin.get("protocol") or ""),
        "call_id": str(origin.get("call_id") or ""),
        "turn_id": str(origin.get("turn_id") or ""),
        "origin_sha256": hashlib.sha256(
            str(origin.get("text") or "").encode("utf-8")
        ).hexdigest(),
    }
    descriptor = os.open(
        audit_path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(
            descriptor,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def execute_laptop_action(
    action: str,
    target: str,
    *,
    origin: Mapping[str, object],
    audit_path: Path = DEFAULT_AUDIT_PATH,
    runner=subprocess.run,
    spawner=subprocess.Popen,
) -> LaptopActionResult:
    action = _clean(action).replace(" ", "_")
    target = str(target or "").strip()
    receipt_id = uuid.uuid4().hex
    denial = (
        f"{action!r} is outside the reversible laptop allowlist"
        if action not in LOW_RISK_ACTIONS
        else _authority_denial(action, target, origin)
    )
    if denial:
        result = LaptopActionResult(
            False, "denied", action, target, denial, receipt_id
        )
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result
    try:
        command = _command_for(action, target)
        if action in {"open_app", "open_url"}:
            # gtk-launch runs the application in the foreground and waits for
            # it to EXIT, so a successful launch looked like a 10s timeout
            # while the app sat open on screen. Launches are fire-and-forget:
            # started detached means done.
            process = spawner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with contextlib.suppress(subprocess.TimeoutExpired):
                exited = process.wait(timeout=1.5)
                if exited != 0:
                    raise RuntimeError(f"launcher exited with status {exited}")
        else:
            completed = runner(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "command failed").strip()
                raise RuntimeError(detail[:300])
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        result = LaptopActionResult(
            False,
            "failed",
            action,
            target,
            f"{type(exc).__name__}: {exc}",
            receipt_id,
        )
    else:
        result = LaptopActionResult(
            True,
            "completed",
            action,
            target,
            "reversible local laptop action completed",
            receipt_id,
        )
    _append_audit(result, origin=origin, audit_path=audit_path)
    return result


def read_laptop_context(*, runner=subprocess.run) -> dict[str, object]:
    """Return bounded, read-only context from the current Linux desktop."""

    context: dict[str, object] = {"session_type": os.environ.get("XDG_SESSION_TYPE", "")}
    xdotool = _trusted_executable("xdotool")
    if xdotool:
        try:
            window = runner(
                [xdotool, "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
            if window.isdigit():
                title = runner(
                    [xdotool, "getwindowname", window],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                ).stdout.strip()
                pid = runner(
                    [xdotool, "getwindowpid", window],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                ).stdout.strip()
                context["active_window"] = {
                    "title": title[:300],
                    "pid": int(pid) if pid.isdigit() else None,
                }
        except (OSError, subprocess.SubprocessError):
            pass
    pactl = _trusted_executable("pactl")
    if pactl:
        try:
            volume = runner(
                [pactl, "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
            mute = runner(
                [pactl, "get-sink-mute", "@DEFAULT_SINK@"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
            percent = re.search(r"(\d+)%", volume)
            context["audio"] = {
                "volume_percent": int(percent.group(1)) if percent else None,
                "muted": "yes" in mute.casefold(),
            }
        except (OSError, subprocess.SubprocessError):
            pass
    return context
