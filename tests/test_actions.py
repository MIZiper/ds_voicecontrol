"""动作执行测试（全部 dry-run，不会真的按键）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voice_control.actions import (
    EXTENDED_VKS,
    ActionContext,
    ActionError,
    ActionExecutor,
    _integrity_name,
    foreground_window,
    known_key_names,
    process_integrity_level,
    resolve_key,
    window_process_id,
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
    context = ActionContext(dry_run=True, text="今天天气", slots={"query": "今天天气"})
    results = _executor().execute({"type": "text", "text": "搜索：{query}"}, context)
    assert results[0].ok
    assert "搜索：今天天气" in results[0].detail


def test_named_slot_placeholder() -> None:
    context = ActionContext(dry_run=True, slots={"text": "你好世界"})
    results = _executor().execute({"type": "text", "text": "{text}"}, context)
    assert results[0].ok
    assert "你好世界" in results[0].detail


def test_declared_slot_missing_is_empty() -> None:
    """规则声明了 query 槽位但模型没给 -> 渲染成空，宁可报错也不要输入 "{query}"。"""
    context = ActionContext(dry_run=True, declared_slots=("query",))
    results = _executor().execute({"type": "text", "text": "{query}"}, context)
    assert not results[0].ok
    assert "为空" in results[0].detail


def test_unknown_placeholder_is_kept_literal() -> None:
    results = _executor().execute({"type": "text", "text": "a{nope}b"})
    assert "a{nope}b" in results[0].detail


def test_empty_slot_marks_failure() -> None:
    results = _executor().execute({"type": "text", "text": "{query}"}, ActionContext(dry_run=True, declared_slots=("query",)))
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
            {"type": "text", "text": "{query}"},
            {"type": "press", "keys": ["enter"]},
        ],
    }
    results = _executor().execute(action, ActionContext(dry_run=True, slots={"query": "天气"}))
    assert all(item.ok for item in results)
    assert [item.type for item in results] == ["hotkey", "text", "press", "sequence"]


def test_sequence_stops_after_failure() -> None:
    action = {
        "type": "sequence",
        "steps": [
            {"type": "hotkey", "keys": []},
            {"type": "press", "keys": ["enter"]},
        ],
    }
    results = _executor().execute(action)
    assert "press" not in [item.type for item in results]
    assert any("后续步骤已跳过" in item.detail for item in results)


def test_focus_dry_run() -> None:
    results = _executor().execute({"type": "focus", "title": "记事本"})
    assert results[0].ok
    assert "记事本" in results[0].detail


def test_focus_requires_title() -> None:
    results = _executor().execute({"type": "focus"})
    assert not results[0].ok
    assert "title" in results[0].detail


def test_focus_requires_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.name", "posix")
    executor = _executor(dry_run=False)
    with pytest.raises(ActionError):
        executor._focus_window("记事本")


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


def test_key_resolution_errors_are_reported() -> None:
    results = _executor().execute({"type": "press", "keys": ["not-a-key"]}, ActionContext(dry_run=False))
    assert not results[0].ok
    assert "不认识的按键名" in results[0].detail


def test_known_key_names_contains_expected() -> None:
    names = known_key_names()
    for name in ("ctrl", "alt", "shift", "win", "enter", "pageup", "volumeup", "playpause"):
        assert name in names


# --------------------------------------------------------------------------- #
# 按键名 -> 虚拟键码（纯逻辑，不真的发键）
# --------------------------------------------------------------------------- #
def test_resolve_named_keys() -> None:
    assert resolve_key("ctrl").value == 0x11
    assert resolve_key("win").value == 0x5B
    assert resolve_key("enter").value == 0x0D
    assert resolve_key("pageup").value == 0x21
    assert resolve_key("volumeup").value == 0xAF
    assert resolve_key("playpause").value == 0xB3
    assert resolve_key("ctrl").is_modifier
    assert not resolve_key("enter").is_modifier


def test_resolve_function_keys() -> None:
    assert resolve_key("f1").value == 0x70
    assert resolve_key("f12").value == 0x7B
    assert resolve_key("f24").value == 0x87


def test_resolve_letters_and_digits() -> None:
    assert resolve_key("a").value == ord("A")
    assert resolve_key("T").value == ord("T")
    assert resolve_key("5").value == ord("5")


@pytest.mark.skipif(os.name != "nt", reason="符号键需要 Windows 的 VkKeyScan")
def test_resolve_symbol_needs_shift() -> None:
    spec = resolve_key("+")
    assert spec.kind == "vk"
    assert spec.shift is True
    assert resolve_key("/").kind in ("vk", "char")


def test_resolve_unknown_key_raises() -> None:
    with pytest.raises(ActionError):
        resolve_key("这个键不存在")


def test_extended_keys_are_flagged() -> None:
    for vk in (0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x2E, 0x5B, 0xAF):
        assert vk in EXTENDED_VKS
    assert 0x0D not in EXTENDED_VKS  # 回车不是扩展键


# --------------------------------------------------------------------------- #
# 完整性级别诊断（用来判断“日志成功但没反应”）
# --------------------------------------------------------------------------- #
def test_integrity_name_mapping() -> None:
    assert _integrity_name(0x0000) == "Untrusted"
    assert _integrity_name(0x1000) == "Low"
    assert _integrity_name(0x2000) == "Medium"
    assert _integrity_name(0x3000) == "High"
    assert _integrity_name(0x4000) == "System"


@pytest.mark.skipif(os.name != "nt", reason="完整性级别是 Windows 概念")
def test_process_integrity_level_returns_known_label() -> None:
    level = process_integrity_level()
    assert level in {"Untrusted", "Low", "Medium", "High", "System", "Protected", "Unknown"}


@pytest.mark.skipif(os.name != "nt", reason="窗口查询是 Windows 专用")
def test_window_probe_helpers_do_not_crash() -> None:
    hwnd, title = foreground_window()
    assert isinstance(title, str)
    assert isinstance(window_process_id(hwnd), int)
