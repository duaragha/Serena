"""Small, provider-neutral validation for user-supplied image inputs."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_TURN = 1
MAX_IMAGE_WIRE_BYTES = 8 * 1024 * 1024
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


def _detected_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def clean_image_input(value: object) -> dict[str, str]:
    """Return one strict base64 image descriptor or raise a readable error."""

    if not isinstance(value, Mapping) or set(value) != {"media_type", "data"}:
        raise ValueError("that clipboard image is invalid")
    media_type = str(value.get("media_type") or "").strip().lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
        raise ValueError("that image type is not supported; use png, jpeg, gif, or webp")
    encoded = value.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("that clipboard image is invalid")
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("that image is too large; keep it under 5 MB")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("that clipboard image is invalid") from error
    if not decoded:
        raise ValueError("that clipboard image is empty")
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("that image is too large; keep it under 5 MB")
    if _detected_media_type(decoded) != media_type:
        raise ValueError("that clipboard image does not match its image type")
    return {"media_type": media_type, "data": encoded}


def clean_image_inputs(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 0
    ):
        return []
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not 1 <= len(value) <= MAX_IMAGES_PER_TURN
    ):
        raise ValueError("attach one clipboard image at a time")
    return [clean_image_input(image) for image in value]


def data_url(image: Mapping[str, str]) -> str:
    return f"data:{image['media_type']};base64,{image['data']}"
