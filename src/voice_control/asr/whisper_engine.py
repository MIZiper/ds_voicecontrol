"""faster-whisper 识别后端（可选引擎，需要 `uv sync --extra whisper`）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import AsrError


class WhisperEngine:
    """faster-whisper：准确率更高，但更吃 CPU/内存，模型也更大。"""

    name = "whisper"

    def __init__(
        self,
        model_name: str = "small",
        models_dir: str | Path = "models",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
        hf_endpoint: str = "",
        log: Callable[[str], None] = print,
    ) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:
            raise AsrError(
                f"无法导入 faster-whisper：{exc}\n"
                "请执行 `uv sync --extra whisper` 安装可选依赖。"
            ) from exc

        if hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", hf_endpoint)

        target = Path(models_dir)
        target.mkdir(parents=True, exist_ok=True)
        log(f"正在加载 Whisper 模型：{model_name}（device={device}, compute_type={compute_type}）")
        try:
            self._model: Any = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                download_root=str(target),
            )
        except Exception as exc:
            raise AsrError(f"加载 Whisper 模型失败：{exc}") from exc
        self.language = language or None
        log("Whisper 模型加载完成。")

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        if not pcm:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            segments, _info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=5,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            text = "".join(segment.text for segment in segments)
        except Exception as exc:  # pragma: no cover
            raise AsrError(f"识别失败：{exc}") from exc
        return text.strip()

    def close(self) -> None:
        self._model = None
