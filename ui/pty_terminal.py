"""PTY-backed terminals for the browser UI.

Each spawned terminal is tracked by a uuid. The web UI opens a WebSocket to
stream input/output; lifecycle is owned by the browser — close the tab or
hit the terminate endpoint and the child is SIGTERM'd.

POSIX uses ptyprocess + select on the master fd. Windows uses pywinpty's
ConPTY-backed PtyProcess plus a per-terminal reader thread feeding a queue,
because the Windows handle is not selectable.
"""

import errno
import glob
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import suppress
import time
import uuid
from dataclasses import dataclass, field

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    try:
        from winpty import PtyProcess as _PtyProcess
    except ImportError:
        _PtyProcess = None
else:
    try:
        import ptyprocess as _ptyprocess_mod
        _PtyProcess = _ptyprocess_mod.PtyProcess
    except ImportError:
        _PtyProcess = None


def _flowing_event() -> threading.Event:
    """A flow-control gate that starts open (nothing outstanding yet)."""
    event = threading.Event()
    event.set()
    return event


@dataclass
class Terminal:
    id: str
    proc: object
    cols: int
    rows: int
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    # Windows-only: bytes buffer + lock + event for the reader thread → API.
    # Replaces the previous queue-swap race condition (rebuilding the queue
    # under the reader thread dropped output during high-throughput bursts,
    # which is exactly what happens during a resize repaint).
    buf: bytearray = field(default_factory=bytearray)
    buf_lock: threading.Lock = field(default_factory=threading.Lock)
    data_event: threading.Event = field(default_factory=threading.Event)
    eof: bool = False
    reader_thread: threading.Thread | None = None
    session_id: str | None = None
    agent: str = ""
    cwd: str = ""
    pty_backend: str = ""
    runtime_state: str = "live"
    runtime_busy: bool = False
    turn_file_version: tuple[int, int] | None = None
    # The transcript size/mtime last seen by the idle sweep. Separate from
    # turn_file_version, which belongs to the busy lease: this one exists only
    # to notice that the child wrote something while nobody was attached.
    activity_file_version: tuple[int, int] | None = None
    input_draft: str = ""
    work_item_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    # Last time this runtime saw real traffic in either direction. Idle sleep
    # measures from here, so a background agent that is still streaming output
    # is never frozen mid-thought just because the user is looking elsewhere.
    last_activity: float = field(default_factory=time.monotonic)
    reclaimed: bool = False
    # Whether *this* runtime's cgroup accepts a reclaim write. None until it
    # has been tried. Unlike `reclaimed` this survives a wake, because landing
    # outside a user-owned cgroup is a property of how the child was spawned
    # and does not change for the life of the process.
    reclaim_supported: bool | None = None
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    # Browser flow control + reattach state. Guarded by flow_lock, which is
    # deliberately separate from state_lock: the reader thread touches it on
    # every chunk and must never contend with runtime/reservation bookkeeping.
    flow_lock: threading.Lock = field(default_factory=threading.Lock)
    flow_resume: threading.Event = field(default_factory=_flowing_event)
    flow_enabled: bool = False
    flow_unacked: int = 0
    attach_seq: int = 0
    attached: bool = False
    detached_at: float | None = None
    backlog: bytearray = field(default_factory=bytearray)
    backlog_truncated: bool = False
    drain_thread: threading.Thread | None = None


_terminals: dict[str, Terminal] = {}
_registry_lock = threading.Lock()
_web_focus_sid: str | None = None
_web_split_sids: tuple[str, ...] = ()

MIN_COLS = 10
MIN_ROWS = 4


def _terminal_environment(environ: dict[str, str]) -> dict[str, str]:
    """Restore user CLI directories omitted by long-running service hosts.

    Electron attaches to ``serena-mobile-host`` when it is available. That
    systemd service can inherit its PATH before the login shell exports
    ``~/.local/bin`` or the active NVM directory, leaving Claude or Codex
    installed but invisible to every terminal launched from the app.
    """

    env = dict(environ)
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    user_dirs = [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".claude", "local"),
        os.path.join(home, ".cargo", "bin"),
    ]
    if not _IS_WINDOWS:
        user_dirs.extend(
            sorted(
                glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")),
                reverse=True,
            )
        )
    user_dirs.extend(
        [
            os.path.join(home, ".bun", "bin"),
            os.path.join(home, "bin"),
        ]
    )

    existing = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    merged: list[str] = []
    seen: set[str] = set()
    for entry in [*(path for path in user_dirs if os.path.isdir(path)), *existing]:
        key = os.path.normcase(os.path.normpath(entry))
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    env["PATH"] = os.pathsep.join(merged)
    return env


def _windows_reader_loop(term: "Terminal") -> None:
    proc = term.proc
    while True:
        try:
            chunk = proc.read(8192)
        except EOFError:
            with term.buf_lock:
                term.eof = True
            term.data_event.set()
            return
        except OSError:
            with term.buf_lock:
                term.eof = True
            term.data_event.set()
            return
        if not chunk:
            time.sleep(0.01)
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        with term.buf_lock:
            term.buf.extend(chunk)
        term.data_event.set()


