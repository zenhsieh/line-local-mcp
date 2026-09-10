from __future__ import annotations

import json
from pathlib import Path

from line_local_mcp.cockpit import (
    EVENT_MARKER_RE,
    _source_labels,
    _task_views,
    dashboard_snapshot,
    pane_progress,
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


def test_task_views_separate_running_pending_and_completed():
    views = _task_views(
        [(1, "[進行] Active"), (2, "Queued"), (3, "[執行] Also active")],
        [(4, "Done")],
    )
    assert [row[0] for row in views["running"]] == [1, 3]
    assert [row[0] for row in views["pending"]] == [2]
    assert [row[0] for row in views["completed"]] == [4]


def test_dashboard_preserves_dependency_indentation(tmp_path):
    profile = _profile(tmp_path)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 3 -->\n\n"
        "- [ ] [01] Root task\n"
        "  - [ ] [02] [執行] Dependent task\n",
        encoding="utf-8",
    )

    pending, completed, _events, _state = dashboard_snapshot(profile)
    assert completed == []
    assert pending == [(1, "Root task"), (2, "  [執行] Dependent task")]


def test_pane_progress_keeps_latest_event_and_renders_hierarchy(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    events = [
        {
            "fingerprint": "d" * 64,
            "messages": [
                {"time": "2026-09-10 09:00", "from": "Synthetic Contact", "text": "Control"},
                {
                    "time": "2026-09-10 09:01",
                    "from": "worker_x",
                    "parent": "Synthetic Contact",
                    "state": "running",
                    "progress": "First step",
                    "text": "Older detail",
                },
            ],
            "attachments": [],
        },
        {
            "fingerprint": "e" * 64,
            "messages": [
                {
                    "time": "2026-09-10 09:02",
                    "from": "worker_x",
                    "parent": "Synthetic Contact",
                    "state": "done",
                    "progress": "Second step",
                    "text": "Newer detail",
                }
            ],
            "attachments": [],
        },
    ]
    profile.inbox_file.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")

    assert pane_progress(profile) == [
        "· Synthetic Contact Control",
        "└─ ✓ worker_x Second step",
    ]
