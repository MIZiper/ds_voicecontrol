"""命令行入口：``vc <子命令>``。

工作方式：本地只做「判断有没有说话」+「听唤醒词」，命令理解交给大模型，动作在本地执行。

子命令一览：

* ``vc init``        生成带注释的默认配置
* ``vc devices``     列出可用麦克风
* ``vc meter``       实时电平表（看麦克风有没有声音、门限合不合适）
* ``vc calibrate``   监听一小段，测环境底噪并写入静音门限
* ``vc setup``       下载本地唤醒词模型
* ``vc wake``        只测本地唤醒词（不联网、不调用大模型）
* ``vc rules``       查看允许执行的命令表
* ``vc keytest``     按键自测（排查“日志成功但程序没反应”）
* ``vc doctor``      自检 + 真实调用一次大模型，排查“为什么没执行”
* ``vc say 文本``    不走麦克风，直接把一句话交给大模型
* ``vc run``         开始监听（主功能）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__, ui
from .actions import (
    ActionError,
    ActionExecutor,
    chain_looks_like_interactive_shell,
    cursor_position,
    foreground_window,
    integrity_via_whoami,
    known_key_names,
    process_integrity_level,
    process_parent_chain,
    window_integrity_level,
    window_process_id,
)
from .audio import (
    AudioError,
    Microphone,
    read_wav_mono,
    list_input_devices,
    measure_noise,
    rms_of,
)
from .config import (
    Config,
    ConfigError,
    load_config,
    load_config_or_defaults,
    resolve_config_path,
    save_noise_threshold,
    write_default_config,
)
from .kws import KwsError, WakeWordSpotter, ensure_keywords_file
from .listener import Listener
from .models import ModelError, ensure_model, installed_models, model_info

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPT = 130
SELF_TEST_TITLE = "vc keytest（可以关掉我）"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _level_bar(level: float, threshold: float, width: int = 32) -> str:
    ceiling = max(threshold * 3.0, 0.02)
    filled = min(width, int(round(level / ceiling * width)))
    bar = "#" * filled + "-" * (width - filled)
    state = "说话" if level >= threshold else "静音"
    return f"[{bar}] 电平={level:.4f} 门限={threshold:.4f} {state}"


def _print_warnings(config: Config) -> None:
    for message in config.warnings:
        ui.warn(message)


def _load_for_run(args: argparse.Namespace) -> Config:
    path = resolve_config_path(args.config)
    if not path.exists():
        write_default_config(path)
        ui.info(f"首次运行：已生成默认配置 {path}（可以直接编辑它）")
    config = load_config(path)
    if getattr(args, "device", None):
        config.audio.device = args.device
    _print_warnings(config)
    problems = config.problems()
    if problems:
        raise ConfigError("配置有问题：" + "；".join(problems))
    return config


def _countdown(seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"\r  {remaining} 秒后开始……", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 24 + "\r", end="", flush=True)


def _prepare_spotter(config: Config, threshold: float | None = None) -> WakeWordSpotter:
    wake = config.wake
    model_dir = ensure_model(
        config.models_dir, wake.model, auto_download=wake.auto_download, log=ui.plain
    )
    keywords_file = ensure_keywords_file(
        model_dir, wake.words, wake.keywords_file or None, log=ui.info
    )
    return WakeWordSpotter(
        model_dir,
        keywords_file,
        threshold=wake.threshold if threshold is None else threshold,
        score=wake.score,
        num_threads=wake.num_threads,
        sample_rate=config.audio.sample_rate,
        log=ui.plain,
    )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    path = resolve_config_path(args.config)
    try:
        written = write_default_config(path, force=args.force)
    except ConfigError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    ui.ok(f"已生成配置文件：{written}")
    ui.plain("接下来的推荐步骤：")
    ui.plain("  1) 设置大模型 API Key，例如： $env:DASHSCOPE_API_KEY = \"sk-...\"")
    ui.plain("  2) uv run vc calibrate    # 监听 5 秒测环境底噪，写入静音门限")
    ui.plain("  3) uv run vc setup        # 下载本地唤醒词模型（约 31MB）")
    ui.plain("  4) uv run vc wake         # 只测唤醒词，确认能叫醒")
    ui.plain("  5) uv run vc doctor       # 自检并真实调用一次大模型")
    ui.plain("  6) uv run vc run          # 正式监听（Ctrl+C 退出）")
    return EXIT_OK


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        devices = list_input_devices()
    except AudioError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    if not devices:
        ui.warn("没有找到任何输入设备")
        return EXIT_ERROR
    ui.title("可用的输入设备（把编号或名称片段填到配置的 audio.device）：")
    for device in devices:
        ui.plain("  " + device.describe())
    return EXIT_OK


def cmd_meter(args: argparse.Namespace) -> int:
    config = load_config_or_defaults(args.config)
    threshold = args.threshold if args.threshold is not None else config.audio.noise_threshold
    mic = Microphone(
        sample_rate=config.audio.sample_rate,
        frame_ms=config.audio.frame_ms,
        device=args.device or config.audio.device,
    )
    ui.info(f"实时电平表（{args.seconds:.0f} 秒）。请正常说话，观察电平是否明显高于门限。")
    try:
        with mic:
            ui.plain(f"  输入设备 : {mic.describe()}")
            deadline = time.monotonic() + args.seconds
            peak = 0.0
            while time.monotonic() < deadline:
                level = rms_of(mic.read_frame())
                peak = max(peak, level)
                print("\r  " + _level_bar(level, threshold), end="", flush=True)
            print()
    except AudioError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        print()
        return EXIT_OK
    ui.plain(f"  这段时间的峰值电平：{peak:.4f}")
    if peak < threshold:
        ui.warn("峰值都没超过门限：麦克风可能没收到声音，或门限太高（把 audio.noise_threshold 调小）")
    elif peak > 0.6:
        ui.warn("峰值很高，可能离麦克风太近或增益过大（会削波，识别会变差）")
    return EXIT_OK


def cmd_calibrate(args: argparse.Namespace) -> int:
    path = resolve_config_path(args.config)
    config = load_config_or_defaults(path)
    seconds = float(args.seconds)
    ui.title("环境底噪校准")
    ui.plain("  说明：请保持平时的环境（空调/风扇开着没关系），校准期间尽量不要说话。")
    if args.device:
        config.audio.device = args.device
    mic = Microphone(
        sample_rate=config.audio.sample_rate,
        frame_ms=config.audio.frame_ms,
        device=config.audio.device,
    )
    try:
        with mic:
            ui.plain(f"  输入设备 : {mic.describe()}")
            _countdown(args.countdown)

            def show(level: float) -> None:
                print("\r  " + _level_bar(level, config.audio.noise_threshold), end="", flush=True)

            stats = measure_noise(mic, seconds=seconds, on_level=show)
            print()
    except AudioError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        print()
        ui.warn("已取消")
        return EXIT_INTERRUPT

    ui.plain("  " + stats.summary())
    suggested = stats.suggest_threshold()
    ui.ok(f"  建议静音门限 noise_threshold = {suggested:.5f}")
    ui.plain(
        f"  （运行时实际门限 = max(noise_threshold, 实时底噪 × {config.audio.threshold_factor})）"
    )
    if stats.max_rms > stats.median * 15 and stats.max_rms > 0.03:
        ui.warn("校准期间检测到明显的声音，建议安静时重测一次")
    if not args.no_write:
        try:
            save_noise_threshold(suggested, path)
            ui.ok(f"  已写入配置：{path} -> [audio] noise_threshold = {suggested:.5f}")
        except ConfigError as exc:
            ui.error(str(exc))
            return EXIT_ERROR
    else:
        ui.plain("  （--no-write：没有修改配置文件）")
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    config = load_config_or_defaults(args.config)
    models_dir = config.models_dir
    ui.info(f"模型目录：{models_dir}")
    installed = installed_models(models_dir)
    if installed:
        ui.plain("  已安装的唤醒词模型：")
        for path in installed:
            ui.plain(f"    - {path.name}")
    name = args.model or config.wake.model
    info = model_info(name)
    ui.plain(f"  目标模型：{name} —— {info.description}")
    try:
        path = ensure_model(models_dir, name, auto_download=True, log=ui.plain)
    except ModelError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    ui.ok(f"唤醒词模型就绪：{path}")
    try:
        keywords_file = ensure_keywords_file(
            path, config.wake.words, config.wake.keywords_file or None, log=ui.info
        )
        ui.plain(f"  唤醒词文件：{keywords_file}")
        for line in keywords_file.read_text(encoding="utf-8").splitlines():
            ui.plain(f"    {line}")
    except KwsError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    ui.plain("  下一步：`uv run vc wake` 测一下唤醒词，`uv run vc doctor` 自检大模型。")
    return EXIT_OK


def cmd_wake(args: argparse.Namespace) -> int:
    config = load_config_or_defaults(args.config)
    _print_warnings(config)
    if not config.wake.enabled:
        ui.warn("配置里 wake.enabled = false（不需要唤醒词），无需测试唤醒。")
    try:
        spotter = _prepare_spotter(config, threshold=args.threshold)
    except (ModelError, KwsError) as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    ui.info(f"唤醒词：{'、'.join(spotter.keywords) or '（未知）'}")
    if args.wav:
        try:
            pcm = read_wav_mono(args.wav, target_rate=config.audio.sample_rate)
        except AudioError as exc:
            ui.error(str(exc))
            return EXIT_ERROR
        frame_bytes = int(config.audio.sample_rate * config.audio.frame_ms / 1000) * 2
        hits = []
        for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            hit = spotter.feed_frame(pcm[offset : offset + frame_bytes])
            if hit is not None:
                hits.append(hit.keyword)
        if hits:
            ui.ok(f"命中唤醒词：{'、'.join(hits)}")
            return EXIT_OK
        ui.warn("没有检测到唤醒词。可以调低 wake.threshold（例如 0.15）再试。")
        return EXIT_ERROR

    mic = Microphone(
        sample_rate=config.audio.sample_rate,
        frame_ms=config.audio.frame_ms,
        device=args.device or config.audio.device,
    )
    ui.info(f"只测唤醒词，不联网（{args.seconds:.0f} 秒）。喊一句唤醒词看看有没有反应。")
    hits = 0
    last_hit = 0.0
    try:
        with mic:
            ui.plain(f"  输入设备 : {mic.describe()}")
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                frame = mic.read_frame()
                hit = spotter.feed_frame(frame)
                now = time.monotonic()
                if hit is not None and now - last_hit > 1.0:
                    last_hit = now
                    hits += 1
                    ui.ok(f"  第 {hits} 次命中：{hit.keyword}")
    except AudioError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        print()
    if hits:
        ui.ok(f"共命中 {hits} 次。")
        return EXIT_OK
    ui.warn(
        "一次都没命中。排查顺序：① `vc meter` 确认麦克风有电平；② 说的是配置里的唤醒词；"
        "③ 调低 wake.threshold（0.25 -> 0.15）；④ 换更常见的唤醒词。"
    )
    return EXIT_ERROR


def _make_executor(config: Config, *, dry_run: bool = False) -> ActionExecutor:
    """按配置创建动作执行器。"""
    return ActionExecutor(
        allow=tuple(config.commands.allow),
        allow_shell=config.commands.allow_shell,
        dry_run=dry_run,
    )


def _self_window_key_test() -> tuple[str, str]:
    """在自己创建的窗口里发一段文字和一次组合键并读回，验证按键链路。"""
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover
        return "skip", f"没有 tkinter，跳过自测：{exc}"
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover
        return "skip", f"无法创建测试窗口（可能没有桌面会话）：{exc}"
    marker = "vc-keytest-中英ABC123"
    our_pid = os.getpid()
    try:
        root.title(SELF_TEST_TITLE)
        root.geometry("460x140+120+120")
        widget = tk.Text(root, width=44, height=4)
        widget.pack()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        widget.focus_set()
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)

        hwnd, title = foreground_window()
        if window_process_id(hwnd) != our_pid:
            return (
                "skip",
                f"测试窗口没能拿到前台焦点（当前前台是 {title!r}），"
                "为了不把文字打进你正在用的窗口，已跳过按键自测",
            )

        executor = ActionExecutor(allow=("text", "hotkey"), dry_run=False)
        text_result = executor.execute({"type": "text", "text": marker})[0]
        hotkey_result = executor.execute({"type": "hotkey", "keys": ["ctrl", "a"]})[0]
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        content = widget.get("1.0", "end-1c")
        selected = widget.get("sel.first", "sel.last") if widget.tag_ranges("sel") else ""
        if content == marker and selected.strip() == marker:
            return "ok", f"文字与组合键都生效：{text_result.detail}；{hotkey_result.detail}"
        return (
            "fail",
            f"窗口读到 {content!r} / 选中 {selected!r}；"
            f"动作结果：{text_result.detail}；{hotkey_result.detail}",
        )
    finally:
        try:
            root.destroy()
        except Exception:  # pragma: no cover
            pass


def cmd_keytest(args: argparse.Namespace) -> int:
    ui.title("按键自测（判断“为什么日志显示成功但没反应”）")
    level = process_integrity_level()
    independent = integrity_via_whoami()
    ui.plain(f"  进程完整性级别：{level}（用系统 whoami 独立校验：{independent}）")
    if independent != "Unknown" and level != "Unknown" and independent != level:
        ui.warn(
            f"两次读取结果不一致（{level} vs {independent}），请把这段输出发给我，"
            "并以此为准：whoami 是系统自己给的。"
        )
    if level == "Low" or independent == "Low":
        ui.warn(
            "低完整性 = 这个进程被以受限方式启动的（长得像普通 PowerShell 也一样），"
            "Windows 的 UIPI 会静默拦掉模拟按键：日志显示 [OK]，但目标程序一个键都收不到。"
        )
        chain = process_parent_chain()
        if chain:
            ui.plain("  启动链（每一层都标了它自己的完整性级别）：")
            ui.plain("    " + "  ←  ".join(chain))
            first_medium = next(
                (link for link in chain if "[Medium]" in link or "[High]" in link), ""
            )
            if first_medium:
                ui.plain(
                    f"    ↑ 链上有 Medium 的进程（例如 {first_medium.split('[')[0]}），"
                    "而当前是 Low → 是它（或它中间的一层）把权限降下来后启动了这条命令。"
                )
            elif not chain_looks_like_interactive_shell(chain):
                ui.plain(
                    "    ↑ 链里没有 explorer / 终端宿主：说明这条命令是被某个启动器"
                    "（助手、IDE 任务、终端面板…）拉起的，不是你在交互式终端里敲的。"
                )
        ui.plain(
            "  自证方法：在同一个终端里执行  whoami /groups | findstr /i Mandatory\n"
            "            Medium = 正常终端；Low = 这个终端被限制过，按键会被系统丢掉。"
        )
        ui.plain(
            "  解决办法：开一个“干净”的终端再跑——按 Win+R 输入 wt（或从开始菜单打开 "
            "Windows Terminal / PowerShell），cd 到项目目录后执行 uv run vc run。"
        )

    hwnd, title = foreground_window()
    ui.plain(f"  当前前台窗口：{title!r}（所属进程完整性：{window_integrity_level(hwnd)}）")
    ui.plain("  说明：按键只会发给当前前台窗口；目标程序以管理员身份运行时，普通权限发不进去。")

    status, detail = _self_window_key_test()
    if status == "ok":
        ui.ok(f"  自测通过：{detail}")
    elif status == "skip":
        ui.warn(f"  自测跳过：{detail}")
    else:
        ui.error(f"  自测失败：{detail}")

    if args.cross_app:
        ui.plain("")
        ui.warn("6 秒后会向【当前前台窗口】输入一段测试文字，请先切到记事本并把光标放到输入区。")
        _countdown(6)
        marker = "vc-keytest-ok"
        executor = ActionExecutor(allow=("text", "hotkey"), dry_run=False)
        results = executor.execute({"type": "text", "text": marker})
        ui.plain(f"  已发送 {marker!r}：{results[0]}")
        ui.plain("  如果目标窗口里没出现这段文字，说明按键被系统拦了：")
        ui.plain("    · 进程在沙箱里（完整性 Low）→ 换普通终端")
        ui.plain("    · 目标程序以管理员身份运行 → 让 vc run 也以管理员运行")
        ui.plain("    · 或者发给了别的前台窗口 → 用 focus 动作先切窗口")
    return EXIT_OK if status != "fail" else EXIT_ERROR


def cmd_pos(args: argparse.Namespace) -> int:
    """实时显示鼠标坐标，用来量 click 动作要填的 x / y。"""
    ui.title("鼠标坐标（把鼠标移到目标按钮上，读最后一行）")
    if process_integrity_level() == "Low":
        ui.warn(
            "当前进程完整性级别是 Low：模拟点击和按键都会被系统丢掉。"
            "真要用点击功能，请换成不受限的解释器/终端。"
        )
    seconds = float(getattr(args, "seconds", 0) or 0)
    deadline = time.monotonic() + seconds if seconds > 0 else None
    last: tuple[int, int] | None = None
    ui.plain("")
    try:
        while True:
            last = cursor_position()
            print(f"\r  x = {last[0]:<6d} y = {last[1]:<6d}", end="", flush=True)
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    except ActionError as exc:
        print()
        ui.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        pass
    print()
    if last is None:
        return EXIT_OK
    x, y = last
    ui.ok(f"最后位置：x = {x}, y = {y}")
    ui.plain("  填进 config.toml 的 [[rules]]，例如：")
    ui.plain("    type = \"click\"")
    ui.plain(f"    x = {x}")
    ui.plain(f"    y = {y}")
    return EXIT_OK


def cmd_click(args: argparse.Namespace) -> int:
    """在指定坐标点一下，验证点击能不能生效。"""
    executor = ActionExecutor(allow=("click",), dry_run=args.dry_run)
    try:
        before = cursor_position()
    except ActionError:
        before = None
    if before is not None:
        ui.plain(f"  点击前鼠标位置：x = {before[0]}, y = {before[1]}")
    if process_integrity_level() == "Low":
        ui.warn("当前进程完整性级别是 Low：点击会被系统丢掉（和按键是同一个限制）。")
    results = executor.execute(
        {
            "type": "click",
            "x": args.x,
            "y": args.y,
            "button": args.button,
            "count": args.count,
        }
    )
    for item in results:
        (ui.ok if item.ok else ui.error)(f"  {item}")
    if not all(item.ok for item in results):
        return EXIT_ERROR
    ui.plain("  点击已发出。如果屏幕上什么都没发生：")
    ui.plain("    · 目标窗口以管理员身份运行 → 让 vc 也以管理员运行")
    ui.plain("    · 坐标不对 → 用 `vc pos` 重新量（窗口/全屏两种状态位置可能不同）")
    return EXIT_OK


def cmd_rules(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError:
        config = load_config_or_defaults(args.config)
    _print_warnings(config)
    rules = config.rules if args.all else config.enabled_rules()
    if not rules:
        ui.warn("没有配置任何命令，编辑 config.toml 的 [[rules]] 添加。")
        return EXIT_OK
    ui.title(f"允许执行的命令（{len(rules)} 条）：大模型只能从这里挑")
    for rule in rules:
        flag = "" if rule.enabled else "（已停用）"
        ui.plain(f"  [{rule.id}] {rule.name}{flag}")
        if rule.description:
            ui.plain(f"      说明：{rule.description}")
        ui.plain(f"      示例：{'、'.join(rule.phrases)}")
        if rule.slots:
            ui.plain(f"      槽位：{', '.join(rule.slots)}")
        ui.plain(f"      动作：{rule.summary()}")
    ui.plain("")
    ui.plain(f"  允许的动作类型：{', '.join(config.commands.allow)}")
    if not config.commands.allow_shell:
        ui.plain("  （shell 动作默认被禁止，需要 commands.allow_shell = true）")
    ui.plain("  可用按键名：" + ", ".join(known_key_names()[:40]) + " ...")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    ui.title("ds_voicecontrol 自检")
    ok_all = True

    path = resolve_config_path(args.config)
    if not path.exists():
        ui.error(f"[!!] 找不到配置文件：{path}（先运行 `vc init`）")
        return EXIT_ERROR
    config = load_config(path)
    ui.plain(f"[ok] 配置文件：{path}")
    _print_warnings(config)

    problems = config.problems()
    for problem in problems:
        ui.error(f"[!!] {problem}")
        ok_all = False
    if not problems:
        ui.plain("[ok] 配置项检查通过")

    key = config.resolve_api_key()
    env_name = config.llm.api_key_env or "DASHSCOPE_API_KEY"
    if key:
        masked = key[:6] + "…" + key[-4:] if len(key) > 12 else "***"
        source = "配置 llm.api_key" if config.llm.api_key.strip() else f"环境变量 {env_name}"
        ui.plain(f"[ok] API Key 已设置（{source}）：{masked}")
    else:
        ui.error(f"[!!] 没有找到 API Key：设置环境变量 {env_name}，或填 llm.api_key")
        ok_all = False

    level = process_integrity_level()
    independent = integrity_via_whoami()
    hwnd, title = foreground_window()
    if level == "Low" or independent == "Low":
        ui.error(
            "[!!] 进程完整性级别是 Low：这个终端被以受限方式启动，模拟按键会被 Windows 静默拦掉"
            "（日志会显示 [OK] 但程序没反应）。请用 `uv run vc keytest` 看启动链，"
            "并改在干净终端（Win+R → wt）里运行 `vc run`。"
        )
        chain = process_parent_chain()
        if chain:
            ui.plain("      启动链：" + "  ←  ".join(chain))
        ui.plain(f"      当前前台窗口：{title!r}（完整性 {window_integrity_level(hwnd)}）")
        ok_all = False
    else:
        ui.plain(
            f"[ok] 进程完整性级别：{level}（whoami 独立校验：{independent}），模拟按键可以发给普通程序"
        )
        ui.plain(f"     当前前台窗口：{title!r}（完整性 {window_integrity_level(hwnd)}）")

    try:
        model_dir = ensure_model(
            config.models_dir,
            config.wake.model,
            auto_download=False,
            log=ui.plain,
        )
        ui.plain(f"[ok] 唤醒词模型：{model_dir}")
        keywords_file = ensure_keywords_file(
            model_dir, config.wake.words, config.wake.keywords_file or None, log=None
        )
        ui.plain(f"[ok] 唤醒词文件：{keywords_file}")
        for line in keywords_file.read_text(encoding="utf-8").splitlines():
            ui.plain(f"       {line}")
    except (ModelError, KwsError) as exc:
        ui.error(f"[!!] 唤醒词模型不可用：{exc}")
        ui.plain("     运行 `uv run vc setup` 下载模型。")
        ok_all = False

    ui.plain(f"[..] 正在真实调用一次大模型（{config.llm.model}）……")
    listener = Listener(
        config,
        spotter=object(),  # 只测大模型，不加载唤醒词模型
        executor=_make_executor(config, dry_run=True),
    )
    status = listener.handle_text(args.text)
    if status == "llm-error":
        ui.error("[!!] 大模型调用失败，具体原因见上面的错误信息")
        ok_all = False
    else:
        ui.ok(f"[ok] 大模型调用成功（结果：{status}）")
    ui.plain("")
    if ok_all:
        ui.ok("自检通过，可以 `uv run vc run` 开始监听了。")
        return EXIT_OK
    ui.warn("自检发现问题，请按上面的提示逐条处理。")
    return EXIT_ERROR


def cmd_say(args: argparse.Namespace) -> int:
    config = load_config_or_defaults(args.config)
    _print_warnings(config)
    executor = _make_executor(config, dry_run=args.dry_run)
    listener = Listener(config, spotter=object(), executor=executor)
    if args.dry_run:
        ui.info("演练模式：只显示会执行什么，不会真的按键/打开程序。")
    status = listener.handle_text(args.text)
    ui.plain(f"  结果：{status}")
    return EXIT_OK if not status.startswith(("failed", "llm-error")) else EXIT_ERROR


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = _load_for_run(args)
    except ConfigError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    executor = _make_executor(config, dry_run=args.dry_run)
    listener = Listener(config, executor=executor)
    if args.no_wake:
        listener.wake_enabled = False
        listener.state = "capturing"
    if args.dry_run:
        ui.info("演练模式：识别和匹配照常，但不会真的执行动作。")
    try:
        if args.wav:
            return listener.run(wav=Path(args.wav))
        return listener.run(once=args.once, duration=args.duration)
    except AudioError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except ModelError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except KwsError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        ui.plain("")
        ui.info("已停止监听。")
        return EXIT_INTERRUPT


def cmd_version(args: argparse.Namespace) -> int:
    ui.plain(f"ds_voicecontrol {__version__}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vc",
        description="本地语音控制：本地听唤醒词，语音交给大模型选命令，本地执行按键/操作",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用流程：\n"
            "  uv run vc init        生成 config.toml\n"
            "  uv run vc calibrate   测环境底噪\n"
            "  uv run vc setup       下载唤醒词模型\n"
            "  uv run vc doctor      自检（含真实调用一次大模型）\n"
            "  uv run vc run         开始监听\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"ds_voicecontrol {__version__}")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    subparsers = parser.add_subparsers(dest="command", metavar="子命令")

    def add_common(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--config", "-c", help="配置文件路径（默认 ./config.toml）")
        return sub

    sub = add_common(subparsers.add_parser("init", help="生成默认配置文件"))
    sub.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    sub.set_defaults(func=cmd_init)

    sub = add_common(subparsers.add_parser("devices", help="列出可用麦克风"))
    sub.set_defaults(func=cmd_devices)

    sub = add_common(subparsers.add_parser("meter", help="实时电平表（检查麦克风与门限）"))
    sub.add_argument("--seconds", type=float, default=10.0, help="观察时长（秒），默认 10")
    sub.add_argument("--device", help="临时指定输入设备（编号或名称片段）")
    sub.add_argument("--threshold", type=float, help="临时指定门限，默认用配置里的值")
    sub.set_defaults(func=cmd_meter)

    sub = add_common(subparsers.add_parser("calibrate", help="测环境底噪并写入静音门限"))
    sub.add_argument("--seconds", type=float, default=5.0, help="采集时长（秒），默认 5")
    sub.add_argument("--countdown", type=int, default=3, help="开始前的倒计时秒数，默认 3")
    sub.add_argument("--device", help="临时指定输入设备（编号或名称片段）")
    sub.add_argument("--no-write", action="store_true", help="只显示结果，不修改配置文件")
    sub.set_defaults(func=cmd_calibrate)

    sub = add_common(subparsers.add_parser("setup", help="下载本地唤醒词模型"))
    sub.add_argument("--model", help="模型名，例如 sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")
    sub.set_defaults(func=cmd_setup)

    sub = add_common(subparsers.add_parser("wake", help="只测本地唤醒词（不联网）"))
    sub.add_argument("--seconds", type=float, default=20.0, help="测试时长（秒），默认 20")
    sub.add_argument("--threshold", type=float, help="临时指定命中阈值（默认用配置值 0.25）")
    sub.add_argument("--device", help="临时指定输入设备（编号或名称片段）")
    sub.add_argument("--wav", help="不接麦克风，直接测一个 wav 文件")
    sub.set_defaults(func=cmd_wake)

    sub = add_common(subparsers.add_parser("rules", help="查看允许执行的命令表"))
    sub.add_argument("--all", action="store_true", help="包括已停用的命令")
    sub.set_defaults(func=cmd_rules)

    sub = add_common(subparsers.add_parser("keytest", help="按键自测（排查“成功但没反应”）"))
    sub.add_argument(
        "--cross-app",
        action="store_true",
        help="额外做一次跨程序测试：倒计时后向当前前台窗口输入一段文字（需你手动聚焦目标窗口）",
    )
    sub.set_defaults(func=cmd_keytest)

    sub = add_common(subparsers.add_parser("pos", help="实时显示鼠标坐标（给 click 动作量位置）"))
    sub.add_argument("--seconds", type=float, default=0, help="显示多少秒后自动结束（默认一直显示到 Ctrl+C）")
    sub.set_defaults(func=cmd_pos)

    sub = add_common(subparsers.add_parser("click", help="在指定屏幕坐标测试点击"))
    sub.add_argument("x", type=float, help="屏幕 X 坐标（用 vc pos 量）")
    sub.add_argument("y", type=float, help="屏幕 Y 坐标")
    sub.add_argument("--button", default="left", choices=["left", "right", "middle"], help="鼠标键（默认 left）")
    sub.add_argument("--count", type=int, default=1, help="连点次数（2 = 双击）")
    sub.add_argument("--dry-run", action="store_true", help="只显示，不真的点击")
    sub.set_defaults(func=cmd_click)

    sub = add_common(subparsers.add_parser("doctor", help="自检 + 真实调用一次大模型"))
    sub.add_argument("--text", default="复制一下", help="用这句话测试大模型，默认“复制一下”")
    sub.set_defaults(func=cmd_doctor)

    sub = add_common(subparsers.add_parser("say", help="不走麦克风，直接把一句话交给大模型"))
    sub.add_argument("text", help="要测试的话，例如 \"把音量调小一点\"")
    sub.add_argument("--dry-run", action="store_true", help="只显示，不真正执行")
    sub.set_defaults(func=cmd_say)

    sub = add_common(subparsers.add_parser("run", help="开始监听（主功能）"))
    sub.add_argument("--once", action="store_true", help="处理一条命令就退出")
    sub.add_argument("--duration", type=float, help="监听指定秒数后自动退出（便于自动化测试）")
    sub.add_argument("--dry-run", action="store_true", help="识别匹配照常，但不真正执行动作")
    sub.add_argument("--no-wake", action="store_true", help="本次运行不需要唤醒词")
    sub.add_argument("--wav", help="不接麦克风，直接处理一个 wav 文件（用于离线测试）")
    sub.add_argument("--device", help="临时指定输入设备（编号或名称片段）")
    sub.set_defaults(func=cmd_run)

    sub = add_common(subparsers.add_parser("version", help="显示版本"))
    sub.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    # 必须在 argparse 之前设置，否则 --help 的中文会因控制台编码报错
    ui.setup_console(color=False if "--no-color" in raw_args else None)
    parser = build_parser()
    args = parser.parse_args(raw_args)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args))
    except ConfigError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        print()
        return EXIT_INTERRUPT


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
