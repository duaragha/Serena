"""Private, session-bound attachments for structured agent input."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import warnings
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from PIL import Image

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 16
_IMAGES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


class WorkspaceUploads:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _directory(self, sid):
        if not isinstance(sid, str) or not sid or len(sid) > 200:
            raise ValueError("An exact session ID is required")
        return self.root / hashlib.sha256(sid.encode()).hexdigest()

    def save(self, sid: str, name: str, stream, media_type: str = "") -> dict:
        raw = stream.read(MAX_UPLOAD_BYTES + 1)
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError("Attach a non-empty file no larger than 25 MB")
        name = str(name or "attachment").replace("\\", "/").rsplit("/", 1)[-1][:200]
        suffix = Path(name).suffix.lower()
        image_type = ""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    image_type = _IMAGES.get(image.format, "")
                    image.verify()
        except Exception as error:
            if (
                image_type
                or media_type.startswith("image/")
                or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
            ):
                raise ValueError("Image cannot be decoded safely") from error
        if media_type.startswith("image/") and not image_type:
            raise ValueError("Use PNG, JPEG, GIF or WebP images")
        if image_type:
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }[image_type]
        elif not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ".bin"
        token = uuid4().hex
        directory = self._directory(sid) / token
        directory.mkdir(parents=True, mode=0o700)
        record = {
            "token": token,
            "name": name,
            "media_type": image_type or "application/octet-stream",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "file": "content" + suffix,
        }
        try:
            for path, data in [
                (directory / record["file"], raw),
                (directory / "metadata.json", json.dumps(record).encode()),
            ]:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
        except BaseException:
            shutil.rmtree(directory)
            raise
        return {key: record[key] for key in ("token", "name", "media_type", "size")}

    def resolve(self, sid: str, token: str) -> tuple[Path, dict]:
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("Invalid attachment ID")
        directory = self._directory(sid) / token
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError("Attachment storage changed")
        try:
            metadata = directory / "metadata.json"
            if metadata.is_symlink():
                raise ValueError("Attachment metadata changed")
            record = json.loads(metadata.read_text())
            if not re.fullmatch(r"content\.[a-z0-9]{1,12}", record["file"]):
                raise ValueError("Invalid attachment path")
            path = directory / record["file"]
            if path.is_symlink() or path.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValueError("Attachment storage changed")
            raw = path.read_bytes()
            if len(raw) != record["size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
                raise ValueError("Attachment changed after upload")
            return path, record
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("Attachment is unavailable in this session") from error

    def codex_inputs(self, sid: str, inputs: list[dict]) -> list[dict]:
        return self._inputs(sid, inputs, provider="codex")

    def decorate_event(self, sid: str, event: dict) -> dict:
        result = deepcopy(event)
        params = result.get("params", {})
        items = [params.get("item", {})]
        if result.get("method") in {"workspace/history", "workspace/historyPage"}:
            turns = (params.get("thread", {}).get("turns", [])
                     if result["method"] == "workspace/history" else params.get("turns", []))
            items = [
                item
                for turn in turns
                for item in turn.get("items", [])
            ]
        for item in items:
            if item.get("type") != "userMessage":
                continue
            for part in item.get("content", []):
                if part.get("type") != "localImage" or not isinstance(part.get("path"), str):
                    continue
                path = Path(part["path"])
                if path.parent.parent != self._directory(sid):
                    continue
                try:
                    resolved, record = self.resolve(sid, path.parent.name)
                    if resolved == path and record["media_type"].startswith("image/"):
                        part["previewToken"] = record["token"]
                        part["name"] = record["name"]
                except ValueError:
                    pass
        return result

    def claude_inputs(self, sid: str, inputs: list[dict]) -> list[dict]:
        return self._inputs(sid, inputs, provider="claude")

    def _inputs(self, sid: str, inputs: list[dict], *, provider: str) -> list[dict]:
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= MAX_ATTACHMENTS + 1:
            raise ValueError("Invalid message or attachment count")
        result = []
        for item in inputs:
            if not isinstance(item, dict):
                raise ValueError("Invalid message input")
            if (
                item.get("type") == "text"
                and set(item) == {"type", "text"}
                and isinstance(item["text"], str)
            ):
                result.append(dict(item))
            elif item.get("type") == "upload" and set(item) == {"type", "token"}:
                path, record = self.resolve(sid, item["token"])
                if record["media_type"].startswith("image/"):
                    if provider == "claude":
                        raw = path.read_bytes()
                        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                            raise ValueError("Attachment changed after upload")
                        result.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": record["media_type"],
                                    "data": base64.b64encode(raw).decode("ascii"),
                                },
                            }
                        )
                    else:
                        result.append({"type": "localImage", "path": str(path)})
                else:
                    result.append(
                        {
                            "type": "text",
                            "text": "User-attached file: "
                            + json.dumps({"name": record["name"], "path": str(path)}),
                        }
                    )
            else:
                raise ValueError("Use session-bound attachment IDs, not arbitrary file paths")
        return result
