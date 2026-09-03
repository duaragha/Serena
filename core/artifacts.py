"""Scoped capability links for artifacts produced by Serena work jobs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARTIFACT_ROOT = Path.home() / ".local" / "state" / "serena" / "artifacts"
DEFAULT_ARTIFACT_DB = Path.home() / ".local" / "state" / "serena" / "artifacts.sqlite3"
DEFAULT_ARTIFACT_KEY = Path.home() / ".config" / "serena" / "artifact.key"
DEFAULT_ARTIFACT_TTL_SECONDS = 24 * 60 * 60
MAX_ARTIFACT_BYTES = 512 * 1024
ARTIFACT_RECEIPT_MAX_AGE_SECONDS = 5 * 60
ARTIFACT_RECEIPT_RETENTION_SECONDS = 60 * 60
MAX_ACTIVE_ARTIFACT_RECEIPTS = 1_024
_RECEIPT_DOMAIN = b"serena-artifact-receipt-v1\0"


class ArtifactReceiptCapacityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactLink:
    artifact_id: str
    job_id: str
    name: str
    path: Path
    content_type: str
    size: int
    sha256: str
    expires_at: int
    token: str
    origin_session_id: str = ""
    fleet_run_id: str = ""
    fleet_worker_key: str = ""
    created_at: float = 0.0

    @property
    def url(self) -> str:
        return f"/artifacts/{self.token}"


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    link: ArtifactLink
    data: bytes


class ArtifactRegistry:
    def __init__(
        self,
        *,
        root: Path | None = None,
        db_path: Path | None = None,
        key_path: Path | None = None,
    ) -> None:
        self.root = Path(
            root or os.environ.get("SERENA_ARTIFACT_ROOT", "").strip() or DEFAULT_ARTIFACT_ROOT
        ).expanduser()
        self.db_path = Path(
            db_path or os.environ.get("SERENA_ARTIFACT_DB", "").strip() or DEFAULT_ARTIFACT_DB
        ).expanduser()
        self.key_path = Path(
            key_path or os.environ.get("SERENA_ARTIFACT_KEY", "").strip() or DEFAULT_ARTIFACT_KEY
        ).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def job_directory(self, job_id: str) -> Path:
        if not _safe_identifier(job_id):
            raise ValueError("invalid job id")
        root = self.root.resolve()
        directory = root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError("artifact job directory may not be a symlink")
        return directory

    def write_job_artifact(
        self,
        *,
        job_id: str,
        name: str,
        content: str | bytes,
    ) -> Path:
        """Atomically replace one artifact without following attacker-made links."""

        if not _safe_identifier(job_id):
            raise ValueError("invalid job id")
        clean_name = _clean_artifact_name(name)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if not data or len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact size is outside the allowed range")

        root = self.root.resolve()
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(root, directory_flags)
        job_fd = -1
        temporary = f".{clean_name}.{uuid.uuid4().hex}.tmp"
        try:
            with suppress(FileExistsError):
                os.mkdir(job_id, mode=0o700, dir_fd=root_fd)
            job_fd = os.open(
                job_id,
                directory_flags | nofollow,
                dir_fd=root_fd,
            )
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=job_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("artifact write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary,
                clean_name,
                src_dir_fd=job_fd,
                dst_dir_fd=job_fd,
            )
            os.fsync(job_fd)
        finally:
            if job_fd >= 0:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=job_fd)
                os.close(job_fd)
            os.close(root_fd)
        return root / job_id / clean_name

    def register(
        self,
        *,
        job_id: str,
        path: Path,
        name: str,
        ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
        origin_session_id: str = "",
        fleet_run_id: str = "",
        fleet_worker_key: str = "",
    ) -> ArtifactLink:
        if not _safe_identifier(job_id):
            raise ValueError("invalid job id")
        origin_session_id = _bounded_origin(origin_session_id, "origin session id")
        fleet_run_id = _bounded_origin(fleet_run_id, "Fleet run id")
        fleet_worker_key = _bounded_origin(fleet_worker_key, "Fleet worker key")
        if fleet_worker_key and not fleet_run_id:
            raise ValueError("Fleet worker provenance requires a Fleet run id")
        source = Path(path)
        root = self.root.resolve()
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("artifact path escapes the managed root")
        if _has_symlink_between(root, source):
            raise ValueError("artifact path may not contain symlinks")
        size = resolved.stat().st_size
        if size < 1 or size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact size is outside the allowed range")
        clean_name = _clean_artifact_name(name)
        artifact_id = str(uuid.uuid4())
        expires_at = int(time.time()) + max(60, min(int(ttl_seconds), 7 * 24 * 60 * 60))
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        content_type = mimetypes.guess_type(clean_name)[0] or "application/octet-stream"
        created_at = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, job_id, name, path, content_type, size,
                    sha256, expires_at, created_at, origin_session_id,
                    fleet_run_id, fleet_worker_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    job_id,
                    clean_name,
                    str(resolved),
                    content_type,
                    size,
                    digest,
                    expires_at,
                    created_at,
                    origin_session_id,
                    fleet_run_id,
                    fleet_worker_key,
                ),
            )
        token = self._token(artifact_id, job_id, expires_at)
        return ArtifactLink(
            artifact_id=artifact_id,
            job_id=job_id,
            name=clean_name,
            path=resolved,
            content_type=content_type,
            size=size,
            sha256=digest,
            expires_at=expires_at,
            token=token,
            origin_session_id=origin_session_id,
            fleet_run_id=fleet_run_id,
            fleet_worker_key=fleet_worker_key,
            created_at=created_at,
        )

    def search(
        self,
        query: str = "",
        *,
        origin_session_id: str = "",
        fleet_run_id: str = "",
        limit: int = 100,
        include_expired: bool = False,
    ) -> list[ArtifactLink]:
        """Return bounded capability links without exposing managed file paths."""

        clean_query = " ".join(str(query or "").split())[:200]
        clauses: list[str] = []
        values: list[object] = []
        if not include_expired:
            clauses.append("expires_at >= ?")
            values.append(int(time.time()))
        if origin_session_id:
            clauses.append("origin_session_id = ?")
            values.append(_bounded_origin(origin_session_id, "origin session id"))
        if fleet_run_id:
            clauses.append("fleet_run_id = ?")
            values.append(_bounded_origin(fleet_run_id, "Fleet run id"))
        if clean_query:
            pattern = "%" + _like(clean_query) + "%"
            clauses.append(
                "(name LIKE ? ESCAPE '\\' OR job_id LIKE ? ESCAPE '\\' "
                "OR origin_session_id LIKE ? ESCAPE '\\' "
                "OR fleet_run_id LIKE ? ESCAPE '\\' "
                "OR fleet_worker_key LIKE ? ESCAPE '\\')"
            )
            values.extend([pattern] * 5)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(min(200, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts"
                + where
                + " ORDER BY created_at DESC, artifact_id DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [self._link_from_row(row) for row in rows]

    def attach_provenance(
        self,
        artifact_id: str,
        *,
        origin_session_id: str = "",
        fleet_run_id: str = "",
        fleet_worker_key: str = "",
    ) -> bool:
        """Attach a late-arriving origin without changing artifact capability data."""

        if not _safe_identifier(artifact_id):
            raise ValueError("invalid artifact id")
        session = _bounded_origin(origin_session_id, "origin session id")
        run = _bounded_origin(fleet_run_id, "Fleet run id")
        worker = _bounded_origin(fleet_worker_key, "Fleet worker key")
        if worker and not run:
            raise ValueError("Fleet worker provenance requires a Fleet run id")
        if not any((session, run, worker)):
            raise ValueError("artifact provenance is required")
        assignments: list[str] = []
        values: list[object] = []
        for column, value in (
            ("origin_session_id", session),
            ("fleet_run_id", run),
            ("fleet_worker_key", worker),
        ):
            if value:
                assignments.append(f"{column} = ?")
                values.append(value)
        values.append(artifact_id)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE artifacts SET " + ", ".join(assignments) + " WHERE artifact_id = ?",
                tuple(values),
            )
        return updated.rowcount == 1

    def resolve(self, token: str) -> ArtifactLink | None:
        payload = self.read(token)
        return payload.link if payload is not None else None

    def read(self, token: str) -> ArtifactPayload | None:
        """Verify and snapshot an artifact so serving cannot race a path swap."""

        payload = self._verify_token(token)
        if payload is None:
            return None
        artifact_id, job_id, expires_at = payload
        if expires_at < int(time.time()):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ? AND job_id = ?",
                (artifact_id, job_id),
            ).fetchone()
        if row is None or int(row["expires_at"]) != expires_at:
            return None
        path = Path(str(row["path"])).absolute()
        try:
            root = self.root.resolve()
            relative = path.relative_to(root)
            data = _read_regular_file(root, relative, int(row["size"]))
        except (OSError, ValueError):
            return None
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, str(row["sha256"])):
            return None
        link = ArtifactLink(
            artifact_id=artifact_id,
            job_id=job_id,
            name=str(row["name"]),
            path=path,
            content_type=str(row["content_type"]),
            size=len(data),
            sha256=digest,
            expires_at=expires_at,
            token=token,
            origin_session_id=str(row["origin_session_id"] or ""),
            fleet_run_id=str(row["fleet_run_id"] or ""),
            fleet_worker_key=str(row["fleet_worker_key"] or ""),
            created_at=float(row["created_at"]),
        )
        return ArtifactPayload(link=link, data=data)

    def _link_from_row(self, row: sqlite3.Row) -> ArtifactLink:
        artifact_id = str(row["artifact_id"])
        job_id = str(row["job_id"])
        expires_at = int(row["expires_at"])
        return ArtifactLink(
            artifact_id=artifact_id,
            job_id=job_id,
            name=str(row["name"]),
            path=Path(str(row["path"])).absolute(),
            content_type=str(row["content_type"]),
            size=int(row["size"]),
            sha256=str(row["sha256"]),
            expires_at=expires_at,
            token=self._token(artifact_id, job_id, expires_at),
            origin_session_id=str(row["origin_session_id"] or ""),
            fleet_run_id=str(row["fleet_run_id"] or ""),
            fleet_worker_key=str(row["fleet_worker_key"] or ""),
            created_at=float(row["created_at"]),
        )

    def issue_receipt(self, link: ArtifactLink) -> str:
        """Mint short-lived proof that this registry served a verified snapshot."""

        issued_at = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune_receipts(connection, now=issued_at)
            reusable = connection.execute(
                "SELECT nonce, issued_at FROM artifact_receipts "
                "WHERE artifact_id = ? AND job_id = ? AND sha256 = ? "
                "AND consumed_at IS NULL AND issued_at >= ? AND issued_at <= ? "
                "ORDER BY issued_at DESC LIMIT 1",
                (
                    link.artifact_id,
                    link.job_id,
                    link.sha256,
                    issued_at - ARTIFACT_RECEIPT_MAX_AGE_SECONDS,
                    issued_at + 30,
                ),
            ).fetchone()
            if reusable is not None:
                return self._encode_receipt(
                    link,
                    issued_at=int(reusable["issued_at"]),
                    nonce=str(reusable["nonce"]),
                )
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_receipts WHERE consumed_at IS NULL"
                ).fetchone()[0]
            )
            if active >= MAX_ACTIVE_ARTIFACT_RECEIPTS:
                raise ArtifactReceiptCapacityError(
                    "artifact receipt capacity is temporarily exhausted"
                )
            nonce = _b64encode(secrets.token_bytes(12))
            connection.execute(
                "INSERT INTO artifact_receipts("
                "nonce, artifact_id, job_id, sha256, issued_at, consumed_at"
                ") VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    nonce,
                    link.artifact_id,
                    link.job_id,
                    link.sha256,
                    issued_at,
                ),
            )
        return self._encode_receipt(link, issued_at=issued_at, nonce=nonce)

    def _encode_receipt(
        self,
        link: ArtifactLink,
        *,
        issued_at: int,
        nonce: str,
    ) -> str:
        payload = json.dumps(
            {
                "a": link.artifact_id,
                "j": link.job_id,
                "h": link.sha256,
                "t": issued_at,
                "n": nonce,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = _b64encode(payload)
        signature = hmac.new(
            self._key(), _RECEIPT_DOMAIN + encoded.encode(), hashlib.sha256
        ).digest()
        return f"v1.{encoded}.{_b64encode(signature)}"

    @staticmethod
    def _prune_receipts(connection: sqlite3.Connection, *, now: int) -> None:
        """Bound receipt history without invalidating any still-usable proof."""

        connection.execute(
            "DELETE FROM artifact_receipts "
            "WHERE issued_at < ? OR consumed_at IS NOT NULL",
            (now - ARTIFACT_RECEIPT_RETENTION_SECONDS,),
        )

    def verify_receipt(
        self,
        receipt: str,
        *,
        job_id: str,
        artifact_id: str,
        sha256: str,
        max_age_seconds: int = ARTIFACT_RECEIPT_MAX_AGE_SECONDS,
    ) -> bool:
        """Verify proof of a recent successful artifact response."""

        nonce = self._decode_receipt(
            receipt,
            job_id=job_id,
            artifact_id=artifact_id,
            sha256=sha256,
            max_age_seconds=max_age_seconds,
        )
        if nonce is None:
            return False
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipts.issued_at, artifacts.sha256, artifacts.expires_at "
                "FROM artifact_receipts AS receipts "
                "JOIN artifacts ON artifacts.artifact_id = receipts.artifact_id "
                "AND artifacts.job_id = receipts.job_id "
                "WHERE receipts.nonce = ? AND receipts.artifact_id = ? "
                "AND receipts.job_id = ? AND receipts.sha256 = ?",
                (nonce, artifact_id, job_id, sha256),
            ).fetchone()
        return bool(
            row is not None
            and int(row["expires_at"]) >= now
            and hmac.compare_digest(str(row["sha256"]), sha256)
        )

    def consume_receipt(
        self,
        receipt: str,
        *,
        job_id: str,
        artifact_id: str,
        sha256: str,
        max_age_seconds: int = ARTIFACT_RECEIPT_MAX_AGE_SECONDS,
    ) -> bool:
        """Atomically accept one recent fetch receipt exactly once."""

        nonce = self._decode_receipt(
            receipt,
            job_id=job_id,
            artifact_id=artifact_id,
            sha256=sha256,
            max_age_seconds=max_age_seconds,
        )
        if nonce is None:
            return False
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            artifact = connection.execute(
                "SELECT sha256, expires_at FROM artifacts WHERE artifact_id = ? AND job_id = ?",
                (artifact_id, job_id),
            ).fetchone()
            if (
                artifact is None
                or int(artifact["expires_at"]) < now
                or not hmac.compare_digest(str(artifact["sha256"]), sha256)
            ):
                return False
            updated = connection.execute(
                "UPDATE artifact_receipts SET consumed_at = ? "
                "WHERE nonce = ? AND artifact_id = ? AND job_id = ? "
                "AND sha256 = ? AND consumed_at IS NULL",
                (now, nonce, artifact_id, job_id, sha256),
            )
            consumed = updated.rowcount == 1
            if consumed:
                connection.execute(
                    "DELETE FROM artifact_receipts WHERE consumed_at IS NOT NULL"
                )
            return consumed

    def _decode_receipt(
        self,
        receipt: str,
        *,
        job_id: str,
        artifact_id: str,
        sha256: str,
        max_age_seconds: int,
    ) -> str | None:
        try:
            version, encoded, supplied = receipt.split(".", 2)
            if version != "v1":
                return None
            supplied_bytes = _b64decode(supplied)
            payload_bytes = _b64decode(encoded)
            if _b64encode(supplied_bytes) != supplied or _b64encode(payload_bytes) != encoded:
                return None
            expected = hmac.new(
                self._key(), _RECEIPT_DOMAIN + encoded.encode(), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(supplied_bytes, expected):
                return None
            payload = json.loads(payload_bytes)
            issued_at = payload["t"]
            nonce = payload["n"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            payload.get("a") != artifact_id
            or payload.get("j") != job_id
            or payload.get("h") != sha256
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or not isinstance(nonce, str)
            or not 8 <= len(nonce) <= 64
        ):
            return None
        now = int(time.time())
        age_limit = max(1, min(int(max_age_seconds), 60 * 60))
        if issued_at > now + 30 or now - issued_at > age_limit:
            return None
        return nonce

    def _token(self, artifact_id: str, job_id: str, expires_at: int) -> str:
        payload = json.dumps(
            {"a": artifact_id, "j": job_id, "e": expires_at},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = _b64encode(payload)
        signature = hmac.new(self._key(), encoded.encode(), hashlib.sha256).digest()
        return f"{encoded}.{_b64encode(signature)}"

    def _verify_token(self, token: str) -> tuple[str, str, int] | None:
        try:
            encoded, supplied = token.split(".", 1)
            supplied_bytes = _b64decode(supplied)
            payload_bytes = _b64decode(encoded)
            if _b64encode(supplied_bytes) != supplied or _b64encode(payload_bytes) != encoded:
                return None
            expected = hmac.new(self._key(), encoded.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(supplied_bytes, expected):
                return None
            payload = json.loads(payload_bytes)
            artifact_id = payload["a"]
            job_id = payload["j"]
            expires_at = payload["e"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(artifact_id, str)
            or not _safe_identifier(artifact_id)
            or not isinstance(job_id, str)
            or not _safe_identifier(job_id)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            return None
        return artifact_id, job_id, expires_at

    def _key(self) -> bytes:
        try:
            key = self.key_path.read_bytes()
        except OSError:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    self.key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = self.key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(key)
        if len(key) < 32:
            raise RuntimeError("artifact capability key is invalid")
        if os.name != "nt":
            with suppress(OSError):
                self.key_path.chmod(0o600)
        return key

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    origin_session_id TEXT NOT NULL DEFAULT '',
                    fleet_run_id TEXT NOT NULL DEFAULT '',
                    fleet_worker_key TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            for name in ("origin_session_id", "fleet_run_id", "fleet_worker_key"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE artifacts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_receipts (
                    nonce TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    issued_at INTEGER NOT NULL,
                    consumed_at INTEGER,
                    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifact_receipts_cleanup "
                "ON artifact_receipts(issued_at, consumed_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_origin "
                "ON artifacts(origin_session_id, fleet_run_id, created_at)"
            )
            self._prune_receipts(connection, now=int(time.time()))
        if os.name != "nt":
            with suppress(OSError):
                self.db_path.chmod(0o600)


def artifact_client_allowed(remote_addr: str | None) -> bool:
    try:
        address = ipaddress.ip_address((remote_addr or "").split("%", 1)[0])
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return address in ipaddress.ip_network("100.64.0.0/10")
    return address in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _safe_identifier(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _clean_artifact_name(name: str) -> str:
    clean = Path(name).name.strip()
    if not clean or clean != name or len(clean) > 128:
        raise ValueError("artifact name is invalid")
    return clean


def _bounded_origin(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if len(clean) > 160 or any(ord(char) < 32 for char in clean):
        raise ValueError(f"invalid {label}")
    return clean


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _read_regular_file(root: Path, relative: Path, expected_size: int) -> bytes:
    parts = relative.parts
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact path is invalid")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                directory_flags | nofollow,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | nofollow,
            dir_fd=directory_fd,
        )
        try:
            details = os.fstat(file_fd)
            if not stat.S_ISREG(details.st_mode) or details.st_size != expected_size:
                raise ValueError("artifact metadata changed")
            chunks: list[bytes] = []
            remaining = expected_size + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) != expected_size:
                raise ValueError("artifact content changed")
            return data
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _has_symlink_between(root: Path, path: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return True
    current = root.absolute()
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


_DEFAULT_REGISTRY: ArtifactRegistry | None = None


def get_default_artifact_registry() -> ArtifactRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ArtifactRegistry()
    return _DEFAULT_REGISTRY
