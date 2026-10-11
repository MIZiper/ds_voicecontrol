"""动作执行的单元测试。

所有测试都用 dry_run=True，绝不会真的按键或启动程序。
"""

from __future__ import annotations

from pathlib import Path

from voice_control.actions import (
    ActionContext,
    ActionExecutor,
    known_key_names,
)
from voice_control.config import ACTION_TYPES


def _executor(**kwargs) -> ActionExecutor:
    params = dict(allow=ACTION_TYPES, allow_shell=False, dry_run=True)
    params.update(kwargs)
    return ActionExecutor(**params)


def test_hotkey_dry_run() -> None:
    results = _executor().execute({"type": "hotkey", "keys": ["ctrl", "shift", "t"]})
    assert len(results) == 1
    assert results[0].ok
    assert "ctrl+shift+t" in results[0].detail


def test_press_with_repeat() -> None:
    results = _executor().execute({"type": "press", "keys": ["volumeup"], "repeat": 3})
    assert results[0].ok
    assert "x3" in results[0].detail


def test_text_uses_slot() -> None:
    context = ActionContext(dry_run=True, text="今天天气")
    results = _executor().execute({"type": "text", "text": "搜索：{text}"}, context)
    assert results[0].ok
    assert "搜索：今天天气" in results[0].detail


def test_text_empty_slot_fails() -> None:
    results = _executor().execute({"type": "text", "text": "{text}"})
    assert not results[0].ok
    assert "为空" in results[0].detail


def test_date_placeholder() -> None:
    results = _executor().execute({"type": "text", "text": "今天是{date}"})
    assert results[0].ok
    assert "今天是2" in results[0].detail


def test_sequence_runs_all_steps() -> None:
    action = {
        "type": "sequence",
        "steps": [
            {"type": "hotkey", "keys": ["ctrl", "t"]},
            {"type": "text", "text": "{text}"},
            {"type": "press", "keys": ["enter"]},
        ],
    }
    results = _executor().execute(action, ActionContext(dry_run=True, text="天气"))
    assert all(item.ok for item in results)
    kinds = [item.type for item in results]
    assert kinds == ["hotkey", "text", "press", "sequence"]


def test_sequence_stops_after_failure() -> None:
    action = {
        "type": "sequence",
        "steps": [
            {"type": "hotkey", "keys": []},
            {"type": "press", "keys": ["enter"]},
        ],
    }
    results = _executor().execute(action)
    kinds = [item.type for item in results]
    assert "press" not in kinds
    assert any("后续步骤已跳过" in item.detail for item in results)


def test_shell_blocked_by_allow_shell() -> None:
    results = _executor().execute({"type": "shell", "command": "echo hi"})
    assert not results[0].ok
    assert "allow_shell" in results[0].detail


def test_shell_blocked_by_whitelist() -> None:
    executor = _executor(allow=("hotkey",), allow_shell=True)
    results = executor.execute({"type": "shell", "command": "echo hi"})
    assert not results[0].ok
    assert "白名单" in results[0].detail


def test_unknown_action_type() -> None:
    results = _executor().execute({"type": "teleport"})
    assert not results[0].ok
    assert "未知动作类型" in results[0].detail


def test_wait_dry_run_does_not_sleep() -> None:
    import time

    started = time.monotonic()
    results = _executor().execute({"type": "wait", "ms": 5000})
    assert results[0].ok
    assert time.monotonic() - started < 1.0


def test_app_dry_run(tmp_path: Path) -> None:
    results = _executor().execute({"type": "app", "target": "https://example.com"})
    assert results[0].ok
    assert "example.com" in results[0].detail


def test_known_key_names_contains_expected() -> None:
    names = known_key_names()
    for name in ("ctrl", "alt", "shift", "win", "enter", "pageup", "volumeup", "playpause"):
        assert name in names
