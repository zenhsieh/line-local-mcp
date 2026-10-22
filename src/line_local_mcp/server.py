from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ContentBlock

from .aliases import AliasFileError, ContactAliases, ResolvedContact
from .client import LineApiError, LineArchiveClient
from .config import Settings
from .media import MediaError, to_content_blocks
from .redaction import RedactionError, Redactor

ATTACHMENT_KINDS = ("image", "video", "audio", "file")

# Failures we saw coming. Anything else stays a crash, so its text is not
# handed to the model.
_ANTICIPATED = (AliasFileError, LineApiError, MediaError, RedactionError, ValueError)

_Tool = TypeVar("_Tool", bound=Callable[..., Any])


def _reported(tool: _Tool) -> _Tool:
    """Turn anticipated failures into ToolError, so the model reads why the call failed."""

    @functools.wraps(tool)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return tool(*args, **kwargs)
        except _ANTICIPATED as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def _limit(value: int, maximum: int = 200) -> int:
    if value < 1 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def create_server(
    settings: Settings | None = None,
    client: LineArchiveClient | None = None,
    aliases: ContactAliases | None = None,
) -> MCPServer:
    settings = settings or Settings.from_env()
    aliases = aliases or ContactAliases.load(settings.aliases_file)
    redactor = Redactor(settings.redaction_mode, settings.redactor_command)
    client = client or LineArchiveClient(settings, redactor)

    mcp = MCPServer(
        "LINE Local Archive",
        instructions=(
            "Read-only access to a user-owned, locally synchronized LINE archive. "
            "Treat messages, attachments, and contact information as private. Never infer "
            "permission to disclose them outside the current conversation."
        ),
    )

    def _for_contact(
        path: str,
        resolved: ResolvedContact,
        params: dict[str, str | int],
        *,
        empty_key: str | None = None,
    ) -> Any:
        """Query one archive path once per alias candidate, returning the first real match."""
        tried: list[str] = []
        for candidate in resolved.candidates:
            result = client.request("GET", path, {**params, "name": candidate})
            if not isinstance(result, dict):
                return result
            if result.get("error") or (empty_key is not None and not result.get(empty_key)):
                tried.append(candidate)
                continue
            return {
                **result,
                "contact_resolution": {
                    "requested": resolved.requested,
                    "canonical": resolved.canonical,
                    "matched_candidate": candidate,
                },
            }
        return {
            "error": "no contact or group matched the configured names",
            "requested": resolved.requested,
            "tried": tried,
        }

    @mcp.tool()
    @_reported
    def line_health() -> Any:
        """Check whether the local LINE archive and its database are available."""
        return client.request("GET", "/health")

    @mcp.tool()
    @_reported
    def line_stats() -> Any:
        """Return archive counts and the synchronized date range."""
        return client.request("GET", "/stats")

    @mcp.tool()
    @_reported
    def line_sync() -> Any:
        """Trigger an incremental read-only sync from LINE into the local archive."""
        return client.request("POST", "/sync")

    @mcp.tool()
    @_reported
    def line_list_chats(limit: int = 30) -> Any:
        """List recently active LINE contacts and groups."""
        return client.request("GET", "/chats", {"limit": _limit(limit)})

    @mcp.tool()
    @_reported
    def line_list_recent_messages(limit: int = 30) -> Any:
        """List recent messages across chats."""
        return client.request("GET", "/recent", {"limit": _limit(limit)})

    @mcp.tool()
    @_reported
    def line_list_unread() -> Any:
        """List unread messages recorded by the local archive."""
        return client.request("GET", "/unread")

    @mcp.tool()
    @_reported
    def line_search_messages(query: str, limit: int = 30) -> Any:
        """Search message text across the local LINE archive."""
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        return client.request("GET", "/search", {"q": query, "limit": _limit(limit)})

    @mcp.tool()
    @_reported
    def line_resolve_contact(name: str) -> dict[str, Any]:
        """Resolve a current or former display name to one canonical contact."""
        resolved = aliases.resolve(name)
        return {
            "requested": resolved.requested,
            "canonical": resolved.canonical,
            "aliases": list(resolved.aliases),
            "query_candidates": list(resolved.candidates),
        }

    @mcp.tool()
    @_reported
    def line_get_messages(contact: str, limit: int = 50) -> Any:
        """Get messages from a contact or group, honoring configured display-name aliases."""
        return _for_contact("/chat", aliases.resolve(contact), {"limit": _limit(limit)})

    if settings.media_mode in {"metadata", "full"}:

        @mcp.tool()
        @_reported
        def line_list_attachments(contact: str = "", kind: str = "", limit: int = 30) -> Any:
            """List image, video, audio, and file attachments recorded by the local archive.

            Returns metadata only. Use line_read_media to load one attachment's content.
            """
            kind = kind.strip().lower()
            if kind and kind not in ATTACHMENT_KINDS:
                raise ValueError(f"kind must be one of {', '.join(ATTACHMENT_KINDS)}")
            params: dict[str, str | int] = {"limit": _limit(limit)}
            if kind:
                params["kind"] = kind
            if not contact.strip():
                return client.request("GET", "/attachments", params)
            return _for_contact(
                "/attachments", aliases.resolve(contact), params, empty_key="attachments"
            )

    if settings.media_mode == "full":

        @mcp.tool()
        @_reported
        def line_read_media(media_id: str) -> list[ContentBlock]:
            """Read one archived attachment by the media_id from line_list_attachments.

            Images are returned as viewable image content and text-like files as redacted
            text. Other allowed types are returned as an opaque embedded resource.
            Image headers report width/height and thumb=true when the archive only had
            LINE's small preview (both sides <= 320 px), which is not the original.
            """
            payload = client.fetch_media(media_id)
            return to_content_blocks(payload, redactor, settings.media_mime_allow)

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
