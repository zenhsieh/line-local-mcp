from __future__ import annotations

import json
from pathlib import Path

import pytest

from line_local_mcp.pipeline import (
    _message_rows,
    dashboard_text,
    inject_pending,
    load_profile,
    todo_add,
    todo_complete,
)


def write_config(tmp_path: Path, *, injection: str = "") -> tuple[Path, object]:
    config = tmp_path / "pipeline.toml"
    config.write_text(
        f"""
[profiles.client]
project_label = "Client"
contact = "New Name"
incoming_aliases = ["New Name", "Old Name"]
state_dir = "{tmp_path / 'state'}"
todo_file = "{tmp_path / 'tasks.md'}"
[profiles.client.mcp]
command = ["/bin/true"]
{injection}
""",
        encoding="utf-8",
    )
    return config, load_profile(config, "client")


def test_profile_isolates_aliases_and_paths(tmp_path):
    _config, profile = write_config(tmp_path)
    assert profile.incoming_aliases == {"New Name", "Old Name"}
    assert profile.inbox_file == tmp_path / "state" / "inbox.jsonl"
    assert profile.todo_file == tmp_path / "tasks.md"
    assert not profile.injection_enabled


def test_duplicate_identical_messages_get_stable_occurrence_fingerprints():
    message = {"time": "2026-01-01 10:00", "from": "A", "type": "text", "text": "same"}
    first = _message_rows([message, message])
    second = _message_rows([message, message])
    assert [fingerprint for fingerprint, _row in first] == [
        fingerprint for fingerprint, _row in second
    ]
    assert first[0][0] != first[1][0]


def test_todo_numbers_are_never_reused(tmp_path):
    _config, profile = write_config(tmp_path)
    assert todo_add(profile, "first") == 1
    assert todo_add(profile, "second") == 2
    assert todo_complete(profile, 2)
    # Even removing the newest row cannot lower the persisted high-water mark.
    profile.todo_file.write_text(
        "\n".join(
            line for line in profile.todo_file.read_text(encoding="utf-8").splitlines()
            if "second" not in line
        )
        + "\n",
        encoding="utf-8",
    )
    assert todo_add(profile, "third") == 3
    rendered = dashboard_text(profile)
    assert "01 [待決] first" in rendered
    assert "03 [待決] third" in rendered


def test_injection_is_off_by_default_and_does_not_consume_inbox(tmp_path):
    _config, profile = write_config(tmp_path)
    profile.state_dir.mkdir(parents=True)
    profile.inbox_file.write_text('{"messages": [{"text": "hello"}]}\n', encoding="utf-8")
    assert inject_pending(profile) == {"sent": 0, "failed": 0, "disabled": 1, "paused": 0}
    assert not profile.injection_state_file.exists()


def test_resolver_must_return_exactly_one_target(tmp_path):
    injection = """
[profiles.client.injection]
enabled = true
target_strategy = "resolver-command"
target_resolver_command = ["/bin/printf", "one\\ntwo\\n"]
agent_command = ["/bin/true", "{target}", "{prompt}"]
"""
    _config, profile = write_config(tmp_path, injection=injection)
    profile.state_dir.mkdir(parents=True)
    profile.inbox_file.write_text(
        json.dumps({"messages": [{"text": "hello"}]}) + "\n", encoding="utf-8"
    )
    assert inject_pending(profile) == {"sent": 0, "failed": 1, "disabled": 0, "paused": 0}
    assert not profile.injection_state_file.exists()


def test_missing_profile_is_rejected(tmp_path):
    config, _profile = write_config(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        load_profile(config, "missing")
