"""麦克风采集、底噪校准，以及基于音量门限的语音分段（VAD-lite）。

设计要点：
* 不依赖任何云端或重型模型，只用音量（RMS）判断“有没有人在说话”；
* 分段逻辑（Segmenter）是纯计算，不碰声卡，方便单元测试；
* 校准只测环境底噪，门限 = max(校准值, 实时底噪 * 倍数)，因此空调/风扇变化也不怕。
"""

from __future__ import annotations

import math
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

try:  # pragma: no cover - 环境缺少 PortAudio 时给出友好提示
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None  # type: ignore[assignment]


class AudioError(RuntimeError):
    """音频设备或参数相关错误。"""


def _require_sd():
    if sd is None:  # pragma: no cover
        raise AudioError("未安装 sounddevice，请先执行 `uv sync`")
    return sd


# --------------------------------------------------------------------------- #
# 基础计算
# --------------------------------------------------------------------------- #
def rms_of(frame: bytes) -> float:
    """int16 PCM 帧的均方根音量（0~1）。"""
    if not frame:
        return 0.0
    data = np.frombuffer(frame, dtype=np.int16)
    if data.size == 0:
        return 0.0
    floats = data.astype(np.float32) / 32768.0
    return float(math.sqrt(float(np.mean(np.square(floats))) + 1e-12))


def peak_of(frame: bytes) -> float:
    if not frame:
        return 0.0
    data = np.frombuffer(frame, dtype=np.int16)
    if data.size == 0:
        return 0.0
    return float(np.max(np.abs(data.astype(np.float32))) / 32768.0)


def effective_threshold(threshold: float, floor: float, factor: float, adaptive: bool = True) -> float:
    """实际静音门限 = max(校准值, 实时底噪 × 倍数)。"""
    if not adaptive:
        return float(threshold)
    return float(max(float(threshold), float(floor) * max(0.0, float(factor))))


@dataclass
class LevelGate:
    """只看「现在是不是有人在说话」：静音门限 + 底噪自适应。

    ``Segmenter`` 内部也用同一套公式；唤醒后等待口令开始时用它来判断“开始说了”。
    """

    threshold: float = 0.010
    threshold_factor: float = 2.5
    adaptive: bool = True
    alpha: float = 0.02
    floor: float | None = None

    def __post_init__(self) -> None:
        if self.floor is None:
            self.floor = float(self.threshold) / max(1.0, float(self.threshold_factor))

    @property
    def effective(self) -> float:
        return effective_threshold(self.threshold, self.floor or 0.0, self.threshold_factor, self.adaptive)

    def push(self, level: float) -> bool:
        """喂一个音量值，返回是否超过门限（说话中）。"""
        if level >= self.effective:
            return True
        if self.adaptive:
            self.floor = (1 - self.alpha) * float(self.floor or 0.0) + self.alpha * level
        return False


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #
@dataclass
class InputDevice:
    index: int
    name: str
    channels: int
    default_samplerate: float
    is_default: bool

    def describe(self) -> str:
        flag = " (系统默认)" if self.is_default else ""
        return f"[{self.index}] {self.name} 声道={self.channels} 采样率={self.default_samplerate:.0f}{flag}"


def list_input_devices() -> list[InputDevice]:
    sdl = _require_sd()
    try:
        devices = sdl.query_devices()
        default_index = sdl.default.device[0]
    except Exception as exc:  # pragma: no cover
        raise AudioError(f"无法读取音频设备列表：{exc}") from exc
    result: list[InputDevice] = []
    for index, info in enumerate(devices):
        if int(info.get("max_input_channels", 0)) <= 0:
            continue
        result.append(
            InputDevice(
                index=index,
                name=str(info.get("name", "")),
                channels=int(info.get("max_input_channels", 0)),
                default_samplerate=float(info.get("default_samplerate", 0.0)),
                is_default=(index == default_index),
            )
        )
    return result


def resolve_device(spec: str | int | None) -> int | None:
    """把配置里的设备描述解析为 sounddevice 设备索引；None 表示系统默认。"""
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    text = str(spec).strip()
    if not text or text.lower() in {"default", "auto", "系统默认"}:
        return None
    if text.isdigit():
        return int(text)
    try:
        devices = list_input_devices()
    except AudioError:
        return None
    lowered = text.lower()
    exact = [d for d in devices if d.name.lower() == lowered]
    partial = [d for d in devices if lowered in d.name.lower()]
    hits = exact or partial
    if not hits:
        names = "、".join(d.name for d in devices) or "（没有可用输入设备）"
        raise AudioError(f"找不到匹配 “{text}” 的输入设备。当前可用：{names}")
    if len(hits) > 1:
        default_hits = [d for d in hits if d.is_default]
        if default_hits:
            return default_hits[0].index
    return hits[0].index


