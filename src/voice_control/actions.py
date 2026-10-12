"""动作执行：把命令表里的动作变成真实的按键 / 程序操作。

支持的动作类型（和配置里的 type 对应）：

* ``hotkey``   组合键，例如 ctrl+c（先按修饰键，再点主键）
* ``press``    依次按下的键，可重复（repeat / interval_ms）
* ``text``     直接输入文字，支持槽位 ``{query}`` 和中文
* ``app``      打开程序、文件或网址
* ``url``      打开网址（域名自动补 https://，只允许 http/https）
* ``focus``    把标题匹配的窗口切到前台（按键发错窗口时很有用）
* ``click``    在屏幕坐标处模拟鼠标点击（x / y / button / count，坐标用 `vc pos` 测量）
* ``seek``     跳到当前网页视频的第 N 秒（bilibili / YouTube：改写地址栏 ``t=`` 参数）
* ``wait``     等待若干毫秒
* ``shell``    执行命令行（默认被 commands.allow_shell = false 拦住）
* ``sequence`` 组合动作，按顺序执行

Windows 下按键和文字都用同一套 ``SendInput`` 实现（``VK`` 码给组合键、``KEYEVENTF_UNICODE``
给任意字符），因此不依赖输入法、也不受键盘布局影响；只导入一次 user32，避免多个库互相覆盖
``argtypes`` 导致 ``expected LP_INPUT instance`` 这类错误。其它平台用 pynput 兜底（可选依赖）。
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import os
import re
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import ACTION_TYPES

# --------------------------------------------------------------------------- #
# 虚拟键码表
# --------------------------------------------------------------------------- #
VK_CODES: dict[str, int] = {
    # 修饰键
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "windows": 0x5B,
    "cmd": 0x5B,
    "super": 0x5B,
    "meta": 0x5B,
    # 常用
    "enter": 0x0D,
    "return": 0x0D,
    "回车": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "空格": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "page_up": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "page_down": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "capslock": 0x14,
    "caps_lock": 0x14,
    "numlock": 0x90,
    "num_lock": 0x90,
    "printscreen": 0x2C,
    "print_screen": 0x2C,
    "scrolllock": 0x91,
    "scroll_lock": 0x91,
    "pause": 0x13,
    "menu": 0x5D,
    # 媒体键
    "volumeup": 0xAF,
    "volume_up": 0xAF,
    "volumedown": 0xAE,
    "volume_down": 0xAE,
    "mute": 0xAD,
    "volume_mute": 0xAD,
    "playpause": 0xB3,
    "play_pause": 0xB3,
    "nexttrack": 0xB0,
    "next": 0xB0,
    "prevtrack": 0xB1,
    "previous": 0xB1,
    "stop": 0xB2,
}

# 需要 KEYEVENTF_EXTENDEDKEY 的键（方向键、翻页、媒体键、Win 键等）
EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # PgUp/PgDn/End/Home/方向键
    0x2C,  # PrintScreen
    0x2D, 0x2E,  # Insert / Delete
    0x5B, 0x5C, 0x5D,  # Win / Win / Menu
    0x6F,  # 小键盘除号
    0x90,  # NumLock
    0xA3,  # 右 Ctrl
    0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,  # 音量/媒体
}

MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C}

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1

# --- 鼠标 ------------------------------------------------------------------- #
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

MOUSE_BUTTONS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# GetSystemMetrics 的索引：虚拟屏幕（多显示器时可能是负数坐标）
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def known_key_names() -> list[str]:
    names = sorted(VK_CODES)
    return names + [f"f{i}" for i in range(1, 25)] + ["a-z", "0-9", "单个符号"]


# --------------------------------------------------------------------------- #
# 按键解析（纯逻辑，方便单测）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KeySpec:
    """一个按键：``vk`` 走虚拟键码，``char`` 走 Unicode 事件。"""

    kind: str  # "vk" | "char"
    value: int  # vk 码或 Unicode 码点
    shift: bool = False  # 该字符需要 Shift（例如 "+"、"?"）

    @property
    def is_modifier(self) -> bool:
        return self.kind == "vk" and self.value in MODIFIER_VKS


def resolve_key(name: str) -> KeySpec:
    """把配置里的按键名解析成 :class:`KeySpec`。"""
    raw = str(name).strip()
    lowered = raw.lower()
    if lowered in VK_CODES:
        return KeySpec("vk", VK_CODES[lowered])
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", lowered):
        return KeySpec("vk", 0x6F + int(lowered[1:]))  # F1 = 0x70
    if len(raw) == 1:
        return _char_spec(raw)
    raise ActionError(f"不认识的按键名 “{name}”。可用：{', '.join(known_key_names())}")


def _char_spec(char: str) -> KeySpec:
    if os.name != "nt":  # pragma: no cover - 非 Windows 由 pynput 兜底
        return KeySpec("char", ord(char))
    if char.isascii() and char.isalnum():
        return KeySpec("vk", ord(char.upper()))
    user32 = _user32()
    result = int(user32.VkKeyScanW(ctypes.c_wchar(char)))
    if result == -1:
        return KeySpec("char", ord(char))
    vk = result & 0xFF
    shift = bool((result >> 8) & 0x01)
    return KeySpec("vk", vk, shift=shift)


# --------------------------------------------------------------------------- #
# Windows SendInput
# --------------------------------------------------------------------------- #
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("u", _INPUTUNION))


_user32_handle: Any = None
_sendinput_original: tuple[Any, Any] | None = None


def _user32():
    """初始化 user32，并记录 ``SendInput`` 原本的参数类型。

    之所以要记录：其它同样调用 ``user32.SendInput`` 的库（各种按键/自动化工具）有的依赖
    “不设 argtypes”。我们调用前后恢复原状，就不会出现
    ``expected LP_INPUT instance`` 这类互相打架的错误。
    """
    global _user32_handle, _sendinput_original
    if _user32_handle is None:
        if os.name != "nt":  # pragma: no cover
            raise ActionError("按键发送目前只支持 Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        _sendinput_original = (user32.SendInput.argtypes, user32.SendInput.restype)
        user32.SendInput.argtypes = (
            ctypes.c_uint,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        user32.SendInput.restype = ctypes.c_uint
        _user32_handle = user32
    return _user32_handle


def _map_scan(vk: int) -> int:
    try:
        return int(_user32().MapVirtualKeyW(vk, 0)) & 0xFF  # MAPVK_VK_TO_VSC
    except Exception:  # pragma: no cover
        return 0


def _vk_input(vk: int, up: bool) -> _INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return _INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(vk, _map_scan(vk), flags, 0, None)))


def _char_inputs(char: str, up: bool) -> list[_INPUT]:
    """任意字符（含中文）走 KEYEVENTF_UNICODE，必要时用代理对表示。"""
    code = ord(char)
    units = [code] if code <= 0xFFFF else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return [_INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(0, unit, flags, 0, None))) for unit in units]


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, data, flags, 0, None)))


def normalized_point(
    x: float,
    y: float,
    left: int,
    top: int,
    width: int,
    height: int,
) -> tuple[int, int]:
    """屏幕坐标 -> SendInput 要的 0~65535 绝对坐标（纯函数，方便单测）。

    多显示器时 ``left``/``top`` 可能是负数，所以先减掉虚拟屏幕原点再按宽高归一化。
    """
    if width <= 1 or height <= 1:
        return (0, 0)
    nx = int(round((float(x) - float(left)) * 65535 / (width - 1)))
    ny = int(round((float(y) - float(top)) * 65535 / (height - 1)))
    return (max(0, min(65535, nx)), max(0, min(65535, ny)))


def virtual_screen() -> tuple[int, int, int, int]:
    """虚拟屏幕（含全部显示器）的 ``(left, top, width, height)``。"""
    user32 = _user32()
    return (
        int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


def cursor_position() -> tuple[int, int]:
    """当前鼠标位置（屏幕坐标），给 `vc pos` 量坐标用。"""
    if os.name != "nt":  # pragma: no cover - 非 Windows 不做鼠标
        raise ActionError("读取鼠标位置目前只支持 Windows")
    from ctypes import wintypes

    point = wintypes.POINT()
    if not _user32().GetCursorPos(ctypes.byref(point)):
        raise ActionError("读取鼠标位置失败")
    return (int(point.x), int(point.y))


def send_events(events: Sequence[_INPUT]) -> None:
    if not events:
        return
    user32 = _user32()
    struct_size = ctypes.sizeof(_INPUT)
    batch = 64
    for start in range(0, len(events), batch):
        chunk = list(events[start : start + batch])
        array = (_INPUT * len(chunk))(*chunk)
        sent = user32.SendInput(len(chunk), array, struct_size)
        if not sent:
            error = ctypes.get_last_error()
            raise ActionError(
                f"SendInput 发送失败（系统错误码 {error}）。"
                "常见原因：目标窗口以管理员权限运行，或前台程序屏蔽了模拟按键。"
            )
        time.sleep(0.005)
    # 用完恢复原样，避免干扰其它同样使用 SendInput 的库
    if _sendinput_original is not None:
        try:
            user32.SendInput.argtypes, user32.SendInput.restype = _sendinput_original
        except Exception:  # pragma: no cover
            pass


class ActionError(RuntimeError):
    """动作无法执行时抛出。"""


def click_at(
    x: float,
    y: float,
    button: str = "left",
    count: int = 1,
    interval_ms: int = 60,
) -> None:
    """在屏幕坐标 ``(x, y)`` 处模拟鼠标点击（连点 ``count`` 次）。"""
    if os.name != "nt":  # pragma: no cover - 非 Windows 不做鼠标
        raise ActionError("模拟点击目前只支持 Windows")
    name = str(button or "left").strip().lower()
    if name not in MOUSE_BUTTONS:
        raise ActionError(f"不认识的鼠标键 “{button}”。可用：{'/'.join(MOUSE_BUTTONS)}")
    down, up = MOUSE_BUTTONS[name]
    left, top, width, height = virtual_screen()
    nx, ny = normalized_point(x, y, left, top, width, height)
    move = _mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)
    total = max(1, int(count))
    for index in range(total):
        if index:
            time.sleep(max(0.0, float(interval_ms)) / 1000.0)
        # 每次点击单独发一批：连点之间的间隔要够，否则会被当成一次点击
        send_events([move, _mouse_input(down), _mouse_input(up)])


# --------------------------------------------------------------------------- #
# 网址 / 时间 / 剪贴板（url 与 seek 动作用）
# --------------------------------------------------------------------------- #
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*)://", re.IGNORECASE)
# 只认 ASCII 域名：主机名 + 至少两位的顶级域，可带端口和路径
_DOMAIN = re.compile(
    r"^[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)*\.[a-z]{2,}(?::\d+)?(?:[/?#].*)?$",
    re.IGNORECASE,
)
_TIME_UNITS = {"小时": 3600, "分钟": 60, "分": 60, "秒钟": 1, "秒": 1}
_CN_DIGITS = {
    "零": "0",
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}


def normalize_url(value: str) -> str:
    """把槽位里的域名补成完整网址（只接受 http/https）。"""
    text = str(value or "").strip().strip("。，、,;；")
    if not text:
        raise ActionError("没有拿到要打开的网址（槽位是空的）")
    scheme = _SCHEME.match(text)
    if scheme:
        if scheme.group(1).lower() not in ("http", "https"):
            raise ActionError(f"只允许打开 http/https 网址，收到 “{text}”")
        return text
    if _DOMAIN.match(text):
        return "https://" + text
    raise ActionError(
        f"“{text}” 不像网址。请在规则说明里让模型把网站名转成域名或完整链接"
        "（例如 baidu.com、https://www.zhihu.com）"
    )


def normalize_time_text(text: str) -> str:
    """把“三分二十秒”“一分半”这类中文写法换成阿拉伯数字，方便统一解析。"""
    out = str(text or "")
    for chinese, digit in _CN_DIGITS.items():
        out = out.replace(chinese, digit)
    # 十 / 十二 / 二十五 -> 10 / 12 / 25
    out = re.sub(
        r"([0-9]*)十([0-9]*)",
        lambda match: str(
            (int(match.group(1)) if match.group(1) else 1) * 10
            + (int(match.group(2)) if match.group(2) else 0)
        ),
        out,
    )
    # “3分半” = 3.5 分；“1小时半” = 1.5 小时
    out = re.sub(r"(\d+(?:\.\d+)?)\s*(小时|分钟|分)\s*半", r"\1.5\2", out)
    return out


def parse_seconds(value: Any) -> int:
    """把 ``200`` / ``3:20`` / ``1:02:03`` / ``3分20秒`` / ``一分半`` 解析成秒数。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(round(float(value))))
    text = normalize_time_text(str(value or "")).strip()
    if not text:
        raise ActionError("没有拿到时间（槽位是空的）")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(0, int(round(float(text))))
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        parts = [int(item) for item in text.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    total = 0.0
    matched = False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?|半)\s*(小时|分钟|分|秒钟|秒)", text):
        amount = 0.5 if number == "半" else float(number)
        total += amount * _TIME_UNITS[unit]
        matched = True
    if matched:
        return max(0, int(round(total)))
    raise ActionError(
        f"看不懂时间 “{value}”：可以试 “3:20”“3分20秒”“一分半”“200”（秒）"
    )


