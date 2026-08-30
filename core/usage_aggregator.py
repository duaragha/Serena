"""Account-global rate-limit reduction and atomic state materialization."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
WINDOWS = ("five_hour", "seven_day")
CODEX_WINDOW_MINUTES = {
    "five_hour": 5 * 60,
    "seven_day": 7 * 24 * 60,
}
RESET_JITTER_SECONDS = 60
_IS_WINDOWS = os.name == "nt"
PROVIDER_FIELDS = (
    "available",
    "source",
    "updated_at",
    "model",
    "version",
    "plan_type",
    "limit_id",
)


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number


def codex_window_name(window: Any, fallback: str) -> str:
    """Map a Codex rate-limit window by duration, independent of API slot."""
    if fallback not in WINDOWS or not isinstance(window, dict):
        return fallback
    minutes = _number(window.get("window_minutes"))
    if minutes is None or minutes <= 0:
        return fallback
    return min(
        CODEX_WINDOW_MINUTES,
        key=lambda name: abs(minutes - CODEX_WINDOW_MINUTES[name]),
    )


def _epoch(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    epoch = int(number)
    return epoch if epoch > 0 else None


def _window(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    used = _number(value.get("used_percentage", value.get("used_percent")))
    reset = _epoch(value.get("resets_at"))
    observed = _number(value.get("observed_at"))
    if used is not None:
        out["used_percentage"] = used
    if reset is not None:
        out["resets_at"] = reset
    if observed is not None:
        out["observed_at"] = observed
    source = value.get("source")
    if isinstance(source, str) and source:
        out["source"] = source
    return out


def _provider(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out = {key: value[key] for key in PROVIDER_FIELDS if key in value}
    for name in WINDOWS:
        window = _window(value.get(name))
        if window:
            out[name] = window
    return out


def _state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"schema_version": SCHEMA_VERSION}
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    updated_at = _number(value.get("updated_at"))
    if updated_at is not None:
        out["updated_at"] = updated_at
    for provider in ("claude", "codex"):
        service = _provider(value.get(provider))
        if service:
            out[provider] = service
    return out


def merge_window(
    current: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    *,
    observed_at: float | int | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Merge one rate-limit generation without turning unknowns into zero."""
    old = _window(current)
    new = _window(observed)
    if not new:
        return old

    incoming_at = _number(new.get("observed_at", observed_at))
    incoming_source = new.get("source") or source
    old_reset = old.get("resets_at")
    new_reset = new.get("resets_at")
    same_generation = (
        old_reset is not None
        and new_reset is not None
        and abs(new_reset - old_reset) <= RESET_JITTER_SECONDS
    )

    if (
        old_reset is not None
        and new_reset is not None
        and new_reset < old_reset
        and not same_generation
    ):
        return old

    generation_changed = new_reset is not None and (
        old_reset is None or (new_reset > old_reset and not same_generation)
    )
    if generation_changed:
        out = {"resets_at": new_reset}
        if "used_percentage" in new:
            out["used_percentage"] = new["used_percentage"]
        if incoming_at is not None:
            out["observed_at"] = incoming_at
        if incoming_source:
            out["source"] = incoming_source
        return out

    out = dict(old)
    if old_reset is None and new_reset is not None:
        out["resets_at"] = new_reset

    old_used = old.get("used_percentage")
    new_used = new.get("used_percentage")
    # A percentage with no reset cannot be assigned to a known generation.
    comparable = same_generation or old_reset == new_reset or old_reset is None
    if new_used is not None and comparable and (old_used is None or new_used > old_used):
        out["used_percentage"] = new_used
        if incoming_source:
            out["source"] = incoming_source

    old_at = _number(old.get("observed_at"))
    if incoming_at is not None and (old_at is None or incoming_at > old_at):
        out["observed_at"] = incoming_at
    if "source" not in out and incoming_source:
        out["source"] = incoming_source
    return out


def merge_provider(
    current: dict[str, Any] | None,
    observation: dict[str, Any],
) -> dict[str, Any]:
    old = _provider(current)
    source = observation.get("source")
    source = source if isinstance(source, str) and source else None
    observed_at = _number(observation.get("observed_at"))
    windows = observation.get("windows")
    windows = windows if isinstance(windows, dict) else {}

    out = old
    accepted_window = False
    for name in WINDOWS:
        incoming = windows.get(name)
        if not isinstance(incoming, dict):
            continue
        merged = merge_window(out.get(name), incoming, observed_at=observed_at, source=source)
        if merged != _window(out.get(name)):
            accepted_window = True
        if merged:
            out[name] = merged

    old_at = _number(out.get("updated_at"))
    if observed_at is not None and (old_at is None or observed_at >= old_at):
        out["updated_at"] = observed_at
        if source:
            out["source"] = source
        for key in ("model", "version", "plan_type", "limit_id"):
            value = observation.get(key)
            if value is not None:
                out[key] = value
    elif source and "source" not in out:
        out["source"] = source

    if accepted_window or any(
        isinstance(out.get(name), dict) and out[name].get("used_percentage") is not None
        for name in WINDOWS
    ):
        out["available"] = True
    elif "available" not in out:
        out["available"] = False
    return out


