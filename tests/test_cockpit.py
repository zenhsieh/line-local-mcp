from __future__ import annotations

import json
from pathlib import Path

from line_local_mcp.cockpit import (
    EVENT_MARKER_RE,
    REPLIED_MARKER_RE,
    dashboard_snapshot,
    mark_replied_tasks,
    reconcile_todos,
)
from line_local_mcp.pipeline import _append_jsonl, load_profile, todo_add


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


def test_dashboard_shows_full_bidirectional_conversation(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    profile.inbox_file.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "time": "2026-09-09T09:00:00+08:00",
                        "from": "Synthetic Contact",
                        "type": "text",
                        "text": "Ping",
                    }
                ],
                "attachments": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _append_jsonl(
        profile.outbox_file,
        {
            "messages": [
                {
                    "time": "2026-09-09T09:05:00+08:00",
                    "from": "u-self",
                    "type": "text",
                    "text": "Pong",
                }
            ]
        },
    )

    _pending, _completed, events, _state = dashboard_snapshot(profile)
    assert events == ["09:05 我: Pong", "09:00 Synthetic Contact: Ping"]


def test_mark_replied_tasks_flags_reply_without_closing_or_guessing_archive(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    # The real archive uses "YYYY-MM-DD HH:MM" (a space, not "T"); the marker regex
    # must not silently fail to match this and re-mark the task on every run.
    event = {
        "fingerprint": "e" * 64,
        "messages": [
            {
                "time": "2026-09-09 09:00",
                "from": "Synthetic Contact",
                "type": "text",
                "text": "Need a callback",
            }
        ],
        "attachments": [],
    }
    profile.inbox_file.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert reconcile_todos(profile)["created"] == 1

    # No outgoing reply recorded yet: nothing gets marked.
    assert mark_replied_tasks(profile) == {"marked": 0}
    assert "已回覆" not in profile.todo_file.read_text(encoding="utf-8")

    _append_jsonl(
        profile.outbox_file,
        {
            "messages": [
                {
                    "time": "2026-09-09 16:52",
                    "from": "u-self",
                    "type": "text",
                    "text": "好的 收到",
                }
            ]
        },
    )
    assert mark_replied_tasks(profile) == {"marked": 1}
    text = profile.todo_file.read_text(encoding="utf-8")
    assert "[已回覆 16:52・歸檔未知]" in text
    assert "- [ ] [01] " in text  # replying never auto-closes, and never drops the task id
    assert REPLIED_MARKER_RE.search(text)

    pending, completed, _events, _state = dashboard_snapshot(profile)
    assert completed == []
    assert pending == [
        (1, "[已回覆 16:52・歸檔未知] [LINE] LINE 2026-09-09 09:00 Synthetic Contact: Need a callback")
    ]
    assert "<!-- replied-at" not in pending[0][1]  # hidden marker never leaks into display

    # Idempotent across repeated full reconcile cycles: no duplicate badges, no
    # renumbering, whether called directly or through the same path cockpit run uses.
    for _ in range(3):
        assert reconcile_todos(profile)["created"] == 0
        assert mark_replied_tasks(profile) == {"marked": 0}
    final = profile.todo_file.read_text(encoding="utf-8")
    assert final.count("已回覆 16:52") == 1
    assert final.count("<!-- replied-at") == 1
    assert "- [ ] [01] " in final


def test_mark_replied_tasks_ignores_outgoing_before_the_task(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    event = {
        "fingerprint": "f" * 64,
        "messages": [
            {
                "time": "2026-09-09T16:00:00+08:00",
                "from": "Synthetic Contact",
                "type": "text",
                "text": "New ask after the earlier reply",
            }
        ],
        "attachments": [],
    }
    profile.inbox_file.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert reconcile_todos(profile)["created"] == 1
    _append_jsonl(
        profile.outbox_file,
        {
            "messages": [
                {
                    "time": "2026-09-09T09:00:00+08:00",
                    "from": "u-self",
                    "type": "text",
                    "text": "An earlier, unrelated reply",
                }
            ]
        },
    )

    assert mark_replied_tasks(profile) == {"marked": 0}
    assert "已回覆" not in profile.todo_file.read_text(encoding="utf-8")
