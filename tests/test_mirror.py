from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path

import pytest

from line_local_mcp.mirror import (
    Mirror,
    classify_sync,
    collect,
    load_collector_config,
)
from line_local_mcp.pipeline import load_profile, watch

MESSAGES = {
    "Client": [
        {"time": "2026-01-01 10:00", "from": "Client", "chatId": "c1", "type": "text", "text": "hi"},
        {"time": "2026-01-01 10:01", "from": "Me", "chatId": "c1", "type": "text", "text": "hello"},
    ],
    "Other": [
        {"time": "2026-01-01 11:00", "from": "Other", "chatId": "c2", "type": "text", "text": "yo"},
    ],
}
ATTACHMENTS = {
    "Client": [{"time": "2026-01-01 10:02", "from": "Client", "media_id": "m1", "kind": "image"}],
    "Other": [],
}


class FakeArchive:
    """Stands in for LineArchiveClient.  Records every call; can be told to fail /sync."""

    def __init__(self, *, sync_result=None, sync_raises=None):
        self.calls: list[tuple[str, str]] = []
        self.sync_result = sync_result if sync_result is not None else {
            "full": False,
            "sync": "[sync] upserted 3 rows | total messages=10 | watermark=1 | full=False",
        }
        self.sync_raises = sync_raises

    def request(self, method, path, params=None):
        self.calls.append((method, path))
        if path == "/sync":
            if self.sync_raises:
                raise self.sync_raises
            return self.sync_result
        name = params["name"]
        if path == "/chat":
            return {"messages": list(MESSAGES[name])}
        if path == "/attachments":
            return {"attachments": list(ATTACHMENTS[name])}
        raise AssertionError(path)


def write_config(
    tmp_path: Path, *, profile_source: str = "mirror", extra: str = "", profile_extra: str = ""
) -> Path:
    config = tmp_path / "pipeline.toml"
    config.write_text(
        f"""
[collector]
mirror_db = "{tmp_path / 'mirror' / 'line.sqlite'}"
chats = ["Client", "Other", "Client"]
poll_limit = 50
{extra}

[profiles.client]
project_label = "Client"
contact = "Client"
incoming_aliases = ["Client"]
state_dir = "{tmp_path / 'state'}"
todo_file = "{tmp_path / 'tasks.md'}"
source = "{profile_source}"
{profile_extra}
""",
        encoding="utf-8",
    )
    return config


def test_collector_config_dedupes_chats_and_expands_paths(tmp_path):
    config = load_collector_config(write_config(tmp_path))
    assert config.chats == ("Client", "Other")
    assert config.mirror_db == tmp_path / "mirror" / "line.sqlite"
    assert config.trigger_sync is True


def test_collect_is_idempotent_and_records_sync(tmp_path):
    config = load_collector_config(write_config(tmp_path))
    archive = FakeArchive()
    first = collect(config, archive)
    assert first["sync_ok"] is True
    assert first["new_events"] == 4  # 3 messages + 1 attachment
    assert archive.calls.count(("POST", "/sync")) == 1

    second = collect(config, archive)
    assert second["new_events"] == 0
    assert second["chats"]["Client"] == {"fetched": 3, "new": 0}

    mirror = Mirror(config.mirror_db, readonly=True)
    try:
        assert mirror.stats()["messages"] == 3
        assert mirror.stats()["attachments"] == 1
        assert mirror.get_meta("last_sync")["ok"] is True
        assert mirror.get_meta("last_sync_error") is None
        assert oct(config.mirror_db.stat().st_mode & 0o777) == "0o600"
        assert mirror.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        mirror.close()


def test_crashed_sync_is_recorded_not_raised_and_pull_continues(tmp_path):
    config = load_collector_config(write_config(tmp_path))
    traceback_text = (
        'Traceback (most recent call last):\n  File "line_sync.py", line 123, in main\n'
        "apsw.BusyError: database is locked"
    )
    result = collect(config, FakeArchive(sync_result={"full": False, "sync": traceback_text}))
    assert result["sync_ok"] is False
    assert "database is locked" in result["sync_error"]
    assert result["new_events"] == 4  # the pull still happened

    mirror = Mirror(config.mirror_db, readonly=True)
    try:
        assert mirror.get_meta("last_sync")["ok"] is False
        assert "database is locked" in mirror.get_meta("last_sync_error")
    finally:
        mirror.close()


