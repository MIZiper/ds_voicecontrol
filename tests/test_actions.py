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
    format_seconds,
    known_key_names,
    normalize_url,
    normalized_point,
    parse_seconds,
    process_integrity_level,
    resolve_key,
    window_process_id,
    with_time_param,
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


# --------------------------------------------------------------------------- #
# 打开网址（url 动作）
# --------------------------------------------------------------------------- #
def test_normalize_url_adds_https() -> None:
    assert normalize_url("bilibili.com") == "https://bilibili.com"
    assert normalize_url("www.baidu.com/s?wd=x") == "https://www.baidu.com/s?wd=x"
    assert normalize_url("  github.com/x/y  ") == "https://github.com/x/y"


def test_normalize_url_keeps_existing_scheme() -> None:
    assert normalize_url("https://www.zhihu.com/question/1") == "https://www.zhihu.com/question/1"
    assert normalize_url("http://example.com") == "http://example.com"


@pytest.mark.parametrize("bad", ["百度", "notepad", "file:///c:/x", "百度的.com", "。。"])
def test_normalize_url_rejects_non_url(bad: str) -> None:
    with pytest.raises(ActionError):
        normalize_url(bad)


def test_url_action_dry_run() -> None:
    context = ActionContext(dry_run=True, slots={"url": "bilibili.com"})
    results = _executor().execute({"type": "url", "target": "{url}"}, context)
    assert results[0].ok
    assert "https://bilibili.com" in results[0].detail


def test_url_action_reports_bad_slot() -> None:
    context = ActionContext(dry_run=True, slots={"url": "百度"})
    results = _executor().execute({"type": "url", "target": "{url}"}, context)
    assert not results[0].ok
    assert "网址" in results[0].detail


# --------------------------------------------------------------------------- #
# 跳到指定时间（seek 动作）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("200", 200),
        (200, 200),
        ("3:20", 200),
        ("1:02:03", 3723),
        ("3分20秒", 200),
        ("三分二十秒", 200),
        ("一分半", 90),
        ("半小时", 1800),
        ("1小时2分", 3720),
    ],
)
def test_parse_seconds(value: object, expected: int) -> None:
    assert parse_seconds(value) == expected


def test_parse_seconds_rejects_garbage() -> None:
    with pytest.raises(ActionError):
        parse_seconds("随便说点什么")


def test_format_seconds() -> None:
    assert format_seconds(200) == "3:20"
    assert format_seconds(59) == "0:59"
    assert format_seconds(3723) == "1:02:03"


def test_with_time_param_replaces_and_keeps_others() -> None:
    url = with_time_param("https://www.bilibili.com/video/BV1x?p=2&t=30&spm=a.b", 200)
    assert url == "https://www.bilibili.com/video/BV1x?p=2&spm=a.b&t=200"


def test_with_time_param_appends_when_missing() -> None:
    assert with_time_param("https://www.bilibili.com/video/BV1x", 90).endswith("?t=90")


def test_seek_dry_run_uses_slot() -> None:
    context = ActionContext(dry_run=True, slots={"sec": "3分20秒"})
    results = _executor().execute({"type": "seek", "slot": "sec"}, context)
    assert results[0].ok
    assert "3:20" in results[0].detail


def test_seek_without_time_fails() -> None:
    context = ActionContext(dry_run=True, declared_slots=("sec",))
    results = _executor().execute({"type": "seek", "slot": "sec"}, context)
    assert not results[0].ok
    assert "sec" in results[0].detail


def test_seek_literal_seconds() -> None:
    results = _executor().execute({"type": "seek", "seconds": 125})
    assert results[0].ok
    assert "2:05" in results[0].detail


# --------------------------------------------------------------------------- #
# 模拟点击（click 动作）
# --------------------------------------------------------------------------- #
def test_normalized_point_maps_corners() -> None:
    assert normalized_point(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert normalized_point(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)


def test_normalized_point_handles_negative_origin() -> None:
    """多显示器时虚拟屏幕原点可能是负数。"""
    assert normalized_point(-1920, 0, -1920, 0, 3840, 1080) == (0, 0)
    assert normalized_point(0, 0, -1920, 0, 3840, 1080)[0] > 32000


def test_click_dry_run() -> None:
    results = _executor().execute({"type": "click", "x": 1850, "y": 60})
    assert results[0].ok
    assert "(1850, 60)" in results[0].detail


def test_click_double_dry_run() -> None:
    results = _executor().execute({"type": "click", "x": 960, "y": 540, "count": 2})
    assert results[0].ok
    assert "x2" in results[0].detail


def test_click_missing_coordinates_fails() -> None:
    results = _executor().execute({"type": "click", "x": 10})
    assert not results[0].ok
    assert "x 和 y" in results[0].detail


def test_click_bad_button_fails() -> None:
    results = _executor().execute({"type": "click", "x": 10, "y": 10, "button": "脚"})
    assert not results[0].ok
    assert "鼠标键" in results[0].detail
