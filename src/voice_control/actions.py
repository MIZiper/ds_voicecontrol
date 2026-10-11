"""动作执行：把口令规则里的动作变成真实的按键 / 程序操作。

支持的动作类型（和配置里的 type 对应）：

* ``hotkey``   组合键，例如 ctrl+c（先按修饰键，再点主键）
* ``press``    依次按下的键，可重复（repeat / interval_ms）
* ``text``     直接输入文字，支持 CJK（Windows 下用 SendInput 的 Unicode 模式，
               不依赖输入法，也不受键盘布局影响）
* ``app``      打开程序、文件或网址
* ``wait``     等待若干毫秒
* ``shell``    执行命令行（默认被 commands.allow_shell = false 拦住）
* ``sequence`` 组合动作，按顺序执行
"""

from __future__ import annotations

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

from .config import ACTION_TYPES

MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "cmd",
    "windows": "cmd",
    "cmd": "cmd",
    "super": "cmd",
    "meta": "cmd",
}

SPECIAL_ALIASES = {
    "enter": "enter",
    "return": "enter",
    "回车": "enter",
    "esc": "esc",
    "escape": "esc",
    "space": "space",
    "空格": "space",
    "tab": "tab",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "ins": "insert",
    "home": "home",
    "end": "end",
    "pageup": "page_up",
    "pgup": "page_up",
    "page_up": "page_up",
    "pagedown": "page_down",
    "pgdn": "page_down",
    "page_down": "page_down",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "capslock": "caps_lock",
    "caps_lock": "caps_lock",
    "numlock": "num_lock",
    "num_lock": "num_lock",
    "printscreen": "print_screen",
    "print_screen": "print_screen",
    "scrolllock": "scroll_lock",
    "scroll_lock": "scroll_lock",
    "pause": "pause",
    "menu": "menu",
    "volumeup": "media_volume_up",
    "volume_up": "media_volume_up",
    "volumedown": "media_volume_down",
    "volume_down": "media_volume_down",
    "mute": "media_volume_mute",
    "volume_mute": "media_volume_mute",
    "playpause": "media_play_pause",
    "play_pause": "media_play_pause",
    "nexttrack": "media_next",
    "next": "media_next",
    "prevtrack": "media_previous",
    "previous": "media_previous",
    "stop": "media_stop",
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_URL_PREFIX = re.compile(r"^(https?|mailto|file)://", re.IGNORECASE)


class ActionError(RuntimeError):
    """动作无法执行时抛出。"""


def known_key_names() -> list[str]:
    names = set(MODIFIER_ALIASES) | set(SPECIAL_ALIASES)
    extras = [f"f{i}" for i in range(1, 21)] + ["a-z", "0-9", "单个符号"]
    return sorted(names) + extras


@dataclass
class ActionContext:
    """执行上下文：槽位文本、白名单、是否演练。"""

    dry_run: bool = False
    text: str = ""
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
# Windows Unicode 文本注入
# --------------------------------------------------------------------------- #
def _utf16_units(char: str) -> list[int]:
    code = ord(char)
    if code <= 0xFFFF:
        return [code]
    code -= 0x10000
    return [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]


def _send_unicode_windows(text: str) -> int:
    """用 SendInput 的 KEYEVENTF_UNICODE 输入任意字符（含中文）。返回发送的事件数。"""
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        )

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        )

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class INPUTUNION(ctypes.Union):
        _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))

    class INPUT(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("u", INPUTUNION))

    keyeventf_keyup = 0x0002
    keyeventf_unicode = 0x0004

    events: list[Any] = []
    for char in text:
        units = _utf16_units(char)
        for unit in units:
            events.append(INPUT(type=1, u=INPUTUNION(ki=KEYBDINPUT(0, unit, keyeventf_unicode, 0, 0))))
        for unit in reversed(units):
            events.append(
                INPUT(
                    type=1,
                    u=INPUTUNION(
                        ki=KEYBDINPUT(0, unit, keyeventf_unicode | keyeventf_keyup, 0, 0)
                    ),
                )
            )
    if not events:
        return 0

    send_input = ctypes.windll.user32.SendInput
    send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    send_input.restype = wintypes.UINT

    struct_size = ctypes.sizeof(INPUT)
    batch = 64
    sent_total = 0
    for start in range(0, len(events), batch):
        chunk = events[start : start + batch]
        array = (INPUT * len(chunk))(*chunk)
        sent = send_input(len(chunk), array, struct_size)
        if not sent:
            raise ActionError("SendInput 输入失败（可能被前台程序或权限策略拦截）")
        sent_total += int(sent)
        time.sleep(0.01)
    return sent_total


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
@dataclass
class ActionExecutor:
    allow: Sequence[str] = ACTION_TYPES
    allow_shell: bool = False
    dry_run: bool = False
    _controller: Any = field(default=None, init=False, repr=False)

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
        except Exception as exc:  # pragma: no cover - 兜底，避免一条规则崩掉整个监听
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
            return ActionResult(False, "text", "渲染后的文本为空（可能是 {text} 槽位没有内容）")
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

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "text":
                return ctx.text
            if key == "date":
                return now.date().isoformat()
            if key == "time":
                return now.strftime("%H:%M")
            if key == "datetime":
                return now.strftime("%Y-%m-%d %H:%M")
            return match.group(0)

        return _PLACEHOLDER.sub(replace, template)

    # -- 键盘 -------------------------------------------------------------- #
    @property
    def controller(self):
        if self._controller is None:
            try:
                from pynput.keyboard import Controller
            except Exception as exc:  # pragma: no cover
                raise ActionError(f"无法加载键盘控制库 pynput：{exc}") from exc
            self._controller = Controller()
        return self._controller

    @staticmethod
    def _keyboard_module():
        try:
            from pynput import keyboard
        except Exception as exc:  # pragma: no cover
            raise ActionError(f"无法加载键盘控制库 pynput：{exc}") from exc
        return keyboard

    def _resolve_key(self, name: str):
        keyboard = self._keyboard_module()
        raw = str(name).strip()
        lowered = raw.lower()
        if lowered in MODIFIER_ALIASES:
            return getattr(keyboard.Key, MODIFIER_ALIASES[lowered])
        if lowered in SPECIAL_ALIASES:
            attr = SPECIAL_ALIASES[lowered]
            key = getattr(keyboard.Key, attr, None)
            if key is None:
                raise ActionError(f"当前 pynput 版本不支持按键 “{name}”")
            return key
        if len(raw) == 1:
            return keyboard.KeyCode.from_char(raw.lower())
        if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", lowered):
            key = getattr(keyboard.Key, lowered, None)
            if key is None:
                raise ActionError(f"当前 pynput 版本不支持按键 “{name}”")
            return key
        raise ActionError(
            f"不认识的按键名 “{name}”。可用：{', '.join(known_key_names())}"
        )

    def _modifier_set(self) -> set:
        keyboard = self._keyboard_module()
        names = (
            "ctrl",
            "ctrl_l",
            "ctrl_r",
            "alt",
            "alt_l",
            "alt_r",
            "alt_gr",
            "shift",
            "shift_l",
            "shift_r",
            "cmd",
            "cmd_l",
            "cmd_r",
        )
        return {getattr(keyboard.Key, name) for name in names if hasattr(keyboard.Key, name)}

    def _send_hotkey(self, keys: Sequence[str]) -> None:
        resolved = [self._resolve_key(key) for key in keys]
        modifiers = self._modifier_set()
        mods = [key for key in resolved if key in modifiers]
        mains = [key for key in resolved if key not in modifiers]
        if not mains and mods:
            mains = [mods.pop()]
        controller = self.controller
        for mod in mods:
            controller.press(mod)
        try:
            for key in mains:
                controller.press(key)
                controller.release(key)
        finally:
            for mod in reversed(mods):
                controller.release(mod)

    def _tap(self, name: str) -> None:
        key = self._resolve_key(name)
        controller = self.controller
        controller.press(key)
        controller.release(key)

    def _type_text(self, text: str) -> None:
        if os.name == "nt":
            for index, chunk in enumerate(text.split("\n")):
                if index:
                    self._tap("enter")
                if chunk:
                    _send_unicode_windows(chunk)
            return
        # 非 Windows：交给 pynput（受键盘布局限制，但至少能用）
        try:
            self.controller.type(text)
        except Exception as exc:  # pragma: no cover
            raise ActionError(f"输入文本失败：{exc}") from exc

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