# ── user-owned cgroup placement ─────────────────────────────────────────────
# memory.reclaim is only writable inside a cgroup this user owns. A plain fork
# inherits the login session scope, which logind creates and the user does not
# own, so reclaim_memory()'s write fails with EPERM there and every idle pane
# keeps its whole heap resident. Asking the *user's own* systemd manager for a
# transient scope puts each child under user@<uid>.service/app.slice instead,
# which is user-owned and therefore writable.
#
# --scope is the mode that matters here: systemd-run moves its own process into
# the new scope and then execs the target, so the pid ptyprocess hands back is
# still the real agent pid, keeps its pgid, and keeps the pty as its
# controlling terminal. pause()/resume()'s killpg and the RSS accounting need
# no knowledge of any of this, and reclaim_memory() finds the writable knob on
# its own because it already resolves the cgroup from /proc/<pid>/cgroup.
#
# The pid is stable from the moment spawn() returns, but for the few ms the
# D-Bus round trip takes it is still systemd-run rather than the agent, and the
# scope does not exist yet. Nothing has to wait for that: measured at 4-13ms
# here, against pause()'s five second prewarm floor, and the cgroup is resolved
# lazily at reclaim time rather than being cached at spawn.

_SCOPE_PROBE_TIMEOUT = 5.0
_scope_supported: bool | None = None
_scope_probe_lock = threading.Lock()


def _scope_argv(argv: list[str]) -> list[str]:
    """Wrap *argv* so the user's systemd manager owns the child's cgroup."""

    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        # Without this a scope that fails lingers in the user manager forever;
        # panes are spawned and closed all day, so they have to clean up.
        "--collect",
        # The owning host's pid is in the unit name on purpose. A scope is NOT
        # in the host's cgroup, so when the host restarts systemd leaves these
        # children running while the PTY master they were talking to is gone.
        # From the user's side the terminal "died"; in reality it leaked and
        # kept its memory. Stamping the owner lets a fresh host identify which
        # scopes are unreachable garbage and which belong to a live sibling.
        f"--unit=serena-pty-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        # systemd derives a unit's description from the command line when none
        # is given, and an agent's argv carries the whole system prompt. That
        # put thousands of characters into the journal on every single pane
        # spawn; 109 spawns in one day helped push it to 2.4GB.
        "--description=Serena terminal pane",
        "--",
        *argv,
    ]


# systemd appends ".scope" itself, so the same pattern has to read both the
# name we pass to --unit and the name that comes back from list-units.
_SCOPE_UNIT = re.compile(r"^serena-pty-(\d+)-[0-9a-f]+(?:\.scope)?$")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reap_orphaned_scopes() -> list[str]:
    """Stop pane scopes whose owning host is gone, and report what was stopped.

    A pane talks to its host through a PTY master fd that only that host holds.
    When the host dies the child survives in its own scope with nothing on the
    other end, so it can never be reattached: it is unreachable, invisible in
    the UI, and still holding its memory. One was found 22 hours old. Panes
    belonging to a host that is still running are left strictly alone, so a
    second host starting up cannot kill a sibling's terminals.
    """

    if _IS_WINDOWS or shutil.which("systemctl") is None:
        return []
    try:
        listing = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=scope", "--no-pager", "--plain"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    reaped: list[str] = []
    for line in listing.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        match = _SCOPE_UNIT.match(unit)
        if not match or _process_alive(int(match.group(1))):
            continue
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                capture_output=True,
                timeout=15,
                check=False,
            )
            reaped.append(unit)
    if reaped:
        print(
            f"[runtime] reaped {len(reaped)} orphaned pane scope(s) left by a "
            "previous host",
            file=sys.stderr,
            flush=True,
        )
    return reaped


