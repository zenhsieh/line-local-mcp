"""Interactive Case Cockpit over the shared read-only LINE pipeline.

The cockpit composes existing inbox, task, and injection contracts.  It does
not send LINE messages, infer business truth, or own the adjacent agent.
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
    Profile,
    _load_json,
    _write_private,
    ensure_task_ids,
    inbox_rows,
    inject_pending,
    injection_active,
    injection_switch,
    load_profile,
    set_injection_switch,
    todo_add,
    watch,
)

EVENT_MARKER = "<!-- line-event:{fingerprint} -->"
EVENT_MARKER_RE = re.compile(r"\s*<!-- line-event:[0-9a-f]{64} -->")
TASK_RE = re.compile(
    r"^(?P<prefix>- \[(?P<checked>[ xX])\]\s+)"
    r"(?:\[(?:#)?(?P<task_id>\d{2,})\]\s+)?(?P<body>.*)$"
)
MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
STATUS_BADGES = {
    "[完成]": ("完成", "\033[38;5;250;48;5;238m"),
    "[LINE]": ("LINE", "\033[1;38;5;159;48;5;24m"),
    "[進行]": ("執行", "ACTIVE"),
    "[執行]": ("執行", "\033[38;5;153;48;5;24m"),
    "[設計]": ("設計", "\033[38;5;182;48;5;53m"),
    "[盤點]": ("盤點", "\033[38;5;186;48;5;58m"),
    "[待問]": ("待問", "\033[38;5;181;48;5;52m"),
    "[待決]": ("待決", "\033[38;5;181;48;5;52m"),
}


@contextmanager
def _cockpit_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "cockpit.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _one_line(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _task_summary(event: dict[str, Any]) -> str:
    messages = event.get("messages", [])
    attachments = event.get("attachments", [])
    if messages:
        first = messages[0]
        when = _one_line(first.get("time"), 32)
        sender = _one_line(first.get("from"), 48)
        body = _one_line(first.get("text"))
        suffix = f" (+{len(messages) - 1} messages)" if len(messages) > 1 else ""
        return f"LINE {when} {sender}: {body}{suffix}".strip()
    if attachments:
        first = attachments[0]
        when = _one_line(first.get("time"), 32)
        sender = _one_line(first.get("from"), 48)
        name = _one_line(first.get("filename") or first.get("kind") or "attachment")
        suffix = f" (+{len(attachments) - 1} attachments)" if len(attachments) > 1 else ""
        return f"LINE {when} {sender}: [attachment] {name}{suffix}".strip()
    return "LINE archive event"


def reconcile_todos(profile: Profile) -> dict[str, int]:
    """Create exactly one durable task per inbox event across retries/deletion."""

    ensure_task_ids(profile)
    state_file = profile.state_dir / "todo-state.json"
    state = _load_json(state_file, {"assigned": []})
    assigned = list(state.get("assigned", []))
    assigned_set = set(assigned)
    todo_text = profile.todo_file.read_text(encoding="utf-8")
    created = recovered = 0

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
        todo_add(profile, f"{_task_summary(event)} {marker}", status="LINE")
        todo_text = profile.todo_file.read_text(encoding="utf-8")
        assigned.append(fingerprint)
        assigned_set.add(fingerprint)
        _write_private(state_file, {"assigned": assigned})
        created += 1

    return {"created": created, "recovered": recovered, "known": len(assigned)}


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
        body = EVENT_MARKER_RE.sub("", task.group("body")).strip()
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
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)

    events: list[str] = []
    previous_minute = None
    for timestamp, _index, rendered in rows[:10]:
        minute = _hhmm(timestamp)
        body = rendered[6:] if rendered.startswith(minute + " ") else rendered
        events.append(f"{minute} {body}" if minute != previous_minute else f"      {body}")
        previous_minute = minute
    return pending, completed, events, _load_json(profile.state_file, {})


def _render(profile: Profile, active_tab: str, offset: int, blink_on: bool) -> int:
    size = shutil.get_terminal_size((115, 12))
    columns = max(80, size.columns)
    rows_available = max(1, min(10, size.lines - 1))
    gap = 3
    left_width = max(40, min(columns - gap - 34, round(columns * 0.62)))
    right_width = max(34, columns - left_width - gap)
    pending, completed, events, state = dashboard_snapshot(profile)
    selected = pending if active_tab == "pending" else completed
    offset = min(max(0, offset), max(0, len(selected) - rows_available))
    visible = selected[offset : offset + rows_available]
    lines: list[str] = []

    for index in range(rows_available):
        left = f"{visible[index][0]:02d}. {visible[index][1]}" if index < len(visible) else ""
        if left and active_tab == "completed":
            left = "[完成] " + re.sub(r"^\[[^\]]+\]\s*", "", left)
        if index == 0:
            title = "最新 LINE"
            first = events[0] if events else ""
            event_width = max(1, right_width - _width(title) - 1)
            event_cell = _fit(first, event_width).rstrip()
            right = event_cell + " " * max(1, right_width - _width(event_cell) - _width(title)) + title
        else:
            right = events[index] if index < len(events) else ""

        badge_render = None
        for label, (caption, color) in STATUS_BADGES.items():
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
        right_cell = _fit(right, right_width)
        if "[附件]" in right_cell:
            right_cell = right_cell.replace("[附件]", "\033[33m[附件]" + RESET, 1)
        if index == 0:
            right_cell = right_cell.replace("最新 LINE", BOLD + "\033[38;5;180m最新 LINE" + RESET, 1)
        lines.append(left_cell + DIM + " │ " + RESET + right_cell)

    pending_tab = f" 待辦 {len(pending)} "
    completed_tab = f" 已完成 {len(completed)} "
    toggle_tab, toggle_color = _injection_badge(profile, blink_on)
    controls = pending_tab + "  " + completed_tab + "  " + toggle_tab + "  點選｜← →｜i 注入｜滾輪"
    sync = f"更新 {datetime.now().astimezone():%H:%M} 同步 {_hhmm(state.get('last_checked_at'))}"
    title = f"{profile.contact} LINE"
    lines.append(
        ("\033[1;30;46m" if active_tab == "pending" else "\033[2;37m")
        + pending_tab + RESET + "  "
        + ("\033[1;30;46m" if active_tab == "completed" else "\033[2;37m")
        + completed_tab + RESET + "  "
        + toggle_color + toggle_tab + RESET + DIM + "  點選｜← →｜i 注入｜滾輪" + RESET
        + " " * max(0, left_width - _width(controls)) + DIM + " │ " + RESET
        + BOLD + "\033[38;5;109m" + title + RESET
        + " " * max(1, right_width - _width(title) - _width(sync) - 2)
        + DIM + sync + RESET + "  "
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
        active_tab, offset = "pending", 0
    if "\x1b[C" in data or "\t" in data:
        active_tab, offset = "completed", 0
    if profile is not None and ("i" in data or "I" in data) and "\x1b" not in data:
        toggle_injection(profile)
    for button, x, y, action in MOUSE_RE.findall(data):
        button, x, y = int(button), int(x), int(y)
        if action == "M" and button == 0 and y == bottom_row:
            pending_end = _width(" 待辦 99 ")
            completed_end = pending_end + 2 + _width(" 已完成 99 ")
            toggle_end = completed_end + 2 + _width(TOGGLE_TEXT[False])
            if x <= pending_end:
                active_tab, offset = "pending", 0
            elif pending_end + 2 < x <= completed_end:
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
            signature = repr((dashboard_snapshot(profile), active_tab, offset, shutil.get_terminal_size(), blink_on, injection_switch(profile)))
            if signature != previous:
                offset = _render(profile, active_tab, offset, blink_on)
                previous = signature
            if not watch_mode:
                sys.stdout.write("\n")
                return
            ready, _write, _errors = select.select([stdin_fd], [], [], min(max(0.25, interval), 1.0))
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
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        profile = load_profile(args.config, args.profile)
        if args.command == "dashboard":
            dashboard(profile, args.watch, args.interval)
            return 0
        if args.command == "injection":
            if args.action == "status":
                status = {"configured": profile.injection_configured,
                          "switch": injection_switch(profile), "active": injection_active(profile)}
            else:
                status = set_injection_switch(profile, args.action == "on")
            print(json.dumps(status, ensure_ascii=False, sort_keys=True))
            return 0
        with _cockpit_lock(profile.state_dir):
            if args.command == "reconcile":
                result: dict[str, Any] = {"todo": reconcile_todos(profile)}
            else:
                result = {"watch": asyncio.run(watch(profile, bootstrap=args.bootstrap))}
                if args.bootstrap:
                    ensure_task_ids(profile)
                    result["todo"] = {"created": 0, "recovered": 0}
                    result["injection"] = {"sent": 0, "failed": 0, "disabled": 1}
                else:
                    result["todo"] = reconcile_todos(profile)
                    result["injection"] = inject_pending(profile)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"line-local-case-cockpit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
