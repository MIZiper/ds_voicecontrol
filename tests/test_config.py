"""配置读取/写入的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_control.config import (
    ConfigError,
    load_config,
    load_config_or_defaults,
    save_noise_threshold,
    write_default_config,
)


def test_init_and_load_default_config(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    config = load_config(path)
    assert config.asr.engine == "vosk"
    assert config.wake.words == ["小助手", "你好小助手"]
    assert config.audio.sample_rate == 16000
    assert len(config.rules) >= 10
    names = [rule.name for rule in config.rules]
    assert "复制" in names and "搜索" in names
    copy_rule = next(rule for rule in config.rules if rule.name == "复制")
    assert copy_rule.phrases == ["复制", "拷贝"]
    assert copy_rule.action == {"type": "hotkey", "keys": ["ctrl", "c"]}
    assert config.problems() == []


def test_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    with pytest.raises(ConfigError):
        write_default_config(path)
    assert write_default_config(path, force=True).exists()


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_load_config_or_defaults_falls_back(tmp_path: Path) -> None:
    config = load_config_or_defaults(tmp_path / "nope.toml")
    assert config.rules, "缺省配置也应该带内置规则"
    assert any("默认配置" in message for message in config.warnings)


def test_unknown_key_and_wrong_type_are_warnings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[audio]
sample_rate = "16000"
nonsense = 1

[commands]
unknown = "log"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.audio.sample_rate == 16000  # 类型不对 -> 用默认值
    messages = "\n".join(config.warnings)
    assert "audio.nonsense" in messages
    assert "audio.sample_rate" in messages


def test_invalid_rules_are_skipped_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[wake]
words = ["小助手"]

[[rules]]
name = "没有动作"
phrases = ["喂"]

[[rules]]
name = "动作类型错"
phrases = ["喂喂"]
type = "teleport"

[[rules]]
name = "正常"
phrases = ["复制"]
type = "hotkey"
keys = ["ctrl", "c"]
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert [rule.name for rule in config.rules] == ["正常"]
    messages = "\n".join(config.warnings)
    assert "没有动作" in messages
    assert "teleport" in messages


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


def test_save_noise_threshold_keeps_comments(tmp_path: Path) -> None:
    path = write_default_config(tmp_path / "config.toml")
    save_noise_threshold(0.00321, path)
    text = path.read_text(encoding="utf-8")
    assert "noise_threshold = 0.00321" in text
    assert "# 静音门限" in text  # 注释没有被 tomlkit 丢掉
    assert load_config(path).audio.noise_threshold == pytest.approx(0.00321)


def test_config_problems_detects_bad_engine(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[asr]
engine = "magic"

[wake]
words = []
""",
        encoding="utf-8",
    )
    config = load_config(path)
    problems = "\n".join(config.problems())
    assert "asr.engine" in problems
    assert "wake.words" in problems


def test_rule_summary_and_action_table_form(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[rules]]
name = "表格式动作"
phrases = ["音量加"]
action = { type = "press", keys = ["volumeup"], repeat = 3 }
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert len(config.rules) == 1
    assert config.rules[0].summary() == "press: volumeup x3"
