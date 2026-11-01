"""Upload checks for licence images: magic-byte type sniffing, size limit, base64 decoding.
Port of limoRTService's ``imageValidator.ts``; the declared type is only a fallback."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

ALLOWED_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/tiff", "image/bmp", "image/webp", "image/gif", "application/pdf"}
)
_BASE64 = re.compile(r"[A-Za-z0-9+/=\s]+")


@dataclass(frozen=True, slots=True)
class ImageCheck:
    valid: bool
    mime_type: str | None
    error: str | None = None


def detect_mime_type(data: bytes) -> str | None:
    if len(data) < 4:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, max_bytes: int, declared_mime: str | None = None) -> ImageCheck:
    if not data:
        return ImageCheck(False, None, "Image buffer is empty")
    if len(data) > max_bytes:
        limit_mb = max_bytes / 1024 / 1024
        actual_mb = len(data) / 1024 / 1024
        return ImageCheck(False, None, f"File size {actual_mb:.2f} MB exceeds the {limit_mb:.1f} MB limit")
    mime = detect_mime_type(data) or declared_mime or None
    if not mime:
        return ImageCheck(False, None, "Unable to determine file type from content")
    if mime not in ALLOWED_MIME_TYPES:
        return ImageCheck(
            False, mime, f'Unsupported file type "{mime}". Allowed: JPEG, PNG, TIFF, BMP, WebP, GIF, PDF'
        )
    return ImageCheck(True, mime)


def decode_base64_image(value: str) -> bytes | None:
    """Base64, with or without a ``data:image/...;base64,`` prefix. None if invalid."""
    payload = value.split(",")[1] if "," in value else value
    if not payload or not _BASE64.fullmatch(payload):
        return None
    try:
        return base64.b64decode(re.sub(r"\s", "", payload), validate=False)
    except (binascii.Error, ValueError):
        return None
