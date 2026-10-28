import base64
from io import BytesIO

import pytest
from mcp.types import EmbeddedResource, ImageContent, TextContent
from PIL import Image

from line_local_mcp.media import (
    DEFAULT_MIME_ALLOW,
    MediaError,
    MediaPayload,
    image_size,
    is_textual,
    mime_allowed,
    normalize_media_id,
    normalize_mime,
    parse_filename,
    to_content_blocks,
)
from line_local_mcp.redaction import Redactor


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('attachment; filename="report.pdf"', "report.pdf"),
        ("attachment; filename*=UTF-8''%E5%9C%96%E7%89%87.png", "圖片.png"),
        ('attachment; filename="../../etc/passwd"', "passwd"),
        ('attachment; filename="..\\\\..\\\\secret"', "secret"),
        ('attachment; filename=".."', None),
        ("attachment", None),
        (None, None),
    ],
)
def test_parse_filename_returns_a_safe_basename(value, expected):
    assert parse_filename(value) == expected


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("image/PNG; charset=binary", None, "image/png"),
        ("application/octet-stream", "invoice.pdf", "application/pdf"),
        ("", "notes.txt", "text/plain"),
        ("application/octet-stream", "blob.unknown", "application/octet-stream"),
        (None, None, "application/octet-stream"),
    ],
)
def test_normalize_mime_falls_back_to_the_filename(content_type, filename, expected):
    assert normalize_mime(content_type, filename) == expected


@pytest.mark.parametrize(
    ("mime", "allowed"),
    [
        ("image/png", True),
        ("text/csv", True),
        ("application/pdf", True),
        ("application/zip", False),
        ("application/x-msdownload", False),
    ],
)
def test_default_allow_list_covers_readable_types_only(mime, allowed):
    assert mime_allowed(mime, DEFAULT_MIME_ALLOW) is allowed


def test_wildcard_allow_list_permits_everything():
    assert mime_allowed("application/x-msdownload", ("*",))


@pytest.mark.parametrize(
    "mime", ["text/plain", "application/json", "application/ld+json", "image/svg+xml"]
)
def test_textual_mimes_are_decoded(mime):
    assert is_textual(mime)


def test_binary_mimes_are_not_decoded():
    assert not is_textual("application/pdf")


@pytest.mark.parametrize("value", ["", "   ", "a b", "line\nbreak", "x" * 513])
def test_normalize_media_id_rejects_unusable_identifiers(value):
    with pytest.raises(ValueError):
        normalize_media_id(value)


def test_normalize_media_id_keeps_opaque_identifiers():
    assert normalize_media_id("  msg_42:1/image.png  ") == "msg_42:1/image.png"


def test_image_payload_becomes_viewable_image_content():
    payload = MediaPayload("m1", "image/png", "shot.png", b"\x89PNG\r\n\x1a\n")
    header, body = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)

    assert isinstance(header, TextContent)
    assert "bytes=8" in header.text
    assert isinstance(body, ImageContent)
    assert base64.b64decode(body.data) == payload.data


def test_text_payload_is_decoded_and_redacted():
    payload = MediaPayload("m2", "text/plain", "notes.txt", b"token=" + b"a" * 30)
    _, body = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)

    assert isinstance(body, TextContent)
    assert "aaaa" not in body.text
    assert "⟦redacted:" in body.text


def test_invalid_utf8_text_is_replaced_not_raised():
    payload = MediaPayload("m3", "text/plain", "broken.txt", b"ok\xff\xfe")
    _, body = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)
    assert body.text.startswith("ok")


def test_binary_payload_becomes_embedded_resource_with_stable_uri():
    payload = MediaPayload("msg 7/1", "application/pdf", "invoice.pdf", b"%PDF-1.7")
    _, body = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)

    assert isinstance(body, EmbeddedResource)
    assert str(body.resource.uri) == "line-archive://media/msg%207%2F1"
    assert base64.b64decode(body.resource.blob) == payload.data


def test_disallowed_mime_is_refused_before_any_bytes_are_encoded():
    payload = MediaPayload("m4", "application/zip", "bundle.zip", b"PK\x03\x04")
    with pytest.raises(MediaError, match="LINE_MCP_MEDIA_MIME_ALLOW"):
        to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)


def test_header_metadata_is_redacted_too():
    payload = MediaPayload("m5", "text/plain", "password=hunter2.txt", b"hi")
    header, _ = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)
    assert "hunter2" not in header.text


def _png(width: int, height: int) -> bytes:
    import struct

    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _jpeg(width: int, height: int) -> bytes:
    import struct

    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dqt = b"\xff\xdb" + struct.pack(">H", 4) + b"\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + dqt + sof + b"\xff\xda\x00\x02" + b"\x00" * 8 + b"\xff\xd9"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_png(267, 132), (267, 132)),
        (_jpeg(267, 166), (267, 166)),
        (_jpeg(1640, 813), (1640, 813)),
        (b"GIF89a" + bytes([0x2C, 0x01, 0x64, 0x00]), (300, 100)),
        (b"\x89PNG\r\n\x1a\n", None),
        (b"\xff\xd8\xff\xda\x00\x02", None),
        (b"\xff\xd8\xff\xe0\x00", None),
        (b"not an image", None),
    ],
)
def test_image_size_reads_headers_only(data, expected):
    assert image_size(data) == expected


def test_preview_thumbnail_is_flagged_with_warning_and_meta():
    payload = MediaPayload("637203087899230855", "image/jpeg", "1792633941819.jpg", _jpeg(267, 132))
    header, body = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)

    assert "width=267, height=132, thumb=true" in header.text
    assert "LINE 預覽縮圖（267x132），不是原圖" in header.text
    assert header.meta == {"line_media": {"width": 267, "height": 132, "thumb": True}}
    assert isinstance(body, ImageContent)


def test_full_size_image_is_not_flagged():
    payload = MediaPayload("m2", "image/jpeg", "orig.jpg", _jpeg(1640, 813))
    header, _ = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)

    assert "thumb=false" in header.text
    assert "縮圖" not in header.text
    assert header.meta == {"line_media": {"width": 1640, "height": 813, "thumb": False}}


def test_one_long_side_is_not_a_thumbnail():
    payload = MediaPayload("m3", "image/png", "tall.png", _png(300, 1200))
    header, _ = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)

    assert "thumb=false" in header.text


def test_unparseable_image_keeps_the_plain_header():
    payload = MediaPayload("m4", "image/webp", "x.webp", b"RIFF\x00\x00\x00\x00WEBP")
    header, _ = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)

    assert "thumb=" not in header.text
    assert header.meta is None


def test_large_image_defaults_to_jpeg_1280_and_zero_restores_original():
    original = BytesIO()
    Image.new("RGB", (2400, 1200), "blue").save(original, format="PNG")
    payload = MediaPayload("m5", "image/png", "phone.png", original.getvalue())
    header, image = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW)
    assert "width=2400, height=1200, thumb=false" in header.text
    assert "sent_width=1280, sent_height=640" in header.text
    assert image.mime_type == "image/jpeg"
    with Image.open(BytesIO(base64.b64decode(image.data))) as decoded:
        assert decoded.size == (1280, 640)
    _, full = to_content_blocks(payload, Redactor("basic"), DEFAULT_MIME_ALLOW, max_side=0)
    assert full.mime_type == "image/png"
    assert base64.b64decode(full.data) == original.getvalue()
