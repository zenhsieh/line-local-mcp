import dataclasses
import json
from typing import Any

import pytest
from mcp import Client
from mcp.types import EmbeddedResource, ImageContent, TextContent

from line_local_mcp.aliases import ContactAliases
from line_local_mcp.config import Settings
from line_local_mcp.media import DEFAULT_MIME_ALLOW, MediaPayload
from line_local_mcp.server import create_server

BASE_TOOLS = {
    "line_health",
    "line_stats",
    "line_sync",
    "line_list_chats",
    "line_list_recent_messages",
    "line_list_unread",
    "line_search_messages",
    "line_resolve_contact",
    "line_get_messages",
}


class FakeClient:
    def __init__(self, media: MediaPayload | None = None):
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.media_ids: list[str] = []
        self.media = media

    def request(self, method, path, params=None):
        self.calls.append((method, path, params))
        if path == "/chat" and params["name"] == "Old Name":
            return {"error": "not found"}
        if path == "/chat":
            return {"messages": [{"text": "hello"}]}
        if path == "/attachments":
            if params.get("name") == "Old Name":
                return {"attachments": []}
            return {"attachments": [{"media_id": "m1", "mime": "image/png"}]}
        return {"ok": True}

    def fetch_media(self, media_id):
        self.media_ids.append(media_id)
        assert self.media is not None
        return dataclasses.replace(self.media, media_id=media_id)


@pytest.fixture
def settings():
    return Settings(
        api_base="http://127.0.0.1:8765",
        api_token="test",
        token_command=None,
        aliases_file=None,
        redaction_mode="basic",
        redactor_command=None,
        timeout_seconds=30,
        max_response_bytes=1024,
        media_mode="full",
        max_media_bytes=1024,
        media_mime_allow=DEFAULT_MIME_ALLOW,
    )


@pytest.mark.asyncio
async def test_server_exposes_only_expected_read_tools(settings):
    server = create_server(settings, FakeClient(), ContactAliases())
    async with Client(server) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools.tools}
    assert names == BASE_TOOLS | {"line_list_attachments", "line_read_media"}
    assert not any("send" in name or "delete" in name for name in names)


@pytest.mark.asyncio
async def test_get_messages_falls_back_from_old_to_canonical_name(settings):
    fake = FakeClient()
    server = create_server(settings, fake, ContactAliases({"New Name": ("Old Name",)}))
    async with Client(server) as client:
        result = await client.call_tool(
            "line_get_messages", {"contact": "Old Name", "limit": 5}
        )
    assert not result.is_error
    assert [call[2]["name"] for call in fake.calls] == ["Old Name", "New Name"]


@pytest.mark.asyncio
async def test_text_default_and_json_escape_hatch(settings):
    server = create_server(settings, FakeClient(), ContactAliases())
    async with Client(server) as client:
        compact = await client.call_tool("line_get_messages", {"contact": "New Name"})
        original = await client.call_tool("line_get_messages", {"contact": "New Name", "format": "json"})
    assert "hello" in compact.content[0].text
    assert "contact_resolution" not in compact.content[0].text
    assert json.loads(original.content[0].text)["messages"][0]["text"] == "hello"



@pytest.mark.asyncio
async def test_metadata_mode_lists_attachments_but_hides_content(settings):
    settings = dataclasses.replace(settings, media_mode="metadata")
    server = create_server(settings, FakeClient(), ContactAliases())
    async with Client(server) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools.tools} == BASE_TOOLS | {"line_list_attachments"}


@pytest.mark.asyncio
async def test_media_mode_off_registers_no_media_tools(settings):
    settings = dataclasses.replace(settings, media_mode="off")
    server = create_server(settings, FakeClient(), ContactAliases())
    async with Client(server) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools.tools} == BASE_TOOLS


@pytest.mark.asyncio
async def test_read_media_returns_viewable_image(settings):
    fake = FakeClient(MediaPayload("", "image/png", "shot.png", b"\x89PNG\r\n\x1a\n"))
    server = create_server(settings, fake, ContactAliases())
    async with Client(server) as client:
        result = await client.call_tool("line_read_media", {"media_id": "m1"})
    assert not result.is_error
    assert fake.media_ids == ["m1"]
    header, image = result.content
    assert isinstance(header, TextContent)
    assert "shot.png" in header.text
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"


@pytest.mark.asyncio
async def test_read_media_redacts_text_attachments(settings):
    fake = FakeClient(MediaPayload("", "text/plain", "notes.txt", b"password=hunter2"))
    server = create_server(settings, fake, ContactAliases())
    async with Client(server) as client:
        result = await client.call_tool("line_read_media", {"media_id": "m2"})
    assert not result.is_error
    body = result.content[1]
    assert isinstance(body, TextContent)
    assert "hunter2" not in body.text
    assert "⟦redacted:" in body.text


@pytest.mark.asyncio
async def test_read_media_returns_other_allowed_types_as_embedded_resource(settings):
    fake = FakeClient(MediaPayload("", "application/pdf", "invoice.pdf", b"%PDF-1.7"))
    server = create_server(settings, fake, ContactAliases())
    async with Client(server) as client:
        result = await client.call_tool("line_read_media", {"media_id": "m3"})
    assert not result.is_error
    assert isinstance(result.content[1], EmbeddedResource)


@pytest.mark.asyncio
async def test_read_media_refuses_mime_outside_allow_list(settings):
    fake = FakeClient(MediaPayload("", "application/zip", "bundle.zip", b"PK\x03\x04"))
    server = create_server(settings, fake, ContactAliases())
    async with Client(server) as client:
        result = await client.call_tool("line_read_media", {"media_id": "m4"})
    assert result.is_error
    assert "LINE_MCP_MEDIA_MIME_ALLOW" in result.content[0].text


@pytest.mark.asyncio
async def test_list_attachments_falls_back_when_a_candidate_has_none(settings):
    fake = FakeClient()
    server = create_server(settings, fake, ContactAliases({"New Name": ("Old Name",)}))
    async with Client(server) as client:
        result = await client.call_tool(
            "line_list_attachments", {"contact": "Old Name", "kind": "image"}
        )
    assert not result.is_error
    assert [call[2]["name"] for call in fake.calls] == ["Old Name", "New Name"]
    assert all(call[2]["kind"] == "image" for call in fake.calls)


@pytest.mark.asyncio
async def test_list_attachments_rejects_unknown_kind(settings):
    server = create_server(settings, FakeClient(), ContactAliases())
    async with Client(server) as client:
        result = await client.call_tool("line_list_attachments", {"kind": "sticker"})
    assert result.is_error
