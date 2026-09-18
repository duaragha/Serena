"""The real transports behind Serena's one notification authority.

`core.notification_authority` decides *whether* Raghav gets interrupted. This
decides *how*, and nothing else. Keeping them apart is the point: the authority
stays testable with fake senders, and the transports stay dumb enough that
adding one cannot accidentally add a policy exemption.

Five channels, fixed:

- `voice` puts a line in front of the desktop voice bridge, which is how she
  actually talks to him when he is at the machine.
- `imessage` is the fallback for when he is not: whichever text line
  `phone-line.json` points at (her Telegram bot, her Apple ID, or his own
  self-thread through the Unified hub). The channel name predates the switch.
- `telegram` is the legacy bot path through `chats text`, kept for old queued
  notices; new ones reach the bot as `imessage` when the line points there.
- `desktop` is the silent overlay notice, for things worth showing but not
  worth saying out loud.
- `call` rings his phone over her SIP line and says the line when he answers.

Each sender returns True only when the transport genuinely accepted the
message. Returning True on a best-effort send is how an assistant ends up
believing she told someone something she did not, so a send that cannot be
confirmed returns False and lets the authority retry it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
)

HOME = Path.home()
OVERLAY_EVENT_SOCKET = HOME / ".local" / "state" / "serena" / "brain-events.sock"
TEXT_TIMEOUT_SECONDS = 15


# Windows has no AF_UNIX, and asking for one raises AttributeError rather than
# the OSError these senders were catching. Every caller of this is a "tell him
# what happened" path, so an unguarded socket turned a Fleet notice into a
# crash on the one machine Fleet runs on.
UNIX_DATAGRAMS_AVAILABLE = hasattr(socket, "AF_UNIX")
MAX_OVERLAY_DATAGRAM_BYTES = 60_000


def overlay_datagram(
    message: dict[str, object],
    socket_path: Path | None = None,
) -> bool:
    """Hand one event to the overlay/voice bridge over its unix socket.

    Returns False rather than raising when this machine has no unix sockets,
    when nothing is listening, or when the payload is too large to send. A
    notice that cannot be delivered is not an error worth unwinding a run for;
    the caller falls back to another channel.
    """

    target = OVERLAY_EVENT_SOCKET if socket_path is None else socket_path
    if not UNIX_DATAGRAMS_AVAILABLE or not target.exists():
        return False
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_OVERLAY_DATAGRAM_BYTES:
        return False
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    except (AttributeError, OSError):
        return False
    try:
        client.sendto(payload, str(target))
    except OSError:
        return False
    finally:
        client.close()
    return True


def _overlay_datagram(message: dict[str, object]) -> bool:
    """Kept for existing callers."""

    return overlay_datagram(message)


def send_voice(request: NotificationRequest) -> bool:
    """Say it out loud, through the desktop bridge she already speaks from."""

    return _overlay_datagram(
        {
            "type": "serena_notice",
            "kind": request.kind,
            "text": request.summary,
            "urgency": request.urgency,
            "notice_id": request.dedupe_key,
            "source_surface": request.source_surface,
        }
    )


def send_desktop(request: NotificationRequest) -> bool:
    """Show it without saying it."""

    return _overlay_datagram(
        {
            "type": "serena_banner",
            "kind": request.kind,
            "summary": request.summary,
            "notice_id": request.dedupe_key,
        }
    )


# Windows entry points are chats.exe, and Path.is_file() on a bare "chats" is
# simply false there. Without these the interpreter's own sibling never
# matched, PATH won instead, and the deployed runtime sent its notices through
# whichever other Serena install happened to come first -- on the PC that was a
# user-site chats running the synced dev tree, not the runtime's own code.
_BINARY_SUFFIXES = ("", ".exe", ".cmd", ".bat") if os.name == "nt" else ("",)


def _chats_binary() -> str | None:
    """The chats CLI belonging to this install, falling back to PATH.

    The sibling of the running interpreter comes first on purpose: the code
    sending a notice should use the CLI from the same installation, not
    whatever another one put on PATH ahead of it.
    """

    sibling = Path(sys.executable).resolve().parent / "chats"
    roots = [sibling, HOME / ".local" / "bin" / "chats"]
    for root in roots:
        for suffix in _BINARY_SUFFIXES:
            candidate = root.with_name(root.name + suffix)
            if candidate.is_file():
                return str(candidate)
    found = shutil.which("chats")
    return found if found and Path(found).is_file() else None


def send_telegram(request: NotificationRequest) -> bool:
    """Text his phone through the existing bot, via the chats CLI."""

    executable = _chats_binary()
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "text", request.summary],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TEXT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def send_imessage(request: NotificationRequest) -> bool:
    """Text his phone on whichever line phone-line.json owns."""

    from core import phone_line

    try:
        return phone_line.send(request.summary, key=request.dedupe_key or "")
    except Exception:
        return False


def send_call(request: NotificationRequest) -> bool:
    """Ring his phone; the line accepts it or this returns False."""

    from core import phone_call

    if not phone_call.enabled():
        return False
    try:
        return phone_call.place(request.summary, key=request.dedupe_key or "")
    except Exception:
        return False


DEFAULT_SENDERS = {
    "voice": send_voice,
    "imessage": send_imessage,
    "telegram": send_telegram,
    "desktop": send_desktop,
    "call": send_call,
}


def observe_notification_result(
    request: NotificationRequest,
    result,
) -> None:
    """Mirror an authority verdict back to the durable source surface."""

    if request.source_surface != "fleet" or not request.job_id:
        return
    notice_id = str(request.metadata.get("fleet_notice_id") or "")
    if not notice_id:
        return
    from fleet.store import FleetStore

    event_type = (
        "run.notification.delivered" if result.sent else "run.notification.failed"
    )
    FleetStore().append_event(
        request.job_id,
        event_type,
        {
            "notice_id": notice_id,
            "state": str(request.metadata.get("fleet_state") or ""),
            "channel": result.channel,
            "notification_id": result.notification_id,
            "authority_decision": result.decision,
            "error": result.reason if not result.sent else "",
        },
    )


def _policy_from_environment() -> NotificationPolicy:
    """Quiet hours and limits, overridable for a machine that needs it."""

    def number(name: str, fallback: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return int(raw) if raw else fallback
        except ValueError:
            return fallback

    return NotificationPolicy(
        quiet_start_hour=number("SERENA_QUIET_START_HOUR", 22),
        quiet_end_hour=number("SERENA_QUIET_END_HOUR", 8),
        hourly_limit=number("SERENA_NOTIFY_HOURLY_LIMIT", 12),
    )


_AUTHORITY: NotificationAuthority | None = None


def default_authority(*, refresh: bool = False) -> NotificationAuthority:
    """The one authority every Serena sender should go through.

    Fleet alerts, spoken job results, scheduled reminders, and webhook notices
    all resolve to this object, so quiet hours and deduplication are decided in
    one place instead of five.
    """

    global _AUTHORITY
    if _AUTHORITY is None or refresh:
        _AUTHORITY = NotificationAuthority(
            policy=_policy_from_environment(),
            senders=dict(DEFAULT_SENDERS),
            result_observer=observe_notification_result,
        )
    return _AUTHORITY


def notify(
    kind: str,
    summary: str,
    *,
    channel: str = "voice",
    urgency: str = "normal",
    dedupe_key: str = "",
    source_surface: str = "system",
    job_id: str | None = None,
    fallback_channel: str | None = "imessage",
    answers_request: bool = False,
    authority: NotificationAuthority | None = None,
):
    """Ask the authority to tell Raghav something, with one fallback hop.

    The fallback exists because the voice bridge is only reachable when he is
    at the machine. It is a channel change, not a policy bypass: the fallback
    request goes through the same authority and obeys the same quiet hours,
    limits, and dedupe window.
    """

    target = authority or default_authority()
    result = target.request(
        NotificationRequest(
            kind=kind,
            summary=summary,
            channel=channel,
            urgency=urgency,
            dedupe_key=dedupe_key,
            source_surface=source_surface,
            job_id=job_id,
            answers_request=answers_request,
        )
    )
    if result.sent or not fallback_channel or fallback_channel == channel:
        return result
    if result.decision != "failed":
        # Suppressed, deferred, or held for approval are real answers. Only a
        # transport that could not deliver earns a second channel.
        return result
    return target.request(
        NotificationRequest(
            kind=kind,
            summary=summary,
            channel=fallback_channel,
            urgency=urgency,
            # A distinct key, or the dedupe window would swallow the fallback
            # as a duplicate of the attempt that just failed.
            dedupe_key=f"{dedupe_key}:{fallback_channel}" if dedupe_key else "",
            source_surface=source_surface,
            job_id=job_id,
            answers_request=answers_request,
        )
    )
