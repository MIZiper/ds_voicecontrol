"""唤醒词模型管理：查找本地模型、按需下载（sherpa-onnx 官方 KWS 模型）。

模型文件比较多（encoder/decoder/joiner + tokens.txt），这里统一负责：
下载 tar.bz2、解压、校验目录结构。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

KWS_MODEL_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models"
MIRROR_ENV = "VC_MODEL_MIRROR"

KNOWN_MODELS: dict[str, tuple[str, int]] = {
    "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01": (
        "中文唤醒词模型（约 31MB，支持自定义中文唤醒词）",
        31,
    ),
    "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20": (
        "中英双语唤醒词模型（约 38MB）",
        38,
    ),
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01": (
        "英文唤醒词模型（约 19MB）",
        19,
    ),
}


class ModelError(RuntimeError):
    """模型缺失或下载失败。"""


@dataclass
class ModelInfo:
    name: str
    description: str
    size_mb: int
    url: str
    mirror_url: str = ""


def model_info(name: str, mirror: str = "") -> ModelInfo:
    description, size = KNOWN_MODELS.get(name, ("自定义/未收录的唤醒词模型", 0))
    base = (mirror or os.environ.get(MIRROR_ENV, "")).strip().rstrip("/")
    url = f"{KWS_MODEL_BASE}/{name}.tar.bz2"
    mirror_url = f"{base}/{name}.tar.bz2" if base else ""
    return ModelInfo(name=name, description=description, size_mb=size, url=url, mirror_url=mirror_url)


def looks_like_model(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "tokens.txt").is_file()
        and any(path.glob("encoder-*.onnx"))
        and any(path.glob("joiner-*.onnx"))
    )


def find_model(models_dir: Path, name: str) -> Path | None:
    """在 models_dir 下找已安装的唤醒词模型目录。"""
    if not models_dir.is_dir():
        return None
    direct = models_dir / name
    if looks_like_model(direct):
        return direct
    for child in sorted(models_dir.iterdir()):
        if child.name.lower() == name.lower() and looks_like_model(child):
            return child
    return None


def installed_models(models_dir: Path) -> list[Path]:
    if not models_dir.is_dir():
        return []
    return [child for child in sorted(models_dir.iterdir()) if looks_like_model(child)]


def _download(url: str, dest: Path, log: Callable[[str], None] = print) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"下载唤醒词模型：{url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            step = 2 * 1024 * 1024
            last_percent = -1
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(step)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if total:
                        percent = int(received * 100 / total)
                        if percent // 10 != last_percent // 10:
                            last_percent = percent
                            log(f"  已下载 {received / 1048576:.1f}/{total / 1048576:.1f} MB（{percent}%）")
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        raise ModelError(
            f"下载失败：{exc}\n"
            f"可以手动下载 {url} 解压到 {dest.parent} 目录；"
            f"或者设置环境变量 {MIRROR_ENV}=<镜像地址前缀> 使用镜像。"
        ) from exc
    log("下载完成。")
    return dest


def _extract(archive: Path, target: Path, log: Callable[[str], None] = print) -> None:
    log(f"解压到：{target}")
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:bz2") as handle:
            try:
                handle.extractall(target, filter="data")  # Python 3.12+
            except TypeError:  # pragma: no cover - 老版本 Python
                handle.extractall(target)
        return
    except Exception as exc:  # pragma: no cover - 退回系统 tar
        log(f"Python 解压失败（{exc}），改用系统 tar")
    result = subprocess.run(
        ["tar", "-xjf", str(archive), "-C", str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover
        raise ModelError(f"解压失败：{result.stderr.strip() or result.stdout.strip()}")


def ensure_model(
    models_dir: Path,
    name: str,
    auto_download: bool = True,
    mirror: str = "",
    log: Callable[[str], None] = print,
) -> Path:
    """确保唤醒词模型可用，返回模型目录。"""
    found = find_model(models_dir, name)
    if found is not None:
        return found
    info = model_info(name, mirror)
    if not auto_download:
        raise ModelError(
            f"没有找到唤醒词模型 “{name}”（查找目录：{models_dir}）。\n"
            f"运行 `vc setup` 自动下载，或手动下载 {info.url} 解压到该目录。"
        )
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"{name}.tar.bz2"
        try:
            _download(info.url, archive, log=log)
        except ModelError:
            if info.mirror_url:
                log(f"改用镜像：{info.mirror_url}")
                _download(info.mirror_url, archive, log=log)
            else:
                raise
        _extract(archive, models_dir, log=log)
    found = find_model(models_dir, name)
    if found is None:
        raise ModelError(f"解压后仍然找不到模型目录，请检查 {models_dir}")
    log(f"唤醒词模型就绪：{found}")
    return found


def remove_model(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
