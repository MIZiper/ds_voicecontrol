"""本地唤醒词：sherpa-onnx 关键词检测（KWS）。

只做一件事：在连续音频流里听有没有人喊唤醒词。不上云、不做整句识别，
所以 CPU 占用很低，也不会把唤醒词之外的语音发出去。

中文唤醒词会自动转成模型要的「声母 + 韵母」token 序列，例如::

    牛来  ->  n iú l ái  @牛来

转换用 pypinyin（带声调），生成的关键词文件会在唤醒词变化时自动重建。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

# 声母表（按长度优先匹配，zh/ch/sh 要在 z/c/s 之前）
_INITIALS = (
    "zh",
    "ch",
    "sh",
    "b",
    "p",
    "m",
    "f",
    "d",
    "t",
    "n",
    "l",
    "g",
    "k",
    "h",
    "j",
    "q",
    "x",
    "r",
    "z",
    "c",
    "s",
    "y",
    "w",
)

KEYWORDS_HEADER = "# 本文件由 ds_voicecontrol 根据 config.toml 的 [wake] words 自动生成，请勿手改\n"


class KwsError(RuntimeError):
    """唤醒词模型或关键词相关错误。"""


# --------------------------------------------------------------------------- #
# 中文 -> 模型 token
# --------------------------------------------------------------------------- #
def syllable_to_tokens(syllable: str) -> list[str]:
    """把带声调的拼音音节拆成模型 token：``xiǎo`` -> ``["x", "iǎo"]``。"""
    text = (syllable or "").strip().lower()
    if not text:
        return []
    for initial in _INITIALS:
        if text.startswith(initial) and len(text) > len(initial):
            return [initial, text[len(initial):]]
    return [text]


def is_chinese_word(word: str) -> bool:
    return bool(word) and all("\u4e00" <= char <= "\u9fff" for char in word)


def word_to_tokens(word: str) -> list[str]:
    """把中文唤醒词转成 token 序列（需要 pypinyin）。"""
    if not word:
        return []
    if not is_chinese_word(word):
        raise KwsError(
            f"唤醒词 “{word}” 里有非中文内容。当前用的中文关键词模型只支持纯中文唤醒词；"
            "如果要用英文唤醒词，请换用 sherpa-onnx 的英文/中英双语 KWS 模型。"
        )
    try:
        from pypinyin import Style, pinyin
    except ImportError as exc:  # pragma: no cover
        raise KwsError("需要 pypinyin 才能把中文唤醒词转成拼音，请执行 `uv sync`") from exc

    tokens: list[str] = []
    for item in pinyin(word, style=Style.TONE, errors="default"):
        syllable = item[0] if isinstance(item, (list, tuple)) else str(item)
        tokens.extend(syllable_to_tokens(syllable))
    return tokens


def load_token_set(model_dir: str | os.PathLike[str]) -> set[str]:
    path = Path(model_dir) / "tokens.txt"
    if not path.exists():
        raise KwsError(f"找不到模型文件：{path}")
    tokens: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        tokens.add(line.split()[0])
    return tokens


def keywords_content(model_dir: str | os.PathLike[str], words: Sequence[str]) -> str:
    """生成关键词文件内容（sherpa-onnx 要求纯数据行，不能有注释），并校验 token。"""
    token_set = load_token_set(model_dir)
    lines: list[str] = []
    for word in words:
        tokens = word_to_tokens(word)
        if not tokens:
            continue
        missing = [token for token in tokens if token not in token_set]
        if missing:
            raise KwsError(
                f"唤醒词 “{word}” 转出来的拼音 {missing} 不在模型的 token 表里，"
                "请换一个更常见的词（例如“牛来”“你好牛来”）。"
            )
        lines.append(" ".join(tokens) + f" @{word}")
    if not lines:
        raise KwsError("没有可用的唤醒词")
    return "\n".join(lines) + "\n"


def ensure_keywords_file(
    model_dir: str | os.PathLike[str],
    words: Sequence[str],
    target: str | os.PathLike[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    """确保关键词文件存在且与当前唤醒词一致，返回文件路径。"""
    model_path = Path(model_dir)
    path = Path(target) if target else (model_path.parent / "wake-keywords.txt")
    content = keywords_content(model_path, words)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if log is not None:
        log(f"已生成唤醒词文件：{path}")
    return path


# --------------------------------------------------------------------------- #
# 模型文件
# --------------------------------------------------------------------------- #
def find_model_files(model_dir: str | os.PathLike[str]) -> tuple[Path, Path, Path]:
    """在模型目录里挑一组 onnx 文件（优先 int8 + epoch-12）。"""
    directory = Path(model_dir)
    if not directory.is_dir():
        raise KwsError(
            f"找不到唤醒词模型目录：{directory}\n先运行 `vc setup` 下载模型。"
        )

    def pick(kind: str) -> Path:
        candidates = sorted(directory.glob(f"{kind}-*.onnx"))
        if not candidates:
            raise KwsError(f"模型目录里没有 {kind}-*.onnx：{directory}")
        int8 = [p for p in candidates if p.name.endswith(".int8.onnx")]
        pool = int8 or candidates
        preferred = [p for p in pool if "epoch-12" in p.name]
        return (preferred or pool)[0]

    return pick("encoder"), pick("decoder"), pick("joiner")


# --------------------------------------------------------------------------- #
# 检测器
# --------------------------------------------------------------------------- #
@dataclass
class KwsHit:
    keyword: str
    tokens: list[str] = field(default_factory=list)


class WakeWordSpotter:
    """把连续的 16bit PCM 帧喂进来，命中唤醒词时返回 :class:`KwsHit`。"""

    def __init__(
        self,
        model_dir: str | os.PathLike[str],
        keywords_file: str | os.PathLike[str],
        *,
        threshold: float = 0.25,
        score: float = 1.0,
        num_threads: int = 2,
        sample_rate: int = 16000,
        log: Callable[[str], None] | None = None,
    ) -> None:
        try:
            import sherpa_onnx
        except Exception as exc:  # pragma: no cover
            raise KwsError(f"无法导入 sherpa-onnx：{exc}\n请执行 `uv sync` 安装依赖。") from exc

        self.model_dir = Path(model_dir)
        self.keywords_file = Path(keywords_file)
        self.sample_rate = int(sample_rate)
        self.encoder, self.decoder, self.joiner = find_model_files(self.model_dir)
        self.tokens_path = self.model_dir / "tokens.txt"
        if not self.tokens_path.exists():
            raise KwsError(f"找不到 tokens.txt：{self.tokens_path}")
        if not self.keywords_file.exists():
            raise KwsError(f"找不到唤醒词文件：{self.keywords_file}")

        if log is not None:
            log(f"正在加载唤醒词模型：{self.model_dir.name}（{self.encoder.name}）")
        try:
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(self.tokens_path),
                encoder=str(self.encoder),
                decoder=str(self.decoder),
                joiner=str(self.joiner),
                keywords_file=str(self.keywords_file),
                num_threads=max(1, int(num_threads)),
                sample_rate=self.sample_rate,
                keywords_score=float(score),
                keywords_threshold=float(threshold),
                provider="cpu",
            )
        except Exception as exc:
            raise KwsError(f"加载唤醒词模型失败：{exc}") from exc
        self._stream = self._spotter.create_stream()
        if log is not None:
            log("唤醒词模型加载完成。")

    # -- 使用 -------------------------------------------------------------- #
    @property
    def keywords(self) -> list[str]:
        result: list[str] = []
        try:
            for line in self.keywords_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                if "@" in line:
                    result.append(line.rsplit("@", 1)[1].strip())
        except OSError:  # pragma: no cover
            pass
        return result

    def feed_frame(self, frame: bytes) -> KwsHit | None:
        """喂一帧 16bit 单声道 PCM，命中返回关键词。"""
        if not frame:
            return None
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            self._stream.accept_waveform(self.sample_rate, samples)
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
        except Exception as exc:  # pragma: no cover
            raise KwsError(f"唤醒词检测失败：{exc}") from exc

        keyword = result if isinstance(result, str) else str(getattr(result, "keyword", "") or "")
        if not keyword:
            return None
        tokens: list[str] = []
        try:
            tokens = list(self._spotter.tokens(self._stream))
        except Exception:
            tokens = []
        try:
            self._spotter.reset_stream(self._stream)
        except Exception:  # pragma: no cover
            pass
        return KwsHit(keyword=keyword, tokens=tokens)

    def feed_pcm(self, pcm: bytes, frame_bytes: int = 960) -> list[KwsHit]:
        """离线把一段 PCM 全部喂进去（用于 wav 测试）。"""
        hits: list[KwsHit] = []
        for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            hit = self.feed_frame(pcm[offset : offset + frame_bytes])
            if hit is not None:
                hits.append(hit)
        return hits

    def reset(self) -> None:
        try:
            self._spotter.reset_stream(self._stream)
        except Exception:  # pragma: no cover
            pass

    def close(self) -> None:
        self._stream = None
        self._spotter = None
