"""监听主循环：采集音频 -> 语音识别 -> 唤醒词判定 -> 口令匹配 -> 执行动作。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import audio as audio_mod
from . import ui
from .actions import ActionContext, ActionExecutor
from .config import Config
from .matching import find_wake_word, match_rules


class Feedback:
    """提示音：Windows 用 winsound，其它平台退化为终端响铃。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self._winsound: Any = None
        if self.enabled:
            try:  # pragma: no cover - 平台相关
                import winsound  # type: ignore

                self._winsound = winsound
            except Exception:
                self._winsound = None

    def _beep(self, frequency: int, duration: int) -> None:
        if not self.enabled:
            return
        if self._winsound is not None:
            def play() -> None:
                try:
                    self._winsound.Beep(frequency, duration)
                except Exception:
                    pass

            threading.Thread(target=play, daemon=True).start()
        else:
            print("\a", end="", flush=True)

    def wake(self) -> None:
        self._beep(880, 90)

    def matched(self, ok: bool = True) -> None:
        self._beep(1320 if ok else 440, 80)

    def unknown(self) -> None:
        self._beep(300, 120)


class Listener:
    """把配置、识别引擎、动作执行器串起来的主对象。

    ``handle_text`` 是纯逻辑入口（不依赖麦克风），因此可以直接用它做规则测试。
    """

    def __init__(
        self,
        config: Config,
        *,
        engine: Any = None,
        mic: audio_mod.Microphone | None = None,
        executor: ActionExecutor | None = None,
        create_engine: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.mic = mic
        self.executor = executor or ActionExecutor(
            allow=tuple(config.commands.allow),
            allow_shell=config.commands.allow_shell,
        )
        self.require_wake = bool(config.wake.require_wake)
        self.feedback = Feedback(config.feedback.beep)
        self._window_until = 0.0
        self._last_exec: dict[str, float] = {}
        self._create_engine = create_engine
        self.messages: list[str] = []

    # -- 资源 -------------------------------------------------------------- #
    def ensure_engine(self):
        if self.engine is None:
            factory = self._create_engine
            if factory is None:
                from .asr import create_engine as factory  # type: ignore[assignment]

            self.engine = factory(self.config, models_dir=self.config.models_dir, log=ui.info)  # type: ignore[misc]
        return self.engine

    def ensure_mic(self) -> audio_mod.Microphone:
        if self.mic is None:
            self.mic = audio_mod.Microphone(
                sample_rate=self.config.audio.sample_rate,
                frame_ms=self.config.audio.frame_ms,
                device=self.config.audio.device,
            )
        return self.mic

    def build_segmenter(self) -> audio_mod.Segmenter:
        audio = self.config.audio
        return audio_mod.Segmenter(
            sample_rate=audio.sample_rate,
            frame_ms=audio.frame_ms,
            threshold=audio.noise_threshold,
            pre_roll_ms=audio.pre_roll_ms,
            min_speech_ms=audio.min_speech_ms,
            max_utterance_ms=audio.max_utterance_ms,
            silence_ms=audio.silence_ms,
            adaptive_noise=audio.adaptive_noise,
            threshold_factor=audio.threshold_factor,
        )

    # -- 文本处理 ---------------------------------------------------------- #
    def handle_text(self, text: str, *, source: str = "mic") -> str:
        """处理一句识别文本，返回状态字符串（便于脚本/测试判断）。"""
        text = (text or "").strip()
        if not text:
            return "empty"
        ui.plain(f"{ui.paint('听到', 'dim')}: {text}")

        if self.config.wake.enabled and self.require_wake:
            word, remainder = find_wake_word(
                text, self.config.wake.words, self.config.wake.fuzzy
            )
            if word:
                ui.info(f"已唤醒（唤醒词：{word}）")
                self.feedback.wake()
                self._window_until = time.monotonic() + float(self.config.wake.window_s)
                if not remainder:
                    ui.dim(f"请接着说出口令（{self.config.wake.window_s:.0f} 秒内有效）")
                    return "wake-only"
                return self._dispatch(remainder)
            if time.monotonic() >= self._window_until:
                if self.config.general.log_level == "debug":
                    ui.dim("（没有唤醒词，忽略）")
                return "ignored"

        return self._dispatch(text)

    def _dispatch(self, text: str) -> str:
        match = match_rules(text, self.config.rules, self.config.commands.match_threshold)
        if match is None:
            return self._on_unknown(text)
        rule = match.rule
        cooldown = float(self.config.commands.cooldown_ms) / 1000.0
        now = time.monotonic()
        if cooldown > 0 and now - self._last_exec.get(rule.name, 0.0) < cooldown:
            ui.dim(f"（「{rule.name}」冷却中，本次忽略）")
            return "cooldown"
        self._last_exec[rule.name] = now

        slot = f" 槽位={match.remainder!r}" if match.remainder else ""
        ui.ok(f"命中「{rule.name}」得分={match.score:.2f}{slot}")
        context = ActionContext(
            dry_run=self.executor.dry_run,
            text=match.remainder,
            allow=tuple(self.config.commands.allow),
            allow_shell=self.config.commands.allow_shell,
        )
        results = self.executor.execute(rule.action, context)
        ok = all(item.ok for item in results)
        for item in results:
            (ui.ok if item.ok else ui.error)(f"    {item}")
        self.feedback.matched(ok)
        self._window_until = 0.0
        return f"executed:{rule.name}" if ok else f"failed:{rule.name}"

    def _on_unknown(self, text: str) -> str:
        mode = self.config.commands.unknown
        if mode == "beep":
            self.feedback.unknown()
        if mode != "ignore":
            ui.warn(f"没有匹配的口令：{text}（可以用 `vc rules` 查看已配置的口令）")
        return "unknown"

    # -- 音频处理 ---------------------------------------------------------- #
    def process_utterance(self, utterance: audio_mod.Utterance) -> str:
        engine = self.ensure_engine()
        try:
            text = engine.transcribe(utterance.pcm, self.config.audio.sample_rate)
        except Exception as exc:
            ui.error(f"识别失败：{exc}")
            return "asr-error"
        if self.config.general.log_level == "debug":
            ui.dim(
                f"（音频 {utterance.duration_ms:.0f}ms，有效说话 {utterance.voiced_ms:.0f}ms，"
                f"峰值 {utterance.peak_rms:.4f}）"
            )
        return self.handle_text(text)

    # -- 运行 -------------------------------------------------------------- #
    def banner(self) -> None:
        audio = self.config.audio
        ui.title("=" * 66)
        ui.title("  ds_voicecontrol · 语音控制已启动")
        ui.title("=" * 66)
        engine_name = self.config.asr.engine
        if self.config.asr.engine == "vosk":
            engine_name = f"vosk（{self.config.asr.vosk_model}）"
        elif self.config.asr.engine == "whisper":
            engine_name = f"faster-whisper（{self.config.asr.whisper_model}）"
        ui.plain(f"  识别引擎 : {engine_name}")
        ui.plain(f"  静音门限 : {audio.noise_threshold:.4f}（动态倍数 {audio.threshold_factor}，"
                 f"跟随底噪={'开' if audio.adaptive_noise else '关'}）")
        if self.config.wake.enabled and self.require_wake:
            ui.plain(f"  唤醒词   : {'、'.join(self.config.wake.words)}"
                     f"（只说唤醒词后 {self.config.wake.window_s:.0f} 秒内可直接说口令）")
        else:
            ui.plain("  唤醒词   : 已关闭，听到口令就会执行")
        ui.plain(f"  口令规则 : {len(self.config.enabled_rules())} 条（`vc rules` 查看）")
        ui.plain(f"  动作白名单: {', '.join(self.config.commands.allow)}"
                 f"{'（允许 shell）' if self.config.commands.allow_shell else ''}")
        ui.plain("  退出     : Ctrl+C")
        ui.title("=" * 66)

    def run(
        self,
        *,
        wav: str | Path | None = None,
        once: bool = False,
        duration: float | None = None,
    ) -> int:
        if wav is not None:
            return self.run_wav(Path(wav))
        self.banner()
        mic = self.ensure_mic()
        segmenter = self.build_segmenter()
        self.ensure_engine()
        handled = 0
        deadline = None if not duration else time.monotonic() + float(duration)
        with mic:
            ui.plain(f"  输入设备 : {mic.describe()}")
            ui.plain("正在监听……")
            for frame in mic.frames():
                utterance = segmenter.feed(frame)
                if utterance is None:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    continue
                self.process_utterance(utterance)
                handled += 1
                if once:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
        if deadline is not None:
            ui.dim(f"监听结束（{duration:.0f} 秒，处理了 {handled} 段语音）")
        return 0

    def run_wav(self, path: Path) -> int:
        audio = self.config.audio
        try:
            pcm = audio_mod.read_wav_mono(path, target_rate=audio.sample_rate)
        except audio_mod.AudioError as exc:
            ui.error(str(exc))
            return 1
        segmenter = self.build_segmenter()
        self.ensure_engine()
        ui.info(f"处理音频文件：{path}（{len(pcm) / 2 / audio.sample_rate:.1f} 秒）")
        step = segmenter.frame_bytes
        utterances = []
        for offset in range(0, len(pcm) - step + 1, step):
            frame = pcm[offset : offset + step]
            utterance = segmenter.feed(frame)
            if utterance is not None:
                utterances.append(utterance)
        tail = segmenter.flush()
        if tail is not None:
            utterances.append(tail)
        if not utterances:
            ui.warn(
                "没有从音频里检测到说话。可以把 audio.noise_threshold 调小，"
                "或确认录音音量（`vc meter` 可以实时看电平）。"
            )
            return 0
        for utterance in utterances:
            self.process_utterance(utterance)
        return 0
