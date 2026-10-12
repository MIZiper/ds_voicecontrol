"""监听状态机测试：本地唤醒词 -> 只把「唤醒后的那句口令」交给大模型 -> 本地执行动作。

用假的唤醒词检测器和假的大模型，不依赖麦克风、模型文件或网络。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from voice_control.actions import ActionExecutor
from voice_control.config import Config, load_config, write_default_config
from voice_control.kws import KwsHit
from voice_control.listener import CAPTURING, LISTENING, WAITING, Listener
from voice_control.llm import Decision, LlmError

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def loud(amplitude: float = 0.3) -> bytes:
    t = np.arange(FRAME_SAMPLES) / SAMPLE_RATE
    wave = (np.sin(2 * math.pi * 220 * t) * amplitude * 32767).astype("<i2")
    return wave.tobytes()


def quiet() -> bytes:
    return b"\x00\x00" * FRAME_SAMPLES


class FakeSpotter:
    """在第 ``hit_at`` 帧命中一次唤醒词。"""

    def __init__(self, hit_at: int | None = None, keyword: str = "小助手") -> None:
        self.hit_at = hit_at
        self.keyword = keyword
        self.frames = 0
        self.resets = 0

    def feed_frame(self, frame: bytes) -> KwsHit | None:
        self.frames += 1
        if self.hit_at is not None and self.frames == self.hit_at:
            return KwsHit(keyword=self.keyword)
        return None

    def reset(self) -> None:
        self.resets += 1


class FakeLlm:
    def __init__(self, decision: Decision | None = None, error: str = "") -> None:
        self.decision = decision or Decision(command_id="copy", transcript="复制")
        self.error = error
        self.audio_calls: list[bytes] = []
        self.text_calls: list[str] = []

    def decide_audio(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Decision:
        if self.error:
            raise LlmError(self.error)
        self.audio_calls.append(pcm)
        return self.decision

    def decide_text(self, text: str) -> Decision:
        if self.error:
            raise LlmError(self.error)
        self.text_calls.append(text)
        return self.decision


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    path = write_default_config(tmp_path / "config.toml")
    cfg = load_config(path)
    cfg.feedback.beep = False
    return cfg


def build(config: Config, spotter=None, llm=None) -> Listener:
    executor = ActionExecutor(allow=tuple(config.commands.allow), dry_run=True)
    return Listener(
        config,
        spotter=spotter or FakeSpotter(),
        llm=llm or FakeLlm(),
        executor=executor,
    )


def feed_frames(listener: Listener, frames: list[bytes]) -> list[str]:
    return [listener.feed(frame) for frame in frames]


# --------------------------------------------------------------------------- #
# 基础：没唤醒就不该联网
# --------------------------------------------------------------------------- #
def test_no_wake_word_means_no_llm_call(config: Config) -> None:
    llm = FakeLlm()
    listener = build(config, spotter=FakeSpotter(hit_at=None), llm=llm)
    statuses = feed_frames(listener, [quiet()] * 10 + [loud()] * 20 + [quiet()] * 30)
    assert llm.audio_calls == []
    assert listener.stats["wake"] == 0
    assert set(statuses) == {"listening"}
    assert listener.state == LISTENING


def test_wake_word_then_command_sends_only_command_audio(config: Config) -> None:
    llm = FakeLlm(Decision(command_id="copy", transcript="复制"))
    listener = build(config, spotter=FakeSpotter(hit_at=61), llm=llm)
    frames = [quiet()] * 60 + [loud()] * 2 + [quiet()] * 2 + [loud()] * 10 + [quiet()] * 30
    feed_frames(listener, frames)

    assert listener.stats["wake"] == 1
    assert len(llm.audio_calls) == 1
    sent = llm.audio_calls[0]
    # 只发唤醒之后的那一小段，而不是前面 60 帧的静音 + 唤醒词
    assert 0 < len(sent) < 40 * FRAME_SAMPLES * 2
    assert listener.stats["command"] == 1
    assert listener.state == LISTENING


def test_wake_word_alone_only_opens_window(config: Config) -> None:
    llm = FakeLlm()
    listener = build(config, spotter=FakeSpotter(hit_at=31), llm=llm)
    feed_frames(listener, [quiet()] * 30 + [quiet()] * 40)
    assert listener.state == WAITING

    # 超过 wake.wait_ms（默认 5 秒）都没开口 -> 回到 listening，且没有请求大模型
    feed_frames(listener, [quiet()] * 200)
    assert listener.state == LISTENING
    assert llm.audio_calls == []
    assert listener.stats["wake"] == 1


def test_wake_word_tail_is_not_treated_as_command(config: Config) -> None:
    """唤醒词自己的尾音（在 200ms 冷却内）不能被当成口令发出去。"""
    llm = FakeLlm()
    listener = build(config, spotter=FakeSpotter(hit_at=31), llm=llm)
    frames = [quiet()] * 30 + [loud()] * 5 + [quiet()] * 60
    feed_frames(listener, frames)
    assert llm.audio_calls == []


def test_command_within_window_after_wake_word(config: Config) -> None:
    """先喊唤醒词，停一下再说口令，也应该能正常执行。"""
    llm = FakeLlm(Decision(command_id="paste", transcript="粘贴"))
    listener = build(config, spotter=FakeSpotter(hit_at=31), llm=llm)
    frames = (
        [quiet()] * 30          # 唤醒词之前的静音
        + [loud()] * 4          # 唤醒词（第 31 帧命中）
        + [quiet()] * 40        # 停顿 1.2 秒
        + [loud()] * 20         # 口令
        + [quiet()] * 30        # 句尾静音
    )
    feed_frames(listener, frames)
    assert len(llm.audio_calls) == 1
    assert listener.stats["command"] == 1


# --------------------------------------------------------------------------- #
# 不启用唤醒词
# --------------------------------------------------------------------------- #
def test_wake_disabled_sends_every_utterance(config: Config) -> None:
    config.wake.enabled = False
    llm = FakeLlm(Decision(command_id="copy", transcript="复制"))
    listener = build(config, spotter=FakeSpotter(hit_at=None), llm=llm)
    assert listener.state == CAPTURING
    feed_frames(listener, [loud()] * 20 + [quiet()] * 40)
    assert len(llm.audio_calls) == 1
    feed_frames(listener, [loud()] * 20 + [quiet()] * 40)
    assert len(llm.audio_calls) == 2


# --------------------------------------------------------------------------- #
# 决策处理
# --------------------------------------------------------------------------- #
def test_unknown_command_is_reported(config: Config) -> None:
    llm = FakeLlm(Decision(command_id=None, transcript="来一份炸鸡"))
    listener = build(config, spotter=FakeSpotter(hit_at=1), llm=llm)
    status = listener.handle_text("来一份炸鸡")
    assert status == "unknown"
    assert listener.stats["ignored"] == 1


def test_local_fallback_matches_transcript(config: Config) -> None:
    llm = FakeLlm(Decision(command_id=None, transcript="帮我复制一下"))
    listener = build(config, spotter=FakeSpotter(hit_at=1), llm=llm)
    assert config.commands.fallback_local is True
    assert listener.handle_text("帮我复制一下") == "executed:copy"


def test_local_fallback_can_be_disabled(config: Config) -> None:
    config.commands.fallback_local = False
    llm = FakeLlm(Decision(command_id=None, transcript="帮我复制一下"))
    listener = build(config, spotter=FakeSpotter(hit_at=1), llm=llm)
    assert listener.handle_text("帮我复制一下") == "unknown"


def test_slots_are_passed_to_action(config: Config) -> None:
    llm = FakeLlm(
        Decision(command_id="type_text", transcript="输入你好世界", slots={"text": "你好世界"})
    )
    listener = build(config, spotter=FakeSpotter(hit_at=1), llm=llm)
    assert listener.handle_text("输入你好世界") == "executed:type_text"


def test_allowlist_blocks_action(config: Config) -> None:
    config.commands.allow = ["press"]
    executor = ActionExecutor(allow=("press",), dry_run=True)
    llm = FakeLlm(Decision(command_id="copy", transcript="复制"))
    listener = Listener(config, spotter=FakeSpotter(), llm=llm, executor=executor)
    assert listener.handle_text("复制") == "failed:copy"


def test_cooldown_blocks_repeat(config: Config) -> None:
    config.commands.cooldown_ms = 5000
    llm = FakeLlm(Decision(command_id="copy", transcript="复制"))
    listener = build(config, spotter=FakeSpotter(hit_at=1), llm=llm)
    assert listener.handle_text("复制") == "executed:copy"
    assert listener.handle_text("复制") == "cooldown"


def test_llm_error_is_reported(config: Config) -> None:
    llm = FakeLlm(error="连接大模型失败：网络不可达")
    listener = build(config, spotter=FakeSpotter(hit_at=1), llm=llm)
    assert listener.handle_text("复制") == "llm-error"
    assert listener.stats["error"] == 1


def test_disabled_rule_is_not_executable(config: Config) -> None:
    for rule in config.rules:
        if rule.id == "copy":
            rule.enabled = False
    listener = Listener(
        config, spotter=FakeSpotter(), llm=FakeLlm(Decision(command_id="copy", transcript="复制"))
    )
    listener.catalog = type(listener.catalog).from_config(config)
    assert listener.handle_text("复制") == "unknown"


# --------------------------------------------------------------------------- #
# 段与门限
# --------------------------------------------------------------------------- #
def test_build_segmenter_uses_config(config: Config) -> None:
    listener = build(config)
    segmenter = listener.build_segmenter()
    assert segmenter.threshold == config.audio.noise_threshold
    assert segmenter.frame_samples == FRAME_SAMPLES
    capped = listener.build_segmenter(max_utterance_ms=3000)
    assert capped.max_utterance_ms == 3000


def test_level_gate_tracks_noise_floor(config: Config) -> None:
    from voice_control.audio import LevelGate

    gate = LevelGate(threshold=0.02, threshold_factor=3.0, adaptive=True)
    assert gate.effective == pytest.approx(0.02)
    assert gate.push(0.015) is False  # 低于门限
    for _ in range(300):
        gate.push(0.05)  # 环境变吵
    assert gate.effective > 0.02
    assert gate.push(0.05) is True
