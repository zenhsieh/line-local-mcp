#!/usr/bin/env python3
"""Synthetic demo archive API for line-local-mcp.

Serves the exact contract documented in the repository README — /health,
/stats, /sync, /chats, /recent, /unread, /search, /chat, /attachments, and
/media — over 100% generated data. There is no real LINE account, no network
call to LINE, and no scraping: every contact, message, and attachment byte in
this file is fabricated for demonstration.

Usage:
    python3 examples/demo_archive.py

Binds to 127.0.0.1 only, on port 8765 by default (override with --port or
DEMO_ARCHIVE_PORT). Prints a freshly generated bearer token and a ready-to-run
`claude mcp add` command on startup. Stop it with Ctrl-C; nothing is written
to disk and no state is kept between runs.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import struct
import sys
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"  # Loopback only. There is no flag to change this.
DEFAULT_PORT = 8765
ATTACHMENT_KINDS = ("image", "video", "audio", "file")


def _png(width: int, height: int, pixel: callable[[int, int], tuple[int, int, int]]) -> bytes:
    """Encode a truecolor PNG by hand (stdlib only, no image library)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)  # filter type: none
        for x in range(width):
            scanlines.extend(pixel(x, y))
    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", header)
    raw += chunk(b"IDAT", zlib.compress(bytes(scanlines), 9))
    raw += chunk(b"IEND", b"")
    return raw


def _demo_sunset_png() -> bytes:
    """A small checkered gradient — clearly synthetic, not a photo."""

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        if (x // 2 + y // 2) % 2 == 0:
            return (255, 140 + x * 4, 60)
        return (255, 90 + y * 4, 30)

    return _png(16, 16, pixel)


def _demo_agenda_pdf(lines: list[str]) -> bytes:
    """A minimal, structurally valid single-page PDF with correct xref offsets."""
    content_stream = "\n".join(
        f"BT /F1 12 Tf 40 {760 - 18 * i} Td ({line}) Tj ET" for i, line in enumerate(lines)
    ).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content_stream), content_stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode()
    return bytes(out)


_NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def _iso(offset_minutes: int) -> str:
    return (_NOW + timedelta(minutes=offset_minutes)).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Attachment:
    media_id: str
    chat: str
    kind: str
    mime: str
    filename: str
    sender: str
    timestamp: str
    data: bytes


@dataclass(frozen=True)
class Message:
    id: str
    chat: str
    sender: str
    text: str
    timestamp: str
    direction: str
    unread: bool = False
    attachment_id: str | None = None


@dataclass(frozen=True)
class Contact:
    name: str
    kind: str
    unread_count: int = field(default=0)


ATTACHMENTS: tuple[Attachment, ...] = (
    Attachment(
        media_id="att-sunset",
        chat="Demo Buddy",
        kind="image",
        mime="image/png",
        filename="sunset.png",
        sender="Demo Buddy",
        timestamp=_iso(-30),
        data=_demo_sunset_png(),
    ),
    Attachment(
        media_id="att-wifi-note",
        chat="Demo Squad",
        kind="file",
        mime="text/plain",
        filename="wifi-note.txt",
        sender="Ken (demo)",
        timestamp=_iso(-90),
        data=(
            b"Front desk wifi for the offsite (demo fixture, not a real network):\n"
            b"SSID: demo-offsite\n"
            b"password: hunter2-not-real\n"
            b"Ask Mia if the badge reader is still down.\n"
        ),
    ),
    Attachment(
        media_id="att-agenda",
        chat="Demo Squad",
        kind="file",
        mime="application/pdf",
        filename="agenda.pdf",
        sender="Mia (demo)",
        timestamp=_iso(-60),
        data=_demo_agenda_pdf(
            [
                "Demo Squad Offsite Agenda (synthetic fixture)",
                "09:00 Coffee",
                "09:30 Roadmap sync",
                "11:00 Retro",
            ]
        ),
    ),
    Attachment(
        media_id="att-clip",
        chat="Demo Squad",
        kind="video",
        mime="video/mp4",
        filename="clip.mp4",
        sender="Ken (demo)",
        timestamp=_iso(-45),
        data=b"FAKE-MP4-DEMO-BYTES-NOT-A-REAL-VIDEO",
    ),
)