def test_sync_transport_error_is_recorded(tmp_path):
    config = load_collector_config(write_config(tmp_path))
    result = collect(config, FakeArchive(sync_raises=RuntimeError("cannot reach LINE API")))
    assert result["sync_ok"] is False
    assert result["sync_error"].startswith("RuntimeError")
    assert result["new_events"] == 4


def test_classify_sync_variants():
    assert classify_sync({"sync": "[sync] upserted 0 rows"}) is None
    assert classify_sync({"error": "busy"}) == "busy"
    assert "BusyError" in classify_sync({"sync": "Traceback ...\napsw.BusyError: database is locked"})
    assert classify_sync("nope") is not None


def test_concurrent_collector_run_is_skipped(tmp_path):
    config = load_collector_config(write_config(tmp_path))
    config.lock_file.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(config.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        archive = FakeArchive()
        result = collect(config, archive)
        assert result["skipped"]
        assert archive.calls == []  # the archive was never touched
    finally:
        os.close(holder)


def test_watcher_reads_mirror_without_network(tmp_path):
    config_path = write_config(tmp_path)
    collect(load_collector_config(config_path), FakeArchive())
    profile = load_profile(config_path, "client")
    assert profile.source == "mirror"
    assert profile.mirror_db == tmp_path / "mirror" / "line.sqlite"
    assert profile.mcp_command == ()

    first = asyncio.run(watch(profile))
    assert first["new_messages"] == 1  # only the message from the client, not from "Me"
    assert first["new_attachments"] == 1
    inbox = [json.loads(line) for line in profile.inbox_file.read_text().splitlines()]
    assert len(inbox) == 1
    assert {row["from"] for row in inbox[0]["messages"]} == {"Client"}

    second = asyncio.run(watch(profile))
    assert second["new_messages"] == 0
    assert second["new_attachments"] == 0
    assert len(profile.inbox_file.read_text().splitlines()) == 1
    state = json.loads(profile.state_file.read_text())
    assert state["last_sync"]["source"] == "mirror"
    assert state["last_sync"]["last_sync_error"] is None


def test_watcher_captures_outgoing_as_evidence_not_as_a_task(tmp_path):
    config_path = write_config(tmp_path, profile_extra='self_sender_id = "Me"')
    collect(load_collector_config(config_path), FakeArchive())
    profile = load_profile(config_path, "client")

    result = asyncio.run(watch(profile))
    assert result["new_messages"] == 1  # still only the client's message
    assert result["new_outgoing"] == 1

    outbox = [json.loads(line) for line in profile.outbox_file.read_text().splitlines()]
    assert len(outbox) == 1
    assert outbox[0]["messages"] == [
        {"time": "2026-01-01 10:01", "from": "Me", "chatId": "c1", "type": "text", "text": "hello"}
    ]
    inbox = [json.loads(line) for line in profile.inbox_file.read_text().splitlines()]
    assert all(row["from"] != "Me" for event in inbox for row in event["messages"])

    # a second pass sees nothing new in either direction
    second = asyncio.run(watch(profile))
    assert second["new_messages"] == 0
    assert second["new_outgoing"] == 0
    assert len(profile.outbox_file.read_text().splitlines()) == 1


def test_outgoing_capture_is_off_without_self_sender_id(tmp_path):
    config_path = write_config(tmp_path)  # no self_sender_id configured
    collect(load_collector_config(config_path), FakeArchive())
    profile = load_profile(config_path, "client")

    result = asyncio.run(watch(profile))
    assert result["new_outgoing"] == 0
    assert not profile.outbox_file.exists()


def test_watcher_reports_missing_mirror(tmp_path):
    profile = load_profile(write_config(tmp_path), "client")
    with pytest.raises(RuntimeError, match="run the collector"):
        asyncio.run(watch(profile))


def test_mirror_profile_needs_a_database(tmp_path):
    config = tmp_path / "pipeline.toml"
    config.write_text(
        f"""
[profiles.client]
project_label = "Client"
contact = "Client"
state_dir = "{tmp_path / 'state'}"
todo_file = "{tmp_path / 'tasks.md'}"
source = "mirror"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mirror_db"):
        load_profile(config, "client")


def test_mcp_profile_still_requires_mcp_configuration(tmp_path):
    config = tmp_path / "pipeline.toml"
    config.write_text(
        f"""
[profiles.client]
project_label = "Client"
contact = "Client"
state_dir = "{tmp_path / 'state'}"
todo_file = "{tmp_path / 'tasks.md'}"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mcp.command"):
        load_profile(config, "client")
