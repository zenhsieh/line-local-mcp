from __future__ import annotations

import base64
import mimetypes
import posixpath
import struct
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import quote

from mcp.types import (
    BlobResourceContents,
    ContentBlock,
    EmbeddedResource,
    ImageContent,
    TextContent,
)

from .redaction import Redactor

MEDIA_MODES = ("off", "metadata", "full")

DEFAULT_MIME_ALLOW = (
    "image/*",
    "text/*",
    "application/json",
    "application/pdf",
    "application/xml",
)

_OPAQUE_MIME = "application/octet-stream"

# Textual payloads are decoded and passed through the redactor. Everything else
# is returned as opaque bytes that no redactor can inspect.
_TEXTUAL_MIMES = frozenset(
    {
        "application/csv",
        "application/javascript",
        "application/json",
        "application/sql",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
    }
)

MEDIA_ID_MAX_LENGTH = 512

# The archive's /media often serves LINE's cached *preview* instead of the original
# picture (observed: JPEG 267 px wide, ~10 KB).  Anything this small on both sides
# is flagged so callers do not mistake it for the original.
THUMBNAIL_MAX_SIDE = 320

THUMBNAIL_WARNING = (
    "⚠️ 這是 LINE 預覽縮圖（{width}x{height}），不是原圖：請在 LINE 點開原圖後另存到 "
    "syncthing inbox，或請對方重傳"
)

# JPEG start-of-frame markers that carry the frame dimensions (excludes DHT/JPG/DAC).
_JPEG_SOF = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


class MediaError(RuntimeError):
    """Raised when attachment bytes cannot be returned under the current policy."""


def normalize_media_id(value: str) -> str:
    media_id = value.strip()
    if not media_id:
        raise ValueError("media_id must not be empty")
    if len(media_id) > MEDIA_ID_MAX_LENGTH:
        raise ValueError(f"media_id must be at most {MEDIA_ID_MAX_LENGTH} characters")
    if any(character.isspace() or ord(character) < 0x20 for character in media_id):
        raise ValueError("media_id must not contain whitespace or control characters")
    return media_id


def parse_filename(content_disposition: str | None) -> str | None:
    """Extract a safe basename from a Content-Disposition header."""
    if not content_disposition:
        return None
    message = EmailMessage()
    message["Content-Disposition"] = content_disposition
    raw = message.get_filename()
    if not raw:
        return None
    name = posixpath.basename(raw.replace("\\", "/")).strip()
    if name in {"", ".", ".."}:
        return None
    return name


def normalize_mime(content_type: str | None, filename: str | None = None) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in {"", _OPAQUE_MIME} and filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed.lower()
    return mime or _OPAQUE_MIME


def mime_allowed(mime: str, allow: tuple[str, ...]) -> bool:
    for pattern in allow:
        if pattern in {"*", "*/*", mime}:
            return True
        if pattern.endswith("/*") and mime.startswith(pattern[:-1]):
            return True
    return False


def is_textual(mime: str) -> bool:
    return mime.startswith("text/") or mime in _TEXTUAL_MIMES or mime.endswith(("+json", "+xml"))


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length field
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # EOI / start of scan: no frame header seen
            return None
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if i + 9 > n:
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` from a PNG, JPEG or GIF header, or None if unknown.

    Header-only parsing, no image library: the bytes are never decoded.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"\xff\xd8"):
        size = _jpeg_size(data)
        if size is None:
            return None
        width, height = size
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
    else:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def is_thumbnail(size: tuple[int, int] | None) -> bool:
    return size is not None and max(size) <= THUMBNAIL_MAX_SIDE


@dataclass(frozen=True)
class MediaPayload:
    media_id: str
    mime: str
    filename: str | None
    data: bytes

    @property
    def uri(self) -> str:
        return "line-archive://media/" + quote(self.media_id, safe="")


def to_content_blocks(
    payload: MediaPayload, redactor: Redactor, allow: tuple[str, ...], max_side: int = 1280
) -> list[ContentBlock]:
    """Render one attachment as MCP content, refusing MIME types outside the allow list."""
    if max_side < 0:
        raise ValueError("max_side must be zero or greater")
    if not mime_allowed(payload.mime, allow):
        raise MediaError(
            f"attachment {payload.media_id!r} has MIME type {payload.mime!r}, which is not in "
            "LINE_MCP_MEDIA_MIME_ALLOW"
        )

    summary = {
        "media_id": payload.media_id,
        "mime": payload.mime,
        "bytes": len(payload.data),
        "filename": payload.filename or "(none)",
    }
    described = redactor.redact(summary)
    text = "LINE attachment " + ", ".join(
        f"{key}={described[key]}" for key in ("media_id", "filename", "mime", "bytes")
    )
    meta: dict[str, object] | None = None
    image_data, image_mime = payload.data, payload.mime
    if payload.mime.startswith("image/"):
        size = image_size(payload.data)
        if size is not None:
            width, height = size
            thumb = is_thumbnail(size)
            text += f", width={width}, height={height}, thumb={'true' if thumb else 'false'}"
            meta = {"line_media": {"width": width, "height": height, "thumb": thumb}}
            if thumb:
                text += "\n" + THUMBNAIL_WARNING.format(width=width, height=height)
        if max_side and size is not None and max(size) > max_side:
            from io import BytesIO

            from PIL import Image, ImageOps, UnidentifiedImageError

            try:
                with Image.open(BytesIO(payload.data)) as source:
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                    if image.mode not in ("RGB", "L"):
                        background = Image.new("RGB", image.size, "white")
                        if "A" in image.getbands():
                            background.paste(image, mask=image.getchannel("A"))
                        else:
                            background.paste(image.convert("RGB"))
                        image = background
                    output = BytesIO()
                    image.save(output, format="JPEG", quality=82, optimize=True)
                    image_data, image_mime = output.getvalue(), "image/jpeg"
                    text += f", sent_width={image.width}, sent_height={image.height}"
            except (OSError, UnidentifiedImageError):
                # Some archives carry a parseable image header but truncated bytes.
                # Preserve the original rather than drop the attachment.
                pass
    header = TextContent(type="text", text=text, _meta=meta)

    if payload.mime.startswith("image/"):
        body: ContentBlock = ImageContent(
            type="image",
            data=base64.b64encode(image_data).decode("ascii"),
            mimeType=image_mime,
        )
    elif is_textual(payload.mime):
        body = TextContent(
            type="text",
            text=redactor.redact(payload.data.decode("utf-8", errors="replace")),
        )
    else:
        body = EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=payload.uri,
                mimeType=payload.mime,
                blob=base64.b64encode(payload.data).decode("ascii"),
            ),
        )
    return [header, body]
