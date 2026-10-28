from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from .media import DEFAULT_MIME_ALLOW, MEDIA_MODES


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or unsafe."""


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_base: str
    api_token: str | None
    token_command: tuple[str, ...] | None
    aliases_file: str | None
    redaction_mode: str
    redactor_command: tuple[str, ...] | None
    timeout_seconds: float
    max_response_bytes: int
    media_mode: str
    max_media_bytes: int
    media_mime_allow: tuple[str, ...]
    self_sender_id: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        api_base = os.getenv("LINE_API_BASE", "").strip().rstrip("/")
        if not api_base:
            raise ConfigurationError("LINE_API_BASE is required")

        parsed = urlparse(api_base)
        is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not is_loopback and not _truthy(
            "LINE_MCP_ALLOW_INSECURE_HTTP"
        ):
            raise ConfigurationError(
                "LINE_API_BASE must use HTTPS unless it is loopback; "
                "set LINE_MCP_ALLOW_INSECURE_HTTP=1 only for a trusted network"
            )

        api_token = os.getenv("LINE_API_TOKEN", "").strip() or None
        token_raw = os.getenv("LINE_API_TOKEN_COMMAND", "").strip()
        token_command = tuple(shlex.split(token_raw)) if token_raw else None
        if not api_token and not token_command:
            raise ConfigurationError(
                "Set LINE_API_TOKEN or LINE_API_TOKEN_COMMAND; secrets are never read from files"
            )

        redaction_mode = os.getenv("LINE_MCP_REDACTION_MODE", "basic").strip().lower()
        if redaction_mode not in {"basic", "external", "off"}:
            raise ConfigurationError(
                "LINE_MCP_REDACTION_MODE must be basic, external, or off"
            )
        redactor_raw = os.getenv("LINE_MCP_REDACTOR_COMMAND", "").strip()
        redactor_command = tuple(shlex.split(redactor_raw)) if redactor_raw else None
        if redaction_mode == "external" and not redactor_command:
            raise ConfigurationError(
                "LINE_MCP_REDACTOR_COMMAND is required when redaction mode is external"
            )

        media_mode = os.getenv("LINE_MCP_MEDIA_MODE", "full").strip().lower()
        if media_mode not in MEDIA_MODES:
            raise ConfigurationError(
                "LINE_MCP_MEDIA_MODE must be " + ", ".join(MEDIA_MODES)
            )
        allow_raw = os.getenv("LINE_MCP_MEDIA_MIME_ALLOW", "").strip()
        media_mime_allow = (
            tuple(
                pattern.strip().lower() for pattern in allow_raw.split(",") if pattern.strip()
            )
            if allow_raw
            else DEFAULT_MIME_ALLOW
        )

        return cls(
            api_base=api_base,
            api_token=api_token,
            token_command=token_command,
            aliases_file=os.getenv("LINE_MCP_ALIASES_FILE", "").strip() or None,
            redaction_mode=redaction_mode,
            redactor_command=redactor_command,
            timeout_seconds=float(os.getenv("LINE_MCP_TIMEOUT", "30")),
            max_response_bytes=int(os.getenv("LINE_MCP_MAX_RESPONSE_BYTES", "5242880")),
            media_mode=media_mode,
            max_media_bytes=int(os.getenv("LINE_MCP_MAX_MEDIA_BYTES", "4194304")),
            media_mime_allow=media_mime_allow,
            self_sender_id=os.getenv("LINE_MCP_SELF_SENDER_ID", "").strip(),
        )