# --------------------------------------------------------------------------- #
# 麦克风
# --------------------------------------------------------------------------- #
class Microphone:
    """打开一个固定帧长的单声道 16bit 输入流。"""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        device: str | int | None = None,
        channels: int = 1,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.channels = int(channels)
        self.frame_samples = max(1, int(self.sample_rate * self.frame_ms / 1000))
        self.device_spec = device
        self.device_index = resolve_device(device)
        self._stream = None

    # -- 生命周期 ---------------------------------------------------------- #
    def open(self) -> "Microphone":
        sdl = _require_sd()
        try:
            self._stream = sdl.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.frame_samples,
                dtype="int16",
                channels=self.channels,
                device=self.device_index,
                latency="low",
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioError(
                f"无法打开麦克风（设备={self.device_spec or '默认'}）：{exc}\n"
                "提示：用 `vc devices` 查看可用设备，在配置里改 audio.device。"
            ) from exc
        return self

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def __enter__(self) -> "Microphone":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 读取 -------------------------------------------------------------- #
    def read_frame(self) -> bytes:
        if self._stream is None:
            raise AudioError("麦克风尚未打开")
        data, overflowed = self._stream.read(self.frame_samples)
        if overflowed:
            # 丢帧不是致命问题，继续用即可
            pass
        return bytes(data)

    def frames(self) -> Iterator[bytes]:
        while True:
            yield self.read_frame()

    # -- 便捷方法 ---------------------------------------------------------- #
    def describe(self) -> str:
        sdl = _require_sd()
        try:
            if self.device_index is None:
                info = sdl.query_devices(kind="input")
            else:
                info = sdl.query_devices(self.device_index)
            name = str(info.get("name", "未知设备"))
        except Exception:
            name = f"设备索引 {self.device_index}"
        return f"{name}（{self.sample_rate} Hz, {self.frame_ms} ms/帧）"

    def calibrate(self, seconds: float = 5.0, on_level: Callable[[float], None] | None = None) -> "NoiseStats":
        return measure_noise(self, seconds=seconds, on_level=on_level)

    def record_seconds(self, seconds: float) -> bytes:
        chunks: list[bytes] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunks.append(self.read_frame())
        return b"".join(chunks)


# --------------------------------------------------------------------------- #
# 底噪校准
# --------------------------------------------------------------------------- #
@dataclass
class NoiseStats:
    frames: int
    seconds: float
    mean: float
    median: float
    p90: float
    p95: float
    max_rms: float

    def suggest_threshold(self, *, floor: float = 0.004, ceiling: float = 0.08) -> float:
        """由底噪统计推荐一个静音门限（运行时的动态倍数由 Segmenter 另外处理）。"""
        base = max(self.p95 * 1.8, self.median * 4.0)
        return float(min(max(base, floor), ceiling))

    def summary(self) -> str:
        return (
            f"采样 {self.seconds:.1f}s / {self.frames} 帧："
            f"均值={self.mean:.4f} 中位={self.median:.4f} "
            f"P90={self.p90:.4f} P95={self.p95:.4f} 峰值={self.max_rms:.4f}"
        )


def measure_noise(
    mic: Microphone,
    seconds: float = 5.0,
    on_level: Callable[[float], None] | None = None,
) -> NoiseStats:
    """安静环境下测量底噪；建议保持风扇/空调等“日常背景音”开着测。"""
    levels: list[float] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = mic.read_frame()
        level = rms_of(frame)
        levels.append(level)
        if on_level is not None:
            on_level(level)
    if not levels:
        raise AudioError("没有采集到任何音频，请检查麦克风权限")
    arr = np.asarray(levels, dtype=np.float64)
    elapsed = len(levels) * mic.frame_ms / 1000.0
    return NoiseStats(
        frames=len(levels),
        seconds=elapsed,
        mean=float(arr.mean()),
        median=float(np.median(arr)),
        p90=float(np.percentile(arr, 90)),
        p95=float(np.percentile(arr, 95)),
        max_rms=float(arr.max()),
    )


# --------------------------------------------------------------------------- #
# 分段（VAD-lite）
# --------------------------------------------------------------------------- #
@dataclass
class Utterance:
    """一段检测到的说话音频。"""

    pcm: bytes
    duration_ms: float
    voiced_ms: float
    peak_rms: float
    reason: str = "silence"

    def __len__(self) -> int:
        return len(self.pcm)


