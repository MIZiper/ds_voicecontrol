"""监听流程（唤醒词 -> 口令 -> 动作）的单元测试，用假引擎避免依赖麦克风/模型。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_control.actions import ActionExecutor, ActionContext
from voice_control.audio import Utterance
from voice_control.config import ACTION_TYPES, Config, load_config, write_default_config
from voice_control.listener import Listener


class FakeEngine:
    name = "fake"

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[bytes] = []

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        self.calls.append(pcm)
        return self.text

    def close(self) -> None:  # pragma: no cover - 协议要求
        pass


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    path = write_default_config(tmp_path / "config.toml")
    return load_config(path)


def _listener(config: Config, **kwargs) -> Listener:
    executor = ActionExecutor(allow=ACTION_TYPES, dry_run=True)
    return Listener(config, engine=FakeEngine(), executor=executor, **kwargs)


def test_wake_word_with_inline_command(config: Config) -> None:
    listener = _listener(config)
    assert listener.handle_text("小助手复制") == "executed:复制"


def test_wake_word_only_opens_window(config: Config) -> None:
    listener = _listener(config)
    assert listener.handle_text("小助手") == "wake-only"
    # 唤醒窗口内的下一句话直接当口令
    assert listener.handle_text("粘贴") == "executed:粘贴"


def test_command_without_wake_word_is_ignored(config: Config) -> None:
    listener = _listener(config)
    assert listener.handle_text("复制") == "ignored"


def test_no_wake_mode_executes_directly(config: Config) -> None:
    listener = _listener(config)
    listener.require_wake = False
    assert listener.handle_text("保存") == "executed:保存"


def test_wake_disabled_in_config(config: Config) -> None:
    config.wake.enabled = False
    listener = _listener(config)
    assert listener.handle_text("全选") == "executed:全选"


def test_unknown_command_after_wake(config: Config) -> None:
    listener = _listener(config)
    assert listener.handle_text("小助手 来一份炸鸡") == "unknown"
    assert listener.handle_text("小助手 复制") == "executed:复制"


def test_cooldown_blocks_repeat(config: Config) -> None:
    config.commands.cooldown_ms = 5000
    listener = _listener(config)
    assert listener.handle_text("小助手复制") == "executed:复制"
    assert listener.handle_text("小助手复制") == "cooldown"


def test_empty_text(config: Config) -> None:
    listener = _listener(config)
    assert listener.handle_text("   ") == "empty"


def test_sequence_with_slot(config: Config) -> None:
    listener = _listener(config)
    status = listener.handle_text("小助手 搜索 今天天气")
    assert status == "executed:搜索"


def test_process_utterance_uses_engine(config: Config) -> None:
    engine = FakeEngine("小助手复制")
    executor = ActionExecutor(allow=ACTION_TYPES, dry_run=True)
    listener = Listener(config, engine=engine, executor=executor)
    status = listener.process_utterance(Utterance(pcm=b"\x00\x00" * 160, duration_ms=300, voiced_ms=300, peak_rms=0.5))
    assert status == "executed:复制"
    assert engine.calls, "应该把音频交给识别引擎"


def test_segmenter_from_config(config: Config) -> None:
    listener = _listener(config)
    segmenter = listener.build_segmenter()
    assert segmenter.threshold == config.audio.noise_threshold
    assert segmenter.frame_samples == int(config.audio.sample_rate * config.audio.frame_ms / 1000)


def test_disabled_rule_not_matched(config: Config) -> None:
    for rule in config.rules:
        if rule.name == "复制":
            rule.enabled = False
    listener = _listener(config)
    assert listener.handle_text("小助手复制") == "unknown"


def test_executor_allowlist_is_respected(config: Config) -> None:
    config.commands.allow = ["press"]
    executor = ActionExecutor(allow=tuple(config.commands.allow), dry_run=True)
    listener = Listener(config, engine=FakeEngine(), executor=executor)
    assert listener.handle_text("小助手复制") == "failed:复制"
