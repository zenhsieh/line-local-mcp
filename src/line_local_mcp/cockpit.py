"""Interactive Case Cockpit over the shared read-only event pipeline.

The cockpit composes existing inbox, task, and injection contracts.  It does
not mutate source events, infer business truth, or own the adjacent agent.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import select
import shutil
import sys
import termios
import time
import tty
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .pipeline import (
    SELF_LABEL,
    Profile,
    _load_json,
    _lock,
    _write_private,
    _write_todo,
    ensure_task_ids,
    inbox_rows,
    inject_pending,
    injection_active,
    injection_switch,
    load_profile,
    outbox_rows,
    set_injection_switch,
    todo_add,
    watch,
)

EVENT_MARKER = "<!-- line-event:{fingerprint} -->"
EVENT_MARKER_RE = re.compile(r"\s*<!-- line-event:[0-9a-f]{64} -->")
EVENT_FINGERPRINT_RE = re.compile(r"<!-- line-event:(?P<fingerprint>[0-9a-f]{64}) -->")
COMPLETION_MARKER = "<!-- completed-at:{date} -->"
COMPLETION_MARKER_RE = re.compile(r"\s*<!-- completed-at:(?P<date>\d{4}-\d{2}-\d{2}) -->")
REPLIED_MARKER = "<!-- replied-at:{timestamp} archived:{status} -->"
REPLIED_MARKER_RE = re.compile(
    r"\s*<!-- replied-at:(?P<timestamp>.+?) archived:(?P<status>unknown|yes|no) -->"
)
ARCHIVE_STATUS_LABELS = {"unknown": "歸檔未知", "yes": "已歸檔", "no": "未歸檔"}
STATUS_PREFIX_RE = re.compile(r"^\[(?!\d{4}\])[^\]]+\]\s+")
VISIBLE_COMPLETION_RE = re.compile(
    r"^(?P<status>\[(?!\d{4}\])[^\]]+\]\s+)?"
    r"\[(?P<stamp>\d{4})\]\s+"
)
COMPLETED_BADGE_RE = re.compile(r"\[(?P<stamp>\d{4})完成\]")
TASK_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<prefix>- \[(?P<checked>[ xX])\]\s+)"
    r"(?:\[(?:#)?(?P<task_id>\d{2,})\]\s+)?(?P<body>.*)$"
)
MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
STATUS_BADGES = {
    "[完成]": ("完成", "\033[38;5;250;48;5;238m"),
    "[LINE]": ("LINE", "\033[1;38;5;159;48;5;24m"),
    "[EVENT]": ("事件", "\033[1;38;5;159;48;5;24m"),
    "[進行]": ("執行", "ACTIVE"),
    "[執行]": ("執行", "ACTIVE"),
    "[設計]": ("設計", "\033[38;5;182;48;5;53m"),
    "[盤點]": ("盤點", "\033[38;5;186;48;5;58m"),
    "[待問]": ("待問", "\033[38;5;181;48;5;52m"),
    "[待決]": ("待決", "\033[38;5;181;48;5;52m"),
    "[待分流]": ("分流", "\033[38;5;181;48;5;52m"),
}
USER_REVIEW_LABELS = ("[待決]", "[待問]", "[審核]", "[核准]", "[批准]")
USER_REVIEW_COLOR = "\033[1;38;5;16;48;5;220m"
USER_CLEAR_COLOR = "\033[1;38;5;255;48;5;28m"
TASK_TABS = ("pending", "review", "completed")


@contextmanager
def _cockpit_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "cockpit.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _one_line(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _source_labels(profile: Profile) -> tuple[str, str, str]:
    """Display labels for this profile's source: summary prefix, panel and window title."""

    if profile.source == "jsonl":
        return "事件", "最新事件", f"{profile.contact} 事件"
    return "LINE", "最新 LINE", f"{profile.contact} LINE"


def _task_summary(event: dict[str, Any], source_name: str = "LINE") -> str:
    messages = event.get("messages", [])
    attachments = event.get("attachments", [])
    if messages:
        first = messages[0]
        when = _one_line(first.get("time"), 32)
        sender = _one_line(first.get("from"), 48)
        body = _one_line(first.get("text"))
        suffix = f" (+{len(messages) - 1} messages)" if len(messages) > 1 else ""
        return f"{source_name} {when} {sender}: {body}{suffix}".strip()
    if attachments:
        first = attachments[0]
        when = _one_line(first.get("time"), 32)
        sender = _one_line(first.get("from"), 48)
        name = _one_line(first.get("filename") or first.get("kind") or "attachment")
        suffix = f" (+{len(attachments) - 1} attachments)" if len(attachments) > 1 else ""
        return f"{source_name} {when} {sender}: [attachment] {name}{suffix}".strip()
    return f"{source_name} archive event"


def _clean_task_body(body: str) -> str:
    """Strip every durable marker so a task body is safe to display."""

    body = EVENT_MARKER_RE.sub("", body)
    body = COMPLETION_MARKER_RE.sub("", body)
    return REPLIED_MARKER_RE.sub("", body).strip()


def _write_todo_lines(profile: Profile, lines: list[str]) -> None:
    """Rewrite the todo file through the pipeline's locked, backed-up writer."""

    with _lock(profile):
        _write_todo(profile, lines)


def _visible_completion(body: str, mmdd: str) -> str:
    current = VISIBLE_COMPLETION_RE.match(body)
    if current:
        status = current.group("status") or ""
        return status + f"[{mmdd}] " + body[current.end() :]
    status = STATUS_PREFIX_RE.match(body)
    offset = status.end() if status else 0
    return body[:offset] + f"[{mmdd}] " + body[offset:]


def _remove_visible_completion(body: str, mmdd: str) -> str:
    current = VISIBLE_COMPLETION_RE.match(body)
    if not current or current.group("stamp") != mmdd:
        return body
    return (current.group("status") or "") + body[current.end() :]