@dataclass
class Segmenter:
    """把连续音频帧切成一句一句的话。

    用法::

        seg = Segmenter(threshold=0.01)
        for frame in mic.frames():
            utt = seg.feed(frame)
            if utt is not None:
                ...
    """

    sample_rate: int = 16000
    frame_ms: int = 30
    threshold: float = 0.01
    pre_roll_ms: int = 300
    min_speech_ms: int = 250
    max_utterance_ms: int = 8000
    silence_ms: int = 700
    adaptive_noise: bool = True
    threshold_factor: float = 2.5
    initial_noise_floor: float | None = None
    adaptive_alpha: float = 0.02

    _preroll: deque = field(default_factory=deque, init=False, repr=False)
    _buffer: list[bytes] = field(default_factory=list, init=False, repr=False)
    _total_ms: float = field(default=0.0, init=False)
    _voiced_ms: float = field(default=0.0, init=False)
    _silence_ms: float = field(default=0.0, init=False)
    _peak_rms: float = field(default=0.0, init=False)
    _floor: float = field(default=0.0, init=False)
    _active: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        maxlen = max(1, int(math.ceil(self.pre_roll_ms / max(1, self.frame_ms))))
        self._preroll = deque(maxlen=maxlen)
        # 底噪估计从 threshold / factor 起步，这样启动时的实际门限正好等于 noise_threshold，
        # 之后只有当环境变吵时才会自动抬高。
        if self.initial_noise_floor is not None:
            self._floor = float(self.initial_noise_floor)
        else:
            self._floor = float(self.threshold) / max(1.0, float(self.threshold_factor))

    # -- 门限 -------------------------------------------------------------- #
    @property
    def frame_samples(self) -> int:
        return max(1, int(self.sample_rate * self.frame_ms / 1000))

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * 2

    @property
    def effective_threshold(self) -> float:
        return effective_threshold(
            self.threshold, self._floor, self.threshold_factor, self.adaptive_noise
        )

    @property
    def noise_floor(self) -> float:
        return float(self._floor)

    @property
    def is_active(self) -> bool:
        return self._active

    def reset(self) -> None:
        self._preroll.clear()
        self._buffer.clear()
        self._total_ms = 0.0
        self._voiced_ms = 0.0
        self._silence_ms = 0.0
        self._peak_rms = 0.0
        self._active = False

    # -- 主流程 ------------------------------------------------------------ #
    def feed(self, frame: bytes, rms: float | None = None) -> Utterance | None:
        level = rms_of(frame) if rms is None else rms
        threshold = self.effective_threshold

        if not self._active:
            self._preroll.append(frame)
            if level >= threshold:
                self._active = True
                self._buffer = list(self._preroll)
                self._total_ms = len(self._buffer) * self.frame_ms
                self._voiced_ms = float(self.frame_ms)
                self._silence_ms = 0.0
                self._peak_rms = level
            elif self.adaptive_noise:
                self._floor = (1 - self.adaptive_alpha) * self._floor + self.adaptive_alpha * level
            return None

        self._buffer.append(frame)
        self._total_ms += self.frame_ms
        self._peak_rms = max(self._peak_rms, level)
        if level >= threshold:
            self._voiced_ms += self.frame_ms
            self._silence_ms = 0.0
        else:
            self._silence_ms += self.frame_ms

        if self._silence_ms >= self.silence_ms:
            return self._finalize("silence")
        if self._total_ms >= self.max_utterance_ms:
            return self._finalize("max-length")
        return None

    def flush(self) -> Utterance | None:
        """音频流结束时把缓冲里的话交出来（用于处理 wav 文件）。"""
        if self._active:
            return self._finalize("flush")
        return None

    def _finalize(self, reason: str) -> Utterance | None:
        pcm = b"".join(self._buffer)
        voiced = self._voiced_ms
        utterance = Utterance(
            pcm=pcm,
            duration_ms=self._total_ms,
            voiced_ms=voiced,
            peak_rms=self._peak_rms,
            reason=reason,
        )
        self.reset()
        if voiced < self.min_speech_ms:
            return None
        return utterance


# --------------------------------------------------------------------------- #
# wav 文件（用于离线测试与自动化验证）
# --------------------------------------------------------------------------- #
def read_wav_mono(path: str | Path, target_rate: int = 16000) -> bytes:
    """读取 wav，转成 16bit 单声道、目标采样率的 PCM。"""
    path = Path(path)
    if not path.exists():
        raise AudioError(f"找不到音频文件：{path}")
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    elif width == 1:
        data = (np.frombuffer(raw, dtype="u1").astype(np.float32) - 128.0) / 128.0 * 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0 * 32768.0
    else:
        raise AudioError(f"不支持的 wav 位深：{width * 8} bit")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != target_rate and data.size > 1:
        src_positions = np.arange(data.size, dtype=np.float64)
        dst_count = int(round(data.size * target_rate / float(rate)))
        dst_positions = np.linspace(0.0, float(data.size - 1), dst_count)
        data = np.interp(dst_positions, src_positions, data).astype(np.float32)
    return np.clip(data, -32768, 32767).astype("<i2").tobytes()


def frames_of(pcm: bytes, frame_samples: int) -> Iterator[bytes]:
    """把 PCM 切成固定长度的帧（不足一帧的丢弃）。"""
    step = frame_samples * 2
    for offset in range(0, len(pcm) - step + 1, step):
        yield pcm[offset : offset + step]
