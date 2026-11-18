import pytest

from line_local_mcp.config import ConfigurationError, Settings


def test_requires_api_base(monkeypatch):
    monkeypatch.delenv("LINE_API_BASE", raising=False)
    monkeypatch.setenv("LINE_API_TOKEN", "test")
    with pytest.raises(ConfigurationError, match="LINE_API_BASE"):
        Settings.from_env()


def test_rejects_plain_http_off_loopback(monkeypatch):
    monkeypatch.setenv("LINE_API_BASE", "http://archive.example")
    monkeypatch.setenv("LINE_API_TOKEN", "test")
    monkeypatch.delenv("LINE_MCP_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_env()


def test_allows_loopback_http(monkeypatch):
    monkeypatch.setenv("LINE_API_BASE", "http://127.0.0.1:8765/")
    monkeypatch.setenv("LINE_API_TOKEN", "test")
    settings = Settings.from_env()
    assert settings.api_base == "http://127.0.0.1:8765"


def test_accepts_token_file_without_printing_command(monkeypatch):
    monkeypatch.setenv("LINE_API_BASE", "http://127.0.0.1:8765")
    monkeypatch.delenv("LINE_API_TOKEN", raising=False)
    monkeypatch.delenv("LINE_API_TOKEN_COMMAND", raising=False)
    monkeypatch.setenv("LINE_API_TOKEN_FILE", "/private/token")
    settings = Settings.from_env()
    assert settings.token_file == "/private/token"
    assert settings.token_command is None



def _base_env(monkeypatch):
    monkeypatch.setenv("LINE_API_BASE", "http://127.0.0.1:8765")
    monkeypatch.setenv("LINE_API_TOKEN", "test")
    for name in ("LINE_MCP_MEDIA_MODE", "LINE_MCP_MEDIA_MIME_ALLOW", "LINE_MCP_MAX_MEDIA_BYTES"):
        monkeypatch.delenv(name, raising=False)


def test_media_defaults_to_full_with_a_readable_type_allow_list(monkeypatch):
    _base_env(monkeypatch)
    settings = Settings.from_env()

    assert settings.media_mode == "full"
    assert settings.max_media_bytes == 4 * 1024 * 1024
    assert "image/*" in settings.media_mime_allow
    assert "application/zip" not in settings.media_mime_allow


def test_rejects_unknown_media_mode(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("LINE_MCP_MEDIA_MODE", "sometimes")
    with pytest.raises(ConfigurationError, match="LINE_MCP_MEDIA_MODE"):
        Settings.from_env()


def test_media_allow_list_is_parsed_and_normalized(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("LINE_MCP_MEDIA_MIME_ALLOW", " Image/* , application/ZIP ,, ")
    assert Settings.from_env().media_mime_allow == ("image/*", "application/zip")