def stamp_completed_tasks(profile: Profile, now: datetime | None = None) -> dict[str, int]:
    """Persist a completion date while keeping the visible task compact."""

    lines = ensure_task_ids(profile)
    today = (now or datetime.now().astimezone()).date().isoformat()
    stamped = retained = reopened = 0
    output: list[str] = []

    for line in lines:
        task = TASK_RE.match(line)
        if not task or not task.group("task_id"):
            output.append(line)
            continue
        body = task.group("body")
        marker = COMPLETION_MARKER_RE.search(body)
        checked = task.group("checked").lower() == "x"

        if checked:
            completed_on = marker.group("date") if marker else today
            clean_body = COMPLETION_MARKER_RE.sub("", body).rstrip()
            clean_body = _visible_completion(clean_body, completed_on[5:7] + completed_on[8:10])
            body = clean_body + " " + COMPLETION_MARKER.format(date=completed_on)
            stamped += int(marker is None)
            retained += int(marker is not None)
        elif marker:
            completed_on = marker.group("date")
            body = COMPLETION_MARKER_RE.sub("", body).rstrip()
            body = _remove_visible_completion(body, completed_on[5:7] + completed_on[8:10])
            reopened += 1

        output.append(
            task.group("indent")
            + task.group("prefix")
            + f"[{int(task.group('task_id')):02d}] "
            + body
        )

    if output != lines:
        _write_todo_lines(profile, output)
    return {"stamped": stamped, "retained": retained, "reopened": reopened}


def reconcile_todos(profile: Profile) -> dict[str, int]:
    """Create exactly one durable task per inbox event across retries/deletion."""

    ensure_task_ids(profile)
    state_file = profile.state_dir / "todo-state.json"
    state = _load_json(state_file, {"assigned": []})
    assigned = list(state.get("assigned", []))
    assigned_set = set(assigned)
    todo_text = profile.todo_file.read_text(encoding="utf-8")
    created = recovered = 0

    source_name, _latest_title, _source_title = _source_labels(profile)
    for fingerprint, event in inbox_rows(profile):
        if fingerprint in assigned_set:
            continue
        marker = EVENT_MARKER.format(fingerprint=fingerprint)
        if marker in todo_text:
            assigned.append(fingerprint)
            assigned_set.add(fingerprint)
            _write_private(state_file, {"assigned": assigned})
            recovered += 1
            continue
        todo_add(
            profile,
            f"{_task_summary(event, source_name)} {marker}",
            status=profile.new_task_status,
        )
        todo_text = profile.todo_file.read_text(encoding="utf-8")
        assigned.append(fingerprint)
        assigned_set.add(fingerprint)
        _write_private(state_file, {"assigned": assigned})
        created += 1

    completion = stamp_completed_tasks(profile)
    return {
        "created": created,
        "recovered": recovered,
        "known": len(assigned),
        **completion,
    }


def _event_latest_time(event: dict[str, Any]) -> str:
    times = [str(m.get("time", "")) for m in event.get("messages", [])]
    times += [str(a.get("time", "")) for a in event.get("attachments", [])]
    return max((t for t in times if t), default="")


def _task_archive_status(profile: Profile, event: dict[str, Any]) -> str:
    """Whether the reply's substance made it into the case system.

    No integration with an external case repo exists here, and a wrong "yes" is far
    worse than an honest "unknown" -- a guessed heuristic could launder a real
    hand-off miss into a label that reads as resolved. Always "unknown" until this
    profile is given a concrete, verifiable archive check to call.
    """
    del profile, event
    return "unknown"


def mark_replied_tasks(profile: Profile) -> dict[str, int]:
    """Flag pending tasks that already got an outgoing reply, without closing them.

    Replying is not resolving: see PIPELINE.md on why an auto-close here would be
    wrong (an acknowledgement can leave the underlying ask unresolved). The badge
    carries two independent dimensions -- replied-at and archive status -- because
    conflating them is exactly what let a real hand-off go unnoticed while every
    task looked reassuringly "handled".
    """
    lines = ensure_task_ids(profile)
    events = dict(inbox_rows(profile))
    outgoing_times = sorted(
        {
            str(message.get("time", ""))
            for _fingerprint, event in outbox_rows(profile)
            for message in event.get("messages", [])
            if str(message.get("time", "")).strip()
        }
    )
    marked = 0
    changed = False
    output: list[str] = []
    for line in lines:
        task = TASK_RE.match(line)
        if not task or not task.group("task_id") or task.group("checked").lower() == "x":
            output.append(line)
            continue
        body = task.group("body")
        if REPLIED_MARKER_RE.search(body):
            output.append(line)
            continue
        fingerprint_match = EVENT_FINGERPRINT_RE.search(body)
        event = events.get(fingerprint_match.group("fingerprint")) if fingerprint_match else None
        task_time = _event_latest_time(event) if event else ""
        reply_time = next((t for t in outgoing_times if t > task_time), None) if task_time else None
        if not reply_time:
            output.append(line)
            continue
        archive_status = _task_archive_status(profile, event)
        badge = f"[已回覆 {_hhmm(reply_time)}・{ARCHIVE_STATUS_LABELS[archive_status]}] "
        status_prefix = STATUS_PREFIX_RE.match(body)
        offset = status_prefix.end() if status_prefix else 0
        new_body = (
            body[:offset]
            + badge
            + body[offset:]
            + " "
            + REPLIED_MARKER.format(timestamp=reply_time, status=archive_status)
        )
        output.append(
            task.group("indent")
            + task.group("prefix")
            + f"[{int(task.group('task_id')):02d}] "
            + new_body
        )
        marked += 1
        changed = True
    if changed:
        _write_todo_lines(profile, output)
    return {"marked": marked}