def merge_observation(state: dict[str, Any] | None, observation: dict[str, Any]) -> dict[str, Any]:
    """Return a new bounded materialized state for one provider observation."""
    out = _state(state)
    provider = observation.get("provider")
    if provider not in ("claude", "codex"):
        raise ValueError("provider must be 'claude' or 'codex'")

    out[provider] = merge_provider(out.get(provider), observation)
    observed_at = _number(observation.get("observed_at"))
    updated_at = _number(out.get("updated_at"))
    if observed_at is not None and (updated_at is None or observed_at > updated_at):
        out["updated_at"] = observed_at
    return out


def reduce_observations(
    observations: Iterable[dict[str, Any]], state: dict[str, Any] | None = None
) -> dict[str, Any]:
    out = _state(state)
    for observation in observations:
        out = merge_observation(out, observation)
    return out


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"), sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        if not _IS_WINDOWS:
            # Fsyncing the directory is what makes the rename durable on POSIX.
            # Windows cannot open a directory as a file at all: this raised
            # PermissionError AFTER the replace had already landed, so every
            # write appeared to fail while actually succeeding. The CLI turned
            # that into exit 2 and the statusline tap reported nothing written.
            # os.replace is atomic on Windows and there is no directory handle
            # to flush.
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _lock_file(lock) -> None:
    if _IS_WINDOWS:
        import msvcrt

        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock) -> None:
    if _IS_WINDOWS:
        import msvcrt

        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def locked_materialize(path: Path | str, observation: dict[str, Any]) -> dict[str, Any]:
    """Lock, merge, and atomically publish one observation."""
    state_path = Path(path).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+b") as lock:
        _lock_file(lock)
        try:
            state = merge_observation(_read_state(state_path), observation)
            _atomic_write(state_path, state)
        finally:
            _unlock_file(lock)
    return state


def observation_from_statusline(
    payload: Any, *, observed_at: float | int | None = None
) -> dict[str, Any] | None:
    """Turn Claude Code's statusline payload into an observation.

    Claude's rate limits are only ever visible here. Unlike Codex, which writes
    its usage into session files that can be read at any time, nothing on disk
    records how much of Claude's window is gone: the numbers arrive on stdin
    when Claude Code renders the status line, and are lost if nobody keeps them.

    Returns None when the payload carries no window, so a render that happens to
    have no rate-limit block never overwrites real numbers with nothing.
    """
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None

    windows: dict[str, Any] = {}
    for name in WINDOWS:
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percentage"))
        resets_at = _epoch(window.get("resets_at"))
        if used is None and resets_at is None:
            continue
        entry: dict[str, Any] = {}
        if used is not None:
            entry["used_percentage"] = used
        if resets_at is not None:
            entry["resets_at"] = resets_at
        windows[name] = entry
    if not windows:
        return None

    model = payload.get("model")
    if isinstance(model, dict):
        model_name = model.get("display_name") or model.get("id") or ""
    else:
        model_name = model or ""

    observation: dict[str, Any] = {
        "provider": "claude",
        "source": "claude-statusline",
        "observed_at": int(observed_at if observed_at is not None else time.time()),
        "windows": windows,
    }
    if model_name:
        observation["model"] = str(model_name).split(" (")[0]
    version = payload.get("version")
    if version:
        observation["version"] = str(version)
    return observation


def default_state_file() -> Path:
    """Where the app looks for live usage, on either platform."""
    from core.config import DATA_DIR

    return Path(DATA_DIR) / "live-usage.json"


def record_statusline(payload: Any, state_file: Path | str | None = None) -> bool:
    """Persist one statusline render. Never raises: a status line must still draw."""
    try:
        observation = observation_from_statusline(payload)
        if observation is None:
            return False
        locked_materialize(state_file or default_state_file(), observation)
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        observation = json.load(os.sys.stdin)
        if not isinstance(observation, dict):
            raise ValueError("observation must be a JSON object")
        locked_materialize(args.state_file, observation)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
