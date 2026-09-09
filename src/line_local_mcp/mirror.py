"""Single-writer local mirror of the read-only LINE archive.

One collector process is the only thing on this host that talks to the archive API.
It triggers one incremental ``/sync``, pulls the configured chats, and appends unseen
events to a local SQLite database opened in WAL mode.  Watchers never call the archive
again: they read the mirror, which has exactly one writer, so readers and the writer
never block each other.

The mirror is append-only.  Nothing here sends, marks read, or otherwise mutates LINE.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .config import ConfigurationError, Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('message', 'attachment')),
    chat         TEXT NOT NULL,
    sender       TEXT,
    time         TEXT,
    payload      TEXT NOT NULL,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_chat_seq ON events (chat, kind, seq);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ArchiveReader(Protocol):
    def request(self, method: str, path: str, params: dict[str, str | int] | None = None) -> Any: ...


@dataclass(frozen=True)
class CollectorConfig:
    mirror_db: Path
    chats: tuple[str, ...]
    poll_limit: int
    env: dict[str, str]
    trigger_sync: bool

    @property
    def lock_file(self) -> Path:
        return self.mirror_db.with_suffix(self.mirror_db.suffix + ".lock")


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def load_collector_config(config_path: Path) -> CollectorConfig:
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw = document.get("collector")
    if not isinstance(raw, dict):
        raise ValueError(f"[collector] table not found in {config_path}")  # noqa: TRY004
    mirror_db = str(raw.get("mirror_db", "")).strip()
    if not mirror_db:
        raise ValueError("collector.mirror_db is required")
    chats = raw.get("chats", [])
    if not isinstance(chats, list) or not chats or not all(isinstance(c, str) for c in chats):
        raise ValueError("collector.chats must be a non-empty array of chat names")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise ValueError("collector.env must contain string values")
    return CollectorConfig(
        mirror_db=Path(_expand(mirror_db)),
        chats=tuple(dict.fromkeys(chats)),
        poll_limit=int(raw.get("poll_limit", 200)),
        env={key: _expand(value) for key, value in env.items()},
        trigger_sync=bool(raw.get("trigger_sync", True)),
    )


def mirror_db_from_config(config_path: Path) -> Path | None:
    """Return collector.mirror_db if the config declares a collector, else None."""
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw = document.get("collector")
    if not isinstance(raw, dict) or not str(raw.get("mirror_db", "")).strip():
        return None
    return Path(_expand(str(raw["mirror_db"])))


class Mirror:
    """Thin wrapper around the SQLite file.  Open one per process."""

    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = path
        if readonly:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            self.conn = sqlite3.connect(path, timeout=15)
            if not existed:
                os.chmod(path, 0o600)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    # -- writes (collector only) -------------------------------------------------

    def insert_events(
        self, rows: list[tuple[str, str, str, dict[str, Any]]], collected_at: str
    ) -> int:
        """Insert (fingerprint, kind, chat, payload) rows; return how many were new."""
        before = self.conn.total_changes
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO events (fingerprint, kind, chat, sender, time, payload,"
                " collected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        fingerprint,
                        kind,
                        chat,
                        payload.get("from"),
                        str(payload.get("time", "")),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        collected_at,
                    )
                    for fingerprint, kind, chat, payload in rows
                ],
            )
        return self.conn.total_changes - before

    def set_meta(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
            )

    # -- reads (watchers) --------------------------------------------------------

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def latest(self, chat: str, kind: str, limit: int) -> list[tuple[str, dict[str, Any]]]:
        """Newest ``limit`` events of one kind for one chat, oldest first."""
        cursor = self.conn.execute(
            "SELECT fingerprint, payload FROM events WHERE chat = ? AND kind = ?"
            " ORDER BY seq DESC LIMIT ?",
            (chat, kind, limit),
        )
        rows = [(row["fingerprint"], json.loads(row["payload"])) for row in cursor]
        rows.reverse()
        return rows

    def stats(self) -> dict[str, Any]:
        counts = {
            row["kind"]: row["n"]
            for row in self.conn.execute("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")
        }
        chats = self.conn.execute("SELECT COUNT(DISTINCT chat) AS n FROM events").fetchone()["n"]
        return {"messages": counts.get("message", 0), "attachments": counts.get("attachment", 0),
                "chats": chats, "last_collected_at": self.get_meta("last_collected_at"),
                "last_sync": self.get_meta("last_sync"), "last_sync_error": self.get_meta("last_sync_error")}


@contextmanager
def _single_flight(lock_file: Path) -> Iterator[bool]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def classify_sync(result: Any) -> str | None:
    """Return an error string when the archive reported a failed sync, else None.

    The archive currently answers HTTP 200 with the sync script's stdout in ``sync``;
    a crash shows up as a traceback in that string.  Treat that as a failure so the
    mirror never records a crashed sync as success.
    """
    if not isinstance(result, dict):
        return f"unexpected sync response type {type(result).__name__}"
    if result.get("error"):
        return str(result["error"])
    text = str(result.get("sync", ""))
    if "Traceback" in text or "Error" in text.split("\n")[-1]:
        return text.strip().splitlines()[-1][:300]
    return None


def collect(config: CollectorConfig, client: ArchiveReader) -> dict[str, Any]:
    """One collection pass.  Safe to run from a timer; overlapping runs skip."""
    from .pipeline import _attachment_rows, _message_rows  # local import: avoids a cycle

    with _single_flight(config.lock_file) as acquired:
        if not acquired:
            return {"skipped": "collector already running", "mirror": str(config.mirror_db)}
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        mirror = Mirror(config.mirror_db)
        try:
            sync_error: str | None = None
            sync_result: Any = None
            if config.trigger_sync:
                try:
                    sync_result = client.request("POST", "/sync")
                    sync_error = classify_sync(sync_result)
                except Exception as exc:  # noqa: BLE001 - keep pulling; another caller may have synced
                    sync_error = f"{type(exc).__name__}: {exc}"
            new_total = 0
            per_chat: dict[str, dict[str, int]] = {}
            pull_errors: dict[str, str] = {}
            for chat in config.chats:
                try:
                    messages = client.request(
                        "GET", "/chat", {"name": chat, "limit": config.poll_limit}
                    )
                    attachments = client.request(
                        "GET", "/attachments", {"name": chat, "limit": config.poll_limit}
                    )
                except Exception as exc:  # noqa: BLE001
                    pull_errors[chat] = f"{type(exc).__name__}: {exc}"
                    continue
                message_list = messages.get("messages", []) if isinstance(messages, dict) else []
                attachment_list = (
                    attachments.get("attachments", []) if isinstance(attachments, dict) else []
                )
                rows = [(fp, "message", chat, row) for fp, row in _message_rows(message_list)]
                rows += [(fp, "attachment", chat, row) for fp, row in _attachment_rows(attachment_list)]
                new = mirror.insert_events(rows, now)
                new_total += new
                per_chat[chat] = {"fetched": len(rows), "new": new}
            mirror.set_meta("last_collected_at", now)
            if config.trigger_sync:
                mirror.set_meta("last_sync", {"at": now, "ok": sync_error is None,
                                              "summary": None if sync_error else str(
                                                  (sync_result or {}).get("sync", ""))[:200]})
                mirror.set_meta("last_sync_error", sync_error)
            return {
                "collected_at": now,
                "mirror": str(config.mirror_db),
                "sync_ok": None if not config.trigger_sync else sync_error is None,
                "sync_error": sync_error,
                "new_events": new_total,
                "chats": per_chat,
                "pull_errors": pull_errors,
            }
        finally:
            mirror.close()


def _client_from_env(env: dict[str, str]) -> ArchiveReader:
    from .client import LineArchiveClient
    from .redaction import Redactor

    os.environ.update(env)
    settings = Settings.from_env()
    return LineArchiveClient(settings, Redactor(settings.redaction_mode, settings.redactor_command))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="line-local-collector")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="one read-only collection pass into the mirror")
    commands.add_parser("stats", help="print mirror counts and last sync state")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        config = load_collector_config(args.config)
        if args.command == "stats":
            mirror = Mirror(config.mirror_db, readonly=True)
            try:
                result = mirror.stats()
            finally:
                mirror.close()
        else:
            result = collect(config, _client_from_env(config.env))
    except (ValueError, ConfigurationError, sqlite3.Error) as exc:
        print(f"line-local-collector: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not result.get("pull_errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