def _systemd_scope_supported() -> bool:
    """Whether this machine can put a child in a user-owned transient scope.

    Probed once with a throwaway unit rather than inferred from the presence of
    $XDG_RUNTIME_DIR, because those variables can be set while the user bus is
    unreachable. Inferring would push that failure onto a real agent spawn;
    probing pays ~25ms once and then answers from cache.
    """

    global _scope_supported
    with _scope_probe_lock:
        if _scope_supported is not None:
            return _scope_supported
        _scope_supported = False
        if _IS_WINDOWS or shutil.which("systemd-run") is None:
            return _scope_supported
        if not (
            os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        ):
            return _scope_supported
        try:
            probe = subprocess.run(
                _scope_argv(["true"]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=_SCOPE_PROBE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return _scope_supported
        _scope_supported = probe.returncode == 0
        if not _scope_supported:
            print(
                "[runtime] transient user scopes unavailable: "
                f"{(probe.stderr or b'').decode(errors='replace').strip()[:200]}. "
                "Panes still freeze when idle; their pages stay resident until "
                "the kernel swaps them under pressure.",
                file=sys.stderr,
                flush=True,
            )
        return _scope_supported


def spawn(
    argv: list[str],
    cwd: str,
    cols: int = 100,
    rows: int = 30,
    *,
    session_id: str | None = None,
    agent: str = "",
    env: dict[str, str] | None = None,
) -> str:
    if _PtyProcess is None:
        raise RuntimeError(
            "PTY backend not installed. Install 'pywinpty' on Windows or "
            "'ptyprocess' on POSIX."
        )

    cols = max(MIN_COLS, int(cols))
    rows = max(MIN_ROWS, int(rows))

    # Ensure claude/codex see a real terminal type. On Windows, the inherited
    # env from pythonw.exe has no TERM, so apps fall back to plain ASCII
    # rendering — claude's TUI then can't position its statusline/input bar
    # correctly + may skip the alt-screen switch entirely.
    env = _terminal_environment(dict(os.environ if env is None else env))
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("COLUMNS", str(cols))
    env.setdefault("LINES", str(rows))
    if _IS_WINDOWS:
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        proc = _PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols), env=env)
    else:
        launch = argv
        if _systemd_scope_supported():
            # ptyprocess reports a missing binary by raising FileNotFoundError
            # for argv[0], and the HTTP layer turns that into "<agent> CLI not
            # found on PATH". Wrapping would put systemd-run — which certainly
            # does exist — in that slot and downgrade the error to a pane that
            # dies on its own, so keep the check pointed at the real command.
            if shutil.which(argv[0], path=env.get("PATH")) is None:
                raise FileNotFoundError(
                    f"The command was not found or was not executable: {argv[0]}."
                )
            launch = _scope_argv(argv)
        try:
            proc = _PtyProcess.spawn(launch, cwd=cwd, dimensions=(rows, cols), env=env)
        except Exception as exc:
            if launch is argv:
                raise
            # The probe passed but this particular scope did not start. A pane
            # that runs without reclaim beats no pane at all, so drop the
            # wrapper for this spawn rather than failing the terminal. Say so:
            # this child lands in the inherited cgroup, so it is the one case
            # where an otherwise reclaimable machine has an unreclaimable pane,
            # and silence here is a debugging dead end later.
            print(
                f"[runtime] transient scope spawn failed ({exc}); falling back "
                "to a plain fork. This pane still freezes when idle, but its "
                "pages stay resident.",
                file=sys.stderr,
                flush=True,
            )
            proc = _PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols), env=env)
    tid = uuid.uuid4().hex
    term = Terminal(
        id=tid,
        proc=proc,
        cols=cols,
        rows=rows,
        session_id=session_id,
        agent=(agent or "").lower(),
        cwd=cwd,
    )

    if _IS_WINDOWS:
        term.reader_thread = threading.Thread(
            target=_windows_reader_loop,
            args=(term,),
            daemon=True,
            name=f"pty-reader-{tid[:8]}",
        )
        term.reader_thread.start()

    with _registry_lock:
        _terminals[tid] = term
    return tid


def get(tid: str) -> Terminal | None:
    with _registry_lock:
        return _terminals.get(tid)


# ── session ↔ terminal mapping ──────────────────────────────────────────────
# Lets server-side callers (the ask-codex / ask-claude bridges) find the live
# PTY for a chat. The GTK shell tracks VTEs per-sid in-process; the
# Windows/pywebview path runs terminals here, so the bridges need this map.
_session_tids: dict[str, str] = {}


def register_session(sid: str, tid: str) -> None:
    with _registry_lock:
        _session_tids[sid] = tid
        term = _terminals.get(tid)
        if term:
            term.session_id = sid


def migrate_session(old_sid: str, new_sid: str, tid: str | None = None) -> bool:
    """Move a live pseudo-session mapping onto its on-disk session id."""
    global _web_focus_sid, _web_split_sids
    if not old_sid or not new_sid:
        return False
    with _registry_lock:
        mapped_tid = tid or _session_tids.get(old_sid)
        if not mapped_tid or mapped_tid not in _terminals:
            return False
        if _session_tids.get(old_sid) == mapped_tid:
            _session_tids.pop(old_sid, None)
        _session_tids[new_sid] = mapped_tid
        _terminals[mapped_tid].session_id = new_sid
        if _web_focus_sid == old_sid:
            _web_focus_sid = new_sid
        if old_sid in _web_split_sids:
            _web_split_sids = tuple(
                new_sid if sid == old_sid else sid for sid in _web_split_sids
            )
    return True


def tid_for_session(sid: str) -> str | None:
    """The live terminal for a session id, or None. Drops stale entries."""
    with _registry_lock:
        tid = _session_tids.get(sid)
    if tid and is_alive(tid):
        return tid
    if tid:
        with _registry_lock:
            _session_tids.pop(sid, None)
    return None


def write(tid: str, data: bytes) -> bool:
    """Write manual or generic bridge input unless a work turn owns the PTY."""
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        if term.work_item_id:
            return False
    return _write_unchecked(term, data)