def format_seconds(seconds: Any) -> str:
    """秒数 -> ``3:20`` / ``1:02:03``（日志显示用）。"""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def with_time_param(url: str, seconds: Any) -> str:
    """把网址的 ``t=`` 参数设成指定秒数，其余参数保留。

    bilibili 与 YouTube 都支持用 ``t=``（秒）从指定时间开始播放。
    """
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise ActionError(f"地址栏里的内容不像网址：{url!r}")
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "t"
    ]
    query.append(("t", str(max(0, int(seconds)))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
_clipboard_ready = False


def _clipboard_api() -> tuple[Any, Any]:
    """取 user32 / kernel32 并设好剪贴板函数的参数类型（只需一次）。

    64 位下句柄是指针，不设 ``restype`` 会被 ctypes 截断成 32 位 int。
    """
    global _clipboard_ready
    user32 = _user32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not _clipboard_ready:
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalUnlock.restype = ctypes.c_int
        kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalFree.restype = ctypes.c_void_p
        user32.OpenClipboard.argtypes = (ctypes.c_void_p,)
        user32.OpenClipboard.restype = ctypes.c_int
        user32.CloseClipboard.restype = ctypes.c_int
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = (ctypes.c_uint,)
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
        _clipboard_ready = True
    return user32, kernel32


def read_clipboard_text() -> str:
    """读剪贴板里的文本；没有文本时返回空串。"""
    if os.name != "nt":  # pragma: no cover - 非 Windows 不做剪贴板
        raise ActionError("读取剪贴板目前只支持 Windows")
    user32, kernel32 = _clipboard_api()
    if not user32.OpenClipboard(None):
        raise ActionError("打不开剪贴板（可能被其它程序占用），稍后再试")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return str(ctypes.wstring_at(pointer))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def write_clipboard_text(text: str) -> bool:
    """把文本写回剪贴板（失败返回 False，不抛异常）。"""
    if os.name != "nt":  # pragma: no cover
        return False
    user32, kernel32 = _clipboard_api()
    buffer = ctypes.create_unicode_buffer(str(text))
    size = ctypes.sizeof(buffer)
    if not user32.OpenClipboard(None):
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, buffer, size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- #
# 执行上下文
# --------------------------------------------------------------------------- #
@dataclass
class ActionContext:
    """执行上下文：槽位文本、白名单、是否演练。"""

    dry_run: bool = False
    text: str = ""
    slots: dict[str, str] = field(default_factory=dict)
    declared_slots: Sequence[str] = ()
    allow: Sequence[str] = ACTION_TYPES
    allow_shell: bool = False


@dataclass
class ActionResult:
    ok: bool
    type: str
    detail: str

    def __str__(self) -> str:
        mark = "OK" if self.ok else "失败"
        return f"[{mark}] {self.type}: {self.detail}"


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
@dataclass
class ActionExecutor:
    allow: Sequence[str] = ACTION_TYPES
    allow_shell: bool = False
    dry_run: bool = False

    # -- 公共入口 ---------------------------------------------------------- #
    def execute(self, action: dict, context: ActionContext | None = None) -> list[ActionResult]:
        ctx = context or ActionContext(
            dry_run=self.dry_run,
            allow=tuple(self.allow),
            allow_shell=self.allow_shell,
        )
        return self._run(action if isinstance(action, dict) else {}, ctx)

    # -- 内部分发 ---------------------------------------------------------- #
    def _run(self, action: dict, ctx: ActionContext) -> list[ActionResult]:
        kind = str(action.get("type", "")).strip().lower()
        if kind not in ACTION_TYPES:
            return [ActionResult(False, kind or "?", f"未知动作类型（可用：{'/'.join(ACTION_TYPES)}）")]
        if kind not in tuple(ctx.allow):
            return [ActionResult(False, kind, "该动作类型不在 commands.allow 白名单中，已拒绝执行")]
        if kind == "shell" and not ctx.allow_shell:
            return [ActionResult(False, kind, "commands.allow_shell = false，已拒绝执行 shell 命令")]
        if kind == "sequence":
            return self._do_sequence(action, ctx)
        handler = getattr(self, f"_do_{kind}")
        try:
            return [handler(action, ctx)]
        except ActionError as exc:
            return [ActionResult(False, kind, str(exc))]
        except Exception as exc:  # pragma: no cover - 兜底，避免一条命令崩掉整个监听
            return [ActionResult(False, kind, f"{type(exc).__name__}: {exc}")]

    # -- 各动作 ------------------------------------------------------------ #
    def _do_hotkey(self, action: dict, ctx: ActionContext) -> ActionResult:
        keys = [str(k) for k in action.get("keys", []) or []]
        if not keys:
            raise ActionError("hotkey 需要 keys，例如 keys = [\"ctrl\", \"c\"]")
        combo = "+".join(keys)
        if ctx.dry_run:
            return ActionResult(True, "hotkey", f"{combo}（演练，未真正按下）")
        self._send_hotkey(keys)
        return ActionResult(True, "hotkey", combo)

    def _do_press(self, action: dict, ctx: ActionContext) -> ActionResult:
        keys = [str(k) for k in action.get("keys", []) or []]
        if not keys:
            raise ActionError("press 需要 keys，例如 keys = [\"enter\"]")
        try:
            repeat = max(1, int(action.get("repeat", 1) or 1))
        except (TypeError, ValueError):
            repeat = 1
        try:
            interval_ms = max(0, int(action.get("interval_ms", 80) or 0))
        except (TypeError, ValueError):
            interval_ms = 80
        detail = f"{'+'.join(keys)} x{repeat}"
        if ctx.dry_run:
            return ActionResult(True, "press", f"{detail}（演练，未真正按下）")
        for index in range(repeat):
            for key in keys:
                self._tap(key)
            if interval_ms and index < repeat - 1:
                time.sleep(interval_ms / 1000.0)
        return ActionResult(True, "press", detail)

    def _do_text(self, action: dict, ctx: ActionContext) -> ActionResult:
        text = self._render(str(action.get("text", "")), ctx)
        if not text:
            return ActionResult(False, "text", "渲染后的文本为空（可能是 {槽位} 没有内容）")
        if ctx.dry_run:
            return ActionResult(True, "text", f"输入 {text!r}（演练，未真正输入）")
        self._type_text(text)
        return ActionResult(True, "text", f"输入 {text!r}")

    def _do_app(self, action: dict, ctx: ActionContext) -> ActionResult:
        target = self._render(str(action.get("target", "")), ctx).strip()
        if not target:
            raise ActionError("app 需要 target，例如 target = \"notepad\" 或网址")
        if ctx.dry_run:
            return ActionResult(True, "app", f"打开 {target}（演练，未真正打开）")
        self._open_target(target, shell=bool(action.get("shell", False)), cwd=action.get("cwd"))
        return ActionResult(True, "app", f"打开 {target}")

    def _do_url(self, action: dict, ctx: ActionContext) -> ActionResult:
        raw = self._render(str(action.get("target", "")), ctx).strip()
        target = normalize_url(raw)
        if ctx.dry_run:
            return ActionResult(True, "url", f"打开 {target}（演练，未真正打开）")
        webbrowser.open(target)
        return ActionResult(True, "url", f"打开 {target}")

    def _do_click(self, action: dict, ctx: ActionContext) -> ActionResult:
        try:
            x = float(action.get("x"))
            y = float(action.get("y"))
        except (TypeError, ValueError) as exc:
            raise ActionError("click 需要 x 和 y（屏幕坐标，先用 `vc pos` 量一下）") from exc
        button = str(action.get("button", "left") or "left").strip().lower()
        if button not in MOUSE_BUTTONS:
            raise ActionError(f"不认识的鼠标键 “{button}”。可用：{'/'.join(MOUSE_BUTTONS)}")
        try:
            count = max(1, int(action.get("count", 1) or 1))
        except (TypeError, ValueError):
            count = 1
        try:
            interval_ms = max(0, int(action.get("interval_ms", 60) or 0))
        except (TypeError, ValueError):
            interval_ms = 60
        detail = f"({x:.0f}, {y:.0f}) {button} x{count}"
        if ctx.dry_run:
            return ActionResult(True, "click", f"点击 {detail}（演练，未真正点击）")
        click_at(x, y, button, count, interval_ms)
        return ActionResult(True, "click", f"点击 {detail}")

    def _do_seek(self, action: dict, ctx: ActionContext) -> ActionResult:
        seconds = self._seek_seconds(action, ctx)
        stamp = format_seconds(seconds)
        if ctx.dry_run:
            return ActionResult(True, "seek", f"跳到 {stamp}（演练，未跳转）")
        if os.name != "nt":  # pragma: no cover - 非 Windows 不操作地址栏
            raise ActionError("seek 动作目前只支持 Windows（需要操作浏览器地址栏）")
        original = ""
        try:
            original = read_clipboard_text()
        except ActionError:
            original = ""
        # 1) 选中地址栏并复制，拿到当前网址
        self._send_hotkey(["ctrl", "l"])
        time.sleep(0.25)
        self._send_hotkey(["ctrl", "c"])
        time.sleep(0.25)
        current = read_clipboard_text().strip()
        if not _SCHEME.match(current):
            if original:
                write_clipboard_text(original)
            raise ActionError(
                "没能从地址栏读到当前网址（先点一下浏览器窗口，让焦点在浏览器上再试）。"
                f"读到的内容是：{current[:80]!r}"
            )
        # 2) 改写 t= 参数后回到地址栏回车
        target = with_time_param(current, seconds)
        self._send_hotkey(["ctrl", "l"])
        time.sleep(0.15)
        self._type_text(target)
        time.sleep(0.1)
        self._tap("enter")
        if original and original != current:
            write_clipboard_text(original)
        return ActionResult(True, "seek", f"跳到 {stamp}：{target}")

    def _slot_value(self, ctx: ActionContext, name: str) -> str | None:
        wanted = str(name).strip().lower()
        for key, value in ctx.slots.items():
            if str(key).strip().lower() == wanted:
                return str(value)
        return None

    def _seek_seconds(self, action: dict, ctx: ActionContext) -> int:
        slot = str(action.get("slot", "") or "").strip()
        if slot:
            value = self._slot_value(ctx, slot)
            if value is None or not value.strip():
                raise ActionError(f"模型没有给出时间（槽位 “{slot}” 是空的）")
            return parse_seconds(value)
        if "seconds" in action:
            return parse_seconds(action.get("seconds"))
        raise ActionError("seek 需要 slot（槽位名）或 seconds（秒数）")

    def _do_focus(self, action: dict, ctx: ActionContext) -> ActionResult:
        title = self._render(str(action.get("title", "")), ctx).strip()
        if not title:
            raise ActionError("focus 需要 title，例如 title = \"记事本\"")
        if ctx.dry_run:
            return ActionResult(True, "focus", f"激活标题包含 {title!r} 的窗口（演练）")
        window = self._focus_window(title)
        return ActionResult(True, "focus", f"已激活窗口：{window}")

    def _do_wait(self, action: dict, ctx: ActionContext) -> ActionResult:
        try:
            ms = float(action.get("ms", 0))
        except (TypeError, ValueError) as exc:
            raise ActionError("wait 的 ms 必须是数字") from exc
        if ctx.dry_run:
            return ActionResult(True, "wait", f"{ms:.0f} ms（演练）")
        time.sleep(max(0.0, ms) / 1000.0)
        return ActionResult(True, "wait", f"{ms:.0f} ms")

    def _do_shell(self, action: dict, ctx: ActionContext) -> ActionResult:
        command = self._render(str(action.get("command", "")), ctx).strip()
        if not command:
            raise ActionError("shell 需要 command")
        if ctx.dry_run:
            return ActionResult(True, "shell", f"{command}（演练，未执行）")
        try:
            proc = subprocess.Popen(command, shell=True, cwd=action.get("cwd") or None)
        except OSError as exc:
            raise ActionError(f"命令启动失败：{exc}") from exc
        return ActionResult(True, "shell", f"已启动（pid={proc.pid}）：{command}")

    def _do_sequence(self, action: dict, ctx: ActionContext) -> list[ActionResult]:
        steps = action.get("steps") or []
        results: list[ActionResult] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                results.append(ActionResult(False, "sequence", f"steps[{index}] 不是表，已跳过"))
                break
            step_results = self._run(step, ctx)
            results.extend(step_results)
            if any(not item.ok for item in step_results):
                results.append(ActionResult(False, "sequence", f"第 {index + 1} 步失败，后续步骤已跳过"))
                break
        ok = all(item.ok for item in results)
        results.append(ActionResult(ok, "sequence", f"共 {len(steps)} 步"))
        return results

    # -- 文本模板 ---------------------------------------------------------- #
    def _render(self, template: str, ctx: ActionContext) -> str:
        now = _dt.datetime.now()
        declared = {str(name).lower() for name in ctx.declared_slots}

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            # 1) 模型填好的槽位优先（包括名叫 text 的槽位）
            for name, value in ctx.slots.items():
                if str(name).lower() == key:
                    return str(value)
            # 2) 内置占位符
            if key == "text":
                return ctx.text
            if key == "date":
                return now.date().isoformat()
            if key == "time":
                return now.strftime("%H:%M")
            if key == "datetime":
                return now.strftime("%Y-%m-%d %H:%M")
            # 3) 规则声明过、但模型没给的槽位 -> 空（宁可失败也不要输入 "{query}" 这种字面量）
            if key in declared:
                return ""
            return match.group(0)

        return _PLACEHOLDER.sub(replace, template)

    # -- 键盘 -------------------------------------------------------------- #
    def _send_hotkey(self, keys: Sequence[str]) -> None:
        if os.name != "nt":  # pragma: no cover - 非 Windows 兜底
            self._send_hotkey_fallback(keys)
            return
        specs = [resolve_key(key) for key in keys]
        mods: list[KeySpec] = [spec for spec in specs if spec.is_modifier]
        mains: list[KeySpec] = [spec for spec in specs if not spec.is_modifier]
        if not mains and mods:
            mains = [mods.pop()]
        # 字符需要 Shift（例如 "+"）时自动补上
        if any(spec.shift for spec in mains) and not any(spec.value == 0x10 for spec in mods):
            mods.append(KeySpec("vk", 0x10))

        events: list[_INPUT] = []
        for mod in mods:
            events.append(_vk_input(mod.value, up=False))
        for main in mains:
            if main.kind == "char":
                events.extend(_char_inputs(chr(main.value), up=False))
                events.extend(_char_inputs(chr(main.value), up=True))
            else:
                events.append(_vk_input(main.value, up=False))
                events.append(_vk_input(main.value, up=True))
        for mod in reversed(mods):
            events.append(_vk_input(mod.value, up=True))
        send_events(events)

    def _tap(self, name: str) -> None:
        if os.name != "nt":  # pragma: no cover
            self._tap_fallback(name)
            return
        spec = resolve_key(name)
        if spec.kind == "char":
            send_events(_char_inputs(chr(spec.value), up=False) + _char_inputs(chr(spec.value), up=True))
            return
        events = [_vk_input(spec.value, up=False), _vk_input(spec.value, up=True)]
        if spec.shift:
            events = [_vk_input(0x10, up=False), *events, _vk_input(0x10, up=True)]
        send_events(events)

    def _type_text(self, text: str) -> None:
        """输入文字：中文/任意字符走 KEYEVENTF_UNICODE，不依赖输入法与键盘布局。"""
        if os.name != "nt":  # pragma: no cover - 非 Windows 兜底
            self._type_text_fallback(text)
            return
        events: list[_INPUT] = []
        for index, chunk in enumerate(text.split("\n")):
            if index:
                events.append(_vk_input(0x0D, up=False))
                events.append(_vk_input(0x0D, up=True))
            for char in chunk:
                events.extend(_char_inputs(char, up=False))
                events.extend(_char_inputs(char, up=True))
        send_events(events)

    # -- 非 Windows 兜底（可选依赖 pynput） -------------------------------- #
    @staticmethod
    def _pynput_controller():  # pragma: no cover - 只在非 Windows 用到
        try:
            from pynput.keyboard import Controller
        except Exception as exc:
            raise ActionError(
                "非 Windows 平台需要安装 pynput 才能发送按键：uv sync --extra pynput"
            ) from exc
        return Controller()

    def _send_hotkey_fallback(self, keys: Sequence[str]) -> None:  # pragma: no cover
        controller = self._pynput_controller()
        for key in keys[:-1]:
            controller.press(key if len(key) > 1 else key)
        controller.press(keys[-1])
        controller.release(keys[-1])
        for key in reversed(keys[:-1]):
            controller.release(key)

    def _tap_fallback(self, name: str) -> None:  # pragma: no cover
        controller = self._pynput_controller()
        controller.press(name)
        controller.release(name)

    def _type_text_fallback(self, text: str) -> None:  # pragma: no cover
        self._pynput_controller().type(text)

    # -- 打开目标 ---------------------------------------------------------- #
    def _open_target(self, target: str, shell: bool = False, cwd: Any = None) -> None:
        if _URL_PREFIX.match(target):
            webbrowser.open(target)
            return
        path = Path(target).expanduser()
        if path.exists():
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":  # pragma: no cover
                subprocess.Popen(["open", str(path)])
            else:  # pragma: no cover
                subprocess.Popen(["xdg-open", str(path)])
            return
        try:
            subprocess.Popen(target, shell=shell, cwd=cwd or None)
        except FileNotFoundError as exc:
            raise ActionError(f"找不到可执行文件 “{target}”") from exc
        except OSError as exc:
            raise ActionError(f"打开 “{target}” 失败：{exc}") from exc

    # -- 窗口激活 ---------------------------------------------------------- #
    def _focus_window(self, title: str) -> str:
        """把标题里包含 ``title`` 的窗口切到前台（只支持 Windows）。"""
        if os.name != "nt":
            raise ActionError("focus 动作目前只支持 Windows")
        from ctypes import wintypes

        user32 = _user32()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wanted = title.lower()
        matches: list[tuple[int, str]] = []

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            name = buffer.value
            if wanted in name.lower():
                matches.append((int(hwnd), name))
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        if not matches:
            raise ActionError(
                f"没有找到标题里包含 “{title}” 的窗口。"
                "（focus 只匹配可见窗口的标题，可以用任务管理器确认窗口标题）"
            )
        hwnd, name = matches[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = kernel32.GetCurrentThreadId()
        attached = False
        if target_thread and target_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            ok = user32.SetForegroundWindow(hwnd)
            if not ok:
                # 前台窗口有锁，用最小化+还原或 SwitchToThisWindow 再试
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ok = user32.SetForegroundWindow(hwnd)
            if not ok:
                try:
                    user32.SwitchToThisWindow(hwnd, True)
                    ok = int(user32.GetForegroundWindow()) == hwnd
                except Exception:  # pragma: no cover
                    ok = False
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, target_thread, False)
        if not ok:
            raise ActionError(
                f"找到窗口 “{name}” 但没能切到前台（Windows 限制后台程序抢焦点）。"
                "可以先手动点一下该窗口，或让这个程序以管理员身份运行后再试。"
            )
        return name


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_URL_PREFIX = re.compile(r"^(https?|mailto|file)://", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 完整性级别诊断（判断“为什么按键没生效”）
# --------------------------------------------------------------------------- #
def _integrity_name(rid: int) -> str:
    if rid < 0x1000:
        return "Untrusted"
    if rid < 0x2000:
        return "Low"
    if rid < 0x3000:
        return "Medium"
    if rid < 0x4000:
        return "High"
    if rid < 0x5000:
        return "System"
    return "Protected"


def _token_integrity(token: Any) -> str:
    """从访问令牌里读完整性级别（Low 说明进程在受限沙箱里）。"""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetTokenInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    )
    advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_ulong)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong))

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = (("Label", SID_AND_ATTRIBUTES),)

    size = ctypes.c_ulong()
    token_integrity_level = 25  # TokenIntegrityLevel
    advapi32.GetTokenInformation(token, token_integrity_level, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value or 64)
    if not advapi32.GetTokenInformation(
        token, token_integrity_level, buffer, size.value, ctypes.byref(size)
    ):
        return "Unknown"
    label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
    count = advapi32.GetSidSubAuthorityCount(label.Label.Sid)
    if not count:
        return "Unknown"
    rid = advapi32.GetSidSubAuthority(label.Label.Sid, count.contents.value - 1)
    if not rid:
        return "Unknown"
    return _integrity_name(int(rid.contents.value))


