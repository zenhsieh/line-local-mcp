from __future__ import annotations

import json
from pathlib import Path

from line_local_mcp.cockpit import (
    EVENT_MARKER_RE,
    _source_labels,
    dashboard_snapshot,
    reconcile_todos,
)
from line_local_mcp.pipeline import load_profile, todo_add


def _profile(tmp_path: Path):
    config = tmp_path / "pipeline.toml"
    config.write_text(
        f'''[profiles.case]
project_label = "Synthetic Case"
contact = "Synthetic Contact"
state_dir = "{tmp_path / 'state'}"
todo_file = "{tmp_path / 'todo.md'}"

[profiles.case.mcp]
command = ["/bin/true"]
''',
        encoding="utf-8",
    )
    return load_profile(config, "case")


def test_event_becomes_one_durable_task_and_marker_is_hidden(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    event = {
        "fingerprint": "a" * 64,
        "messages": [{
            "time": "2026-09-09 09:00",
            "from": "Synthetic Contact",
            "type": "text",
            "text": "Harmless test event",
        }],
        "attachments": [],
    }
    profile.inbox_file.write_text(json.dumps(event) + "\n", encoding="utf-8")

    assert reconcile_todos(profile)["created"] == 1
    assert reconcile_todos(profile)["created"] == 0
    pending, completed, events, _state = dashboard_snapshot(profile)
    assert completed == []
    assert len(pending) == 1
    assert EVENT_MARKER_RE.search(profile.todo_file.read_text(encoding="utf-8"))
    assert "line-event:" not in pending[0][1]
    assert events == ["09:00 Synthetic Contact: Harmless test event"]


def test_deleted_event_task_is_not_recreated_and_id_is_not_reused(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    event = {
        "fingerprint": "b" * 64,
        "messages": [{"time": "2026-09-09 09:00", "type": "text", "text": "First"}],
        "attachments": [],
    }
    profile.inbox_file.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert reconcile_todos(profile)["created"] == 1
    profile.todo_file.write_text(
        "\n".join(
            line for line in profile.todo_file.read_text(encoding="utf-8").splitlines()
            if "First" not in line
        ) + "\n",
        encoding="utf-8",
    )

    assert reconcile_todos(profile)["created"] == 0
    assert todo_add(profile, "Second") == 2


def test_jsonl_source_uses_generic_event_labels(tmp_path):
    config = tmp_path / "pipeline.toml"
    source_file = tmp_path / "events.jsonl"
    source_file.write_text("", encoding="utf-8")
    config.write_text(
        f'''[profiles.case]
project_label = "Synthetic Case"
contact = "case_x"
state_dir = "{tmp_path / 'state'}"
todo_file = "{tmp_path / 'todo.md'}"
source = "jsonl"
source_file = "{source_file}"
''',
        encoding="utf-8",
    )
    profile = load_profile(config, "case")
    profile.state_dir.mkdir(parents=True)
    event = {
        "fingerprint": "c" * 64,
        "messages": [{"time": "2026-09-10 09:00", "from": "pane_x", "text": "Done"}],
        "attachments": [],
    }
    profile.inbox_file.write_text(json.dumps(event) + "\n", encoding="utf-8")

    assert _source_labels(profile) == ("EVENT", "事件", "最新事件", "case_x 事件")
    assert reconcile_todos(profile)["created"] == 1
    todo = profile.todo_file.read_text(encoding="utf-8")
    assert "[EVENT] 事件 2026-09-10 09:00 pane_x: Done" in todo
    assert "LINE" not in todo
