"""配置读取/写入（TOML），以及配置项校验。

配置文件默认是项目根目录下的 `config.toml`，可以用 `--config 路径` 或环境变量
`VC_CONFIG` 指定别的文件。读取时对未知字段、类型错误都会给出警告但不会崩溃，
方便用户边改边试。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .config_default import DEFAULT_CONFIG_TOML

DEFAULT_CONFIG_NAME = "config.toml"
ENV_CONFIG_PATH = "VC_CONFIG"

ACTION_TYPES = ("hotkey", "press", "text", "app", "wait", "sequence", "shell")
ACTION_FIELDS = (
    "type",
    "keys",
    "repeat",
    "interval_ms",
    "text",
    "target",
    "command",
    "cwd",
    "ms",
    "steps",
    "shell",
)
ENGINES = ("vosk", "whisper")
UNKNOWN_MODES = ("log", "ignore", "beep")
LOG_LEVELS = ("debug", "info", "warning", "error")


class ConfigError(RuntimeError):
    """配置文件无法使用时抛出。"""


# --------------------------------------------------------------------------- #
# 各配置段
# --------------------------------------------------------------------------- #
@dataclass
class GeneralConfig:
    language: str = "zh"
    log_level: str = "info"


@dataclass
class AudioConfig:
    device: str = ""
    sample_rate: int = 16000
    frame_ms: int = 30
    pre_roll_ms: int = 300
    min_speech_ms: int = 250
    max_utterance_ms: int = 8000
    silence_ms: int = 700
    noise_threshold: float = 0.010
    threshold_factor: float = 2.5
    adaptive_noise: bool = True


@dataclass
class AsrConfig:
    engine: str = "vosk"
    models_dir: str = "models"
    vosk_model: str = "vosk-model-small-cn-0.22"
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    hf_endpoint: str = ""
    auto_download: bool = True


@dataclass
class WakeConfig:
    enabled: bool = True
    require_wake: bool = True
    words: list[str] = field(default_factory=lambda: ["小助手", "你好小助手"])
    fuzzy: float = 0.82
    window_s: float = 5.0


@dataclass
class CommandConfig:
    allow: list[str] = field(
        default_factory=lambda: ["hotkey", "press", "text", "app", "wait", "sequence"]
    )
    allow_shell: bool = False
    unknown: str = "log"
    match_threshold: float = 0.75
    cooldown_ms: int = 400


@dataclass
class FeedbackConfig:
    beep: bool = True


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
@dataclass
class Rule:
    name: str
    phrases: list[str]
    action: dict[str, Any]
    enabled: bool = True

    @property
    def type(self) -> str:
        return str(self.action.get("type", ""))

    def summary(self) -> str:
        """给 `vc rules` 用的一行摘要。"""
        action = self.action
        kind = self.type
        if kind == "hotkey":
            detail = "+".join(str(k) for k in action.get("keys", []))
        elif kind == "press":
            detail = "+".join(str(k) for k in action.get("keys", []))
            repeat = int(action.get("repeat", 1) or 1)
            if repeat > 1:
                detail += f" x{repeat}"
        elif kind == "text":
            detail = repr(action.get("text", ""))
        elif kind == "app":
            detail = str(action.get("target", ""))
        elif kind == "wait":
            detail = f"{action.get('ms', 0)} ms"
        elif kind == "shell":
            detail = str(action.get("command", ""))
        elif kind == "sequence":
            steps = action.get("steps", []) or []
            detail = " -> ".join(str(step.get("type", "?")) for step in steps)
        else:
            detail = str(action)
        return f"{kind}: {detail}"

    def problems(self) -> list[str]:
        """返回影响执行的问题列表（空列表表示没问题）。"""
        issues: list[str] = []
        kind = self.type
        if kind not in ACTION_TYPES:
            issues.append(f"未知动作类型 “{kind}”（可用：{'/'.join(ACTION_TYPES)}）")
            return issues
        if kind in ("hotkey", "press"):
            keys = self.action.get("keys")
            if not isinstance(keys, list) or not keys:
                issues.append("缺少 keys 列表")
        elif kind == "text":
            if not str(self.action.get("text", "")).strip():
                issues.append("缺少 text")
        elif kind == "app":
            if not str(self.action.get("target", "")).strip():
                issues.append("缺少 target")
        elif kind == "shell":
            if not str(self.action.get("command", "")).strip():
                issues.append("缺少 command")
        elif kind == "wait":
            try:
                if float(self.action.get("ms", 0)) <= 0:
                    issues.append("ms 必须大于 0")
            except (TypeError, ValueError):
                issues.append("ms 必须是数字")
        elif kind == "sequence":
            steps = self.action.get("steps")
            if not isinstance(steps, list) or not steps:
                issues.append("steps 必须是非空列表")
            else:
                for index, step in enumerate(steps):
                    if not isinstance(step, dict):
                        issues.append(f"steps[{index}] 不是表")
                        continue
                    nested = Rule(name=f"{self.name}[{index}]", phrases=["-"], action=step)
                    issues.extend(f"steps[{index}]: {item}" for item in nested.problems())
        return issues


# --------------------------------------------------------------------------- #
# 总配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    path: Path | None = None
    general: GeneralConfig = field(default_factory=GeneralConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    commands: CommandConfig = field(default_factory=CommandConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    rules: list[Rule] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- 路径 -------------------------------------------------------------- #
    @property
    def base_dir(self) -> Path:
        return self.path.parent if self.path is not None else Path.cwd()

    @property
    def models_dir(self) -> Path:
        raw = Path(self.asr.models_dir).expanduser()
        return raw if raw.is_absolute() else (self.base_dir / raw)

    # -- 便捷方法 ---------------------------------------------------------- #
    def problems(self) -> list[str]:
        """检查配置里会影响使用的硬性错误。"""
        issues: list[str] = []
        if self.asr.engine not in ENGINES:
            issues.append(f"asr.engine 只能是 {' 或 '.join(ENGINES)}，当前是 “{self.asr.engine}”")
        if self.audio.sample_rate <= 0:
            issues.append("audio.sample_rate 必须大于 0")
        if self.audio.frame_ms <= 0:
            issues.append("audio.frame_ms 必须大于 0")
        if not self.wake.words and self.wake.enabled:
            issues.append("wake.words 是空的，要么填唤醒词，要么把 wake.enabled 设为 false")
        unknown_types = [t for t in self.commands.allow if t not in ACTION_TYPES]
        if unknown_types:
            issues.append(f"commands.allow 里有未知动作类型：{', '.join(unknown_types)}")
        if self.commands.unknown not in UNKNOWN_MODES:
            issues.append(f"commands.unknown 只能是 {'/'.join(UNKNOWN_MODES)}")
        return issues

    def enabled_rules(self) -> list[Rule]:
        return [rule for rule in self.rules if rule.enabled]


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def resolve_config_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env).expanduser()
    return Path.cwd() / DEFAULT_CONFIG_NAME


def _type_ok(value: Any, default: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    return True


def _fill(cls: type, data: Any, warnings: list[str], prefix: str):
    if not isinstance(data, dict):
        if data is not None:
            warnings.append(f"[{prefix.rstrip('.')}] 应该是表，已忽略并使用默认值")
        return cls()
    defaults = cls()
    kwargs: dict[str, Any] = {}
    known = {f.name for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            warnings.append(f"未知配置项 {prefix}{key}（已忽略）")
            continue
        default = getattr(defaults, key)
        if not _type_ok(value, default):
            warnings.append(
                f"配置项 {prefix}{key} 类型不对（应为 {type(default).__name__}），已使用默认值 {default!r}"
            )
            continue
        if isinstance(default, float) and isinstance(value, int):
            value = float(value)
        kwargs[key] = value
    return cls(**kwargs)


def _parse_rules(raw: Any, warnings: list[str]) -> list[Rule]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        warnings.append("[[rules]] 应该是规则数组，已忽略")
        return []
    rules: list[Rule] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"第 {index + 1} 条规则不是表，已跳过")
            continue
        name = str(item.get("name") or f"规则{index + 1}")
        phrases = item.get("phrases", item.get("phrase", []))
        if isinstance(phrases, str):
            phrases = [phrases]
        if not isinstance(phrases, list):
            warnings.append(f"规则「{name}」的 phrases 不是列表，已跳过")
            continue
        phrases = [str(p).strip() for p in phrases if str(p).strip()]
        if not phrases:
            warnings.append(f"规则「{name}」没有 phrases，已跳过")
            continue
        if isinstance(item.get("action"), dict):
            action = dict(item["action"])
        else:
            action = {key: item[key] for key in ACTION_FIELDS if key in item}
        if not action:
            warnings.append(f"规则「{name}」没有动作字段（type/keys/...），已跳过")
            continue
        rule = Rule(
            name=name,
            phrases=phrases,
            action=action,
            enabled=bool(item.get("enabled", True)),
        )
        issues = rule.problems()
        if issues:
            warnings.append(f"规则「{name}」有问题，已跳过：{'；'.join(issues)}")
            continue
        if not rule.enabled:
            warnings.append(f"规则「{name}」已停用（enabled = false）")
        rules.append(rule)
    return rules


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """读取配置文件。文件不存在时抛 ConfigError（提示运行 `vc init`）。"""
    config_path = resolve_config_path(path)
    if not config_path.exists():
        raise ConfigError(
            f"配置文件不存在：{config_path}\n先运行 `vc init` 生成默认配置。"
        )
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件解析失败（{config_path}）：{exc}") from exc

    warnings: list[str] = []
    cfg = Config(path=config_path, warnings=warnings)
    cfg.general = _fill(GeneralConfig, data.get("general"), warnings, "general.")
    cfg.audio = _fill(AudioConfig, data.get("audio"), warnings, "audio.")
    cfg.asr = _fill(AsrConfig, data.get("asr"), warnings, "asr.")
    cfg.wake = _fill(WakeConfig, data.get("wake"), warnings, "wake.")
    cfg.commands = _fill(CommandConfig, data.get("commands"), warnings, "commands.")
    cfg.feedback = _fill(FeedbackConfig, data.get("feedback"), warnings, "feedback.")
    cfg.rules = _parse_rules(data.get("rules"), warnings)
    cfg.warnings = warnings
    return cfg


def load_config_or_defaults(path: str | os.PathLike[str] | None = None) -> Config:
    """文件不存在时返回默认配置（不落盘）。"""
    config_path = resolve_config_path(path)
    if not config_path.exists():
        cfg = Config(path=config_path)
        cfg.rules = _parse_rules(tomllib.loads(DEFAULT_CONFIG_TOML).get("rules"), cfg.warnings)
        cfg.warnings.append(f"配置文件不存在，正在使用内置默认配置：{config_path}")
        return cfg
    return load_config(config_path)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def write_default_config(path: str | os.PathLike[str] | None = None, force: bool = False) -> Path:
    """把内置模板写到配置文件里。已存在且 force=False 时不覆盖。"""
    config_path = resolve_config_path(path)
    if config_path.exists() and not force:
        raise ConfigError(f"配置文件已存在：{config_path}（要覆盖请加 --force）")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return config_path


def save_noise_threshold(threshold: float, path: str | os.PathLike[str] | None = None) -> Path:
    """把校准得到的门限写回配置文件（保留原有注释）。"""
    import tomlkit

    config_path = resolve_config_path(path)
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在：{config_path}，先运行 `vc init`")
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    if "audio" not in document:
        document["audio"] = tomlkit.table()
    document["audio"]["noise_threshold"] = round(float(threshold), 5)  # type: ignore[index]
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return config_path
