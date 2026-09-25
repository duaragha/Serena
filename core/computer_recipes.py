"""Private, bounded browser flows learned only from completed tasks."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from core.computer_client import state_dir
from core.computer_platform import ComputerError
from core.computer_web import validate_steps

MAX_BYTES = 128 * 1024
MAX_STEPS = 100
MATCH_THRESHOLD = 0.85


def normalize(text):
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def origin(url):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def bounded(batches, *, partial=False):
    if (not batches and not partial) or sum(len(batch) for batch in batches) > MAX_STEPS:
        raise ComputerError("recipe exceeds its step limit or is empty")
    for batch in batches:
        validate_steps(batch)
    if len(json.dumps(batches).encode()) > MAX_BYTES // 2:
        raise ComputerError("recipe exceeds its size limit")
    return json.loads(json.dumps(batches))


class RecipeStore:
    def __init__(self, directory=None, *, clock=time.time):
        self.directory = Path(directory) if directory is not None else state_dir() / "recipes"
        self.clock = clock
        self.lock = threading.RLock()

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ComputerError("invalid recipe id")
        return self.directory / f"{identifier}.json"

    def load(self, identifier):
        path = self._path(identifier)
        try:
            if self.directory.is_symlink() or path.is_symlink():
                raise ValueError("symlink")
            with path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("oversized")
            value = json.loads(raw)
            if value["version"] != 1 or value["id"] != identifier:
                raise ValueError("schema")
            bounded(value["batches"], partial=value.get("partial", False))
            if not isinstance(value["task"], str) or len(value["task"]) > 4000:
                raise ValueError("task")
            if not isinstance(value["origin"], str):
                raise ValueError("origin")
            for field in ("created_at", "last_success", "success_count", "failure_count"):
                if not isinstance(value[field], (int, float)) or value[field] < 0:
                    raise ValueError(field)
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ComputerError("recipe unavailable or invalid") from exc

    def _write(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        if len(raw) > MAX_BYTES:
            raise ComputerError("recipe exceeds its size limit")
        if self.directory.is_symlink():
            raise ComputerError("recipe directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".recipe-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(value["id"]))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, task, starting_url, batches, *, partial=False):
        with self.lock:
            now = self.clock()
            location = ""
            if origin(starting_url):
                parts = urlsplit(starting_url)
                location = origin(starting_url) + parts.path
                if parts.query:
                    location += "?" + parts.query
                if parts.fragment:
                    location += "#" + parts.fragment
                validate_steps([{"goto": location}])
            value = {"version": 1, "id": uuid.uuid4().hex, "task": task[:4000],
                     "origin": origin(starting_url), "batches": bounded(batches, partial=partial),
                     "starting_url": location, "partial": partial,
                     "created_at": now, "last_success": now, "success_count": 1,
                     "failure_count": 0}
            self._write(value)
            return value

    def outcome(self, identifier, ok):
        with self.lock:
            value = self.load(identifier)
            key = "success_count" if ok else "failure_count"
            value[key] += 1
            value["last_success" if ok else "last_failure"] = self.clock()
            self._write(value)

    def matches(self, task, *, threshold=MATCH_THRESHOLD, limit=3):
        tokens = set(normalize(task).split())
        if not tokens:
            return []
        matches = []
        for path in sorted(self.directory.glob("*.json"))[:500]:
            try:
                value = self.load(path.stem)
                if not value["batches"]:
                    continue
                other = set(normalize(value["task"]).split())
                score = len(tokens & other) / len(tokens | other)
                if score >= threshold:
                    matches.append({"recipe_id": value["id"], "origin": value["origin"],
                                    "step_count": sum(map(len, value["batches"])),
                                    "partial": value.get("partial", False),
                                    "last_success": value["last_success"], "score": score})
            except (ComputerError, KeyError, TypeError, ValueError):
                continue
        return sorted(matches, key=lambda item: (-item["score"], -item["last_success"]))[:limit]
