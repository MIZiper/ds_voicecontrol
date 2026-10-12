"""监听主循环：本地判断「有没有说话 + 有没有喊唤醒词」，唤醒后把那段语音交给大模型。

状态机：

* ``listening`` —— 只跑本地唤醒词检测（KWS），CPU 占用很低，音频不出本地；
* ``waiting``   —— 已唤醒，等你在 ``wake.wait_ms`` 内开口；用静音门限判断你什么时候开始说；
* ``capturing`` —— 正在录这一句口令，停顿 ``audio.silence_ms`` 后送去大模型。

这样唤醒词本身的音频不会被当成口令发出去，也只有真正说了话的那一段才会请求大模型。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import audio as audio_mod
from . import ui
from .actions import ActionContext, ActionExecutor, process_integrity_level
from .config import Config
from .kws import WakeWordSpotter, ensure_keywords_file
from .llm import CommandCatalog, Decision, LlmClient, LlmError
from .matching import match_rules
from .models import ensure_model

LISTENING = "listening"
WAITING = "waiting"
CAPTURING = "capturing"

# 唤醒词尾巴可能还在音频里，这段时间内不判定“你开始说口令了”
WAKE_COOLDOWN_MS = 200


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
    """把配置、唤醒词检测、大模型、动作执行器串起来的主对象。

    ``feed`` / ``handle_text`` 都不直接依赖声卡，方便用音频文件或假对象做测试。
    """

    def __init__(
        self,
        config: Config,
        *,
        spotter: Any = None,
        llm: Any = None,
        mic: audio_mod.Microphone | None = None,
        executor: ActionExecutor | None = None,
    ) -> None:
        self.config = config
        self.spotter = spotter
        self.llm = llm
        self.mic = mic
        self.executor = executor or ActionExecutor(
            allow=tuple(config.commands.allow),
            allow_shell=config.commands.allow_shell,
        )
        self.catalog = CommandCatalog.from_config(config)
        self.feedback = Feedback(config.feedback.beep)
        self.wake_enabled = bool(config.wake.enabled)
        self.state = LISTENING if self.wake_enabled else CAPTURING
        self.stats = {"wake": 0, "command": 0, "ignored": 0, "error": 0}

        audio = config.audio
        self._preroll: deque[bytes] = deque(
            maxlen=max(1, int(audio.pre_roll_ms / max(1, audio.frame_ms)))
        )
        self._gate = audio_mod.LevelGate(
            threshold=audio.noise_threshold,
            threshold_factor=audio.threshold_factor,
            adaptive=audio.adaptive_noise,
        )
        self._segmenter: audio_mod.Segmenter | None = None
        # 所有超时/冷却都用「音频时钟」（每喂一帧前进 frame_ms），
        # 这样实时麦克风和离线 wav 的行为完全一致
        self._clock_ms = 0.0
        self._wait_until_ms = 0.0
        self._cooldown_until_ms = 0.0
        self._capture_deadline_ms = 0.0
        self._last_exec: dict[str, float] = {}

    # -- 资源 -------------------------------------------------------------- #
    def ensure_spotter(self):
        if self.spotter is None:
            wake = self.config.wake
            model_dir = ensure_model(
                self.config.models_dir,
                wake.model,
                auto_download=wake.auto_download,
                log=ui.plain,
            )
            keywords_file = ensure_keywords_file(
                model_dir,
                wake.words,
                wake.keywords_file or None,
                log=ui.info,
            )
            self.spotter = WakeWordSpotter(
                model_dir,
                keywords_file,
                threshold=wake.threshold,
                score=wake.score,
                num_threads=wake.num_threads,
                sample_rate=self.config.audio.sample_rate,
                log=ui.plain,
            )
        return self.spotter

    def ensure_llm(self):
        if self.llm is None:
            self.llm = LlmClient(self.config, catalog=self.catalog, log=ui.info)
        return self.llm

    def ensure_mic(self) -> audio_mod.Microphone:
        if self.mic is None:
            self.mic = audio_mod.Microphone(
                sample_rate=self.config.audio.sample_rate,
                frame_ms=self.config.audio.frame_ms,
                device=self.config.audio.device,
            )
        return self.mic

    def build_segmenter(self, max_utterance_ms: int | None = None) -> audio_mod.Segmenter:
        audio = self.config.audio
        return audio_mod.Segmenter(
            sample_rate=audio.sample_rate,
            frame_ms=audio.frame_ms,
            threshold=audio.noise_threshold,
            pre_roll_ms=audio.pre_roll_ms,
            min_speech_ms=audio.min_speech_ms,
            max_utterance_ms=max_utterance_ms or audio.max_utterance_ms,
            silence_ms=audio.silence_ms,
            adaptive_noise=audio.adaptive_noise,
            threshold_factor=audio.threshold_factor,
            initial_noise_floor=self._gate.floor,
        )

    # -- 状态机 ------------------------------------------------------------ #
    def feed(self, frame: bytes) -> str:
        """喂一帧音频，返回这一步的状态字符串（便于测试与调试）。"""
        level = audio_mod.rms_of(frame)
        self._clock_ms += float(self.config.audio.frame_ms)

        if not self.wake_enabled:
            if self._segmenter is None:
                self._segmenter = self.build_segmenter()
            utterance = self._segmenter.feed(frame, level)
            if utterance is None:
                return "capturing"
            return self._handle_utterance(utterance)

        hit = self.ensure_spotter().feed_frame(frame)
        if hit is not None and self.state == LISTENING:
            self._on_wake(hit)

        if self.state == WAITING:
            self._preroll.append(frame)
            if self._clock_ms >= self._cooldown_until_ms and self._gate.push(level):
                self._begin_capture()
                return "start-capture"
            if self._clock_ms >= self._wait_until_ms:
                self._back_to_listening()
                return "wake-timeout"
            return "waiting"

        if self.state == CAPTURING:
            if self._segmenter is None:
                self._segmenter = self.build_segmenter()
            utterance = self._segmenter.feed(frame, level)
            if utterance is not None:
                status = self._handle_utterance(utterance)
                self._back_to_listening()
                return status
            if self._capture_deadline_ms and self._clock_ms >= self._capture_deadline_ms:
                pending = self._segmenter.flush()
                self._back_to_listening()
                if pending is not None:
                    return self._handle_utterance(pending)
                return "capture-timeout"
            return "capturing"

        return "listening"

    def flush(self) -> str:
        """音频结束时把还没提交的音频交出去。"""
        if self.state == CAPTURING and self._segmenter is not None:
            pending = self._segmenter.flush()
            self._back_to_listening()
            if pending is not None:
                return self._handle_utterance(pending)
        return "idle"

    def _on_wake(self, hit: Any) -> None:
        self.stats["wake"] += 1
        ui.ok(f"已唤醒（{getattr(hit, 'keyword', hit)}）")
        self.feedback.wake()
        self.state = WAITING
        self._wait_until_ms = self._clock_ms + float(self.config.wake.wait_ms)
        self._cooldown_until_ms = self._clock_ms + WAKE_COOLDOWN_MS
        self._preroll.clear()
        self._segmenter = None

    def _begin_capture(self) -> None:
        self.state = CAPTURING
        max_ms = min(self.config.audio.max_utterance_ms, self.config.wake.command_max_ms)
        self._segmenter = self.build_segmenter(max_utterance_ms=max_ms)
        for frame in list(self._preroll):
            self._segmenter.feed(frame)
        self._preroll.clear()
        self._capture_deadline_ms = self._clock_ms + float(max_ms)
        ui.dim("  正在听口令……")

    def _back_to_listening(self) -> None:
        self.state = LISTENING if self.wake_enabled else CAPTURING
        self._segmenter = None
        self._preroll.clear()
        self._capture_deadline_ms = 0.0
        if self.spotter is not None:
            try:
                self.spotter.reset()
            except Exception:  # pragma: no cover
                pass

    # -- 决策与执行 -------------------------------------------------------- #
    def _handle_utterance(self, utterance: audio_mod.Utterance) -> str:
        if utterance.voiced_ms < self.config.audio.min_speech_ms:
            return "too-short"
        ui.plain(
            f"  {ui.paint('语音', 'dim')}: {utterance.duration_ms:.0f}ms"
            f"（有效说话 {utterance.voiced_ms:.0f}ms）→ 交给大模型"
        )
        try:
            decision = self.ensure_llm().decide_audio(
                utterance.pcm, self.config.audio.sample_rate
            )
        except LlmError as exc:
            self.stats["error"] += 1
            ui.error(f"大模型调用失败：{exc}")
            self.feedback.matched(False)
            return "llm-error"
        return self.handle_decision(decision)

    def handle_text(self, text: str) -> str:
        """文本模式（`vc say` / `vc doctor`）：直接把一句话交给模型。"""
        ui.plain(f"  输入: {text}")
        try:
            decision = self.ensure_llm().decide_text(text)
        except LlmError as exc:
            self.stats["error"] += 1
            ui.error(f"大模型调用失败：{exc}")
            return "llm-error"
        return self.handle_decision(decision)

    def handle_decision(self, decision: Decision) -> str:
        for note in decision.notes:
            ui.warn(note)
        if decision.transcript:
            ui.plain(f"  模型听到: {decision.transcript}")
        ui.dim(f"  {decision.describe()}")

        rule = self.catalog.rule(decision.command_id)
        slot_text = decision.slot_text(rule)

        if rule is None and self.config.commands.fallback_local and decision.transcript:
            match = match_rules(
                decision.transcript, self.config.rules, self.config.commands.match_threshold
            )
            if match is not None:
                ui.info(f"模型没给命令，本地兜底命中「{match.name}」")
                rule = match.rule
                if not slot_text and match.remainder:
                    slot_text = match.remainder

        if rule is None:
            return self._on_unknown(decision)

        cooldown = float(self.config.commands.cooldown_ms)
        if cooldown > 0 and self._clock_ms - self._last_exec.get(rule.id, -1e9) < cooldown:
            ui.dim(f"（「{rule.name}」冷却中，本次忽略）")
            return "cooldown"
        self._last_exec[rule.id] = self._clock_ms

        ui.ok(f"执行命令「{rule.name}」（id={rule.id}）")
        context = ActionContext(
            dry_run=self.executor.dry_run,
            text=slot_text,
            slots=dict(decision.slots),
            declared_slots=tuple(rule.slots),
            allow=tuple(self.config.commands.allow),
            allow_shell=self.config.commands.allow_shell,
        )
        results = self.executor.execute(rule.action, context)
        ok = all(item.ok for item in results)
        for item in results:
            (ui.ok if item.ok else ui.error)(f"    {item}")
        self.feedback.matched(ok)
        self.stats["command"] += 1
        return f"executed:{rule.id}" if ok else f"failed:{rule.id}"

    def _on_unknown(self, decision: Decision) -> str:
        mode = self.config.commands.unknown
        if mode == "beep":
            self.feedback.unknown()
        if mode != "ignore":
            heard = f"（模型听到：{decision.transcript}）" if decision.transcript else ""
            ui.warn(f"模型没有匹配到任何命令{heard}；可以用 `vc rules` 检查命令表")
        self.stats["ignored"] += 1
        return "unknown"

    # -- 运行 -------------------------------------------------------------- #
    def banner(self) -> None:
        audio = self.config.audio
        wake = self.config.wake
        ui.title("=" * 66)
        ui.title("  ds_voicecontrol · 语音控制已启动")
        ui.title("=" * 66)
        if self.wake_enabled:
            ui.plain(
                f"  本地唤醒 : {'、'.join(wake.words)}"
                f"（sherpa-onnx 关键词检测，阈值 {wake.threshold}，音频不出本地）"
            )
        else:
            ui.plain("  本地唤醒 : 已关闭（检测到说话就直接交给大模型）")
        ui.plain(f"  命令理解 : {self.config.llm.model} @ {self.config.llm.base_url}")
        ui.plain(
            f"  静音门限 : {audio.noise_threshold:.4f}"
            f"（动态倍数 {audio.threshold_factor}，跟随底噪={'开' if audio.adaptive_noise else '关'}）"
        )
        ui.plain(f"  命令表   : {len(self.catalog.rules)} 条（`vc rules` 查看）")
        ui.plain(
            f"  动作白名单: {', '.join(self.config.commands.allow)}"
            f"{'（允许 shell）' if self.config.commands.allow_shell else ''}"
        )
        if self.executor.dry_run:
            ui.plain("  演练模式 : 只显示要执行什么，不会真的按键/打开程序")
        ui.plain("  退出     : Ctrl+C")
        ui.title("=" * 66)
        if process_integrity_level() == "Low":
            ui.warn(
                "这个终端的进程完整性级别是 Low：Windows 会静默拦掉模拟按键，命令会显示成功"
                "但目标程序收不到。用 `uv run vc keytest` 可以看到是谁启动了这个终端；"
                "换成干净终端（Win+R → wt，或开始菜单里的 Windows Terminal）再运行 `vc run`。"
            )

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
        self.ensure_spotter()
        self.ensure_llm()
        deadline = None if not duration else time.monotonic() + float(duration)
        handled = 0
        with mic:
            ui.plain(f"  输入设备 : {mic.describe()}")
            ui.plain("正在监听……（说唤醒词后接着说要做什么）")
            while True:
                frame = mic.read_frame()
                status = self.feed(frame)
                if status.startswith(("executed:", "failed:", "unknown", "llm-error")):
                    handled += 1
                    if once:
                        break
                if deadline is not None and time.monotonic() >= deadline:
                    break
        if deadline is not None:
            ui.dim(
                f"监听结束（{duration:.0f} 秒）：唤醒 {self.stats['wake']} 次，"
                f"执行 {self.stats['command']} 条，未匹配 {self.stats['ignored']} 次"
            )
        return 0

    def run_wav(self, path: Path) -> int:
        audio = self.config.audio
        try:
            pcm = audio_mod.read_wav_mono(path, target_rate=audio.sample_rate)
        except audio_mod.AudioError as exc:
            ui.error(str(exc))
            return 1
        self.ensure_spotter()
        self.ensure_llm()
        frame_bytes = max(2, int(audio.sample_rate * audio.frame_ms / 1000) * 2)
        ui.info(
            f"处理音频文件：{path}（{len(pcm) / 2 / audio.sample_rate:.1f} 秒，"
            f"{'需要唤醒词' if self.wake_enabled else '不需要唤醒词'}）"
        )
        for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            self.feed(pcm[offset : offset + frame_bytes])
        self.flush()
        ui.dim(
            f"处理完成：唤醒 {self.stats['wake']} 次，执行 {self.stats['command']} 条，"
            f"未匹配 {self.stats['ignored']} 次，出错 {self.stats['error']} 次"
        )
        if self.wake_enabled and self.stats["wake"] == 0:
            ui.warn(
                "这段音频里没有检测到唤醒词。可以调低 wake.threshold（例如 0.15），"
                "或用 `vc wake` 单独测试唤醒词。"
            )
        return 0
