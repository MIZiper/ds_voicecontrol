"""终端输出小工具：统一 UTF-8 输出、可选彩色、日志级别。"""

from __future__ import annotations

import os
import sys

_COLORS = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}

_use_color: bool = False


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def setup_console(color: bool | None = None) -> None:
    """让 Windows 控制台按 UTF-8 输出中文，并按需打开 ANSI 颜色。"""
    global _use_color
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    if color is None:
        color = _supports_color()
    _use_color = bool(color)
    if _use_color and os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            _use_color = False


def paint(text: str, color: str) -> str:
    if not _use_color:
        return text
    return f"{_COLORS.get(color, '')}{text}{_COLORS['reset']}"


def plain(message: str = "") -> None:
    print(message)


def info(message: str) -> None:
    print(paint(message, "cyan"))


def ok(message: str) -> None:
    print(paint(message, "green"))


def warn(message: str) -> None:
    print(paint(f"[警告] {message}", "yellow"), file=sys.stderr)


def error(message: str) -> None:
    print(paint(f"[错误] {message}", "red"), file=sys.stderr)


def dim(message: str) -> None:
    print(paint(message, "dim"))


def title(message: str) -> None:
    print(paint(message, "bold"))
