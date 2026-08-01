"""Detect supported image formats from their magic bytes."""

from __future__ import annotations


def detect_image_content_type(data: bytes) -> str | None:
    """Return the MIME type identified by *data*, or ``None`` if unknown."""
    header = bytes(data[:16])
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None
