from __future__ import annotations

import json
from pathlib import Path

from line_local_mcp.cockpit import _handle_input, _injection_badge, _width, toggle_injection
from line_local_mcp.pipeline import (
    _append_jsonl,
    inject_pending,
    injection_active,
    injection_switch,
    load_profile,
    set_injection_switch,
)


def make(tmp_path: Path, *, injection: bool) -> object:
    config = tmp_path / "pipeline.toml"
    block = (
        f"""
[profiles.client.injection]
enabled = true
target_strategy = "explicit"
target = "reviewer"
agent_command = ["{tmp_path / 'fake-agent.sh'}", "{{target}}", "{{prompt}}"]
"""
        if injection
        else ""
    )
    config.write_text(
        f"""
[profiles.client]
project_label = "Client"
contact = "Client"
state_dir = "{tmp_path / 'state'}"
todo_file = "{tmp_path / 'tasks.md'}"
[profiles.client.mcp]
command = ["/bin/true"]
{block}
""",
        encoding="utf-8",
    )
    script = tmp_path / "fake-agent.sh"
    script.write_text("#!/bin/sh\nprintf '%s\\n' \"$2\" >> \"$(dirname \"$0\")/delivered.log\"\n")
    script.chmod(0o700)
    return load_profile(config, "client")


def event(text: str) -> dict:
    return {"messages": [{"time": "2026-01-01 10:00", "from": "Client", "type": "text", "text": text}]}


def test_switch_off_pauses_without_consuming_backlog(tmp_path):
    profile = make(tmp_path, injection=True)
    _append_jsonl(profile.inbox_file, event("one"))
    assert injection_switch(profile) is None
    assert injection_active(profile)

    set_injection_switch(profile, False)
    assert inject_pending(profile) == {"sent": 0, "failed": 0, "disabled": 0, "paused": 1}
    assert not (tmp_path / "delivered.log").exists()
    assert not profile.injection_state_file.exists()  # nothing marked seen while paused


def test_switch_on_skips_backlog_and_delivers_only_new_events(tmp_path):
    profile = make(tmp_path, injection=True)
    _append_jsonl(profile.inbox_file, event("arrived while paused"))
    set_injection_switch(profile, False)
    result = set_injection_switch(profile, True)
    assert result["backlog_skipped"] == 1
    assert inject_pending(profile)["sent"] == 0  # backlog was not replayed

    _append_jsonl(profile.inbox_file, event("fresh"))
    assert inject_pending(profile)["sent"] == 1
    delivered = (tmp_path / "delivered.log").read_text()
    assert "fresh" in delivered and "arrived while paused" not in delivered


def test_unconfigured_profile_has_no_switch(tmp_path):
    profile = make(tmp_path, injection=False)
    assert not profile.injection_configured
    assert toggle_injection(profile) is None
    assert _injection_badge(profile, True)[0].strip() == "注入 —"
    assert inject_pending(profile)["disabled"] == 1


def test_dashboard_key_and_click_toggle(tmp_path):
    profile = make(tmp_path, injection=True)
    assert injection_active(profile)
    _handle_input("i", "pending", 0, 11, profile)
    assert injection_active(profile) is False
    assert json.loads(profile.injection_override_file.read_text())["enabled"] is False
    # arrow keys carry an ESC prefix and must not toggle
    _handle_input("\x1b[C", "pending", 0, 11, profile)
    assert injection_active(profile) is False
    # a click on the badge (just right of the two tabs) toggles back on
    x = (
        _width(" 進行中 99 ")
        + 2
        + _width(" 待辦 99 ")
        + 2
        + _width(" 已完成 99 ")
        + 2
        + 3
    )
    _handle_input(f"\x1b[<0;{x};11M", "pending", 0, 11, profile)
    assert injection_active(profile) is True