def write_operator_prompt(tid: str, data: bytes, mode: str) -> dict[str, object]:
    """Atomically validate and deliver one operator-owned native prompt.

    Runtime state can change after an HTTP capability snapshot.  Keep the
    final check, any wake-up, and the PTY write under the same state lock so a
    draft, work reservation, or turn transition cannot steal the input lane.
    """

    term = get(tid)
    if not term:
        return {"supported": False, "delivered": False, "reason": "native runtime is not open"}
    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in {"correction", "next_turn"}:
        return {"supported": False, "delivered": False, "reason": "unknown prompt mode"}

    with term.state_lock:
        if term.work_item_id:
            return {
                "supported": False,
                "delivered": False,
                "reason": "runtime is reserved by Serena work",
            }
        if term.runtime_state not in {"live", "paused"}:
            return {
                "supported": False,
                "delivered": False,
                "reason": f"runtime state is {term.runtime_state}",
            }
        if term.input_draft.strip():
            return {
                "supported": False,
                "delivered": False,
                "reason": "an unsent draft already owns the input",
            }

        woke_runtime = False
        if clean_mode == "correction":
            if term.agent != "codex":
                return {
                    "supported": False,
                    "delivered": False,
                    "reason": "this native provider has no verified safe mid-turn steering path",
                }
            if term.runtime_state == "paused":
                return {
                    "supported": False,
                    "delivered": False,
                    "reason": "a sleeping runtime cannot be corrected",
                }
            if not term.runtime_busy:
                return {
                    "supported": False,
                    "delivered": False,
                    "reason": "there is no active turn to correct",
                }
        else:
            if term.runtime_busy:
                return {
                    "supported": False,
                    "delivered": False,
                    "reason": "wait for the active turn or queue a correction",
                }
            if term.runtime_state == "paused":
                if not _resume_locked(term):
                    return {
                        "supported": False,
                        "delivered": False,
                        "reason": "sleeping runtime could not be resumed",
                    }
                woke_runtime = True

        mark_busy = clean_mode == "next_turn"
        if mark_busy:
            term.runtime_busy = True
            term.turn_file_version = None
        delivered = _write_unchecked(term, data)
        if not delivered and mark_busy:
            term.runtime_busy = False
            term.turn_file_version = None
        return {
            "supported": delivered,
            "delivered": delivered,
            "reason": (
                "prompt delivered to the native runtime"
                if delivered
                else "native runtime refused the prompt"
            ),
            "wakes_runtime": woke_runtime,
        }


def _write_unchecked(term: Terminal, data: bytes) -> bool:
    term.last_activity = time.monotonic()
    try:
        with term.write_lock:
            if _IS_WINDOWS and isinstance(data, (bytes, bytearray)):
                term.proc.write(data.decode("utf-8", errors="replace"))
            else:
                term.proc.write(data)
        return True
    except (OSError, EOFError):
        return False


def write_reserved(tid: str, data: bytes, item_id: str) -> bool:
    """Write only when ``item_id`` owns the terminal's whole-turn lease."""
    term = get(tid)
    if not term or not item_id:
        return False
    with term.state_lock:
        if term.work_item_id != item_id:
            return False
    return _write_unchecked(term, data)


def reserve_work(tid: str, item_id: str) -> tuple[bool, str]:
    """Atomically reserve an idle Codex PTY for one accepted voice job."""
    term = get(tid)
    if not term:
        return False, "terminal not found"
    if not item_id:
        return False, "item_id required"
    if not is_alive(tid):
        return False, "terminal is not alive"
    with term.state_lock:
        if term.agent != "codex":
            return False, "target is not a codex runtime"
        if term.work_item_id:
            return False, f"runtime reserved by {term.work_item_id}"
        if term.runtime_busy:
            return False, "runtime has an active turn"
        if term.input_draft.strip():
            return False, "runtime has an unsent draft"
        if term.runtime_state not in {"live", "paused"}:
            return False, f"runtime state is {term.runtime_state}"
        term.work_item_id = item_id
    return True, "reserved"


def release_work(tid: str, item_id: str) -> bool:
    term = get(tid)
    if not term or not item_id:
        return False
    with term.state_lock:
        if term.work_item_id != item_id:
            return False
        term.work_item_id = None
    return True


def reservation(tid: str) -> str | None:
    term = get(tid)
    if not term:
        return None
    with term.state_lock:
        return term.work_item_id


def reservation_for_session(sid: str) -> str | None:
    tid = tid_for_session(sid)
    return reservation(tid) if tid else None


def note_manual_input(tid: str, data: bytes) -> None:
    """Maintain a privacy-safe server shadow of the current xterm draft."""
    term = get(tid)
    if not term or not data:
        return
    text = data.decode("utf-8", errors="ignore")
    with term.state_lock:
        draft = term.input_draft
        for char in text:
            if char in "\r" or char == "\x03":
                draft = ""
            elif char in {"\x08", "\x7f"}:
                draft = draft[:-1]
            elif char == "\x17":
                stripped = draft.rstrip()
                split_at = max(stripped.rfind(" "), stripped.rfind("\n"))
                draft = stripped[: split_at + 1] if split_at >= 0 else ""
            elif char == "\n" or char.isprintable():
                draft += char
        term.input_draft = draft


def note_web_context(
    tid: str,
    *,
    focused_sid: str | None = None,
    split_sids: list[str] | tuple[str, ...] | None = None,
    draft: str | None = None,
) -> bool:
    """Record browser-owned focus and draft state sent over its terminal WS."""
    global _web_focus_sid, _web_split_sids
    term = get(tid)
    if not term:
        return False
    if draft is not None:
        with term.state_lock:
            term.input_draft = str(draft)
    clean_split = tuple(
        str(sid) for sid in (split_sids or ()) if isinstance(sid, str) and sid
    )
    with _registry_lock:
        if focused_sid is not None:
            _web_focus_sid = str(focused_sid) or None
        if split_sids is not None:
            _web_split_sids = clean_split[:2]
    return True


