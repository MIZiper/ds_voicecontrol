"""音频分段（VAD-lite）与底噪统计的单元测试。

分段逻辑不碰声卡，因此可以用合成的 int16 帧直接测。
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from voice_control.audio import (
    NoiseStats,
    Segmenter,
    frames_of,
    read_wav_mono,
    rms_of,
)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def tone(amplitude: float, samples: int = FRAME_SAMPLES, frequency: float = 220.0) -> bytes:
    """生成一段正弦波 int16 帧，amplitude 是 0~1 的峰值比例。"""
    t = np.arange(samples) / SAMPLE_RATE
    wave = (np.sin(2 * math.pi * frequency * t) * amplitude * 32767).astype("<i2")
    return wave.tobytes()


def silence(samples: int = FRAME_SAMPLES) -> bytes:
    return b"\x00\x00" * samples


def test_rms_of_silence_and_tone() -> None:
    assert rms_of(silence()) == pytest.approx(0.0, abs=1e-6)
    assert rms_of(tone(0.5)) == pytest.approx(0.5 / math.sqrt(2), abs=0.02)


def _segmenter(**kwargs) -> Segmenter:
    params = dict(
        sample_rate=SAMPLE_RATE,
        frame_ms=FRAME_MS,
        threshold=0.02,
        pre_roll_ms=90,
        min_speech_ms=90,
        max_utterance_ms=2000,
        silence_ms=150,
        adaptive_noise=False,
    )
    params.update(kwargs)
    return Segmenter(**params)


def test_detects_one_utterance() -> None:
    seg = _segmenter()
    results = []
    for frame in [silence()] * 5 + [tone(0.3)] * 10 + [silence()] * 10:
        utterance = seg.feed(frame)
        if utterance is not None:
            results.append(utterance)
    assert len(results) == 1
    utterance = results[0]
    assert utterance.reason == "silence"
    # 10 帧说话全部算作有效语音
    assert utterance.voiced_ms == pytest.approx(10 * FRAME_MS)
    # 前面带一点前置缓冲、后面带一点用于判定结束的静音，但不会无限长
    assert 10 * FRAME_MS <= utterance.duration_ms <= 10 * FRAME_MS + 90 + 150


def test_short_blip_is_ignored() -> None:
    seg = _segmenter(min_speech_ms=300)
    results = [seg.feed(frame) for frame in [silence()] * 3 + [tone(0.3)] * 2 + [silence()] * 20]
    assert all(item is None for item in results)


def test_max_utterance_forces_flush() -> None:
    seg = _segmenter(max_utterance_ms=300, silence_ms=10_000)
    results = []
    for frame in [tone(0.3)] * 10:
        utterance = seg.feed(frame)
        if utterance is not None:
            results.append(utterance)
    assert len(results) == 1
    assert results[0].reason == "max-length"
    assert results[0].duration_ms == pytest.approx(300, abs=FRAME_MS)


def test_long_speech_is_split_into_pieces() -> None:
    seg = _segmenter(max_utterance_ms=300, silence_ms=10_000)
    results = [seg.feed(frame) for frame in [tone(0.3)] * 20]
    assert len([item for item in results if item is not None]) == 2


def test_flush_returns_pending_audio() -> None:
    seg = _segmenter()
    for frame in [tone(0.3)] * 5:
        assert seg.feed(frame) is None
    utterance = seg.flush()
    assert utterance is not None
    assert utterance.voiced_ms >= 90


def test_adaptive_noise_raises_threshold() -> None:
    seg = _segmenter(threshold=0.02, adaptive_noise=True, threshold_factor=3.0)
    base = seg.effective_threshold
    # 持续喂入高于门限以下、但会抬高底噪的噪声
    for _ in range(200):
        seg.feed(tone(0.015))
    assert seg.effective_threshold > base
    assert seg.noise_floor > 0.005


def test_frames_of_splits_pcm() -> None:
    pcm = tone(0.2, samples=FRAME_SAMPLES * 3 + 10)
    chunks = list(frames_of(pcm, FRAME_SAMPLES))
    assert len(chunks) == 3
    assert all(len(chunk) == FRAME_SAMPLES * 2 for chunk in chunks)


def test_read_wav_mono_resamples(tmp_path) -> None:
    import wave

    path = tmp_path / "tone.wav"
    samples = tone(0.4, samples=8000)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(samples)
    pcm = read_wav_mono(path, target_rate=16000)
    # 8kHz 的 0.5 秒 -> 16kHz 的 0.5 秒
    assert len(pcm) == 16000 * 2


def test_noise_stats_suggests_reasonable_threshold() -> None:
    stats = NoiseStats(
        frames=100,
        seconds=3.0,
        mean=0.002,
        median=0.0015,
        p90=0.003,
        p95=0.004,
        max_rms=0.006,
    )
    suggestion = stats.suggest_threshold()
    assert 0.004 <= suggestion <= 0.08
    assert suggestion > stats.median
