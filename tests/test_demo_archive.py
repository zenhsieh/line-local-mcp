import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "demo_archive", Path(__file__).parent.parent / "examples" / "demo_archive.py"
)
demo_archive = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = demo_archive
_SPEC.loader.exec_module(demo_archive)


@pytest.fixture
def archive():
    server = demo_archive.create_server(port=0, token="test-token", quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _base_url(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def _get(server, path, params=None, token="test-token"):
    url = _base_url(server) + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.headers, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, exc.headers, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, exc.headers, body


def _get_bytes(server, path, params=None, token="test-token"):
    url = _base_url(server) + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _post(server, path, token="test-token"):
    url = _base_url(server) + path
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, method="POST", data=b"", headers=headers)
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def test_binds_to_loopback_only(archive):
    assert archive.server_address[0] == "127.0.0.1"


def test_missing_token_is_rejected(archive):
    status, _, body = _get(archive, "/health", token=None)
    assert status == 401
    assert "error" in body


def test_wrong_token_is_rejected(archive):
    status, _, body = _get(archive, "/health", token="wrong")
    assert status == 401
    assert "error" in body


def test_health(archive):
    status, _, body = _get(archive, "/health")
    assert status == 200
    assert body == {"status": "ok", "database": "ok"}


def test_stats_counts_match_the_fixture_data(archive):
    _, _, body = _get(archive, "/stats")
    assert body["messages"] == len(demo_archive.MESSAGES)
    assert body["chats"] == len(demo_archive.CONTACTS)
    assert body["attachments"] == len(demo_archive.ATTACHMENTS)
    assert body["date_range"]["from"] <= body["date_range"]["to"]


def test_sync_is_a_post_only_no_op(archive):
    status, body = _post(archive, "/sync")
    assert status == 200
    assert body["ok"] is True

    status, _, _ = _get(archive, "/sync")
    assert status == 404


def test_chats_lists_both_fixture_contacts(archive):
    _, _, body = _get(archive, "/chats")
    assert {chat["name"] for chat in body["chats"]} == {"Demo Buddy", "Demo Squad"}


def test_recent_respects_limit_and_is_newest_first(archive):
    _, _, body = _get(archive, "/recent", {"limit": 2})
    messages = body["messages"]
    assert len(messages) == 2
    assert messages[0]["timestamp"] >= messages[1]["timestamp"]


def test_unread_only_returns_unread_messages(archive):
    _, _, body = _get(archive, "/unread")
    assert body["messages"]
    assert all(message["unread"] for message in body["messages"])


def test_search_requires_a_query(archive):
    status, _, body = _get(archive, "/search", {"q": ""})
    assert status == 400
    assert "error" in body


def test_search_finds_a_known_message(archive):
    _, _, body = _get(archive, "/search", {"q": "日落"})
    assert any("日落" in result["text"] for result in body["results"])


def test_chat_returns_messages_for_a_known_contact(archive):
    _, _, body = _get(archive, "/chat", {"name": "Demo Buddy"})
    assert body["messages"]
    assert all(message["chat"] == "Demo Buddy" for message in body["messages"])


def test_chat_is_case_insensitive(archive):
    _, _, body = _get(archive, "/chat", {"name": "demo buddy"})
    assert body["messages"]


def test_chat_reports_unknown_contact_as_200_with_an_error_body(archive):
    status, _, body = _get(archive, "/chat", {"name": "Nobody"})
    assert status == 200
    assert "error" in body


def test_attachments_lists_everything_without_a_contact_filter(archive):
    _, _, body = _get(archive, "/attachments")
    assert len(body["attachments"]) == len(demo_archive.ATTACHMENTS)


def test_attachments_filters_by_contact(archive):
    _, _, body = _get(archive, "/attachments", {"name": "Demo Buddy"})
    assert {a["media_id"] for a in body["attachments"]} == {"att-sunset"}


def test_attachments_filters_by_kind(archive):
    _, _, body = _get(archive, "/attachments", {"kind": "image"})
    assert all(a["kind"] == "image" for a in body["attachments"])


def test_attachments_rejects_an_unknown_kind(archive):
    status, _, body = _get(archive, "/attachments", {"kind": "sticker"})
    assert status == 400
    assert "error" in body


def test_attachments_for_an_unknown_contact_is_an_empty_list_not_an_error(archive):
    status, _, body = _get(archive, "/attachments", {"name": "Nobody"})
    assert status == 200
    assert body["attachments"] == []


def test_media_returns_a_genuinely_decodable_png(archive):
    status, headers, data = _get_bytes(archive, "/media", {"id": "att-sunset"})
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert 'filename="sunset.png"' in headers["Content-Disposition"]
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_media_returns_a_parseable_pdf(archive):
    status, headers, data = _get_bytes(archive, "/media", {"id": "att-agenda"})
    assert status == 200
    assert headers["Content-Type"] == "application/pdf"
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")


def test_media_returns_the_text_fixture_verbatim(archive):
    status, _, data = _get_bytes(archive, "/media", {"id": "att-wifi-note"})
    assert status == 200
    assert b"hunter2-not-real" in data


def test_media_unknown_id_is_404_not_a_200_error_body(archive):
    status, _, body = _get(archive, "/media", {"id": "does-not-exist"})
    assert status == 404
    assert "error" in body


def test_no_write_or_send_routes_exist(archive):
    for path in ("/send", "/delete", "/markread", "/retract"):
        status, _, _ = _get(archive, path)
        assert status == 404


def test_generated_png_is_well_formed_for_arbitrary_sizes():
    def solid(_x, _y):
        return (1, 2, 3)

    png = demo_archive._png(3, 2, solid)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in png
    assert b"IEND" in png


def test_token_defaults_to_a_fresh_random_value_each_run():
    server_a = demo_archive.create_server(port=0, quiet=True)
    server_b = demo_archive.create_server(port=0, quiet=True)
    try:
        assert server_a.token != server_b.token
        assert len(server_a.token) >= 20
    finally:
        server_a.server_close()
        server_b.server_close()