def process_integrity_level() -> str:
    """当前进程的完整性级别：Low / Medium / High / System …"""
    if os.name != "nt":
        return "Unknown"
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return "Unknown"
    try:
        return _token_integrity(token)
    finally:
        kernel32.CloseHandle(token)


def window_process_id(hwnd: int) -> int:
    """窗口所属进程的 PID（拿不到返回 0）。"""
    if os.name != "nt" or not hwnd:
        return 0
    from ctypes import wintypes

    user32 = _user32()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_integrity_level(hwnd: int) -> str:
    """某个窗口所属进程的完整性级别（拿不到就返回 Unknown）。"""
    if os.name != "nt" or not hwnd:
        return "Unknown"
    from ctypes import wintypes

    user32 = _user32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    process_query_limited_information = 0x1000

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return "Unknown"
    process = kernel32.OpenProcess(process_query_limited_information, False, pid.value)
    if not process:
        return "Unknown"
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return "Unknown"
        try:
            return _token_integrity(token)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def foreground_window() -> tuple[int, str]:
    """返回 (窗口句柄, 标题)。"""
    if os.name != "nt":
        return 0, ""
    user32 = _user32()
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return 0, ""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return hwnd, buffer.value


def integrity_via_whoami(timeout: float = 5.0) -> str:
    """用系统自带的 whoami 独立校验一次完整性级别（避免只信我们自己读的令牌）。

    解析 SID（``S-1-16-<rid>``）而不是文字，避免不同语言/版本的显示差异。
    """
    if os.name != "nt":
        return "Unknown"
    try:
        result = subprocess.run(
            ["whoami", "/groups", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:  # pragma: no cover - whoami 不可用时静默降级
        return "Unknown"
    if result.returncode != 0:
        return "Unknown"
    match = re.search(r"S-1-16-(\d+)", result.stdout or "")
    if not match:
        return "Unknown"
    return _integrity_name(int(match.group(1)))


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    )


def process_integrity_by_pid(pid: int) -> str:
    """查询任意进程的完整性级别（拿不到返回 Unknown）。"""
    if os.name != "nt" or not pid:
        return "Unknown"
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    process_query_limited_information = 0x1000
    process = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not process:
        return "Unknown"
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return "Unknown"
        try:
            return _token_integrity(token)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def process_parent_chain(max_depth: int = 8) -> list[str]:
    """返回当前进程往上的启动链，每一层带上它自己的完整性级别。

    例如 ``['python.exe(1234)[Low]', 'pwsh.exe(5678)[Medium]', ...]``。
    用来回答“我这个终端到底是谁启动的、权限是在哪一层被降下来的”。
    """
    if os.name != "nt":
        return []
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            parents: dict[int, tuple[int, str]] = {}
            if not kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                return []
            while True:
                parents[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    str(entry.szExeFile),
                )
                if not kernel32.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(snapshot))

        chain: list[str] = []
        pid = os.getpid()
        for _ in range(max_depth):
            info = parents.get(pid)
            if info is None:
                chain.append(f"<pid {pid} 已退出/不可见>")
                break
            parent_pid, name = info
            chain.append(f"{name}({pid})[{process_integrity_by_pid(pid)}]")
            if parent_pid in (0, pid):
                break
            if parent_pid not in parents:
                chain.append(f"<pid {parent_pid} 已退出/不可见>")
                break
            pid = parent_pid
        return chain
    except Exception:  # pragma: no cover
        return []


def chain_looks_like_interactive_shell(chain: list[str]) -> bool:
    """启动链里能看到资源管理器/终端宿主，才像是“人自己在终端里敲的”。"""
    markers = (
        "explorer.exe",
        "windowsterminal.exe",
        "wt.exe",
        "conhost.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
    )
    return any(marker in link.lower() for link in chain for marker in markers)
