"""Config-driven, read-only LINE event pipeline.

The pipeline deliberately separates collection, durable inbox storage, task tracking,
presentation, and optional agent notification.  It never sends or mutates LINE data.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TASK_RE = re.compile(
    r"^(?P<prefix>- \[(?P<checked>[ xX])\]\s+)"
    r"(?:\[(?:#)?(?P<task_id>\d{2,})\]\s+)?(?P<body>.*)$"
)
NEXT_ID_RE = re.compile(r"^<!--\s*next-task-id:\s*(\d+)\s*-->$")
PLACEHOLDER_RE = re.compile(r"\{(profile|project_label|target|prompt)\}")


@dataclass(frozen=True)
class Profile:
    name: str
    project_label: str
    contact: str
    incoming_aliases: frozenset[str]
    state_dir: Path
    todo_file: Path
    poll_limit: int
    keep_fingerprints: int
    mcp_command: tuple[str, ...]
    mcp_env: dict[str, str]
    mcp_config_file: Path | None
    mcp_server_name: str
    injection_enabled: bool
    target_strategy: str
    target: str
    target_resolver_command: tuple[str, ...]
    agent_command: tuple[str, ...]
    prompt_template: str
    source: str = "mcp"
    mirror_db: Path | None = None
    source_file: Path | None = None

    @property
    def state_file(self) -> Path:
        return self.state_dir / "watch-state.json"

    @property
    def inbox_file(self) -> Path:
        return self.state_dir / "inbox.jsonl"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "pipeline.lock"

    @property
    def injection_state_file(self) -> Path:
        return self.state_dir / "injection-state.json"

    @property
    def injection_override_file(self) -> Path:
        """Runtime switch written by the cockpit; absent means follow the profile."""
        return self.state_dir / "injection-override.json"

    @property
    def injection_configured(self) -> bool:
        return self.injection_enabled and self.target_strategy != "disabled"


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise ValueError(f"{field} must be an array of strings")
    return tuple(_expand(part) for part in value)


def load_profile(config_path: Path, name: str) -> Profile:
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw = document.get("profiles", {}).get(name)
    if not isinstance(raw, dict):
        raise ValueError(f"profile {name!r} not found in {config_path}")  # noqa: TRY004
    required = ("project_label", "contact", "state_dir", "todo_file")
    missing = [key for key in required if not str(raw.get(key, "")).strip()]
    if missing:
        raise ValueError(f"profile {name!r} is missing: {', '.join(missing)}")

    mcp = raw.get("mcp", {})
    injection = raw.get("injection", {})
    if not isinstance(mcp, dict) or not isinstance(injection, dict):
        raise ValueError("mcp and injection must be TOML tables")  # noqa: TRY004
    source = str(raw.get("source", "mcp"))
    if source not in {"mcp", "mirror", "jsonl"}:
        raise ValueError("source must be mcp, mirror, or jsonl")
    mirror_db = str(raw.get("mirror_db", "")).strip()
    if source == "mirror" and not mirror_db:
        # fall back to the host-wide collector's database
        from .mirror import mirror_db_from_config

        shared = mirror_db_from_config(config_path)
        if shared is None:
            raise ValueError("source = mirror needs mirror_db or a [collector] table")
        mirror_db = str(shared)
    source_file = str(raw.get("source_file", "")).strip()
    if source == "jsonl" and not source_file:
        raise ValueError("source = jsonl needs source_file")
    direct_command = mcp.get("command", [])
    config_file = mcp.get("config_file")
    if source == "mcp" and not direct_command and not config_file:
        raise ValueError("configure either mcp.command or mcp.config_file")
    env = mcp.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise ValueError("mcp.env must contain string values")
    strategy = str(injection.get("target_strategy", "disabled"))
    if strategy not in {"disabled", "explicit", "resolver-command"}:
        raise ValueError("target_strategy must be disabled, explicit, or resolver-command")

    aliases = raw.get("incoming_aliases", [raw["contact"]])
    return Profile(
        name=name,
        project_label=str(raw["project_label"]),
        contact=str(raw["contact"]),
        incoming_aliases=frozenset(str(item) for item in aliases),
        state_dir=Path(_expand(str(raw["state_dir"]))),
        todo_file=Path(_expand(str(raw["todo_file"]))),
        poll_limit=int(raw.get("poll_limit", 200)),
        keep_fingerprints=int(raw.get("keep_fingerprints", 2000)),
        mcp_command=_string_list(direct_command, "mcp.command") if direct_command else (),
        mcp_env={key: _expand(value) for key, value in env.items()},
        mcp_config_file=Path(_expand(str(config_file))) if config_file else None,
        mcp_server_name=str(mcp.get("server_name", "line-local")),
        injection_enabled=bool(injection.get("enabled", False)),
        target_strategy=strategy,
        target=str(injection.get("target", "")),
        target_resolver_command=_string_list(
            injection.get("target_resolver_command", []), "target_resolver_command"
        ),
        agent_command=_string_list(injection.get("agent_command", []), "agent_command"),
        prompt_template=str(
            injection.get(
                "prompt_template",
                "[{project_label} LINE] New read-only archive event. Review it and propose action; "
                "do not reply to LINE or mutate live systems. Event: {prompt}",
            )
        ),
        source=source,
        mirror_db=Path(_expand(mirror_db)) if mirror_db else None,
        source_file=Path(_expand(source_file)) if source_file else None,
    )


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def _lock(profile: Profile) -> Iterator[None]:
    profile.state_dir.mkdir(parents=True, exist_ok=True)
    with profile.lock_file.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _server_parameters(profile: Profile) -> StdioServerParameters:
    env = os.environ.copy()
    if profile.mcp_command:
        command, *args = profile.mcp_command
        env.update(profile.mcp_env)
        return StdioServerParameters(command=command, args=args, env=env)
    config = _load_json(profile.mcp_config_file or Path(), {})
    server = config.get("mcpServers", {}).get(profile.mcp_server_name)
    if not isinstance(server, dict) or not server.get("command"):
        raise RuntimeError(
            f"MCP server {profile.mcp_server_name!r} missing in {profile.mcp_config_file}"
        )
    env.update({str(key): _expand(str(value)) for key, value in server.get("env", {}).items()})
    env.update(profile.mcp_env)
    return StdioServerParameters(
        command=_expand(str(server["command"])),
        args=[_expand(str(value)) for value in server.get("args", [])],
        env=env,
    )


def _tool_json(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError("LINE MCP tool returned an error")
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise RuntimeError("LINE MCP tool returned no JSON object")


def _fetch_from_mirror(profile: Profile) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any]:
    """Read the newest rows for this contact from the host-wide mirror.  No network."""
    from .mirror import Mirror

    assert profile.mirror_db is not None
    if not profile.mirror_db.exists():
        raise RuntimeError(f"mirror database {profile.mirror_db} does not exist yet; run the collector")
    mirror = Mirror(profile.mirror_db, readonly=True)
    try:
        messages = [row for _fp, row in mirror.latest(profile.contact, "message", profile.poll_limit)]
        attachments = [
            row for _fp, row in mirror.latest(profile.contact, "attachment", profile.poll_limit)
        ]
        sync = {
            "source": "mirror",
            "last_collected_at": mirror.get_meta("last_collected_at"),
            "last_sync": mirror.get_meta("last_sync"),
            "last_sync_error": mirror.get_meta("last_sync_error"),
        }
    finally:
        mirror.close()
    return messages, attachments, sync


def _fetch_from_jsonl(profile: Profile) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any]:
    """Read an append-only local event feed without modifying its cursor or bytes."""
    assert profile.source_file is not None
    if not profile.source_file.exists():
        raise RuntimeError(f"JSONL source {profile.source_file} does not exist")

    messages: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    lines = profile.source_file.read_text(encoding="utf-8").splitlines()
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"invalid JSONL source row {line_number} in {profile.source_file}"
            ) from exc
        if not isinstance(row, dict):
            raise TypeError(
                f"JSONL source row {line_number} in {profile.source_file} is not an object"
            )
        kind = str(row.get("kind", "message"))
        event = {key: value for key, value in row.items() if key != "kind"}
        if kind == "message":
            messages.append(event)
        elif kind == "attachment":
            attachments.append(event)
        else:
            raise RuntimeError(
                f"JSONL source row {line_number} has unsupported kind {kind!r}"
            )

    stat = profile.source_file.stat()
    sync = {
        "source": "jsonl",
        "source_file": str(profile.source_file),
        "line_count": len(lines),
        "last_collected_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
            timespec="seconds"
        ),
        "last_sync_error": None,
    }
    return messages, attachments, sync


async def _fetch(profile: Profile) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any]:
    if profile.source == "mirror":
        return _fetch_from_mirror(profile)
    if profile.source == "jsonl":
        return _fetch_from_jsonl(profile)
    async with (
        stdio_client(_server_parameters(profile)) as streams,
        ClientSession(*streams) as session,
    ):
            await session.initialize()
            sync = _tool_json(await session.call_tool("line_sync", {}))
            messages = _tool_json(
                await session.call_tool(
                    "line_get_messages", {"contact": profile.contact, "limit": profile.poll_limit}
                )
            ).get("messages", [])
            attachments = _tool_json(
                await session.call_tool(
                    "line_list_attachments",
                    {"contact": profile.contact, "limit": profile.poll_limit},
                )
            ).get("attachments", [])
    return messages, attachments, sync


def _message_rows(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    occurrences: Counter[str] = Counter()
    rows = []
    for message in messages:
        stable = {key: message.get(key) for key in ("time", "from", "chatId", "type", "text")}
        raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        occurrence = occurrences[raw]
        occurrences[raw] += 1
        digest = hashlib.sha256(f"message:{raw}:{occurrence}".encode()).hexdigest()
        rows.append((digest, stable))
    return rows


def _attachment_rows(items: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    rows = []
    for item in items:
        raw = str(item.get("media_id") or json.dumps(item, ensure_ascii=False, sort_keys=True))
        rows.append((hashlib.sha256(f"attachment:{raw}".encode()).hexdigest(), item))
    return rows


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


async def watch(profile: Profile, bootstrap: bool = False) -> dict[str, Any]:
    messages, attachments, sync = await _fetch(profile)
    with _lock(profile):
        state = _load_json(profile.state_file, {"seen": []})
        seen = set(state.get("seen", []))
        message_rows = _message_rows(messages)
        attachment_rows = _attachment_rows(attachments)
        incoming_messages = [
            row for fingerprint, row in message_rows
            if fingerprint not in seen
            and row.get("from") in profile.incoming_aliases
            and row.get("type") == "text"
            and str(row.get("text", "")).strip()
        ]
        incoming_attachments = [
            row for fingerprint, row in attachment_rows
            if fingerprint not in seen and row.get("from") in profile.incoming_aliases
        ]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if not bootstrap and (incoming_messages or incoming_attachments):
            _append_jsonl(profile.inbox_file, {
                "detected_at": now,
                "profile": profile.name,
                "project_label": profile.project_label,
                "contact": profile.contact,
                "messages": sorted(incoming_messages, key=lambda row: str(row.get("time", ""))),
                "attachments": sorted(incoming_attachments, key=lambda row: str(row.get("time", ""))),
            })
        newest = [fingerprint for fingerprint, _row in message_rows + attachment_rows]
        merged = list(dict.fromkeys(newest + list(state.get("seen", []))))
        _write_private(profile.state_file, {
            "profile": profile.name,
            "contact": profile.contact,
            "last_checked_at": now,
            "seen": merged[: profile.keep_fingerprints],
            "last_sync": sync,
        })
    return {
        "checked_at": now,
        "bootstrap": bootstrap,
        "new_messages": 0 if bootstrap else len(incoming_messages),
        "new_attachments": 0 if bootstrap else len(incoming_attachments),
        "inbox": str(profile.inbox_file),
    }


def inbox_rows(profile: Profile) -> list[tuple[str, dict[str, Any]]]:
    rows = []
    try:
        lines = profile.inbox_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return rows
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rows.append((hashlib.sha256(raw.encode()).hexdigest(), event))
    return rows


def _resolve_target(profile: Profile) -> str:
    if profile.target_strategy == "explicit":
        if not profile.target:
            raise RuntimeError("explicit injection target is empty")
        return profile.target
    if profile.target_strategy == "resolver-command":
        if not profile.target_resolver_command:
            raise RuntimeError("target_resolver_command is empty")
        result = subprocess.run(
            profile.target_resolver_command, text=True, capture_output=True, check=False, timeout=15
        )
        candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or len(candidates) != 1:
            raise RuntimeError("target resolver must return exactly one non-empty line")
        return candidates[0]
    raise RuntimeError("agent injection is disabled")


def _render_argv(parts: tuple[str, ...], values: dict[str, str]) -> list[str]:
    return [PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], part) for part in parts]


def injection_switch(profile: Profile) -> bool | None:
    """The runtime override: True/False when the cockpit set one, None when untouched."""
    value = _load_json(profile.injection_override_file, {}).get("enabled")
    return value if isinstance(value, bool) else None


def injection_active(profile: Profile) -> bool:
    """Configured in the profile *and* not paused at runtime."""
    return profile.injection_configured and injection_switch(profile) is not False


def set_injection_switch(profile: Profile, enabled: bool) -> dict[str, Any]:
    """Flip the runtime switch.  Turning it on never replays the backlog.

    Everything already in the inbox while injection was off has been visible on the
    dashboard and in the task file; it is marked as seen so only events that arrive
    from now on reach the agent.
    """
    skipped = 0
    if enabled:
        state = _load_json(profile.injection_state_file, {"seen": []})
        seen = list(state.get("seen", []))
        seen_set = set(seen)
        for fingerprint, _event in inbox_rows(profile):
            if fingerprint not in seen_set:
                seen.append(fingerprint)
                seen_set.add(fingerprint)
                skipped += 1
        _write_private(profile.injection_state_file, {"seen": seen[-profile.keep_fingerprints :]})
    _write_private(profile.injection_override_file, {
        "enabled": enabled,
        "changed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    return {"enabled": enabled, "configured": profile.injection_configured, "backlog_skipped": skipped}


def inject_pending(profile: Profile) -> dict[str, int]:
    if not profile.injection_configured:
        return {"sent": 0, "failed": 0, "disabled": 1, "paused": 0}
    if injection_switch(profile) is False:
        return {"sent": 0, "failed": 0, "disabled": 0, "paused": 1}
    if not profile.agent_command:
        raise RuntimeError("injection.agent_command is required when injection is enabled")
    state = _load_json(profile.injection_state_file, {"seen": []})
    seen = list(state.get("seen", []))
    seen_set = set(seen)
    sent = failed = 0
    for fingerprint, event in inbox_rows(profile):
        if fingerprint in seen_set:
            continue
        try:
            target = _resolve_target(profile)
            raw_event = json.dumps(event, ensure_ascii=False)
            prompt = profile.prompt_template.format(
                profile=profile.name,
                project_label=profile.project_label,
                target=target,
                prompt=raw_event[:12000],
            )
            argv = _render_argv(profile.agent_command, {
                "profile": profile.name,
                "project_label": profile.project_label,
                "target": target,
                "prompt": prompt,
            })
            result = subprocess.run(argv, text=True, capture_output=True, check=False, timeout=30)
            if result.returncode:
                failed += 1
                break
        except (OSError, RuntimeError, subprocess.TimeoutExpired, KeyError, ValueError):
            failed += 1
            break
        seen.append(fingerprint)
        seen_set.add(fingerprint)
        _write_private(profile.injection_state_file, {"seen": seen[-profile.keep_fingerprints :]})
        sent += 1
    return {"sent": sent, "failed": failed, "disabled": 0, "paused": 0}


def ensure_task_ids(profile: Profile) -> list[str]:
    try:
        original = profile.todo_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = f"# {profile.project_label} LINE tasks\n\n<!-- next-task-id: 1 -->\n\n"
    lines = original.splitlines()
    used: set[int] = set()
    marker_index = None
    marker_next = 1
    for index, line in enumerate(lines):
        marker = NEXT_ID_RE.match(line)
        task = TASK_RE.match(line)
        if marker:
            marker_index, marker_next = index, max(marker_next, int(marker.group(1)))
        elif task and task.group("task_id"):
            used.add(int(task.group("task_id")))
    next_id = max(marker_next, max(used, default=0) + 1)
    changed = False
    for index, line in enumerate(lines):
        task = TASK_RE.match(line)
        if not task or task.group("task_id"):
            continue
        while next_id in used:
            next_id += 1
        lines[index] = f'{task.group("prefix")}[{next_id:02d}] {task.group("body")}'
        used.add(next_id)
        next_id += 1
        changed = True
    marker_text = f"<!-- next-task-id: {next_id} -->"
    if marker_index is None:
        lines[1:1] = ["", marker_text]
        changed = True
    elif lines[marker_index] != marker_text:
        lines[marker_index] = marker_text
        changed = True
    if changed or not profile.todo_file.exists():
        profile.todo_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = profile.todo_file.with_suffix(profile.todo_file.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(profile.todo_file)
    return lines


def todo_add(profile: Profile, text: str, status: str = "待決") -> int:
    with _lock(profile):
        lines = ensure_task_ids(profile)
        marker_index = next(index for index, line in enumerate(lines) if NEXT_ID_RE.match(line))
        task_id = int(NEXT_ID_RE.match(lines[marker_index]).group(1))  # type: ignore[union-attr]
        lines[marker_index] = f"<!-- next-task-id: {task_id + 1} -->"
        lines.append(f"- [ ] [{task_id:02d}] [{status}] {text}")
        profile.todo_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return task_id


def todo_complete(profile: Profile, task_id: int) -> bool:
    with _lock(profile):
        lines = ensure_task_ids(profile)
        changed = False
        for index, line in enumerate(lines):
            task = TASK_RE.match(line)
            if task and task.group("task_id") and int(task.group("task_id")) == task_id:
                lines[index] = re.sub(r"^- \[[ xX]\]", "- [x]", line, count=1)
                changed = True
                break
        if changed:
            profile.todo_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return changed


def dashboard_text(profile: Profile) -> str:
    pending, completed = [], []
    for line in ensure_task_ids(profile):
        task = TASK_RE.match(line)
        if not task:
            continue
        row = f'{int(task.group("task_id")):02d} {task.group("body")}'
        (completed if task.group("checked").lower() == "x" else pending).append(row)
    events = []
    for _fingerprint, event in inbox_rows(profile)[-20:]:
        for message in event.get("messages", []):
            if message.get("type") == "text" and str(message.get("text", "")).strip():
                events.append((str(message.get("time", "")), str(message["text"]).strip()))
        for item in event.get("attachments", []):
            events.append((str(item.get("time", "")), f"[attachment] {item.get('filename', '')}"))
    events.sort(reverse=True)
    checked = _load_json(profile.state_file, {}).get("last_checked_at", "never")
    lines = [
        f"{profile.project_label} LINE | pending {len(pending)} | completed {len(completed)} | sync {checked}",
        "",
        "PENDING",
        *(pending or ["(none)"]),
        "",
        "LATEST",
        *[f"{timestamp} {text}" for timestamp, text in events[:10]],
    ]
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="line-local-pipeline")
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    watch_parser = commands.add_parser("watch")
    watch_parser.add_argument("profile")
    watch_parser.add_argument("--bootstrap", action="store_true")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("profile")
    run_parser.add_argument("--bootstrap", action="store_true")
    inject_parser = commands.add_parser("inject")
    inject_parser.add_argument("profile")
    dashboard_parser = commands.add_parser("dashboard")
    dashboard_parser.add_argument("profile")
    dashboard_parser.add_argument("--watch", action="store_true")
    dashboard_parser.add_argument("--interval", type=float, default=2.0)
    todo_parser = commands.add_parser("todo")
    todo_parser.add_argument("profile")
    todo_commands = todo_parser.add_subparsers(dest="todo_command", required=True)
    add_parser = todo_commands.add_parser("add")
    add_parser.add_argument("text")
    add_parser.add_argument("--status", default="待決")
    complete_parser = todo_commands.add_parser("complete")
    complete_parser.add_argument("task_id", type=int)
    todo_commands.add_parser("list")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        profile = load_profile(args.config, args.profile)
        if args.command in {"watch", "run"}:
            result = asyncio.run(watch(profile, args.bootstrap))
            if args.command == "run" and not args.bootstrap:
                result["injection"] = inject_pending(profile)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "inject":
            print(json.dumps(inject_pending(profile), ensure_ascii=False))
        elif args.command == "todo":
            if args.todo_command == "add":
                print(f"{todo_add(profile, args.text, args.status):02d}")
            elif args.todo_command == "complete":
                return 0 if todo_complete(profile, args.task_id) else 1
            else:
                print(dashboard_text(profile))
        elif args.command == "dashboard":
            while True:
                print("\033[2J\033[H" + dashboard_text(profile), end="", flush=True)
                if not args.watch:
                    print()
                    break
                time.sleep(max(0.25, args.interval))
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"line-local-pipeline: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
