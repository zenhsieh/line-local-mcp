from __future__ import annotations

import json
from pathlib import Path

from line_local_mcp.cockpit import (
    EVENT_MARKER_RE,
    USER_REVIEW_COLOR,
    _render,
    _source_labels,
    _task_views,
    _user_review_status,
    dashboard_snapshot,
    latest_case_status,
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
        [
            (1, "[進行] Active"),
            (2, "Queued"),
            (3, "[執行] Also active"),
            (5, "[待決] Owner decision"),
        ],
        [(4, "Done")],
    )
    assert [row[0] for row in views["running"]] == [1, 3]
    assert [row[0] for row in views["pending"]] == [2]
    assert [row[0] for row in views["review"]] == [5]
    assert [row[0] for row in views["completed"]] == [4]


def test_user_review_status_uses_only_explicit_pending_labels():
    pending = [
        (7, "[進行] Build artifact"),
        (8, "Text says approval but has no durable review status"),
        (9, "[待決] Approve exact publication"),
        (10, "  [核准] Approve dependent action"),
    ]

    assert _user_review_status(pending) == (2, "#09, #10")
    assert _user_review_status([(11, "[進行] No user action")]) is None


def test_render_pins_colored_review_status_to_first_line(tmp_path, monkeypatch, capsys):
    profile = _profile(tmp_path)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 4 -->\n\n"
        "- [ ] [01] [進行] Active task\n"
        "- [ ] [02] [待決] Approve exact action\n"
        "- [ ] [03] Ordinary queued task\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "line_local_mcp.cockpit.shutil.get_terminal_size",
        lambda _fallback: __import__("os").terminal_size((115, 6)),
    )

    _render(profile, "pending", 0, False)

    rendered = capsys.readouterr().out
    first_line = rendered.removeprefix("\033[2J\033[H").splitlines()[0]
    assert first_line.startswith(USER_REVIEW_COLOR)
    assert "核准狀態｜待核准 1 項 · #02" in first_line
    assert len(rendered.removeprefix("\033[2J\033[H").splitlines()) == 6


def test_render_review_tab_shows_only_user_decisions(tmp_path, monkeypatch, capsys):
    profile = _profile(tmp_path)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 4 -->\n\n"
        "- [ ] [01] [進行] Active task\n"
        "- [ ] [02] [待決] Approve exact action\n"
        "- [ ] [03] Ordinary queued task\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "line_local_mcp.cockpit.shutil.get_terminal_size",
        lambda _fallback: __import__("os").terminal_size((115, 6)),
    )

    _render(profile, "review", 0, False)

    rendered = capsys.readouterr().out
    assert "Approve exact action" in rendered
    assert "Active task" not in rendered
    assert "Ordinary queued task" not in rendered
    assert "待核准 1" in rendered


def test_render_review_tab_puts_badge_and_decision_on_separate_rows(tmp_path, monkeypatch, capsys):
    profile = _profile(tmp_path)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 2 -->\n\n"
        "- [ ] [01] [待決] Approve the fixed-environment wrapper\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "line_local_mcp.cockpit.shutil.get_terminal_size",
        lambda _fallback: __import__("os").terminal_size((115, 7)),
    )

    _render(profile, "review", 0, False)

    left_rows = [line.split(" │ ", 1)[0] for line in capsys.readouterr().out.splitlines()[1:-1]]
    assert "01." in left_rows[0]
    assert "待決" in left_rows[0]
    assert "Approve the fixed-environment wrapper" not in left_rows[0]
    assert "Approve the fixed-environment wrapper" in left_rows[1]


def test_render_review_tab_wraps_long_decision_without_truncating(tmp_path, monkeypatch, capsys):
    profile = _profile(tmp_path)
    detail = "Approve exact publication bound to commit " + "a" * 64 + " and preserve archive branches"
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 2 -->\n\n"
        f"- [ ] [01] [待決] {detail}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "line_local_mcp.cockpit.shutil.get_terminal_size",
        lambda _fallback: __import__("os").terminal_size((90, 8)),
    )

    _render(profile, "review", 0, False)

    rendered = capsys.readouterr().out
    assert "…" not in "\n".join(line.split(" │ ", 1)[0] for line in rendered.splitlines()[1:-1])
    assert "preserve archive branches" in rendered
    assert len(rendered.splitlines()) == 8


def test_render_always_reserves_green_clear_status_line(tmp_path, monkeypatch, capsys):
    profile = _profile(tmp_path)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 2 -->\n\n- [ ] [01] [進行] Active task\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "line_local_mcp.cockpit.shutil.get_terminal_size",
        lambda _fallback: __import__("os").terminal_size((115, 6)),
    )

    _render(profile, "pending", 0, False)

    first_line = capsys.readouterr().out.removeprefix("\033[2J\033[H").splitlines()[0]
    assert "\033[1;38;5;255;48;5;28m" in first_line
    assert "核准狀態｜無待核准事項 │ 尚無 durable 狀態" in first_line


def test_latest_case_status_prefers_latest_progress(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    events = [
        {
            "fingerprint": "a" * 64,
            "messages": [
                {
                    "time": "2026-09-12T08:00:00+08:00",
                    "from": "worker_x",
                    "state": "done",
                    "progress": "older closure",
                    "text": "long older detail",
                },
                {
                    "time": "2026-09-12T08:01:00+08:00",
                    "from": "pilot_x",
                    "state": "running",
                    "progress": "verification routed",
                    "text": "longer detail should not win",
                },
            ],
            "attachments": [],
        }
    ]
    profile.inbox_file.write_text(json.dumps(events[0]) + "\n", encoding="utf-8")

    assert latest_case_status(profile) == "● pilot_x verification routed"


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
