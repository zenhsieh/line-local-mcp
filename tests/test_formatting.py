from line_local_mcp.formatting import format_archive_result


def test_compact_messages_keep_speaker_time_recall_and_one_chat_id():
    data = {
        "messages": [
            {"chatId": "c-1", "from": "u" + "a" * 32, "time": "2026-10-28 09:01", "type": "text", "text": "好"},
            {"chatId": "c-1", "from": "欣晴", "time": "2026-10-28 09:02", "type": "text", "text": ""},
        ],
        "contact_resolution": {"requested": "欣倫", "canonical": "欣晴", "matched_candidate": "欣晴"},
    }
    output = format_archive_result(data, kind="chat", self_sender_id="u" + "a" * 32, current_year=2026)
    assert output.count("chatId=c-1") == 1
    assert "10-28 09:01 我：好" in output
    assert "10-28 09:02 欣晴：（已收回）" in output
    assert "聯絡人 欣倫 → 欣晴 → 欣晴" in output
    assert "u" + "a" * 32 not in output


def test_compact_attachments_keep_ids_filename_and_cross_year_time():
    data = {"attachments": [
        {"chatId": "c", "from": "欣晴", "time": "2025-12-31 23:59", "kind": "image", "media_id": "image-1"},
        {"chatId": "c", "from": "欣晴", "time": "2026-01-01 00:00", "kind": "file", "media_id": "file-2", "filename": "a.pdf"},
    ]}
    output = format_archive_result(data, kind="attachments", current_year=2026)
    assert "2025-12-31 23:59 欣晴：[圖 image-1]" in output
    assert "01-01 00:00 欣晴：[檔 file-2 a.pdf]" in output


def test_compact_chats_and_unread():
    data = {"chats": [{"chatId": "c", "name": "欣晴", "time": "2026-10-28 10:00", "unread": 2, "last": "後續"}], "unread_total": 2, "unread_chats": 1}
    assert "欣晴 未讀2 後續" in format_archive_result(data, kind="chats", current_year=2026)
    assert "未讀 2／對話 1" in format_archive_result(data, kind="unread", current_year=2026)
