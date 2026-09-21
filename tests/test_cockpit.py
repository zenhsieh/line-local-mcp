from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from line_local_mcp import cockpit
from line_local_mcp.cockpit import (
    COMPLETION_MARKER_RE,
    EVENT_MARKER_RE,
    REPLIED_MARKER_RE,
    USER_REVIEW_COLOR,
    _flap_compact_body,
    _flap_signature,
    _rebucket_flap_events,
    _render,
    _source_labels,
    _space_handle_input,
    _space_render,
    _task_views,
    _user_review_status,
    dashboard_snapshot,
    latest_case_status,
    mark_replied_tasks,
    pane_progress,
    reconcile_todos,
    space_conversation_snapshot,
    space_dashboard_snapshot,
    space_queue_snapshot,
    stamp_completed_tasks,
)
from line_local_mcp.pipeline import _append_jsonl, load_profile, todo_add, watch


def _profile(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "pipeline.toml"
    config.write_text(
        f'''[profiles.case]
project_label = "Synthetic Case"
contact = "Synthetic Contact"
state_dir = "{tmp_path / "state"}"
todo_file = "{tmp_path / "todo.md"}"

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
        "messages": [
            {
                "time": "2026-09-09 09:00",
                "from": "Synthetic Contact",
                "type": "text",
                "text": "Harmless test event",
            }
        ],
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
            line
            for line in profile.todo_file.read_text(encoding="utf-8").splitlines()
            if "First" not in line
        )
        + "\n",
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

    assert _source_labels(profile) == ("事件", "最新事件", "case_x 事件")
    assert profile.new_task_status == "EVENT"
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


def test_flap_signature_ignores_state_and_timestamp_but_not_unrelated_text():
    fired = "[進度] 事件 09-19 21:03 infra-fleet-alerts: 新告警：⚠ [pointer] edge-am62 讀不到"
    cleared = "[進度] 事件 09-19 21:30 infra-fleet-alerts: 告警已消失：⚠ [pointer] edge-am62 讀不到"
    other = "[進度] 事件 09-20 12:33 infra-fleet-alerts: 新告警：⚠ [hygiene] LINE watermark 過期"
    plain = "[進行] Build artifact"

    assert _flap_signature(fired) == _flap_signature(cleared) == "⚠ [pointer] edge-am62 讀不到"
    assert _flap_signature(other) != _flap_signature(fired)
    assert _flap_signature(plain) is None


def test_flap_compact_body_drops_boilerplate_but_keeps_tag_time_and_finding():
    fired = "[進度] 事件 09-19 21:03 infra-fleet-alerts: 新告警：⚠ [pointer] edge-am62 讀不到"
    other_source = "[EVENT] 事件 09-14 10:00 infra-fleet-alerts: 新告警：⚠ [host] pve01 連不上"
    plain = "[進行] Build artifact"

    # "事件"/the sender name/"新告警：" all say the same thing on every row of
    # this source -- gone. The `[tag]`, the timestamp, and the actual finding
    # (including the ⚠) survive untouched.
    assert _flap_compact_body(fired) == "[進度] 09-19 21:03 ⚠ [pointer] edge-am62 讀不到"
    assert _flap_compact_body(other_source) == "[EVENT] 09-14 10:00 ⚠ [host] pve01 連不上"
    assert _flap_compact_body(plain) is None


def test_rebucket_flap_events_files_by_latest_state_not_checkbox():
    # everything lands in `pending` first, exactly as reconcile_todos writes it --
    # an unchecked task regardless of whether the event is a firing or a clear.
    pending = [
        (1, "[進度] 事件 09-19 21:03 infra-fleet-alerts: 新告警：⚠ [pointer] am62 讀不到"),
        (2, "[進度] 事件 09-19 21:30 infra-fleet-alerts: 告警已消失：⚠ [pointer] am62 讀不到"),
        (3, "[進度] 事件 09-20 12:33 infra-fleet-alerts: 新告警：⚠ [hygiene] watermark 過期"),
        (4, "[待決] Approve exact publication"),
    ]

    kept_pending, kept_completed = _rebucket_flap_events(pending, completed=[])

    # am62 fired then cleared -- its LATEST state is resolved, so it is filed
    # under completed and gone from pending, not left sitting there forever.
    assert [task_id for task_id, _body in kept_pending] == [3, 4]
    assert [task_id for task_id, _body in kept_completed] == [2]
    assert kept_completed[0][1].endswith(" ×2")
    # the still-open hygiene alert lost its "事件 ... infra-fleet-alerts: 新告警："
    # boilerplate (redundant once a row is filed as pending) but kept its
    # timestamp and the actual finding; the ordinary decision task is untouched
    assert kept_pending[0][1] == "[進度] 09-20 12:33 ⚠ [hygiene] watermark 過期"
    assert kept_pending[1][1] == pending[3][1]


def test_rebucket_flap_events_reopens_pending_after_a_later_firing():
    pending = [
        (1, "[進度] 事件 09-19 21:03 infra-fleet-alerts: 新告警：⚠ [pointer] am62 讀不到"),
        (2, "[進度] 事件 09-19 21:30 infra-fleet-alerts: 告警已消失：⚠ [pointer] am62 讀不到"),
        (3, "[進度] 事件 09-20 03:38 infra-fleet-alerts: 新告警：⚠ [pointer] am62 讀不到"),
    ]

    kept_pending, kept_completed = _rebucket_flap_events(pending, completed=[])

    # the alert re-fired after clearing, so it is open again -- filed under
    # pending at its latest task id, carrying the full lifetime count.
    assert kept_completed == []
    assert [task_id for task_id, _body in kept_pending] == [3]
    assert kept_pending[0][1].endswith(" ×3")


def test_rebucket_flap_events_respects_an_already_checked_row_even_if_it_reads_open():
    """A human (or an earlier manual close) checking the box always wins.

    Regression: a batch of alerts from 2026-09-14 were each fired once and
    then manually checked off (completed-at stamped) with no matching
    "告警已消失" task ever created -- reopening them into pending just
    because their own text still reads "新告警" undid that closure.
    """

    pending: list[tuple[int, str]] = []
    completed = [
        (62, "[EVENT] [0914] 事件 09-14 10:00 infra-fleet-alerts: 新告警：⚠ [host] pve01 連不上"),
    ]

    kept_pending, kept_completed = _rebucket_flap_events(pending, completed)

    assert kept_pending == []
    assert [task_id for task_id, _body in kept_completed] == [62]


def test_flap_signature_ignores_a_trailing_batched_message_count():
    """Regression: the same alert's fire and clear can each batch with a
    different number of unrelated inbox messages landing at the same poll,
    so `_task_summary`'s trailing "(+N messages)" must not be part of the
    identity used to match them -- otherwise a "+1" fire and a "+2" clear
    for the literal same alert never retire each other.
    """

    fired = "[進度] 事件 09-20 03:38 infra-fleet-alerts: 新告警：⚠ [host] edge-am62 連不上 (+1 messages)"
    cleared = "[進度] 事件 09-20 22:46 infra-fleet-alerts: 告警已消失：⚠ [host] edge-am62 連不上 (+2 messages)"

    assert _flap_signature(fired) == _flap_signature(cleared) == "⚠ [host] edge-am62 連不上"

    pending = [(217, fired)]
    completed = [(221, cleared)]
    kept_pending, kept_completed = _rebucket_flap_events(pending, completed)
    assert kept_pending == []
    assert [task_id for task_id, _body in kept_completed] == [221]


def test_render_pending_tab_collapses_a_flapping_alert_with_a_colored_count(
    tmp_path, monkeypatch, capsys
):
    profile = _profile(tmp_path)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 5 -->\n\n"
        "- [ ] [01] [進度] 事件 09-19 21:03 infra-fleet-alerts: "
        "新告警：⚠ [pointer] edge-am62 讀不到\n"
        "- [ ] [02] [進度] 事件 09-19 21:30 infra-fleet-alerts: "
        "告警已消失：⚠ [pointer] edge-am62 讀不到\n"
        "- [ ] [03] [進度] 事件 09-19 21:35 infra-fleet-alerts: "
        "新告警：⚠ [pointer] edge-am62 讀不到\n"
        "- [ ] [04] [進度] 事件 09-20 12:33 infra-fleet-alerts: "
        "新告警：⚠ [hygiene] watermark 過期\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "line_local_mcp.cockpit.shutil.get_terminal_size",
        lambda _fallback: __import__("os").terminal_size((140, 7)),
    )

    _render(profile, "pending", 0, False)

    rendered = capsys.readouterr().out
    left_rows = [line.split(" │ ", 1)[0] for line in rendered.splitlines()[1:-1]]
    joined = "\n".join(left_rows)

    # three flapping am62 rows collapse into one line under the latest task id
    assert "03." in joined
    assert "01." not in joined and "02." not in joined
    assert "×3" in joined
    # the colored badge wraps exactly the count, not the whole line
    assert "\033[1;38;5;208m ×3\033[0m" in joined
    # the unrelated alert is untouched and still its own row
    assert "04." in joined and "watermark" in joined


def test_render_moves_a_resolved_alert_from_pending_to_completed(
    tmp_path, monkeypatch, capsys
):
    """A 告警已消失 that answers the last open firing retires it -- not a sibling row."""

    profile = _profile(tmp_path)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 3 -->\n\n"
        "- [ ] [01] [進度] 事件 09-19 21:03 infra-fleet-alerts: "
        "新告警：⚠ [pointer] edge-am62 讀不到\n"
        "- [ ] [02] [進度] 事件 09-19 21:30 infra-fleet-alerts: "
        "告警已消失：⚠ [pointer] edge-am62 讀不到\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "line_local_mcp.cockpit.shutil.get_terminal_size",
        lambda _fallback: __import__("os").terminal_size((140, 7)),
    )

    _render(profile, "pending", 0, False)
    pending_rendered = capsys.readouterr().out
    assert "edge-am62" not in pending_rendered
    assert "待辦 0" in pending_rendered

    _render(profile, "completed", 0, False)
    completed_rendered = capsys.readouterr().out
    left_rows = [line.split(" │ ", 1)[0] for line in completed_rendered.splitlines()[1:-1]]
    joined = "\n".join(left_rows)
    assert "02." in joined
    assert "edge-am62" in joined
    assert "×2" in joined


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

def test_completion_date_is_stable_and_reopen_removes_it(tmp_path):
    profile = _profile(tmp_path)
    todo_add(profile, "Finish migration")
    text = profile.todo_file.read_text().replace("- [ ]", "- [x]", 1)
    profile.todo_file.write_text(text)

    first = datetime.fromisoformat("2026-09-09T12:00:00+08:00")
    later = datetime.fromisoformat("2026-09-10T12:00:00+08:00")
    assert stamp_completed_tasks(profile, first)["stamped"] == 1
    assert stamp_completed_tasks(profile, later)["retained"] == 1
    text = profile.todo_file.read_text()
    assert "[0909]" in text and "completed-at:2026-09-09" in text

    profile.todo_file.write_text(text.replace("- [x]", "- [ ]", 1))
    assert stamp_completed_tasks(profile, later)["reopened"] == 1
    text = profile.todo_file.read_text()
    assert "[0909]" not in text and not COMPLETION_MARKER_RE.search(text)


def test_space_dashboard_is_task_only(tmp_path):
    profile = _profile(tmp_path)
    todo_add(profile, "Customer action needed")
    profile.state_dir.mkdir(parents=True, exist_ok=True)
    profile.inbox_file.write_text(
        json.dumps({"messages": [{"text": "raw conversation must stay hidden"}]}) + "\n"
    )

    lanes = space_dashboard_snapshot([profile])
    assert lanes[0]["pending"][0][1].endswith("Customer action needed")
    assert "raw conversation must stay hidden" not in repr(lanes)


def test_space_queue_scales_past_four_customers_and_orders_oldest_first(tmp_path):
    profiles = []
    for index in range(7):
        profile = _profile(tmp_path / str(index))
        todo_add(
            profile,
            f"[待分流] LINE 2026-09-{index + 1:02d}T09:00:00+08:00 Customer {index}",
        )
        profiles.append(profile)

    lanes, queue = space_queue_snapshot(list(reversed(profiles)))

    assert len(lanes) == 7
    assert len(queue) == 7
    assert [row["body"].split()[-2:] for row in queue] == [
        ["Customer", str(index)] for index in range(7)
    ]


def test_space_conversation_only_contains_events_for_unfinished_tasks(tmp_path):
    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    pending_event = {
        "fingerprint": "c" * 64,
        "messages": [
            {
                "time": "2026-09-10T09:00:00+08:00",
                "from": "Customer",
                "type": "text",
                "text": "Please handle this task",
            }
        ],
        "attachments": [],
    }
    unrelated_event = {
        "fingerprint": "d" * 64,
        "messages": [
            {
                "time": "2026-09-10T09:01:00+08:00",
                "from": "Customer",
                "type": "text",
                "text": "Unrelated conversation stays private",
            }
        ],
        "attachments": [],
    }
    profile.inbox_file.write_text(json.dumps(pending_event) + "\n", encoding="utf-8")
    assert reconcile_todos(profile)["created"] == 1
    with profile.inbox_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(unrelated_event) + "\n")

    conversations = space_conversation_snapshot(profile)
    assert conversations == ["#01 09:00 Customer: Please handle this task"]
    assert "Unrelated conversation" not in repr(conversations)

    profile.todo_file.write_text(
        profile.todo_file.read_text(encoding="utf-8").replace("- [ ]", "- [x]", 1),
        encoding="utf-8",
    )
    assert space_conversation_snapshot(profile) == []


def _jsonl_profile(tmp_path: Path, source_file: Path, label: str = "Event Case"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "pipeline.toml"
    config.write_text(
        f"""[profiles.case]
project_label = "{label}"
contact = "case_x"
incoming_aliases = ["case_x", "worker_x"]
state_dir = "{tmp_path / "state"}"
todo_file = "{tmp_path / "todo.md"}"
source = "jsonl"
source_file = "{source_file}"
""",
        encoding="utf-8",
    )
    return load_profile(config, "case")


def test_space_dashboard_serves_a_jsonl_lane_beside_a_line_lane(tmp_path, capsys):
    """The pilot view is source-agnostic: a jsonl lane queues and renders like LINE."""

    source = tmp_path / "events.jsonl"
    source.write_text(
        json.dumps(
            {
                "time": "2026-09-11T09:00:00+08:00",
                "from": "worker_x",
                "type": "text",
                "text": "Needs owner decision",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event_lane = _jsonl_profile(tmp_path / "events", source)
    line_lane = _profile(tmp_path / "line")
    todo_add(line_lane, "LINE 2026-09-10T09:00:00+08:00 Customer ask", status="待分流")

    assert asyncio.run(watch(event_lane))["new_messages"] == 1
    assert reconcile_todos(event_lane)["created"] == 1

    lanes, queue = space_queue_snapshot([event_lane, line_lane])
    assert [lane["label"] for lane in lanes] == ["Event Case", "Synthetic Case"]
    # oldest-first across both sources, and the jsonl task carries the generic labels
    assert [row["label"] for row in queue] == ["Synthetic Case", "Event Case"]
    assert queue[1]["body"] == (
        "[EVENT] 事件 2026-09-11T09:00:00+08:00 worker_x: Needs owner decision"
    )

    assert space_conversation_snapshot(event_lane) == [
        "#01 09:00 worker_x: Needs owner decision"
    ]

    _space_render([event_lane, line_lane], "SERVICE · PILOT", 0, 0)
    rendered = capsys.readouterr().out
    assert "Event Case · 待辦 事件" in rendered
    assert "待辦 LINE" not in rendered

    _space_render([event_lane, line_lane], "SERVICE · PILOT", 0, 1)
    assert "Synthetic Case · 待辦 LINE" in capsys.readouterr().out


def test_nested_task_keeps_its_indent_through_completion_and_reply_marking(tmp_path):
    """Merged branches meet here: main rebuilds task lines, jsonl made indent meaningful."""

    profile = _profile(tmp_path)
    profile.state_dir.mkdir(parents=True)
    profile.todo_file.write_text(
        "# Tasks\n\n<!-- next-task-id: 2 -->\n\n"
        "  - [x] [01] [執行] nested dependency\n",
        encoding="utf-8",
    )

    stamped = stamp_completed_tasks(profile, datetime.fromisoformat("2026-09-09T12:00:00+08:00"))
    assert stamped["stamped"] == 1
    line = next(
        row
        for row in profile.todo_file.read_text(encoding="utf-8").splitlines()
        if "nested dependency" in row
    )
    assert line.startswith("  - [x] [01] ")
    assert "completed-at:2026-09-09" in line

    # the visible task body keeps the indent and drops every durable marker
    _pending, completed, _events, _state = dashboard_snapshot(profile)
    assert completed == [(1, "  [執行] [0909] nested dependency")]


def test_cockpit_cli_reports_a_malformed_jsonl_row_instead_of_crashing(tmp_path, capsys, monkeypatch):
    source = tmp_path / "events.jsonl"
    source.write_text('["not", "an", "object"]\n', encoding="utf-8")
    profile = _jsonl_profile(tmp_path / "events", source)

    monkeypatch.setattr(
        "sys.argv",
        ["line-local-case-cockpit", "--config", str(profile.state_dir.parent / "pipeline.toml"),
         "run", "case"],
    )
    assert cockpit.main() == 1
    assert "is not an object" in capsys.readouterr().err


def test_space_customer_arrows_are_mouse_clickable():
    targets = (12, 74, 76, 98, 100)

    assert _space_handle_input("\x1b[<0;98;12M", 0, 0, 4, targets) == (0, 1)
    assert _space_handle_input("\x1b[<0;100;12M", 0, 0, 4, targets) == (0, 1)
    assert _space_handle_input("\x1b[<0;76;12M", 0, 0, 4, targets) == (0, 3)
    assert _space_handle_input("\x1b[<0;77;12M", 0, 0, 4, targets) == (0, 0)
    assert _space_handle_input("\x1b[<0;98;11M", 0, 0, 4, targets) == (0, 0)
    assert _space_handle_input("\x1b[<0;98;12m", 0, 0, 4, targets) == (0, 0)


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
        (1, "[LINE] [已回覆 16:52・歸檔未知] LINE 2026-09-09 09:00 Synthetic Contact: Need a callback")
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