MESSAGES: tuple[Message, ...] = (
    Message(
        id="msg-1",
        chat="Demo Buddy",
        sender="Demo Buddy",
        text="看看昨天拍的日落 🌅",
        timestamp=_iso(-30),
        direction="incoming",
        unread=True,
        attachment_id="att-sunset",
    ),
    Message(
        id="msg-2",
        chat="Demo Buddy",
        sender="Demo Buddy",
        text="明天一樣時間？",
        timestamp=_iso(-10),
        direction="incoming",
        unread=True,
    ),
    Message(
        id="msg-3",
        chat="Demo Squad",
        sender="Mia (demo)",
        text="Agenda for tomorrow's offsite attached.",
        timestamp=_iso(-60),
        direction="incoming",
        attachment_id="att-agenda",
    ),
    Message(
        id="msg-4",
        chat="Demo Squad",
        sender="Ken (demo)",
        text="Wifi note for the venue, and a quick clip from the walkthrough.",
        timestamp=_iso(-45),
        direction="incoming",
        attachment_id="att-clip",
    ),
    Message(
        id="msg-5",
        chat="Demo Squad",
        sender="Ken (demo)",
        text="(also see the wifi note above)",
        timestamp=_iso(-44),
        direction="incoming",
        attachment_id="att-wifi-note",
    ),
    Message(
        id="msg-6",
        chat="Demo Squad",
        sender="you",
        text="Got it, thanks both.",
        timestamp=_iso(-5),
        direction="outgoing",
    ),
)

CONTACTS: tuple[Contact, ...] = (
    Contact(name="Demo Buddy", kind="person", unread_count=2),
    Contact(name="Demo Squad", kind="group", unread_count=0),
)


def _message_view(message: Message) -> dict:
    view = {
        "id": message.id,
        "chat": message.chat,
        "sender": message.sender,
        "text": message.text,
        "timestamp": message.timestamp,
        "direction": message.direction,
        "unread": message.unread,
    }
    if message.attachment_id:
        view["attachment_id"] = message.attachment_id
    return view


def _attachment_view(attachment: Attachment) -> dict:
    return {
        "media_id": attachment.media_id,
        "chat": attachment.chat,
        "kind": attachment.kind,
        "mime": attachment.mime,
        "filename": attachment.filename,
        "sender": attachment.sender,
        "timestamp": attachment.timestamp,
    }


def _clamp_limit(params: dict[str, str], default: int = 30, maximum: int = 200) -> int:
    raw = params.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, maximum))


def _find_contact(name: str) -> Contact | None:
    folded = name.strip().casefold()
    return next((contact for contact in CONTACTS if contact.name.casefold() == folded), None)


class DemoArchiveHandler(BaseHTTPRequestHandler):
    server_version = "line-local-mcp-demo-archive/0.1"

    # -- plumbing -----------------------------------------------------
    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"  # type: ignore[attr-defined]
        return secrets.compare_digest(self.headers.get("Authorization", ""), expected)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: HTTPStatus, data: bytes, mime: str, filename: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token")
            return
        parsed = urlsplit(self.path)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        handler = _GET_ROUTES.get(parsed.path)
        if handler is None:
            self._error(HTTPStatus.NOT_FOUND, f"unknown path {parsed.path!r}")
            return
        handler(self, params)

    def do_POST(self) -> None:
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid bearer token")
            return
        if urlsplit(self.path).path != "/sync":
            self._error(HTTPStatus.NOT_FOUND, f"unknown path {self.path!r}")
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "synced_messages": 0, "note": "demo archive; nothing to sync"},
        )


