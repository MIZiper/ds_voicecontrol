"""ASR 后端接口与工厂函数。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ..models import ensure_vosk_model

_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
)


class AsrError(RuntimeError):
    """识别引擎相关错误。"""


@runtime_checkable
class AsrEngine(Protocol):
    name: str

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:  # pragma: no cover - 协议
        ...

    def close(self) -> None:  # pragma: no cover - 协议
        ...


def is_cjk(text: str) -> bool:
    return bool(text) and all(
        any(low <= ord(char) <= high for low, high in _CJK_RANGES) for char in text
    )


def join_tokens(text: str) -> str:
    """把 Vosk 的空格分隔结果拼回自然文本（中文不留空格，英文保留词间空格）。"""
    if not text:
        return ""
    tokens = [token for token in text.split() if token]
    out = ""
    for token in tokens:
        if not out:
            out = token
            continue
        if is_cjk(token) and is_cjk(out[-1]):
            out += token
        else:
            out += " " + token
    return out.strip()


def create_engine(
    config,
    *,
    models_dir: Path | None = None,
    auto_download: bool | None = None,
    log: Callable[[str], None] = print,
) -> AsrEngine:
    """按配置创建识别引擎。"""
    engine = str(config.asr.engine).strip().lower()
    target_dir = Path(models_dir) if models_dir is not None else config.models_dir
    download = config.asr.auto_download if auto_download is None else auto_download

    if engine == "vosk":
        from .vosk_engine import VoskEngine

        model_path = ensure_vosk_model(
            target_dir, config.asr.vosk_model, auto_download=download, log=log
        )
        return VoskEngine(model_path, log=log)

    if engine in ("whisper", "faster-whisper", "faster_whisper"):
        from .whisper_engine import WhisperEngine

        return WhisperEngine(
            model_name=config.asr.whisper_model,
            models_dir=target_dir,
            device=config.asr.whisper_device,
            compute_type=config.asr.whisper_compute_type,
            language=config.general.language,
            hf_endpoint=config.asr.hf_endpoint,
            log=log,
        )

    raise AsrError(f"未知的识别引擎 “{engine}”，可选：vosk / whisper")


__all__ = [
    "AsrEngine",
    "AsrError",
    "create_engine",
    "is_cjk",
    "join_tokens",
]