def runtime_context_snapshot() -> dict:
    """Return the browser terminal owner's current, privacy-safe state."""
    with _registry_lock:
        terminals = list(_terminals.values())
        focused_sid = _web_focus_sid
        split_sids = list(_web_split_sids)
    entries = []
    for term in terminals:
        with term.state_lock:
            entry = {
                "sid": term.session_id or "",
                "agent": term.agent,
                "cwd": term.cwd,
                "alive": is_alive(term.id),
                "state": term.runtime_state,
                "busy": bool(term.runtime_busy),
                "draft": bool(term.input_draft.strip()),
                "reserved": bool(term.work_item_id),
                "reservation_item_id": term.work_item_id,
                "owner": "xterm",
                "terminal_id": term.id,
            }
        entries.append(entry)
    return {
        "focused_sid": focused_sid,
        "split_pair": split_sids,
        "runtimes": entries,
    }


def mark_turn_started(tid: str, file_version: tuple[int, int] | None = None) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        term.runtime_busy = True
        term.turn_file_version = file_version
    return True


def refresh_turn_state(
    tid: str,
    active: bool | None,
    file_version: tuple[int, int] | None,
) -> bool:
    """Clear a busy lease only after this turn changed the transcript and ended."""
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        # A transcript that grew is the child saying it is still working, and
        # when nobody is attached it is the ONLY thing that says so:
        # last_activity is otherwise touched only by our own reads, and reads
        # stop the moment the user looks at another pane. An agent left to think
        # for longer than the idle window therefore looked idle and was frozen
        # mid-turn, which is exactly what the field's own comment promises will
        # never happen.
        if file_version is not None and file_version != term.activity_file_version:
            if term.activity_file_version is not None:
                term.last_activity = time.monotonic()
            term.activity_file_version = file_version
        if (
            term.runtime_busy
            and active is False
            and file_version is not None
            and file_version != term.turn_file_version
        ):
            term.runtime_busy = False
            term.turn_file_version = file_version
        return term.runtime_busy