def _hhmm(value: object) -> str:
    if not value:
        return "--:--"
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M")
    except ValueError:
        parts = str(value).split()
        return parts[-1][:5] if parts else "--:--"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _fit(value: object, width: int) -> str:
    result: list[str] = []
    used = 0
    for char in str(value).replace("\n", " "):
        char_width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + char_width > width:
            if width >= 2 and result:
                while result and used + 1 > width:
                    removed = result.pop()
                    used -= 2 if unicodedata.east_asian_width(removed) in "WF" else 1
                result.append("…")
                used += 1
            break
        result.append(char)
        used += char_width
    return "".join(result) + " " * max(0, width - used)


def _wrap(value: object, width: int, continuation: str = "    ") -> list[str]:
    """Wrap plain display text without losing wide characters or long identifiers."""

    text = str(value).replace("\n", " ")
    if not text:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if current and used + char_width > width:
            lines.append("".join(current).rstrip())
            current = list(continuation)
            used = _width(continuation)
        current.append(char)
        used += char_width
    lines.append("".join(current).rstrip())
    return lines


def _center_fit(value: object, width: int) -> str:
    fitted = _fit(value, width).rstrip()
    content_width = _width(fitted)
    left = max(0, (width - content_width) // 2)
    return " " * left + fitted + " " * max(0, width - left - content_width)


def _badge_text(caption: str) -> str:
    inside = 6 - _width(caption)
    left = inside // 2
    return " " * left + caption + " " * (inside - left)


def dashboard_snapshot(
    profile: Profile,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], list[str], dict[str, Any]]:
    pending: list[tuple[int, str]] = []
    completed: list[tuple[int, str]] = []
    for line in ensure_task_ids(profile):
        task = TASK_RE.match(line)
        if not task or not task.group("task_id"):
            continue
        body = task.group("indent") + _clean_task_body(task.group("body"))
        row = (int(task.group("task_id")), body)
        (completed if task.group("checked").lower() == "x" else pending).append(row)

    rows: list[tuple[str, int, str]] = []
    event_index = 0
    for _fingerprint, event in inbox_rows(profile):
        for message in event.get("messages", []):
            body = " ".join(str(message.get("text", "")).split())
            if message.get("type") != "text" or not body:
                continue
            event_index += 1
            timestamp = str(message.get("time", ""))
            sender = " ".join(str(message.get("from", "")).split())
            rows.append((timestamp, event_index, f"{_hhmm(timestamp)} {sender}: {body}"))
        for item in event.get("attachments", []):
            event_index += 1
            timestamp = str(item.get("time", ""))
            sender = " ".join(str(item.get("from", "")).split())
            name = item.get("filename") or item.get("kind") or "附件"
            rows.append((timestamp, event_index, f"{_hhmm(timestamp)} {sender}: [附件] {name}"))
    for _fingerprint, event in outbox_rows(profile):
        for message in event.get("messages", []):
            body = " ".join(str(message.get("text", "")).split())
            if not body:
                continue
            event_index += 1
            timestamp = str(message.get("time", ""))
            rows.append((timestamp, event_index, f"{_hhmm(timestamp)} {SELF_LABEL}: {body}"))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)

    events: list[str] = []
    previous_minute = None
    for timestamp, _index, rendered in rows[:10]:
        minute = _hhmm(timestamp)
        body = rendered[6:] if rendered.startswith(minute + " ") else rendered
        events.append(f"{minute} {body}" if minute != previous_minute else f"      {body}")
        previous_minute = minute
    return pending, completed, events, _load_json(profile.state_file, {})


def pane_progress(profile: Profile) -> list[str]:
    """Return the latest milestone per pane as a compact parent/child tree."""

    latest: dict[str, tuple[str, int, dict[str, Any]]] = {}
    event_index = 0
    for _fingerprint, event in inbox_rows(profile):
        for message in event.get("messages", []):
            sender = " ".join(str(message.get("from", "")).split())
            if not sender or message.get("type", "text") != "text":
                continue
            event_index += 1
            candidate = (str(message.get("time", "")), event_index, message)
            if sender not in latest or candidate[:2] > latest[sender][:2]:
                latest[sender] = candidate
    if not latest:
        return []

    root = profile.contact
    children: dict[str, list[str]] = {}
    for sender, (_timestamp, _index, message) in latest.items():
        if sender == root:
            continue
        parent = " ".join(str(message.get("parent") or root).split()) or root
        children.setdefault(parent, []).append(sender)
    for senders in children.values():
        senders.sort(key=lambda sender: latest[sender][:2], reverse=True)

    def summary(sender: str) -> str:
        _timestamp, _index, message = latest[sender]
        state = str(message.get("state", "")).lower()
        symbol = {"running": "●", "blocked": "✕", "done": "✓", "waiting": "○"}.get(state, "·")
        progress = " ".join(str(message.get("progress") or message.get("text", "")).split())
        return f"{symbol} {sender} {progress}".rstrip()

    rows: list[str] = []
    if root in latest or root in children:
        rows.append(summary(root) if root in latest else root)

    def visit(parent: str, prefix: str = "") -> None:
        direct = children.get(parent, [])
        for index, sender in enumerate(direct):
            last = index == len(direct) - 1
            rows.append(prefix + ("└─ " if last else "├─ ") + summary(sender))
            visit(sender, prefix + ("   " if last else "│  "))

    visit(root)
    known = {root}
    known.update(sender for senders in children.values() for sender in senders)
    for sender in sorted(set(latest) - known):
        rows.append(summary(sender))
    return rows[:10]


