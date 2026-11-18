import dataclasses
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from line_local_mcp.client import LineApiError, LineArchiveClient
from line_local_mcp.config import Settings
from line_local_mcp.media import DEFAULT_MIME_ALLOW
from line_local_mcp.redaction import Redactor

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output quiet
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/media?id=missing"):
            self.send_error(404)
            return
        if self.path.startswith("/media?id=huge"):
            body, mime, disposition = b"x" * 4096, "image/png", None
        elif self.path.startswith("/media?id=named"):
            body = PNG
            mime = "application/octet-stream"
            disposition = "attachment; filename*=UTF-8''%E5%9C%96.png"
        else:
            body, mime, disposition = PNG, "image/png; charset=binary", None
        self.send_response(200)
        self.send_header("Content-Type", mime)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def archive():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def client(archive):
    host, port = archive.server_address
    settings = Settings(
        api_base=f"http://{host}:{port}",
        api_token="test-token",
        token_command=None,
        aliases_file=None,
        redaction_mode="basic",
        redactor_command=None,
        timeout_seconds=5,
        max_response_bytes=1024,
        media_mode="full",
        max_media_bytes=2048,
        media_mime_allow=DEFAULT_MIME_ALLOW,
    )
    return LineArchiveClient(settings, Redactor("basic"))


def test_fetch_media_returns_bytes_mime_and_bearer_token(client, archive):
    payload = client.fetch_media("m1")

    assert payload.data == PNG
    assert payload.mime == "image/png"
    assert payload.filename is None
    assert archive.seen[0] == ("/media?id=m1", "Bearer test-token")


def test_token_file_is_read_without_running_command(client, tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("file-token\n")
    token_path.chmod(0o600)
    settings = dataclasses.replace(client.settings, api_token=None, token_file=str(token_path), token_command=("false",))
    monkeypatch.setattr("line_local_mcp.client.subprocess.run", lambda *a, **k: pytest.fail("command ran"))
    assert LineArchiveClient(settings, client.redactor)._token() == "file-token"


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "directory"])
def test_token_file_rejects_unsafe_sources(client, tmp_path, unsafe):
    token_path = tmp_path / "token"
    if unsafe == "directory":
        token_path.mkdir()
    else:
        actual = tmp_path / "actual"
        actual.write_text("test-secret")
        actual.chmod(0o600)
        if unsafe == "symlink":
            token_path.symlink_to(actual)
        else:
            token_path = actual
            token_path.chmod(0o644)
    settings = dataclasses.replace(client.settings, api_token=None, token_file=str(token_path))
    with pytest.raises(LineApiError) as error:
        LineArchiveClient(settings, client.redactor)._token()
    assert "test-secret" not in str(error.value)


def test_fetch_media_uses_content_disposition_to_type_opaque_bytes(client):
    payload = client.fetch_media("named")

    assert payload.filename == "圖.png"
    assert payload.mime == "image/png"


def test_fetch_media_percent_encodes_the_identifier(client, archive):
    client.fetch_media("msg/7:1")
    assert archive.seen[0][0] == "/media?id=msg%2F7%3A1"


def test_fetch_media_enforces_the_media_byte_cap_not_the_json_cap(client):
    with pytest.raises(LineApiError, match="2048 byte limit"):
        client.fetch_media("huge")


def test_fetch_media_reports_a_missing_attachment(client):
    with pytest.raises(LineApiError, match="HTTP 404"):
        client.fetch_media("missing")


def test_fetch_media_rejects_a_malformed_identifier_before_any_request(client, archive):
    with pytest.raises(ValueError):
        client.fetch_media("has space")
    assert archive.seen == []


def test_json_requests_keep_their_own_smaller_cap(client):
    client = LineArchiveClient(
        dataclasses.replace(client.settings, max_response_bytes=8), client.redactor
    )
    with pytest.raises(LineApiError, match="8 byte limit"):
        client.request("GET", "/stats")
