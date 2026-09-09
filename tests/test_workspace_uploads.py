import base64
import io
import json
import os

import pytest
from PIL import Image

from core.workspace_uploads import WorkspaceUploads


def png():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(stream, format="PNG")
    return stream.getvalue()


def test_image_upload_bound_to_exact_session_and_preserves_bytes(tmp_path):
    uploads = WorkspaceUploads(tmp_path)
    raw = png()
    record = uploads.save("exact", "../../photo.png", io.BytesIO(raw), "image/png")
    path, meta = uploads.resolve("exact", record["token"])
    assert path.read_bytes() == raw
    assert meta["name"] == "photo.png"
    assert uploads.codex_inputs("exact", [{"type": "upload", "token": record["token"]}]) == [
        {"type": "localImage", "path": str(path)}
    ]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="unavailable"):
        uploads.resolve("other", record["token"])


def test_document_reference_and_changed_content_rejection(tmp_path):
    uploads = WorkspaceUploads(tmp_path)
    record = uploads.save("exact", "notes.txt", io.BytesIO(b"original notes"))
    path, _ = uploads.resolve("exact", record["token"])
    inputs = uploads.codex_inputs("exact", [{"type": "upload", "token": record["token"]}])
    assert json.loads(inputs[0]["text"].removeprefix("User-attached file: ")) == {
        "name": "notes.txt",
        "path": str(path),
    }
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        uploads.resolve("exact", record["token"])


def test_invalid_image_size_and_arbitrary_paths_are_rejected(tmp_path, monkeypatch):
    from core import workspace_uploads

    uploads = WorkspaceUploads(tmp_path)
    with pytest.raises(ValueError, match="decoded"):
        uploads.save("exact", "fake.png", io.BytesIO(b"not an image"), "image/png")
    monkeypatch.setattr(workspace_uploads, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(ValueError, match="25 MB"):
        uploads.save("exact", "large.txt", io.BytesIO(b"12345"))
    with pytest.raises(ValueError, match="arbitrary"):
        uploads.codex_inputs("exact", [{"type": "localImage", "path": "/etc/passwd"}])
    with pytest.raises(ValueError, match="Invalid attachment"):
        uploads.resolve("exact", "../../etc/passwd")


def test_claude_native_image_and_document_inputs(tmp_path):
    uploads = WorkspaceUploads(tmp_path)
    raw = png()
    image = uploads.save("exact", "photo.png", io.BytesIO(raw))
    document = uploads.save("exact", "notes.txt", io.BytesIO(b"notes"))
    inputs = [
        {"type": "text", "text": "inspect these"},
        {"type": "upload", "token": image["token"]},
        {"type": "upload", "token": document["token"]},
    ]
    mapped = uploads.claude_inputs("exact", inputs)
    assert mapped[0] == inputs[0]
    assert mapped[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }
    assert "notes.txt" in mapped[2]["text"]
    assert inputs[1]["type"] == "upload"
    with pytest.raises(ValueError, match="unavailable"):
        uploads.claude_inputs("other", inputs)
    with pytest.raises(ValueError, match="arbitrary"):
        uploads.claude_inputs("exact", [mapped[1]])


def test_claude_rejects_changed_image(tmp_path):
    uploads = WorkspaceUploads(tmp_path)
    record = uploads.save("exact", "photo.png", io.BytesIO(png()))
    path, _ = uploads.resolve("exact", record["token"])
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        uploads.claude_inputs("exact", [{"type": "upload", "token": record["token"]}])


def test_only_owned_image_paths_get_preview_tokens(tmp_path):
    uploads = WorkspaceUploads(tmp_path)
    record = uploads.save("exact", "photo.png", io.BytesIO(png()))
    path, _ = uploads.resolve("exact", record["token"])
    event = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "userMessage",
                "content": [
                    {"type": "localImage", "path": str(path)},
                    {"type": "localImage", "path": "/private/photo.png"},
                ],
            }
        },
    }
    decorated = uploads.decorate_event("exact", event)
    assert decorated["params"]["item"]["content"][0]["previewToken"] == record["token"]
    assert "previewToken" not in decorated["params"]["item"]["content"][1]
    assert uploads.decorate_event("different", event) == event
    assert "previewToken" not in event["params"]["item"]["content"][0]