def latest_case_status(profile: Profile) -> str:
    """Return the latest durable event status, preferring its compact progress."""

    latest: tuple[str, int, dict[str, Any]] | None = None
    event_index = 0
    for _fingerprint, event in inbox_rows(profile):
        for message in event.get("messages", []):
            if message.get("type", "text") != "text":
                continue
            event_index += 1
            candidate = (str(message.get("time", "")), event_index, message)
            if latest is None or candidate[:2] > latest[:2]:
                latest = candidate
    if latest is None:
        return "尚無 durable 狀態"

    message = latest[2]
    state = str(message.get("state", "")).lower()
    symbol = {"running": "●", "blocked": "✕", "done": "✓", "waiting": "○"}.get(state, "·")
    sender = _one_line(message.get("from"), 32)
    progress = _one_line(message.get("progress") or message.get("text"), 180)
    return f"{symbol} {sender} {progress}".strip()


def _task_views(
    pending: list[tuple[int, str]], completed: list[tuple[int, str]]
) -> dict[str, list[tuple[int, str]]]:
    review = [row for row in pending if any(label in row[1] for label in USER_REVIEW_LABELS)]
    running = [
        row for row in pending
        if row not in review and ("[進行]" in row[1] or "[執行]" in row[1])
    ]
    queued = [row for row in pending if row not in running and row not in review]
    return {"running": running, "pending": queued, "review": review, "completed": completed}


def _user_review_status(pending: list[tuple[int, str]]) -> tuple[int, str] | None:
    """Summarize explicit durable user-review tasks without inferring from prose."""

    review = [row for row in pending if any(label in row[1] for label in USER_REVIEW_LABELS)]
    if not review:
        return None
    task_ids = ", ".join(f"#{task_id:02d}" for task_id, _body in review[:6])
    if len(review) > 6:
        task_ids += f", +{len(review) - 6}"
    return len(review), task_ids


def _review_display_rows(task_id: int, body: str, width: int) -> list[str]:
    """Put a review badge on its own row so the decision text stays visible."""

    stripped = body.strip()
    label = next((item for item in USER_REVIEW_LABELS if stripped.startswith(item)), None)
    if label is None:
        return _wrap(f"{task_id:02d}. {body}", width)
    detail = stripped[len(label) :].strip()
    rows = [f"{task_id:02d}. {label}"]
    if detail:
        rows.extend(_wrap("    " + detail, width))
    return rows


def space_dashboard_snapshot(profiles: list[Profile]) -> list[dict[str, Any]]:
    """Task-only projection for a pilot supervising several customer lanes."""

    lanes: list[dict[str, Any]] = []
    for profile in profiles:
        pending: list[tuple[int, str]] = []
        completed: list[tuple[int, str]] = []
        for line in ensure_task_ids(profile):
            task = TASK_RE.match(line)
            if not task or not task.group("task_id"):
                continue
            body = task.group("indent") + _clean_task_body(task.group("body"))
            row = (int(task.group("task_id")), body)
            (completed if task.group("checked").lower() == "x" else pending).append(row)
        lanes.append(
            {
                "name": profile.name,
                "label": profile.project_label,
                "pending": pending,
                "completed": completed,
                "state": _load_json(profile.state_file, {}),
            }
        )
    return lanes


def _task_sort_key(lane_name: str, task_id: int, body: str) -> tuple[int, str, str, int]:
    """Sort LINE work oldest-first, with undated/manual work after it."""

    timestamp = re.search(
        r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)\b", body
    )
    if timestamp:
        return (0, timestamp.group(1), lane_name, task_id)
    return (1, "", lane_name, task_id)


