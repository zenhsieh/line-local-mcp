"""Compact, loss-aware rendering of the archive's text endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

_KINDS = {"image": "圖", "file": "檔", "video": "影", "audio": "音", "sticker": "貼"}


def _when(value: Any, current_year: int) -> str:
    raw = str(value or "")
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    prefix = "%m-%d %H:%M" if stamp.year == current_year else "%Y-%m-%d %H:%M"
    return stamp.strftime(prefix)


def _sender(row: dict[str, Any], self_sender_id: str) -> str:
    sender = str(row.get("from") or row.get("sender") or "")
    return "我" if self_sender_id and sender == self_sender_id else sender or "?"


def _attachment(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or row.get("type") or "attachment")
    label = _KINDS.get(kind, "附")
    media_id = str(row.get("media_id") or row.get("attachment_id") or "")
    name = str(row.get("filename") or "") if kind == "file" else ""
    return f"[{label} {media_id}{' ' + name if name else ''}]"


def format_archive_result(
    result: Any, *, kind: str, self_sender_id: str = "", current_year: int | None = None
) -> str:
    """Render rows once; keep opaque chat ids in a per-chat header only."""
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        return str(result["error"])
    year = current_year or datetime.now(UTC).astimezone().year
    key = "attachments" if kind == "attachments" else "chats" if kind in {"chats", "unread"} else "messages"
    rows = result.get(key, [])
    if not isinstance(rows, list):
        return str(result)
    lines: list[str] = []
    chats: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        chat_id = str(row.get("chatId") or "")
        chat_name = str(row.get("chat") or row.get("name") or "")
        if chat_id and chat_id not in chats:
            chats[chat_id] = chat_name
    for chat_id, chat_name in chats.items():
        lines.append(f"對話 {chat_name + ' ' if chat_name else ''}chatId={chat_id}")
    resolution = result.get("contact_resolution")
    if isinstance(resolution, dict):
        lines.append(
            "聯絡人 " + " → ".join(
                str(resolution.get(field) or "")
                for field in ("requested", "canonical", "matched_candidate")
            )
        )
    if kind == "search" and result.get("q"):
        lines.append(f"搜尋 {result['q']}")
    if kind == "unread":
        lines.append(f"未讀 {result.get('unread_total', 0)}／對話 {result.get('unread_chats', 0)}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        when = _when(row.get("time") or row.get("timestamp"), year)
        chat = str(row.get("chat") or row.get("name") or "")
        speaker = _sender(row, self_sender_id)
        if kind in {"chats", "unread"}:
            last = str(row.get("last") or "").replace("\n", " ")
            body = f"{chat or '?'} 未讀{row.get('unread', row.get('unread_count', 0))} {last}".rstrip()
        elif kind == "attachments":
            body = f"{speaker}：{_attachment(row)}"
            if row.get("available") is False:
                body += "（不可下載）"
        else:
            message_type = str(row.get("type") or "text")
            body_text = str(row.get("text") or "").replace("\n", " ⏎ ")
            if not body_text.strip():
                body_text = "（已收回）" if message_type == "text" else _attachment(row)
            body = f"{speaker}：{body_text}"
        if len(chats) > 1 and kind not in {"chats", "unread"}:
            body = f"{chat or row.get('chatId') or '?'} {body}"
        lines.append(f"{when} {body}".strip())
    return "\n".join(lines) if lines else "（無資料）"
