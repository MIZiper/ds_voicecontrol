"""Vosk 离线识别后端（默认引擎）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import AsrError, join_tokens


class VoskEngine:
    """Vosk 流式识别：模型只加载一次，每句话新建一个识别器。"""

    name = "vosk"

    def __init__(self, model_path: str | Path, log: Callable[[str], None] = print) -> None:
        try:
            import vosk  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise AsrError(
                f"无法导入 vosk：{exc}\n请先执行 `uv sync` 安装依赖。"
            ) from exc

        self._vosk = vosk
        try:  # 关掉 vosk 的 C 层日志，避免刷屏
            vosk.SetLogLevel(-1)
        except Exception:
            pass

        path = Path(model_path)
        if not path.exists():
            raise AsrError(f"Vosk 模型目录不存在：{path}")
        log(f"正在加载 Vosk 模型：{path}（首次加载需要几秒）")
        try:
            self._model: Any = vosk.Model(str(path))
        except Exception as exc:
            raise AsrError(f"加载 Vosk 模型失败：{exc}") from exc
        log("Vosk 模型加载完成。")

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        if not pcm:
            return ""
        recognizer = self._vosk.KaldiRecognizer(self._model, int(sample_rate))
        ack = recognizer.AcceptWaveform(pcm)
        if ack:
            payload = recognizer.Result()
        else:
            payload = recognizer.FinalResult()
        try:
            data = json.loads(payload or "{}")
        except json.JSONDecodeError:
            return ""
        return join_tokens(str(data.get("text", "")))

    def close(self) -> None:
        self._model = None
