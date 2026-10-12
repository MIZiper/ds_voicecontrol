"""配置读取/写入测试（新架构：本地唤醒 + 大模型理解 + 命令表）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_control.config import (
    ACTION_TYPES,
    ConfigError,
    load_config,
    load_config_or_defaults,
    save_noise_threshold,
    write_default_config,
)


def test_default_config_has_wake_llm_and_rules(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    config = load_config(path)
    assert config.wake.words == ["牛来"]
    assert config.wake.model.startswith("sherpa-onnx-kws")
    assert config.llm.base_url.startswith("https://dashscope.aliyuncs.com")
    assert config.llm.api_key_env == "DASHSCOPE_API_KEY"
    assert config.audio.sample_rate == 16000
    assert len(config.rules) >= 20


def test_default_config_has_new_action_types(tmp_path: Path) -> None:
    config = load_config(write_default_config(tmp_path / "config.toml"))
    for kind in ("url", "click", "seek"):
        assert kind in ACTION_TYPES
        assert kind in config.commands.allow


def test_default_config_has_web_and_site_rules(tmp_path: Path) -> None:
    config = load_config(write_default_config(tmp_path / "config.toml"))
    for rule_id in (
        "open_site",
        "page_back",
        "page_forward",
        "refresh",
        "tab_next",
        "tab_prev",
        "web_play_pause",
        "bili_seek",
        "bili_speed_1x",
        "bili_speed_2x",
        "fullscreen_toggle",
        "fullscreen_browser",
        "exit_fullscreen",
    ):
        assert config.rule_by_id(rule_id) is not None, rule_id
    assert config.rule_by_id("open_site").slots == ["url"]
    assert config.rule_by_id("open_site").action == {"type": "url", "target": "{url}"}
    assert config.rule_by_id("page_back").action["keys"] == ["alt", "left"]
    assert config.rule_by_id("page_forward").action["keys"] == ["alt", "right"]
    assert config.rule_by_id("refresh").action["keys"] == ["f5"]
    assert config.rule_by_id("tab_next").action["keys"] == ["ctrl", "tab"]
    assert config.rule_by_id("tab_prev").action["keys"] == ["ctrl", "shift", "tab"]
    assert config.rule_by_id("web_play_pause").action["keys"] == ["space"]
    assert config.rule_by_id("bili_seek").action["slot"] == "sec"
    assert config.rule_by_id("bili_speed_2x").action["keys"] == ["shift", "2"]
    assert config.rule_by_id("fullscreen_toggle").action["keys"] == ["f"]
    assert config.rule_by_id("fullscreen_browser").action["keys"] == ["f11"]
    assert config.rule_by_id("exit_fullscreen").action["keys"] == ["esc"]


def test_bad_click_rule_is_skipped_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[rules]]
id = "skip"
name = "跳过"
phrases = ["跳过"]
type = "click"
x = 100
button = "脚"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.rule_by_id("skip") is None
    warnings = "\n".join(config.warnings)
    assert "y" in warnings
    assert "button" in warnings


def test_rules_have_id_description_and_slots(tmp_path: Path) -> None:
    config = load_config(write_default_config(tmp_path / "config.toml"))
    search = config.rule_by_id("search")
    assert search is not None
    assert search.name == "搜索"
    assert "搜索" in search.description
    assert search.slots == ["query"]
    assert search.phrases  # 示例说法

    copy_rule = config.rule_by_id("copy")
    assert copy_rule is not None and copy_rule.action == {"type": "hotkey", "keys": ["ctrl", "c"]}
    assert config.rule_by_id("不存在") is None


def test_disabled_rule_is_not_returned_by_id(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[rules]]
id = "copy"
name = "复制"
phrases = ["复制"]
type = "hotkey"
keys = ["ctrl", "c"]
enabled = false
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.rule_by_id("copy") is None
    assert config.enabled_rules() == []


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_load_config_or_defaults_falls_back(tmp_path: Path) -> None:
    config = load_config_or_defaults(tmp_path / "nope.toml")
    assert config.rules
    assert any("默认配置" in message for message in config.warnings)


def test_unknown_key_and_wrong_type_are_warnings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[audio]
sample_rate = "16000"
nonsense = 1

[llm]
model = "qwen-audio-turbo-latest"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.audio.sample_rate == 16000
    messages = "\n".join(config.warnings)
    assert "audio.nonsense" in messages
    assert "audio.sample_rate" in messages


def test_old_asr_section_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[asr]
engine = "vosk"

[[rules]]
id = "copy"
name = "复制"
phrases = ["复制"]
type = "hotkey"
keys = ["ctrl", "c"]
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert any("[asr]" in message for message in config.warnings)


def test_invalid_rules_are_skipped_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[rules]]
name = "没有动作"
phrases = ["喂"]

[[rules]]
name = "动作类型错"
phrases = ["喂喂"]
type = "teleport"

[[rules]]
name = "没有示例"
phrases = []

[[rules]]
name = "正常"
id = "copy"
phrases = ["复制"]
type = "hotkey"
keys = ["ctrl", "c"]
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert [rule.id for rule in config.rules] == ["copy"]
    messages = "\n".join(config.warnings)
    assert "没有动作" in messages
    assert "teleport" in messages
    assert "没有示例" in messages


def test_duplicate_rule_id_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[rules]]
id = "copy"
name = "复制"
phrases = ["复制"]
type = "hotkey"
keys = ["ctrl", "c"]

[[rules]]
id = "copy"
name = "另一个复制"
phrases = ["再复制"]
type = "hotkey"
keys = ["ctrl", "insert"]
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert len(config.rules) == 1
    assert any("重复" in message for message in config.warnings)


def test_nested_sequence_validation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[rules]]
name = "组合"
phrases = ["搜索"]
type = "sequence"
steps = [
  { type = "hotkey", keys = ["ctrl", "t"] },
  { type = "press" },
]
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.rules == []
    assert "steps[1]" in "\n".join(config.warnings)


def test_focus_action_needs_title(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[rules]]
name = "切窗口"
phrases = ["切到记事本"]
type = "focus"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.rules == []
    assert "title" in "\n".join(config.warnings)


def test_save_noise_threshold_keeps_comments(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    save_noise_threshold(0.00321, path)
    text = path.read_text(encoding="utf-8")
    assert "noise_threshold = 0.00321" in text
    assert "# 静音门限" in text
    assert load_config(path).audio.noise_threshold == pytest.approx(0.00321)


def test_api_key_from_config_or_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(write_default_config(tmp_path / "config.toml"))
    monkeypatch.delenv(config.llm.api_key_env, raising=False)
    assert config.resolve_api_key() == ""
    monkeypatch.setenv(config.llm.api_key_env, "env-key")
    assert config.resolve_api_key() == "env-key"
    config.llm.api_key = "inline-key"
    assert config.resolve_api_key() == "inline-key"


def test_problems_detect_missing_key_and_empty_rules(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[wake]
words = ["小助手"]

[llm]
api_key_env = "VC_TEST_NOT_SET_KEY"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    problems = "\n".join(config.problems())
    assert "API Key" in problems
    assert "没有任何可执行的命令" in problems


def test_problems_pass_for_default_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(write_default_config(tmp_path / "config.toml"))
    monkeypatch.setenv(config.llm.api_key_env, "sk-test")
    assert config.problems() == []


def test_models_dir_resolves_next_to_config(tmp_path: Path) -> None:
    config = load_config(write_default_config(tmp_path / "config.toml"))
    assert config.models_dir == tmp_path / "models"
    assert config.wake_model_dir == tmp_path / "models" / config.wake.model
