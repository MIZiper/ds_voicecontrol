"""文本归一化、唤醒词与口令匹配的单元测试。"""

from __future__ import annotations

from dataclasses import dataclass

from voice_control.config import Rule
from voice_control.matching import find_wake_word, match_rules, view


def test_view_strips_punctuation_and_fillers() -> None:
    assert view("帮我复制一下。").norm == "复制"
    assert view("小助手，搜索 天气").norm == "小助手搜索天气"
    assert view("ＡＢＣ").norm == "abc"


def test_view_keeps_index_map() -> None:
    text = "搜索，今天天气"
    text_view = view(text)
    assert text_view.norm == "搜索今天天气"
    # 归一化后的第 2 个字符对应原文本里的 “今”
    assert text[text_view.index_map[2]] == "今"


def test_view_empty() -> None:
    assert view("").norm == ""
    assert not view("   ，。")


def test_find_wake_word_with_command() -> None:
    word, remainder = find_wake_word("小助手帮我复制", ["小助手", "你好小助手"])
    assert word == "小助手"
    assert remainder == "帮我复制"


def test_find_wake_word_only() -> None:
    assert find_wake_word("你好，小助手！", ["小助手"]) == ("小助手", "")


def test_find_wake_word_middle_of_sentence() -> None:
    word, remainder = find_wake_word("那个小助手保存一下", ["小助手"])
    assert word == "小助手"
    assert remainder == "保存一下"


def test_find_wake_word_fuzzy() -> None:
    word, _ = find_wake_word("小助手", ["小助手"], fuzzy=0.82)
    assert word == "小助手"
    # 完全不相关的内容不应该被唤醒
    assert find_wake_word("今天天气不错", ["小助手"])[0] is None


def test_find_wake_word_without_wake() -> None:
    assert find_wake_word("复制", ["小助手"])[0] is None


@dataclass
class _Rules:
    rules: list[Rule]


def _rules(*items: tuple[str, list[str], dict]) -> list[Rule]:
    return [Rule(name=name, phrases=phrases, action=action) for name, phrases, action in items]


def test_match_exact_and_remainder() -> None:
    rules = _rules(
        ("搜索", ["搜索"], {"type": "text", "text": "{text}"}),
        ("复制", ["复制"], {"type": "hotkey", "keys": ["ctrl", "c"]}),
    )
    match = match_rules("搜索今天天气", rules)
    assert match is not None
    assert match.name == "搜索"
    assert match.remainder == "今天天气"


def test_match_prefers_longer_phrase() -> None:
    rules = _rules(
        ("复制", ["复制"], {"type": "hotkey", "keys": ["ctrl", "c"]}),
        ("复制到", ["复制到剪贴板"], {"type": "hotkey", "keys": ["ctrl", "shift", "c"]}),
    )
    match = match_rules("复制到剪贴板", rules)
    assert match is not None
    assert match.name == "复制到"


def test_match_respects_disabled_rule() -> None:
    rule = Rule(name="复制", phrases=["复制"], action={"type": "hotkey", "keys": ["ctrl", "c"]}, enabled=False)
    assert match_rules("复制", [rule]) is None


def test_match_none_when_unrelated() -> None:
    rules = _rules(("复制", ["复制"], {"type": "hotkey", "keys": ["ctrl", "c"]}))
    assert match_rules("打开冰箱", rules) is None


def test_match_threshold_can_disable_fuzzy() -> None:
    rules = _rules(("保存", ["保存文件"], {"type": "hotkey", "keys": ["ctrl", "s"]}))
    assert match_rules("保存文", rules, threshold=0.5) is not None
    assert match_rules("保存文", rules, threshold=0.99) is None
