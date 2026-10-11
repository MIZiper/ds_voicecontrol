"""CLI 子命令的冒烟测试（不碰麦克风、不真正执行动作）。"""

from __future__ import annotations

from pathlib import Path

from voice_control.cli import main


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    assert main(["init", "--config", str(path)]) == 0
    return path


def test_init_writes_config(tmp_path: Path) -> None:
    path = _config(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "[[rules]]" in text
    assert "[audio]" in text and "[wake]" in text and "[commands]" in text


def test_init_twice_needs_force(tmp_path: Path) -> None:
    path = _config(tmp_path)
    assert main(["init", "--config", str(path)]) == 1
    assert main(["init", "--config", str(path), "--force"]) == 0


def test_rules_lists_configured_commands(tmp_path: Path, capsys) -> None:
    path = _config(tmp_path)
    assert main(["rules", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "复制" in out
    assert "hotkey: ctrl+c" in out


def test_say_dry_run_executes_nothing(tmp_path: Path, capsys) -> None:
    path = _config(tmp_path)
    assert main(["say", "小助手 复制", "--config", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "命中「复制」" in out
    assert "演练" in out
    assert "结果：executed:复制" in out


def test_say_no_wake_flag(tmp_path: Path) -> None:
    path = _config(tmp_path)
    assert main(["say", "搜索 天气", "--config", str(path), "--dry-run", "--no-wake"]) == 0


def test_say_without_config_uses_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    assert main(["say", "小助手 复制", "--config", str(missing), "--dry-run"]) == 0


def test_no_subcommand_prints_help(capsys) -> None:
    assert main([]) == 0
    assert "子命令" in capsys.readouterr().out


def test_version() -> None:
    assert main(["version"]) == 0