def _handle_health(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    handler._send_json(HTTPStatus.OK, {"status": "ok", "database": "ok"})


def _handle_stats(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    timestamps = sorted(message.timestamp for message in MESSAGES)
    handler._send_json(
        HTTPStatus.OK,
        {
            "messages": len(MESSAGES),
            "chats": len(CONTACTS),
            "attachments": len(ATTACHMENTS),
            "unread": sum(1 for message in MESSAGES if message.unread),
            "date_range": {"from": timestamps[0], "to": timestamps[-1]},
        },
    )


def _handle_chats(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    limit = _clamp_limit(params)
    chats = [
        {"name": contact.name, "kind": contact.kind, "unread_count": contact.unread_count}
        for contact in CONTACTS
    ][:limit]
    handler._send_json(HTTPStatus.OK, {"chats": chats})


def _handle_recent(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    limit = _clamp_limit(params)
    ordered = sorted(MESSAGES, key=lambda message: message.timestamp, reverse=True)
    handler._send_json(HTTPStatus.OK, {"messages": [_message_view(m) for m in ordered[:limit]]})


def _handle_unread(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    unread = [_message_view(m) for m in MESSAGES if m.unread]
    handler._send_json(HTTPStatus.OK, {"messages": unread})


def _handle_search(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    query = params.get("q", "").strip()
    if not query:
        handler._error(HTTPStatus.BAD_REQUEST, "q must not be empty")
        return
    limit = _clamp_limit(params)
    folded = query.casefold()
    matches = [message for message in MESSAGES if folded in message.text.casefold()]
    handler._send_json(
        HTTPStatus.OK, {"query": query, "results": [_message_view(m) for m in matches[:limit]]}
    )


def _handle_chat(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    name = params.get("name", "")
    contact = _find_contact(name)
    if contact is None:
        handler._send_json(HTTPStatus.OK, {"error": f"no contact or group named {name!r}"})
        return
    limit = _clamp_limit(params, default=50)
    messages = [m for m in MESSAGES if m.chat == contact.name]
    handler._send_json(HTTPStatus.OK, {"messages": [_message_view(m) for m in messages[:limit]]})


def _handle_attachments(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    name = params.get("name")
    kind = params.get("kind")
    if kind and kind not in ATTACHMENT_KINDS:
        handler._error(HTTPStatus.BAD_REQUEST, f"kind must be one of {ATTACHMENT_KINDS}")
        return
    if name:
        contact = _find_contact(name)
        if contact is None:
            handler._send_json(HTTPStatus.OK, {"attachments": []})
            return
        pool = [a for a in ATTACHMENTS if a.chat == contact.name]
    else:
        pool = list(ATTACHMENTS)
    if kind:
        pool = [a for a in pool if a.kind == kind]
    limit = _clamp_limit(params)
    handler._send_json(HTTPStatus.OK, {"attachments": [_attachment_view(a) for a in pool[:limit]]})


def _handle_media(handler: DemoArchiveHandler, params: dict[str, str]) -> None:
    media_id = params.get("id", "")
    attachment = next((a for a in ATTACHMENTS if a.media_id == media_id), None)
    if attachment is None:
        handler._error(HTTPStatus.NOT_FOUND, f"no attachment with id {media_id!r}")
        return
    handler._send_bytes(HTTPStatus.OK, attachment.data, attachment.mime, attachment.filename)


_GET_ROUTES = {
    "/health": _handle_health,
    "/stats": _handle_stats,
    "/chats": _handle_chats,
    "/recent": _handle_recent,
    "/unread": _handle_unread,
    "/search": _handle_search,
    "/chat": _handle_chat,
    "/attachments": _handle_attachments,
    "/media": _handle_media,
}


def create_server(port: int = DEFAULT_PORT, token: str | None = None, quiet: bool = False):
    """Build the demo HTTPServer bound to loopback. Pass port=0 to let the OS pick one."""
    server = ThreadingHTTPServer((HOST, port), DemoArchiveHandler)
    server.token = token or secrets.token_urlsafe(24)  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.getenv("DEMO_ARCHIVE_PORT", DEFAULT_PORT)))
    parser.add_argument("--token", default=os.getenv("DEMO_ARCHIVE_TOKEN"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    server = create_server(port=args.port, token=args.token, quiet=args.quiet)
    base_url = f"http://{HOST}:{server.server_address[1]}"
    print("line-local-mcp demo archive — synthetic data only, no real LINE account", file=sys.stderr)
    print(f"  listening on {base_url} (loopback only)", file=sys.stderr)
    print(f"  bearer token: {server.token}", file=sys.stderr)  # type: ignore[attr-defined]
    print(file=sys.stderr)
    print("Try it:", file=sys.stderr)
    print(
        "  claude mcp add --scope user --transport stdio line-demo \\\n"
        f"    --env LINE_API_BASE={base_url} \\\n"
        f"    --env LINE_API_TOKEN={server.token} \\\n"  # type: ignore[attr-defined]
        "    -- uvx --from git+https://github.com/allencyhsieh/line-local-mcp line-local-mcp",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print("Ctrl-C to stop. No data is written to disk; nothing persists between runs.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