def is_runtime_busy(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        return term.runtime_busy


def mark_turn_finished(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        term.runtime_busy = False
        term.turn_file_version = None
    return True


def _cgroup_dir(pid: int) -> str | None:
    """The unified-hierarchy cgroup holding this process, if there is one."""

    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            for line in handle:
                hierarchy, _, path = line.strip().partition(":")
                if hierarchy == "0":
                    _, _, rel = path.partition(":")
                    candidate = os.path.join("/sys/fs/cgroup", (rel or path).lstrip("/"))
                    return candidate if os.path.isdir(candidate) else None
    except OSError:
        return None
    return None


def _rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


# Whether this *machine* can ever push a frozen runtime's pages out. Only
# reasons that hold for every cgroup on the box may set this, because one
# terminal that missed its scope must not switch reclaim off for the panes
# that did get one. Per-terminal refusals live on Terminal.reclaim_supported.
_reclaim_supported: bool | None = None

# EAGAIN is the kernel saying it reclaimed less than the 4G asked for, which is
# the normal case and the one that does the work: measured here, a 4G request
# against a 261MB process returned EAGAIN having released 254MB. It is the only
# errno that still counts as a successful pass.
_RECLAIM_PARTIAL_ERRNO = errno.EAGAIN

# Nothing about a different cgroup changes these, so the first one to come back
# settles it for the whole process: cgroupfs mounted read-only (sandboxes and
# some containers), or a kernel with no memory.reclaim at all.
_RECLAIM_MACHINE_ERRNOS = frozenset({errno.EROFS, errno.ENOENT, errno.ENODEV})

# Everything else is scoped to the runtime that hit it. EPERM/EACCES mean this
# child sits in a cgroup we do not own — exactly what spawn()'s plain-fork
# fallback produces — and EINVAL means the kernel rejected the payload without
# reclaiming anything (verified: a malformed write returns EINVAL with RSS
# unchanged, so retrying a hardcoded literal forever would be pointless).
_reported_reclaim_failures: set[str] = set()
_reclaim_report_lock = threading.Lock()


def _note_reclaim_failure(term: Terminal, cgroup: str, exc: OSError) -> None:
    """Latch a failed reclaim at the narrowest scope the errno justifies."""

    global _reclaim_supported

    code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
    term.reclaim_supported = False
    machine_wide = exc.errno in _RECLAIM_MACHINE_ERRNOS
    if machine_wide:
        _reclaim_supported = False
    with _reclaim_report_lock:
        if code in _reported_reclaim_failures:
            return
        _reported_reclaim_failures.add(code)
    print(
        f"[runtime] memory reclaim unavailable ({code}): writing "
        f"{cgroup}/memory.reclaim failed. "
        + (
            "No runtime on this machine can be reclaimed."
            if machine_wide
            else "This runtime is not in a cgroup we own; panes that got their "
            "own scope are unaffected."
        )
        + " Idle panes still freeze; their pages stay resident until the "
        "kernel swaps them under pressure.",
        file=sys.stderr,
        flush=True,
    )


def reclaim_memory(tid: str) -> float:
    """Push a frozen runtime's cold pages out, and report the MB released.

    A stopped process still holds its whole heap resident, which is most of
    what an idle agent costs. Waking stays a SIGCONT because pages fault back
    on demand, so this trades idle RSS for refault I/O rather than latency.

    This only works when the runtime sits in a cgroup the user may write to,
    which is why spawn() places each child in its own transient user scope. On
    a machine without a reachable user systemd the child stays in the inherited
    login session scope, which logind owns; this reports zero there rather than
    pretending, and the freeze alone still stops the process burning CPU.
    """

    global _reclaim_supported

    term = get(tid)
    if not term or _IS_WINDOWS:
        return 0.0
    with term.state_lock:
        if term.runtime_state != "paused" or term.reclaimed:
            return 0.0
        try:
            pid = term.proc.pid
        except Exception:
            return 0.0
        cgroup = _cgroup_dir(pid)
        if not cgroup:
            return 0.0
        # Two latches, deliberately separate. The global says the machine can
        # never do this; the per-terminal one says this particular runtime is
        # not in a cgroup we own, which is what spawn()'s plain-fork fallback
        # leaves behind and must not cost the panes that did get a scope.
        if _reclaim_supported is False or term.reclaim_supported is False:
            return 0.0
        before = _rss_mb(pid)
        try:
            with open(os.path.join(cgroup, "memory.reclaim"), "w", encoding="utf-8") as handle:
                handle.write("4G")
        except OSError as exc:
            # EAGAIN is the kernel reporting it freed less than the 4G asked
            # for, which is the pass that actually does the work. Every other
            # errno freed nothing, so it latches at whatever scope it applies.
            if exc.errno != _RECLAIM_PARTIAL_ERRNO:
                _note_reclaim_failure(term, cgroup, exc)
                return 0.0
        _reclaim_supported = True
        term.reclaim_supported = True
        term.reclaimed = True
        return max(0.0, before - _rss_mb(pid))


# How long a runtime is allowed to start before it may be frozen.
#
# An agent CLI is not usable the moment it is spawned. Codex reaches its input
# line in under a second but keeps bringing up MCP servers for another twelve,
# and an unreachable one costs thirty seconds of timeout on top. The linked
# sibling in a split is named standby the instant focus is elsewhere, which
# means zero idle tolerance, so with the old five-second window a Codex pane was
# routinely SIGSTOPped part-way through its own startup. It then rendered
# nothing and never finished, which reads exactly like "the chat will not open".
_PREWARM_SECONDS = 60.0


def pause(
    tid: str,
    *,
    protected: bool = False,
    prewarm_seconds: float = _PREWARM_SECONDS,
    min_idle_seconds: float = 0.0,
) -> bool:
    """Freeze an idle POSIX process group for a millisecond-scale resume.

    ``min_idle_seconds`` keeps a background runtime alive while it is still
    producing output. Turn tracking already covers an agent mid-answer, but a
    process can be busy without a turn being recorded, and freezing that one
    stalls it silently until the user happens to look at the pane again.
    """

    term = get(tid)
    if not term or _IS_WINDOWS:
        return False
    with term.state_lock:
        if (
            protected
            or term.runtime_busy
            or term.work_item_id is not None
            or term.runtime_state == "paused"
            or time.monotonic() - term.started_at < prewarm_seconds
            or (
                min_idle_seconds > 0
                and time.monotonic() - term.last_activity < min_idle_seconds
            )
        ):
            return False
        try:
            os.killpg(os.getpgid(term.proc.pid), signal.SIGSTOP)
        except (OSError, ProcessLookupError):
            return False
        term.runtime_state = "paused"
    return True


def _resume_locked(term: Terminal) -> bool:
    if term.runtime_state != "paused":
        return True
    term.last_activity = time.monotonic()
    term.reclaimed = False
    if _IS_WINDOWS:
        term.runtime_state = "live"
        return True
    try:
        os.killpg(os.getpgid(term.proc.pid), signal.SIGCONT)
    except (OSError, ProcessLookupError):
        return False
    term.runtime_state = "live"
    return True


def resume(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        return _resume_locked(term)


def live_terminal_ids() -> list[str]:
    """Every terminal the server currently owns.

    Idle sleep has to sweep all of them. The client only ever named the focused
    pane and its linked sibling, so every other pane a user had opened stayed
    resident for the life of the app.
    """

    with _registry_lock:
        return list(_terminals.keys())


def get_runtime_state(tid: str) -> str:
    term = get(tid)
    if not term:
        return "closed"
    with term.state_lock:
        return term.runtime_state


def resize(tid: str, rows: int, cols: int) -> bool:
    term = get(tid)
    if not term:
        return False
    try:
        rows = max(MIN_ROWS, int(rows))
        cols = max(MIN_COLS, int(cols))
        term.proc.setwinsize(rows, cols)
        term.rows, term.cols = rows, cols
        return True
    except OSError:
        return False


def read_available(tid: str, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
    """Non-blocking read. Returns b'' when nothing's ready, None when the PTY is gone."""
    term = get(tid)
    if not term:
        return None

    if _IS_WINDOWS:
        # Drain up to max_bytes from the byte buffer atomically — no queue
        # swap, no race with the reader thread. Order is preserved because
        # the reader only ever appends to term.buf under the same lock.
        if not term.data_event.wait(timeout):
            return b""
        with term.buf_lock:
            if term.buf:
                term.last_activity = time.monotonic()
                head = bytes(term.buf[:max_bytes])
                del term.buf[:max_bytes]
                if not term.buf:
                    term.data_event.clear()
                return head
            if term.eof:
                return None
            # Spurious wakeup — clear and indicate nothing ready
            term.data_event.clear()
            return b""

    try:
        fd = term.proc.fd
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return b""
        chunk = os.read(fd, max_bytes)
        if chunk:
            # Output counts as activity: an agent still printing is working,
            # even while the user is reading a different pane.
            term.last_activity = time.monotonic()
        return chunk
    except (OSError, EOFError):
        return None


def is_alive(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    try:
        return term.proc.isalive()
    except Exception:
        return False


def kill(tid: str) -> None:
    global _web_focus_sid, _web_split_sids
    with _registry_lock:
        term = _terminals.pop(tid, None)
        stale_sids = [sid for sid, mapped_tid in _session_tids.items() if mapped_tid == tid]
        for sid in stale_sids:
            _session_tids.pop(sid, None)
        if _web_focus_sid in stale_sids:
            _web_focus_sid = None
        if any(sid in stale_sids for sid in _web_split_sids):
            _web_split_sids = tuple(
                sid for sid in _web_split_sids if sid not in stale_sids
            )
    if not term:
        return
    try:
        if _IS_WINDOWS:
            term.proc.terminate()
        else:
            term.proc.terminate(force=True)
    except Exception:
        pass


def kill_all() -> int:
    """Terminate every PTY owned by this renderer process."""

    with _registry_lock:
        tids = list(_terminals)
    for tid in tids:
        kill(tid)
    return len(tids)


# ── browser flow control + reattach ─────────────────────────────────────────
# Two problems the xterm.js path has that the native GTK VTE never had.
#
# 1. Nothing bounded how fast PTY output was pushed at the renderer. A TUI
#    repainting thousands of lines outruns xterm.js's parser and piles up
#    unparsed frames in the browser. The xterm.js flow control guide's answer
#    is an ack protocol with watermarks: the renderer credits bytes it has
#    actually parsed, in batches, and the sender stops draining the PTY only
#    once too much is outstanding. Pausing after every chunk instead would put
#    a full round trip between each read and collapse throughput.
#
# 2. A dropped websocket killed the child, so a reload, a backend restart, or
#    a flaky link lost the whole claude/codex session. A drop now *detaches*:
#    a bounded tail of output keeps draining so the child never blocks on a
#    full PTY buffer, and reconnecting to the same terminal id replays it.

FLOW_ACK_BYTES = 131072        # renderer acks every 128 KiB it has parsed
FLOW_HIGH_WATER = 524288       # 512 KiB outstanding -> stop draining the PTY
FLOW_LOW_WATER = 131072        # back under 128 KiB -> drain again
DETACH_GRACE_SECONDS = 120.0   # how long an orphaned PTY waits for a reconnect
BACKLOG_MAX_BYTES = 262144     # tail of output kept while nobody is attached
_RESET_SEQUENCE = b"\x1bc"     # RIS, resyncs a renderer whose backlog was cut


def enable_flow_control(tid: str) -> bool:
    """Arm ack accounting once the renderer says it speaks the protocol.

    Off until then, so a client that never acks streams exactly like before
    instead of deadlocking behind a watermark nobody will clear.
    """
    term = get(tid)
    if not term:
        return False
    with term.flow_lock:
        term.flow_enabled = True
        # Keep whatever this attachment already sent. The reconnect backlog
        # goes out before the renderer can hand us its handshake, so zeroing
        # here would let a replay escape the ledger entirely; the renderer
        # acks those bytes anyway once it parses them.
        if term.flow_unacked >= FLOW_HIGH_WATER:
            term.flow_resume.clear()
        else:
            term.flow_resume.set()
    return True


def note_sent(tid: str, nbytes: int) -> bool:
    """Count bytes handed to the renderer. True once the sender must pause."""
    term = get(tid)
    if not term or nbytes <= 0:
        return False
    with term.flow_lock:
        term.flow_unacked += int(nbytes)
        if not term.flow_enabled:
            # Pre-handshake bytes are counted so the ledger is honest once the
            # renderer arms flow control, but they never gate the sender: a
            # client that never acks has to keep streaming, not deadlock.
            return False
        if term.flow_unacked >= FLOW_HIGH_WATER:
            term.flow_resume.clear()
            return True
        return not term.flow_resume.is_set()


def note_ack(tid: str, nbytes: int) -> int:
    """Credit parsed bytes; releases the sender under the low watermark.

    The gap between the two watermarks is the point: resuming at the same
    level we paused at would flap the reader on every chunk.
    """
    term = get(tid)
    if not term:
        return 0
    with term.flow_lock:
        term.flow_unacked = max(0, term.flow_unacked - max(0, int(nbytes)))
        if term.flow_unacked <= FLOW_LOW_WATER:
            term.flow_resume.set()
        return term.flow_unacked


def flow_paused(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.flow_lock:
        return term.flow_enabled and not term.flow_resume.is_set()


def wait_for_flow(tid: str, timeout: float = 0.1) -> bool:
    """Block a sender thread while the renderer is behind. True when clear."""
    term = get(tid)
    if not term:
        return False
    return term.flow_resume.wait(timeout)


def flow_snapshot(tid: str) -> dict | None:
    term = get(tid)
    if not term:
        return None
    with term.flow_lock:
        return {
            "enabled": term.flow_enabled,
            "unacked": term.flow_unacked,
            "paused": term.flow_enabled and not term.flow_resume.is_set(),
            "high_water": FLOW_HIGH_WATER,
            "low_water": FLOW_LOW_WATER,
        }


def attach(tid: str) -> tuple[int, bytes] | None:
    """Bind one renderer socket to this PTY and hand back any missed output.

    Returns ``(token, backlog)``. The token identifies this attachment; a
    later attach supersedes it so a stale socket can't detach or kill a PTY
    that a newer one already owns.
    """
    term = get(tid)
    if not term:
        return None
    with term.flow_lock:
        term.attach_seq += 1
        token = term.attach_seq
        term.attached = True
        term.detached_at = None
        term.flow_enabled = False
        term.flow_unacked = 0
        term.flow_resume.set()
        drain = term.drain_thread
        term.drain_thread = None
    # The detached drain owns the read side while nobody is attached and exits
    # within one poll interval of the attach_seq bump. Joining it before the
    # caller starts its own reader is what keeps bytes ordered across a
    # reconnect: two threads on one fd would interleave the replay.
    stranded = False
    if (
        drain is not None
        and drain.is_alive()
        and drain is not threading.current_thread()
    ):
        drain.join(timeout=2.0)
        # Should be unreachable: the loop's longest blocking call is a 50ms
        # select. If it ever is reachable, the drain can still append after we
        # take the backlog below, and those bytes would be stranded with no
        # reader left to deliver them. Treat that as a cut stream rather than
        # handing the renderer a replay with a silent hole in it.
        stranded = drain.is_alive()
    with term.flow_lock:
        backlog = bytes(term.backlog)
        truncated = term.backlog_truncated or stranded
        term.backlog = bytearray()
        term.backlog_truncated = False
    if truncated:
        # The tail we kept starts mid-stream, so the renderer's parser state
        # and cursor position no longer match it. A full reset in front of the
        # replay is the only way to land in a defined state; the alternative
        # is a scrambled screen.
        backlog = _RESET_SEQUENCE + backlog
    return token, backlog


def is_attached(tid: str, token: int) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.flow_lock:
        return term.attached and term.attach_seq == token


def attachment_snapshot(tid: str) -> dict | None:
    term = get(tid)
    if not term:
        return None
    with term.flow_lock:
        return {
            "attached": term.attached,
            "token": term.attach_seq,
            "detached_at": term.detached_at,
            "backlog_bytes": len(term.backlog),
            "backlog_truncated": term.backlog_truncated,
            "draining": bool(term.drain_thread and term.drain_thread.is_alive()),
        }


def _detach_is_protected(term: Terminal) -> bool:
    """Work in flight outlives a disconnected browser."""
    with term.state_lock:
        return bool(term.work_item_id) or bool(term.runtime_busy)


def _buffer_detached_output(term: Terminal, chunk: bytes) -> None:
    with term.flow_lock:
        term.backlog.extend(chunk)
        overflow = len(term.backlog) - BACKLOG_MAX_BYTES
        if overflow > 0:
            del term.backlog[:overflow]
            term.backlog_truncated = True


def _detached_drain_loop(term: Terminal, token: int, grace: float) -> None:
    """Keep an orphaned PTY readable, then reap it if nobody comes back."""
    deadline = time.monotonic() + grace
    while True:
        if get(term.id) is None:
            return
        with term.flow_lock:
            if term.attach_seq != token or term.attached:
                return
        chunk = read_available(term.id, max_bytes=65536, timeout=0.05)
        if chunk is None:
            kill(term.id)
            return
        if chunk:
            _buffer_detached_output(term, chunk)
        if time.monotonic() < deadline:
            continue
        if _detach_is_protected(term):
            # A reserved or mid-turn runtime is doing real work; a closed
            # browser tab is not a reason to kill it. Re-arm and re-check.
            deadline = time.monotonic() + grace
            continue
        kill(term.id)
        return


def detach(tid: str, token: int, *, grace: float | None = None) -> bool:
    """Release the renderer socket without killing the child."""
    term = get(tid)
    if not term:
        return False
    with term.flow_lock:
        if term.attach_seq != token:
            return False  # a newer socket already took over
        term.attached = False
        term.detached_at = time.monotonic()
        term.flow_enabled = False
        term.flow_unacked = 0
        term.flow_resume.set()
        if term.drain_thread is not None and term.drain_thread.is_alive():
            return True
        thread = threading.Thread(
            target=_detached_drain_loop,
            args=(
                term,
                token,
                DETACH_GRACE_SECONDS if grace is None else float(grace),
            ),
            daemon=True,
            name=f"pty-detached-{tid[:8]}",
        )
        term.drain_thread = thread
    thread.start()
    return True
