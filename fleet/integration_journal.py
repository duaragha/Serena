"""Durable, exact-file integration intents. Caller holds the repository mutex.

An intent is committed before Git changes the combined checkout. Recovery may
only accept exact postimages or restore a mixture of its exact pre/postimages.
Anything else is foreign/uncertain work and is never overwritten.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


class JournalError(RuntimeError):
    pass


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _target(root, name):
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p in {".", "..", ".git"} for p in path.parts):
        raise JournalError("invalid integration journal path")
    target = root / path
    for parent in target.parents:
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise JournalError(f"integration journal refuses redirected parent: {name}")
    return target


def capture(root, paths):
    result = {}
    for name in paths:
        target = _target(root, name)
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            result[name] = None
            continue
        if stat.S_ISLNK(metadata.st_mode):
            mode, data = "120000", os.fsencode(os.readlink(target))
        elif stat.S_ISREG(metadata.st_mode):
            mode = "100755" if metadata.st_mode & 0o111 else "100644"
            data = target.read_bytes()
        else:
            raise JournalError(f"integration journal refuses non-file: {name}")
        result[name] = {"mode": mode, "data": base64.b64encode(data).decode("ascii"),
                        "permissions": stat.S_IMODE(metadata.st_mode)}
    return result


def _same(left, right):
    if left is None or right is None:
        return left is right
    # Git owns executable mode, not unrelated chmod bits. Retain all original
    # permissions for rollback without mistaking umask differences for edits.
    return left["mode"] == right["mode"] and left["data"] == right["data"]


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class IntegrationJournal:
    def __init__(self, store, root, workspace, patch):
        self.store = store
        self.root = Path(root)
        self.identity = {"root": str(self.root.resolve()), "run_id": workspace.run_id,
                         "worker_key": workspace.worker_key, "workspace": workspace.path,
                         "base_head": workspace.base_head, "branch": workspace.branch,
                         "created_at": workspace.created_at,
                         "patch_sha256": hashlib.sha256(patch.encode("utf-8", errors="surrogateescape")).hexdigest()}
        # A branch switch is not an invitation to apply an old intent elsewhere.
        # Keep the lookup key stable across that switch, then reject the changed
        # branch in the stored identity instead of inventing a fresh intent.
        self.key = hashlib.sha256(_encoded(self.identity).encode()).hexdigest()
        from fleet.isolation import _git
        branch = _git(self.root, "symbolic-ref", "--quiet", "HEAD", check=False)
        self.identity["target_branch"] = branch.stdout.strip() or _git(self.root, "rev-parse", "HEAD").stdout.strip()
        self.document = None

    def load(self):
        with self.store._connect() as db:
            row = db.execute("SELECT document, digest FROM fleet_integration_intents WHERE intent_id=?",
                             (self.key,)).fetchone()
        if row is None:
            return False
        document, digest = row
        if hashlib.sha256(document.encode()).hexdigest() != digest:
            raise JournalError("integration journal fingerprint mismatch")
        try:
            self.document = json.loads(document)
            if self.document["identity"] != self.identity or self.document["version"] != 1:
                raise JournalError("integration journal identity or target branch changed")
            if set(self.document["pre"]) != set(self.document["post"]):
                raise ValueError("path set changed")
            for image in (self.document["pre"], self.document["post"]):
                for name, value in image.items():
                    _target(self.root, name)
                    if value is not None:
                        if value["mode"] not in {"100644", "100755", "120000"}:
                            raise ValueError("unknown mode")
                        base64.b64decode(value["data"], validate=True)
                        if not isinstance(value["permissions"], int) or not 0 <= value["permissions"] <= 0o7777:
                            raise ValueError("invalid permissions")
        except (ValueError, KeyError, TypeError) as error:
            raise JournalError("integration journal is malformed") from error
        return True

    def prepare(self, workspace_root, paths, *, rollback_ref=""):
        document = {"version": 1, "identity": self.identity,
                    "pre": capture(self.root, paths), "post": capture(Path(workspace_root), paths),
                    "rollback_ref": rollback_ref}
        encoded = _encoded(document)
        with self.store._connect() as db:
            # The preimage must survive a process/OS crash before Git may write.
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO fleet_integration_intents VALUES (?,?,?,?)",
                       (self.key, self.identity["run_id"], encoded, hashlib.sha256(encoded.encode()).hexdigest()))
        self.document = document

    def state(self):
        pre, post = self.document["pre"], self.document["post"]
        current = capture(self.root, list(pre))
        unknown = [p for p in pre if not _same(current[p], pre[p]) and not _same(current[p], post[p])]
        if unknown:
            raise JournalError("integration journal refuses foreign or partial-file changes: " + ", ".join(unknown[:20]))
        if all(_same(current[p], pre[p]) for p in pre):
            return "pre"
        if all(_same(current[p], post[p]) for p in pre):
            return "post"
        return "mixed"

    def restore_pre(self):
        # Check ALL paths before changing ANY, then recheck each write boundary.
        self.state()
        for name, before in self.document["pre"].items():
            current = capture(self.root, [name])[name]
            if _same(current, before):
                continue
            if not _same(current, self.document["post"][name]):
                raise JournalError("integration journal changed during rollback: " + name)
            target = _target(self.root, name)
            if before is None:
                target.unlink()
                _sync_directory(target.parent)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Rename a complete entry, never truncate the only surviving copy.
            with tempfile.TemporaryDirectory(prefix=".serena-restore-", dir=target.parent) as temporary:
                staged = Path(temporary) / "entry"
                data = base64.b64decode(before["data"], validate=True)
                if before["mode"] == "120000":
                    os.symlink(os.fsdecode(data), staged)
                else:
                    with staged.open("wb") as handle:
                        handle.write(data)
                        handle.flush()
                        if hasattr(os, "fchmod"):
                            os.fchmod(handle.fileno(), before["permissions"])
                        else:
                            os.chmod(staged, before["permissions"])
                        os.fsync(handle.fileno())
                os.replace(staged, target)
                _sync_directory(target.parent)
        if self.state() != "pre":
            raise JournalError("integration journal rollback did not restore its preimage")