def space_queue_snapshot(
    profiles: list[Profile],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flatten customer lanes into the pilot's scalable task queue."""

    lanes = space_dashboard_snapshot(profiles)
    queue: list[dict[str, Any]] = []
    for lane in lanes:
        for task_id, body in lane["pending"]:
            queue.append(
                {
                    "name": lane["name"],
                    "label": lane["label"],
                    "task_id": task_id,
                    "body": body,
                }
            )
    queue.sort(key=lambda row: _task_sort_key(row["name"], row["task_id"], row["body"]))
    return lanes, queue


def space_conversation_snapshot(profile: Profile) -> list[str]:
    """Show only LINE rows referenced by this customer's unfinished tasks."""

    task_ids_by_fingerprint: dict[str, set[int]] = {}
    for line in ensure_task_ids(profile):
        task = TASK_RE.match(line)
        if not task or not task.group("task_id") or task.group("checked").lower() == "x":
            continue
        task_id = int(task.group("task_id"))
        for fingerprint in EVENT_FINGERPRINT_RE.findall(task.group("body")):
            task_ids_by_fingerprint.setdefault(fingerprint, set()).add(task_id)

    rows: list[tuple[str, int, str]] = []
    event_index = 0
    for fingerprint, event in inbox_rows(profile):
        task_ids = task_ids_by_fingerprint.get(fingerprint)
        if not task_ids:
            continue
        task_ref = "/".join(f"#{task_id:02d}" for task_id in sorted(task_ids))
        for message in event.get("messages", []):
            body = " ".join(str(message.get("text", "")).split())
            if message.get("type") != "text" or not body:
                continue
            event_index += 1
            timestamp = str(message.get("time", ""))
            sender = " ".join(str(message.get("from", "")).split())
            rows.append((timestamp, event_index, f"{task_ref} {_hhmm(timestamp)} {sender}: {body}"))
        for item in event.get("attachments", []):
            event_index += 1
            timestamp = str(item.get("time", ""))
            sender = " ".join(str(item.get("from", "")).split())
            name = item.get("filename") or item.get("kind") or "附件"
            rows.append(
                (
                    timestamp,
                    event_index,
                    f"{task_ref} {_hhmm(timestamp)} {sender}: [附件] {name}",
                )
            )
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [rendered for _timestamp, _index, rendered in rows]


def _space_render(
    profiles: list[Profile], title: str, offset: int, lane_index: int
) -> tuple[int, int, tuple[int, int, int, int, int]]:
    size = shutil.get_terminal_size((115, 12))
    columns = max(80, size.columns)
    rows_available = max(1, size.lines - 2)
    gap = 3
    left_width = max(42, min(columns - gap - 32, round(columns * 0.62)))
    right_width = max(32, columns - left_width - gap)
    lanes, queue = space_queue_snapshot(profiles)
    offset = min(max(0, offset), max(0, len(queue) - rows_available))
    lane_index %= len(profiles)
    selected_profile = profiles[lane_index]
    selected_lane = lanes[lane_index]
    conversations = space_conversation_snapshot(selected_profile)
    visible = queue[offset : offset + rows_available]
    lines: list[str] = []

    total_completed = sum(len(lane["completed"]) for lane in lanes)
    position = (
        ""
        if not queue
        else f" · {offset + 1}–{min(len(queue), offset + rows_available)}/{len(queue)}"
    )
    left_title = f"{title} · 待分流 {len(queue)}"
    lane_source_name, _latest_title, _source_title = _source_labels(selected_profile)
    right_title = f"{selected_lane['label']} · 待辦 {lane_source_name}"
    lines.append(
        BOLD
        + "\033[38;5;109m"
        + _center_fit(left_title, left_width)
        + RESET
        + DIM
        + " │ "
        + RESET
        + BOLD
        + "\033[38;5;180m"
        + _center_fit(right_title, right_width)
        + RESET
    )

    longest_label = max((_width(str(lane["label"])) for lane in lanes), default=8)
    label_width = max(10, min(longest_label, max(10, left_width // 4)))
    for row_index in range(rows_available):
        if row_index < len(visible):
            row = visible[row_index]
            left_cell = (
                BOLD
                + _fit(str(row["label"]), label_width)
                + RESET
                + f" #{row['task_id']:02d} "
                + _fit(str(row["body"]), max(1, left_width - label_width - 5))
            )
        elif not queue and row_index == 0:
            left_cell = DIM + _center_fit("目前沒有待分流任務。", left_width) + RESET
        else:
            left_cell = " " * left_width

        if row_index < len(conversations):
            right_cell = _fit(conversations[row_index], right_width)
            if "[附件]" in right_cell:
                right_cell = right_cell.replace("[附件]", "\033[33m[附件]" + RESET, 1)
        elif not conversations and row_index == 0:
            right_cell = DIM + _center_fit("此案目前沒有待辦相關訊息。", right_width) + RESET
        else:
            right_cell = " " * right_width
        lines.append(left_cell + DIM + " │ " + RESET + right_cell)

    sync_values = [
        str(lane["state"].get("last_checked_at"))
        for lane in lanes
        if lane["state"].get("last_checked_at")
    ]
    oldest_sync = _hhmm(min(sync_values)) if sync_values else "--:--"
    left_footer = (
        f"客戶 {len(lanes)} · 待分流 {len(queue)} · 完成 {total_completed} · "
        f"最舊同步 {oldest_sync}{position} · ↑↓ / j k"
    )
    switch_count = f"{lane_index + 1}/{len(lanes)}"
    pending_count = f"({len(selected_lane['pending'])})"
    left_button, right_button = " ◀ ", " ▶ "
    count_width = max(5, _width(switch_count) + 2)
    label_width = max(4, right_width - _width(left_button + right_button) - count_width)
    switch_label = f"{selected_lane['label']} {pending_count}"
    right_footer_plain = (
        left_button
        + _center_fit(switch_count, count_width)
        + _center_fit(switch_label, label_width)
        + right_button
    )
    button_style = RESET + BOLD + "\033[38;5;159;48;5;24m"
    right_footer_cell = right_footer_plain.replace(
        left_button, button_style + left_button + RESET + DIM, 1
    ).replace(right_button, button_style + right_button + RESET + DIM, 1)
    lines.append(DIM + _center_fit(left_footer, left_width) + " │ " + right_footer_cell + RESET)
    sys.stdout.write("\033[2J\033[H" + "\n".join(lines))
    sys.stdout.flush()
    right_start_x = left_width + 4
    left_button_start = right_start_x
    left_button_end = left_button_start + _width(left_button) - 1
    right_button_start = right_start_x + right_width - _width(right_button)
    right_button_end = right_button_start + _width(right_button) - 1
    return (
        offset,
        lane_index,
        (
            len(lines),
            left_button_start,
            left_button_end,
            right_button_start,
            right_button_end,
        ),
    )


def _space_handle_input(
    data: str,
    offset: int,
    lane_index: int,
    lane_count: int,
    mouse_targets: tuple[int, int, int, int, int] | None = None,
) -> tuple[int, int]:
    if "\x1b[A" in data or ("k" in data.lower() and "\x1b" not in data):
        offset = max(0, offset - 1)
    if "\x1b[B" in data or ("j" in data.lower() and "\x1b" not in data):
        offset += 1
    if "\x1b[D" in data or ("h" in data.lower() and "\x1b" not in data):
        lane_index = (lane_index - 1) % lane_count
    if "\x1b[C" in data or ("l" in data.lower() and "\x1b" not in data):
        lane_index = (lane_index + 1) % lane_count
    for button, x, y, action in MOUSE_RE.findall(data):
        x, y = int(x), int(y)
        if int(button) == 64:
            offset = max(0, offset - 1)
        elif int(button) == 65:
            offset += 1
        elif action == "M" and int(button) == 0 and mouse_targets:
            switch_row, left_start, left_end, right_start, right_end = mouse_targets
            if y == switch_row and left_start <= x <= left_end:
                lane_index = (lane_index - 1) % lane_count
            elif y == switch_row and right_start <= x <= right_end:
                lane_index = (lane_index + 1) % lane_count
    return offset, lane_index


def space_dashboard(profiles: list[Profile], watch_mode: bool, interval: float, title: str) -> None:
    lanes = space_dashboard_snapshot(profiles)
    lane_index = next((index for index, lane in enumerate(lanes) if lane["pending"]), 0)
    offset, previous, mouse_targets = 0, None, None
    stdin_fd = sys.stdin.fileno()
    old_termios = termios.tcgetattr(stdin_fd) if sys.stdin.isatty() else None
    try:
        if old_termios:
            tty.setcbreak(stdin_fd)
            sys.stdout.write("\033[?1049h\033[?1000h\033[?1006h")
        while True:
            lane_index %= len(profiles)
            signature = repr(
                (
                    space_dashboard_snapshot(profiles),
                    space_conversation_snapshot(profiles[lane_index]),
                    offset,
                    lane_index,
                    shutil.get_terminal_size(),
                )
            )
            if signature != previous:
                offset, lane_index, mouse_targets = _space_render(
                    profiles, title, offset, lane_index
                )
                previous = signature
            if not watch_mode:
                sys.stdout.write("\n")
                return
            ready, _write, _errors = select.select(
                [stdin_fd], [], [], min(max(0.25, interval), 1.0)
            )
            if ready:
                data = os.read(stdin_fd, 256).decode("utf-8", "ignore")
                offset, lane_index = _space_handle_input(
                    data, offset, lane_index, len(profiles), mouse_targets
                )
                previous = None
    finally:
        if old_termios:
            sys.stdout.write("\033[?1006l\033[?1000l\033[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_termios)


def _completed_item(item: str, repeat: int = 1) -> str:
    item = re.sub(r"^\[[^\]]+\]\s*", "", item)
    completion = re.match(r"^\[(?P<stamp>\d{4})\]\s*", item)
    # The repeat badge sits right after the completion bracket, not appended to
    # the tail: a long alert body already gets "…"-truncated by `_fit` at the
    # right edge, and a count nobody can see defeats the point of counting.
    suffix = f" ×{repeat}" if repeat > 1 else ""
    if not completion:
        return "[完成]" + suffix + " " + item
    return f"[{completion.group('stamp')}完成]" + suffix + " " + item[completion.end() :]


_LEADING_TAG_RE = re.compile(r"^\[[^\]]*\]\s*")
_EVENT_HEADER_RE = re.compile(r"^\S+ \d{2}-\d{2} \d{2}:\d{2} [^:]+:\s*")
_ALERT_STATE_RE = re.compile(r"^(新告警|告警已消失)：")
REPEAT_MARK_RE = re.compile(r" ×\d+$")


def _flap_signature(body: str) -> str | None:
    """The alert identity a pending/completed row carries, ignoring state/timestamp.

    Only messages that look like a `dgfleet`/fleet-alert appear/clear pair carry
    a signature -- everything else returns None so it never gets folded into a
    run. A wrong merge here would hide a distinct event under someone else's
    count, which is worse than never collapsing at all. Leading `[tag]` markers
    (status tag, and for completed rows a `[MMDD]` stamp on top of that) are
    stripped first since both pending and completed bodies carry them.
    """

    rest = body
    for _ in range(2):
        stripped = _LEADING_TAG_RE.sub("", rest, count=1)
        if stripped == rest:
            break
        rest = stripped
    rest = _EVENT_HEADER_RE.sub("", rest, count=1)
    state = _ALERT_STATE_RE.match(rest)
    if not state:
        return None
    return rest[state.end() :].strip()


def _collapse_flapping_rows(rows: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Fold a consecutive run of the same flapping alert into its latest row.

    The durable task log keeps one entry per occurrence -- that's the record
    the pipeline promises never to lose (`reconcile_todos`'s "exactly one
    durable task per inbox event"). This only changes what the dashboard
    *displays*: a chronic alert that fires and clears N times back-to-back
    shows as one line carrying the latest state plus " ×N", instead of N
    lines pushing everything else off the visible window. A gap of unrelated
    events in between breaks the run on purpose, so an alert recurring after
    other things happened still reads as a fresh occurrence, not a silent
    increment on an old count.
    """

    collapsed: list[tuple[int, str]] = []
    index = 0
    total = len(rows)
    while index < total:
        task_id, body = rows[index]
        signature = _flap_signature(body)
        if signature is None:
            collapsed.append((task_id, body))
            index += 1
            continue
        run_end = index + 1
        while run_end < total and _flap_signature(rows[run_end][1]) == signature:
            run_end += 1
        run_length = run_end - index
        latest_id, latest_body = rows[run_end - 1]
        if run_length > 1:
            latest_body = f"{latest_body} ×{run_length}"
        collapsed.append((latest_id, latest_body))
        index = run_end
    return collapsed


def _place_repeat_badge(body: str) -> str:
    """Move a trailing " ×N" repeat marker right after the body's leading tag.

    Keeps it clear of `_fit`'s right-edge "…" truncation, the same reason
    `_completed_item` places its own repeat suffix right after the completion
    bracket instead of at the tail.
    """

    match = REPEAT_MARK_RE.search(body)
    if not match:
        return body
    clean = REPEAT_MARK_RE.sub("", body)
    tag = _LEADING_TAG_RE.match(clean)
    marker = match.group(0).strip()
    if tag:
        return clean[: tag.end()] + marker + " " + clean[tag.end() :]
    return marker + " " + clean


def _render(profile: Profile, active_tab: str, offset: int, blink_on: bool) -> int:
    size = shutil.get_terminal_size((115, 12))
    columns = max(80, size.columns)
    rows_available = max(1, min(10, size.lines - 1))
    gap = 3
    left_width = max(40, min(columns - gap - 34, round(columns * 0.62)))
    right_width = max(34, columns - left_width - gap)
    pending, completed, events, state = dashboard_snapshot(profile)
    _source_name, latest_title, source_title = _source_labels(profile)
    views = _task_views(pending, completed)
    if active_tab == "pending":
        selected = views["running"] + _collapse_flapping_rows(views["pending"])
    elif active_tab == "review":
        selected = views["review"]
    else:
        selected = _collapse_flapping_rows(completed)
    review_status = _user_review_status(pending)
    latest_status = latest_case_status(profile)
    if profile.source == "jsonl":
        events = pane_progress(profile)
        latest_title = "Pane 進度"
    content_rows = max(0, rows_available - 1)
    display_rows: list[str] = []
    for task_id, body in selected:
        if active_tab == "review":
            display_rows.extend(_review_display_rows(task_id, body, left_width))
        elif active_tab == "completed":
            repeat_mark = REPEAT_MARK_RE.search(body)
            repeat = int(repeat_mark.group(0).strip(" ×")) if repeat_mark else 1
            clean_body = REPEAT_MARK_RE.sub("", body) if repeat_mark else body
            display_rows.append(f"{task_id:02d}. {_completed_item(clean_body, repeat)}")
        else:
            display_rows.append(f"{task_id:02d}. {_place_repeat_badge(body)}")
    offset = min(max(0, offset), max(0, len(display_rows) - content_rows))
    visible = display_rows[offset : offset + content_rows]
    lines: list[str] = []

    if review_status:
        review_count, task_ids = review_status
        caption = f" 核准狀態｜待核准 {review_count} 項 · {task_ids} │ {latest_status} "
        color = USER_REVIEW_COLOR
    else:
        caption = f" 核准狀態｜無待核准事項 │ {latest_status} "
        color = USER_CLEAR_COLOR
    lines.append(color + _fit(caption, columns) + RESET)

    for index in range(content_rows):
        left = visible[index] if index < len(visible) else ""
        if index == 0:
            title = latest_title
            first = events[0] if events else ""
            event_width = max(1, right_width - _width(title) - 1)
            event_cell = _fit(first, event_width).rstrip()
            right = (
                event_cell + " " * max(1, right_width - _width(event_cell) - _width(title)) + title
            )
        else:
            right = events[index] if index < len(events) else ""

        badge_render = None
        badges = list(STATUS_BADGES.items())
        completion_badge = COMPLETED_BADGE_RE.search(left)
        if completion_badge:
            stamp = completion_badge.group("stamp")
            badges.insert(
                0,
                (completion_badge.group(0), (f"{stamp}完成", STATUS_BADGES["[完成]"][1])),
            )
        for label, (caption, color) in badges:
            if label in left:
                if color == "ACTIVE":
                    color = "\033[1;38;5;24;48;5;153m" if blink_on else "\033[1;38;5;153;48;5;24m"
                badge = _badge_text(caption)
                left = left.replace(label, badge, 1)
                badge_render = (badge, color)
                break
        left_cell = _fit(left, left_width)
        if badge_render:
            badge, color = badge_render
            left_cell = left_cell.replace(badge, color + badge + RESET, 1)
        repeat_badge = re.search(r" ×\d+", left_cell)
        if repeat_badge:
            left_cell = left_cell.replace(
                repeat_badge.group(0), "\033[1;38;5;208m" + repeat_badge.group(0) + RESET, 1
            )
        right_cell = _fit(right, right_width)
        if "[附件]" in right_cell:
            right_cell = right_cell.replace("[附件]", "\033[33m[附件]" + RESET, 1)
        if index == 0:
            right_cell = right_cell.replace(
                latest_title,
                BOLD + "\033[38;5;180m" + latest_title + RESET,
                1,
            )
        lines.append(left_cell + DIM + " │ " + RESET + right_cell)

    pending_count = len(views["running"]) + len(views["pending"])
    pending_tab = f" 待辦 {pending_count} "
    review_tab = f" 待核准 {len(views['review'])} "
    completed_tab = f" 完成 {len(completed)} "
    toggle_tab, toggle_color = _injection_badge(profile, blink_on)
    controls = (
        pending_tab + "  " + review_tab + "  " + completed_tab + "  " + toggle_tab
        + "  點選｜← →｜i 注入｜滾輪"
    )
    sync = f"更新 {datetime.now().astimezone():%H:%M} 同步 {_hhmm(state.get('last_checked_at'))}"
    title = source_title
    lines.append(
        ("\033[1;30;46m" if active_tab == "pending" else "\033[2;37m")
        + pending_tab
        + RESET
        + "  "
        + ("\033[1;30;43m" if active_tab == "review" else "\033[2;37m")
        + review_tab
        + RESET
        + "  "
        + ("\033[1;30;46m" if active_tab == "completed" else "\033[2;37m")
        + completed_tab
        + RESET
        + "  "
        + toggle_color
        + toggle_tab
        + RESET
        + DIM
        + "  點選｜← →｜i 注入｜滾輪"
        + RESET
        + " " * max(0, left_width - _width(controls))
        + DIM
        + " │ "
        + RESET
        + BOLD
        + "\033[38;5;109m"
        + title
        + RESET
        + " " * max(1, right_width - _width(title) - _width(sync) - 2)
        + DIM
        + sync
        + RESET
        + "  "
    )
    sys.stdout.write("\033[2J\033[H" + "\n".join(lines))
    sys.stdout.flush()
    return offset


TOGGLE_TEXT = {True: " 注入 ON ", False: " 注入 OFF ", None: " 注入 — "}


def _injection_badge(profile: Profile, blink_on: bool) -> tuple[str, str]:
    """Bottom-bar badge for the runtime injection switch.  None = not configured."""
    if not profile.injection_configured:
        return TOGGLE_TEXT[None], "\033[2;37m"
    if injection_active(profile):
        return TOGGLE_TEXT[True], "\033[1;30;42m" if blink_on else "\033[1;30;40;32m"
    return TOGGLE_TEXT[False], "\033[1;37;41m"


def toggle_injection(profile: Profile) -> dict[str, Any] | None:
    """Flip the switch from the dashboard.  No-op when the profile has no injection."""
    if not profile.injection_configured:
        return None
    return set_injection_switch(profile, not injection_active(profile))


def _handle_input(
    data: str, active_tab: str, offset: int, bottom_row: int, profile: Profile | None = None
) -> tuple[str, int]:
    if "\x1b[D" in data:
        active_tab, offset = TASK_TABS[(TASK_TABS.index(active_tab) - 1) % len(TASK_TABS)], 0
    if "\x1b[C" in data or "\t" in data:
        active_tab, offset = TASK_TABS[(TASK_TABS.index(active_tab) + 1) % len(TASK_TABS)], 0
    if profile is not None and ("i" in data or "I" in data) and "\x1b" not in data:
        toggle_injection(profile)
    for button, x, y, action in MOUSE_RE.findall(data):
        button, x, y = int(button), int(x), int(y)
        if action == "M" and button == 0 and y == 1:
            active_tab, offset = "review", 0
        elif action == "M" and button == 0 and y == bottom_row:
            pending_end = _width(" 待辦 99 ")
            review_end = pending_end + 2 + _width(" 待核准 99 ")
            completed_end = review_end + 2 + _width(" 完成 99 ")
            toggle_end = completed_end + 2 + _width(TOGGLE_TEXT[False])
            if x <= pending_end:
                active_tab, offset = "pending", 0
            elif pending_end + 2 < x <= review_end:
                active_tab, offset = "review", 0
            elif review_end + 2 < x <= completed_end:
                active_tab, offset = "completed", 0
            elif completed_end + 2 < x <= toggle_end and profile is not None:
                toggle_injection(profile)
        elif button == 64:
            offset = max(0, offset - 1)
        elif button == 65:
            offset += 1
    return active_tab, offset


def dashboard(profile: Profile, watch_mode: bool, interval: float) -> None:
    active_tab, offset, previous = "pending", 0, None
    stdin_fd = sys.stdin.fileno()
    old_termios = termios.tcgetattr(stdin_fd) if sys.stdin.isatty() else None
    try:
        if old_termios:
            tty.setcbreak(stdin_fd)
            sys.stdout.write("\033[?1049h\033[?1000h\033[?1006h")
        while True:
            blink_on = bool(int(time.monotonic() * 1.6) % 2)
            signature = repr(
                (
                    dashboard_snapshot(profile),
                    active_tab,
                    offset,
                    shutil.get_terminal_size(),
                    blink_on,
                    injection_switch(profile),
                )
            )
            if signature != previous:
                offset = _render(profile, active_tab, offset, blink_on)
                previous = signature
            if not watch_mode:
                sys.stdout.write("\n")
                return
            ready, _write, _errors = select.select(
                [stdin_fd], [], [], min(max(0.25, interval), 1.0)
            )
            if ready:
                data = os.read(stdin_fd, 256).decode("utf-8", "ignore")
                bottom_row = min(10, shutil.get_terminal_size().lines - 1) + 1
                active_tab, offset = _handle_input(data, active_tab, offset, bottom_row, profile)
                previous = None
    finally:
        if old_termios:
            sys.stdout.write("\033[?1006l\033[?1000l\033[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_termios)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="line-local-case-cockpit")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("profile")
    run_parser.add_argument("--bootstrap", action="store_true")
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("profile")
    dashboard_parser = commands.add_parser("dashboard")
    dashboard_parser.add_argument("profile")
    dashboard_parser.add_argument("--watch", action="store_true")
    dashboard_parser.add_argument("--interval", type=float, default=2.0)
    injection_parser = commands.add_parser("injection", help="runtime switch: on|off|status")
    injection_parser.add_argument("profile")
    injection_parser.add_argument("action", choices=["on", "off", "status"])
    space_parser = commands.add_parser("space-dashboard", help="task-only multi-profile pilot view")
    space_parser.add_argument("profiles", nargs="+")
    space_parser.add_argument("--watch", action="store_true")
    space_parser.add_argument("--interval", type=float, default=2.0)
    space_parser.add_argument("--title", default="SERVICE · PILOT")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "space-dashboard":
            profiles = [load_profile(args.config, name) for name in args.profiles]
            space_dashboard(profiles, args.watch, args.interval, args.title)
            return 0
        profile = load_profile(args.config, args.profile)
        if args.command == "dashboard":
            dashboard(profile, args.watch, args.interval)
            return 0
        if args.command == "injection":
            if args.action == "status":
                status = {
                    "configured": profile.injection_configured,
                    "switch": injection_switch(profile),
                    "active": injection_active(profile),
                }
            else:
                status = set_injection_switch(profile, args.action == "on")
            print(json.dumps(status, ensure_ascii=False, sort_keys=True))
            return 0
        with _cockpit_lock(profile.state_dir):
            if args.command == "reconcile":
                result: dict[str, Any] = {"todo": reconcile_todos(profile)}
                result["replied"] = mark_replied_tasks(profile)
            else:
                result = {"watch": asyncio.run(watch(profile, bootstrap=args.bootstrap))}
                if args.bootstrap:
                    ensure_task_ids(profile)
                    result["todo"] = {"created": 0, "recovered": 0}
                    result["injection"] = {"sent": 0, "failed": 0, "disabled": 1}
                else:
                    result["todo"] = reconcile_todos(profile)
                    result["injection"] = inject_pending(profile)
                    result["replied"] = mark_replied_tasks(profile)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        # TypeError: a JSONL source row that is not an object, rejected by _fetch_from_jsonl.
        print(f"line-local-case-cockpit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
