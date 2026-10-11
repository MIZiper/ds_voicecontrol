"""命令行入口：``vc <子命令>``。

子命令一览：

* ``vc init``        生成带注释的默认配置
* ``vc devices``     列出可用麦克风
* ``vc meter``       实时电平表（看麦克风有没有声音、门限合不合适）
* ``vc calibrate``   监听一小段，测环境底噪并写入静音门限
* ``vc setup``       下载离线识别模型
* ``vc rules``       查看当前配置里的口令规则
* ``vc say 文本``    不走麦克风，直接把文本喂给口令匹配（调试规则）
* ``vc run``         开始监听（主功能）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, ui
from .actions import ActionExecutor, known_key_names
from .audio import (
    AudioError,
    Microphone,
    list_input_devices,
    measure_noise,
    rms_of,
)
from .config import (
    ACTION_TYPES,
    Config,
    ConfigError,
    load_config,
    load_config_or_defaults,
    resolve_config_path,
    save_noise_threshold,
    write_default_config,
)
from .listener import Listener
from .models import ModelError, ensure_vosk_model, installed_vosk_models, vosk_model_info

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPT = 130


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
    if getattr(args, "engine", None):
        config.asr.engine = args.engine
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
    ui.plain("  1) uv run vc calibrate    # 监听 5 秒测环境底噪，写入静音门限")
    ui.plain("  2) uv run vc setup        # 下载离线识别模型（首次需要联网，约 42MB）")
    ui.plain("  3) uv run vc run          # 开始监听（先按 Ctrl+C 退出）")
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
                frame = mic.read_frame()
                level = rms_of(frame)
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
    ui.plain("  说明：请保持平时的环境（空调/风扇开着没关系），校准期间尽量不要说话，")
    ui.plain(f"        接下来会连续采集 {seconds:.0f} 秒的环境噪音。")
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
        f"  （运行时实际门限 = max(noise_threshold, 实时底噪 × {config.audio.threshold_factor})，"
        f"所以底噪变化也能自动适应）"
    )
    if stats.max_rms > stats.median * 15 and stats.max_rms > 0.03:
        ui.warn("校准期间检测到明显的声音（可能是说话/敲键盘/关门），建议安静时重测一次")
    if stats.median < 0.0005:
        ui.plain("  底噪很低，这是好事。")
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
    installed = installed_vosk_models(models_dir)
    if installed:
        ui.plain("  已安装的模型：")
        for path in installed:
            ui.plain(f"    - {path.name}")
    engine = args.engine or config.asr.engine
    if engine == "vosk":
        name = args.model or config.asr.vosk_model
        info = vosk_model_info(name)
        ui.plain(f"  目标模型：{name} —— {info.description}")
        try:
            path = ensure_vosk_model(models_dir, name, auto_download=True, log=ui.plain)
        except ModelError as exc:
            ui.error(str(exc))
            return EXIT_ERROR
        ui.ok(f"Vosk 模型就绪：{path}")
        return EXIT_OK
    if engine == "whisper":
        ui.plain(
            "  faster-whisper 的模型由 HuggingFace 缓存在上面这个目录，"
            "第一次运行 `vc run` 时会自动下载。"
        )
        ui.plain("  如果下载慢，可以在配置里设置 asr.hf_endpoint = \"https://hf-mirror.com\"。")
        ui.plain("  另外记得装可选依赖：uv sync --extra whisper")
        return EXIT_OK
    ui.error(f"未知引擎：{engine}")
    return EXIT_ERROR


def cmd_rules(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError:
        config = load_config_or_defaults(args.config)
    _print_warnings(config)
    rules = config.rules if args.all else config.enabled_rules()
    if not rules:
        ui.warn("没有配置任何口令规则，编辑 config.toml 的 [[rules]] 添加。")
        return EXIT_OK
    ui.title(f"当前口令规则（{len(rules)} 条，匹配阈值 {config.commands.match_threshold}）：")
    for rule in rules:
        flag = "" if rule.enabled else "（已停用）"
        ui.plain(f"  「{rule.name}」{flag}")
        ui.plain(f"      触发词：{'、'.join(rule.phrases)}")
        ui.plain(f"      动作　：{rule.summary()}")
    ui.plain("")
    ui.plain(f"  允许的动作类型：{', '.join(config.commands.allow)}")
    if not config.commands.allow_shell:
        ui.plain("  （shell 动作默认被禁止，需要 commands.allow_shell = true）")
    ui.plain("  可用按键名：" + ", ".join(known_key_names()[:40]) + " ...")
    return EXIT_OK


def cmd_say(args: argparse.Namespace) -> int:
    config = load_config_or_defaults(args.config)
    _print_warnings(config)
    executor = ActionExecutor(
        allow=tuple(config.commands.allow),
        allow_shell=config.commands.allow_shell,
        dry_run=args.dry_run,
    )
    listener = Listener(config, executor=executor)
    if args.no_wake:
        listener.require_wake = False
    if args.dry_run:
        ui.info("演练模式：只显示会执行什么，不会真的按键/打开程序。")
    status = listener.handle_text(args.text)
    ui.plain(f"  结果：{status}")
    return EXIT_OK if not status.startswith("failed") else EXIT_ERROR


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = _load_for_run(args)
    except ConfigError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    executor = ActionExecutor(
        allow=tuple(config.commands.allow),
        allow_shell=config.commands.allow_shell,
        dry_run=args.dry_run,
    )
    listener = Listener(config, executor=executor)
    if args.no_wake:
        listener.require_wake = False
    if args.dry_run:
        ui.info("演练模式：识别和匹配照常，但不会真的执行动作。")
    if args.wav:
        return listener.run(wav=Path(args.wav))
    try:
        return listener.run(once=args.once, duration=args.duration)
    except AudioError as exc:
        ui.error(str(exc))
        return EXIT_ERROR
    except ModelError as exc:
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
        description="本地语音控制：唤醒词 -> 离线识别 -> 口令 -> 系统快捷键/操作",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用流程：\n"
            "  uv run vc init        生成 config.toml\n"
            "  uv run vc calibrate   测环境底噪\n"
            "  uv run vc setup       下载识别模型\n"
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

    sub = add_common(subparsers.add_parser("setup", help="下载离线识别模型"))
    sub.add_argument("--engine", choices=["vosk", "whisper"], help="要准备的引擎")
    sub.add_argument("--model", help="Vosk 模型名，例如 vosk-model-small-cn-0.22")
    sub.set_defaults(func=cmd_setup)

    sub = add_common(subparsers.add_parser("rules", help="查看口令规则"))
    sub.add_argument("--all", action="store_true", help="包括已停用的规则")
    sub.set_defaults(func=cmd_rules)

    sub = add_common(subparsers.add_parser("say", help="不走麦克风，直接测试一句话会触发什么"))
    sub.add_argument("text", help="要测试的文本，例如 \"小助手 复制\"")
    sub.add_argument("--dry-run", action="store_true", help="只显示，不真正执行")
    sub.add_argument("--no-wake", action="store_true", help="跳过唤醒词，直接按口令处理")
    sub.set_defaults(func=cmd_say)

    sub = add_common(subparsers.add_parser("run", help="开始监听（主功能）"))
    sub.add_argument("--once", action="store_true", help="只处理一段语音就退出")
    sub.add_argument("--duration", type=float, help="监听指定秒数后自动退出（便于自动化测试）")
    sub.add_argument("--dry-run", action="store_true", help="识别匹配照常，但不真正执行动作")
    sub.add_argument("--no-wake", action="store_true", help="本次运行不需要唤醒词")
    sub.add_argument("--wav", help="不接麦克风，直接处理一个 wav 文件（用于离线测试）")
    sub.add_argument("--device", help="临时指定输入设备（编号或名称片段）")
    sub.add_argument("--engine", choices=["vosk", "whisper"], help="临时切换识别引擎")
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
