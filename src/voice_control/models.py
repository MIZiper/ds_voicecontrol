"""ASR 模型管理：查找本地模型、按需下载。

* Vosk：官方提供 zip 包，这里负责下载 + 解压 + 校验。
* faster-whisper：由 HuggingFace 生态自己缓存到 models_dir，这里只做提示。
"""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

VOSK_MODEL_BASE = "https://alphacephei.com/vosk/models"

KNOWN_VOSK_MODELS: dict[str, tuple[str, int]] = {
    "vosk-model-small-cn-0.22": ("中文小模型（约 42MB，速度快，推荐先用）", 42),
    "vosk-model-cn-0.22": ("中文大模型（约 1.3GB，更准）", 1300),
    "vosk-model-small-en-us-0.15": ("英文小模型（约 40MB）", 40),
    "vosk-model-en-us-0.22": ("英文模型（约 1.8GB）", 1800),
}


class ModelError(RuntimeError):
    """模型缺失或下载失败。"""


@dataclass
class ModelInfo:
    name: str
    description: str
    size_mb: int
    url: str


def vosk_model_info(name: str) -> ModelInfo:
    desc, size = KNOWN_VOSK_MODELS.get(name, ("自定义/未收录的 Vosk 模型", 0))
    return ModelInfo(name=name, description=desc, size_mb=size, url=f"{VOSK_MODEL_BASE}/{name}.zip")


def _looks_like_model(path: Path) -> bool:
    return path.is_dir() and (
        (path / "conf").is_dir() or (path / "am").is_dir() or (path / "model.conf").is_file()
    )


def find_vosk_model(models_dir: Path, name: str) -> Path | None:
    """在 models_dir 下找已安装的模型目录。"""
    if not models_dir.is_dir():
        return None
    exact = models_dir / name
    if _looks_like_model(exact):
        return exact
    for child in sorted(models_dir.iterdir()):
        if child.name.lower() == name.lower() and _looks_like_model(child):
            return child
    # 允许用户把模型解压成别的名字：只有唯一一个模型目录时也认
    candidates = [child for child in sorted(models_dir.iterdir()) if _looks_like_model(child)]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _download(url: str, dest: Path, log: Callable[[str], None] = print) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"下载模型：{url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            step = 4 * 1024 * 1024
            last_reported = -1
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(step)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if total:
                        percent = int(received * 100 / total)
                        if percent // 5 != last_reported // 5:
                            last_reported = percent
                            log(f"  已下载 {received / 1048576:.1f}/{total / 1048576:.1f} MB（{percent}%）")
                    else:
                        log(f"  已下载 {received / 1048576:.1f} MB")
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        raise ModelError(
            f"下载失败：{exc}\n可以手动下载 {url} 并解压到 {dest.parent} 目录下。"
        ) from exc
    log("下载完成。")
    return dest


def _extract(archive: Path, target: Path, log: Callable[[str], None] = print) -> None:
    log(f"解压到：{target}")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    archive.unlink(missing_ok=True)


def ensure_vosk_model(
    models_dir: Path,
    name: str,
    auto_download: bool = True,
    log: Callable[[str], None] = print,
) -> Path:
    """确保 Vosk 模型可用，返回模型目录。"""
    found = find_vosk_model(models_dir, name)
    if found is not None:
        return found
    if not auto_download:
        raise ModelError(
            f"没有找到 Vosk 模型 “{name}”（查找目录：{models_dir}）。\n"
            f"可以直接运行 `vc setup` 自动下载，或手动下载 {vosk_model_info(name).url} "
            f"解压到该目录。"
        )
    archive = models_dir / f"{name}.zip"
    _download(vosk_model_info(name).url, archive, log=log)
    _extract(archive, models_dir, log=log)
    found = find_vosk_model(models_dir, name)
    if found is None:
        raise ModelError(f"解压后仍然找不到模型目录，请检查 {models_dir}")
    log(f"模型就绪：{found}")
    return found


def installed_vosk_models(models_dir: Path) -> list[Path]:
    if not models_dir.is_dir():
        return []
    return [child for child in sorted(models_dir.iterdir()) if _looks_like_model(child)]


def remove_model(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
