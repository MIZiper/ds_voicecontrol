"""CLI 冒烟测试：不碰麦克风，大模型走本地假接口。"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from voice_control.cli import main


def _make_config(tmp_path: Path, base_url: str | None = None, api_key: str = "test-key") -> Path:
    path = tmp_path / "config.toml"
    assert main(["init", "--config", str(path)]) == 0
    if base_url:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
        document["llm"]["base_url"] = base_url  # type: ignore[index]
        document["llm"]["api_key"] = api_key  # type: ignore[index]
        path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return path


def test_init_writes_config(tmp_path: Path) -> None:
    path = _make_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    for section in ("[audio]", "[wake]", "[llm]", "[commands]"):
        assert section in text
    assert "[[rules]]" in text
    assert "DASHSCOPE_API_KEY" in text


def test_init_twice_needs_force(tmp_path: Path) -> None:
    path = _make_config(tmp_path)
    assert main(["init", "--config", str(path)]) == 1
    assert main(["init", "--config", str(path), "--force"]) == 0


def test_rules_lists_command_table(tmp_path: Path, capsys) -> None:
    path = _make_config(tmp_path)
    assert main(["rules", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[copy]" in out
    assert "hotkey: ctrl+c" in out
    assert "说明：" in out


def test_say_uses_llm_and_executes(tmp_path: Path, mock_llm, capsys) -> None:
    path = _make_config(tmp_path, mock_llm.start())
    mock_llm.set_reply({"transcript": "把声音调小一点", "command": "volume_down", "slots": {}})
    assert main(["say", "把声音调小一点", "--config", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "执行命令「音量减」" in out
    assert "演练" in out
    assert "结果：executed:volume_down" in out


def test_say_reports_unknown_command(tmp_path: Path, mock_llm, capsys) -> None:
    path = _make_config(tmp_path, mock_llm.start())
    mock_llm.set_reply({"transcript": "来一份炸鸡", "command": None, "slots": {}})
    assert main(["say", "来一份炸鸡", "--config", str(path), "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "没有匹配到任何命令" in captured.err  # 提示走 stderr
    assert "结果：unknown" in captured.out


# 最小可用模型：只为了验证 doctor 的“模型就绪”分支，不做真实推理
FAKE_TOKENS = ["<blk>", "n", "iú", "l", "ái"]


def _fake_wake_model(tmp_path: Path) -> Path:
    model = tmp_path / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    model.mkdir(parents=True)
    (model / "tokens.txt").write_text(
        "\n".join(f"{token} {index}" for index, token in enumerate(FAKE_TOKENS)) + "\n",
        encoding="utf-8",
    )
    for name in ("encoder-x.onnx", "decoder-x.onnx", "joiner-x.onnx"):
        (model / name).write_bytes(b"x")
    return model


def test_doctor_ok(tmp_path: Path, mock_llm, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make_config(tmp_path, mock_llm.start())
    _fake_wake_model(tmp_path)
    # 完整性级别取决于运行环境（沙箱里是 Low，会判为问题），这里固定成 Medium 便于断言
    monkeypatch.setattr("voice_control.cli.process_integrity_level", lambda: "Medium")
    monkeypatch.setattr("voice_control.cli.integrity_via_whoami", lambda: "Medium")
    monkeypatch.setattr("voice_control.cli.window_integrity_level", lambda hwnd: "Medium")
    assert main(["doctor", "--config", str(path), "--text", "复制一下"]) == 0
    out = capsys.readouterr().out
    assert "ds_voicecontrol 自检" in out
    assert "[ok] 唤醒词模型" in out
    assert "@牛来" in out
    assert "大模型调用成功" in out


def test_doctor_warns_inside_sandbox(tmp_path: Path, mock_llm, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _make_config(tmp_path, mock_llm.start())
    _fake_wake_model(tmp_path)
    monkeypatch.setattr("voice_control.cli.process_integrity_level", lambda: "Low")
    monkeypatch.setattr("voice_control.cli.integrity_via_whoami", lambda: "Low")
    monkeypatch.setattr("voice_control.cli.window_integrity_level", lambda hwnd: "Unknown")
    monkeypatch.setattr("voice_control.cli.process_parent_chain", lambda: ["python.exe(1)", "wt.exe(2)"])
    code = main(["doctor", "--config", str(path), "--text", "复制一下"])
    captured = capsys.readouterr()
    assert "完整性级别是 Low" in captured.err  # 该项是错误提示，走 stderr
    assert code == 1


def test_doctor_fails_on_401(tmp_path: Path, mock_llm, capsys) -> None:
    path = _make_config(tmp_path, mock_llm.start())
    _fake_wake_model(tmp_path)
    mock_llm.status = 401
    code = main(["doctor", "--config", str(path), "--text", "复制一下"])
    out = capsys.readouterr().out
    assert "401" in out
    assert code == 1


def test_run_with_missing_wav_returns_error(tmp_path: Path, capsys) -> None:
    path = _make_config(tmp_path, "http://127.0.0.1:1/v1")
    code = main(["run", "--config", str(path), "--wav", str(tmp_path / "nope.wav"), "--dry-run"])
    assert code == 1


def test_run_without_api_key_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    path = tmp_path / "config.toml"
    assert main(["init", "--config", str(path)]) == 0
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    document["llm"]["api_key_env"] = "VC_TEST_UNSET_KEY"  # type: ignore[index]
    document["llm"]["api_key"] = ""  # type: ignore[index]
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    monkeypatch.delenv("VC_TEST_UNSET_KEY", raising=False)
    code = main(["run", "--config", str(path), "--wav", "whatever.wav"])
    err = capsys.readouterr().err
    assert code == 1
    assert "API Key" in err


def test_no_subcommand_prints_help(capsys) -> None:
    assert main([]) == 0
    assert "子命令" in capsys.readouterr().out


def test_all_subcommands_have_help(capsys) -> None:
    for name in (
        "init",
        "devices",
        "meter",
        "calibrate",
        "setup",
        "wake",
        "rules",
        "keytest",
        "doctor",
        "say",
        "run",
    ):
        with pytest.raises(SystemExit) as excinfo:
            main([name, "--help"])
        assert excinfo.value.code == 0
    capsys.readouterr()


def test_version() -> None:
    assert main(["version"]) == 0
