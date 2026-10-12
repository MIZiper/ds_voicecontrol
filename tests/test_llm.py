"""大模型接入层测试：请求格式、JSON 解析、命令表校验、错误处理。

用 conftest 里的假接口（真起一个本地 HTTP 服务），所以测的是真实的 HTTP 行为。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from voice_control.config import Config, load_config, write_default_config
from voice_control.llm import (
    CommandCatalog,
    Decision,
    LlmClient,
    LlmError,
    extract_json_object,
    pcm_to_wav_bytes,
)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """只读取默认配置（不需要假接口的测试用）。"""
    path = write_default_config(tmp_path / "config.toml")
    return load_config(path)


def _make_config(tmp_path: Path, base_url: str, **llm_overrides) -> Config:
    path = write_default_config(tmp_path / "config.toml")
    cfg = load_config(path)
    cfg.llm.base_url = base_url
    cfg.llm.api_key = "test-key"
    cfg.llm.timeout_s = 10.0
    for key, value in llm_overrides.items():
        setattr(cfg.llm, key, value)
    return cfg


# --------------------------------------------------------------------------- #
# JSON 解析
# --------------------------------------------------------------------------- #
def test_extract_json_plain() -> None:
    data = extract_json_object('{"command": "copy"}')
    assert data["command"] == "copy"


def test_extract_json_with_fence_and_prose() -> None:
    text = '好的，我理解为：\n```json\n{"command": "copy", "slots": {"text": "你好"}}\n```\n希望有帮助。'
    data = extract_json_object(text)
    assert data["command"] == "copy"
    assert data["slots"]["text"] == "你好"


def test_extract_json_rejects_garbage() -> None:
    with pytest.raises(LlmError):
        extract_json_object("我觉得你想说的是复制吧")
    with pytest.raises(LlmError):
        extract_json_object("")


# --------------------------------------------------------------------------- #
# 命令表
# --------------------------------------------------------------------------- #
def test_catalog_prompt_block_lists_rules(config: Config) -> None:
    catalog = CommandCatalog.from_config(config)
    block = catalog.prompt_block()
    assert "copy |" in block
    assert "search |" in block
    assert "query" in block
    prompt = catalog.build_prompt(language="zh", extra="我习惯说“搞一下”表示复制")
    assert "我习惯说" in prompt
    assert "copy" in prompt


def test_catalog_rule_lookup_is_forgiving(config: Config) -> None:
    catalog = CommandCatalog.from_config(config)
    assert catalog.rule("copy") is not None
    assert catalog.rule("COPY") is not None
    assert catalog.rule("open_notepad").name == "打开记事本"
    assert catalog.rule("rm_rf") is None
    assert catalog.rule(None) is None


def test_decision_slot_text_prefers_declared_slot(config: Config) -> None:
    catalog = CommandCatalog.from_config(config)
    rule = catalog.rule("search")
    decision = Decision(
        command_id="search",
        slots={"query": "今天天气", "other": "忽略"},
        transcript="搜索今天天气",
    )
    assert decision.slot_text(rule) == "今天天气"
    # 没有声明槽位时退回到第一个槽位/原话
    assert decision.slot_text(None) == "今天天气"
    assert Decision().slot_text(None) == ""


# --------------------------------------------------------------------------- #
# 音频请求
# --------------------------------------------------------------------------- #
def test_decide_audio_request_shape(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    mock_llm.set_reply(
        {"transcript": "打开记事本", "command": "open_notepad", "slots": {}, "confidence": 0.93}
    )
    client = LlmClient(cfg)
    pcm = b"\x01\x02" * 8000  # 0.5 秒
    decision = client.decide_audio(pcm, 16000)

    assert decision.command_id == "open_notepad"
    assert decision.transcript == "打开记事本"
    assert decision.confidence == pytest.approx(0.93)

    assert len(mock_llm.requests) == 1
    request = mock_llm.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-key"
    body = request["body"]
    assert body["model"] == cfg.llm.model
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}

    content = body["messages"][1]["content"]
    audio_part = next(part for part in content if part["type"] == "input_audio")
    data = audio_part["input_audio"]["data"]
    assert data.startswith("data:;base64,"), "DashScope 要求 base64 音频包成 data URL"
    assert audio_part["input_audio"]["format"] == "wav"
    wav_bytes = base64.b64decode(data.split(",", 1)[1])
    assert wav_bytes[:4] == b"RIFF"
    assert decision.raw_text


def test_decide_text_sends_text_only(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    client = LlmClient(cfg)
    decision = client.decide_text("复制一下")
    assert decision.command_id == "copy"
    content = mock_llm.requests[0]["body"]["messages"][1]["content"]
    assert all(part["type"] == "text" for part in content)
    assert "复制一下" in content[0]["text"]


def test_system_prompt_contains_catalog_and_extra(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start(), extra_prompt="我说“搞”就是复制")
    client = LlmClient(cfg)
    client.decide_text("复制")
    system = mock_llm.requests[0]["body"]["messages"][0]["content"]
    assert "copy" in system and "volume_down" in system
    assert "我说“搞”就是复制" in system


# --------------------------------------------------------------------------- #
# 决策校验
# --------------------------------------------------------------------------- #
def test_unknown_command_id_is_rejected(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    mock_llm.set_reply({"transcript": "删库", "command": "rm_rf", "confidence": 0.99})
    decision = LlmClient(cfg).decide_text("删库")
    assert decision.command_id is None
    assert any("命令表以外" in note for note in decision.notes)


def test_null_command_and_empty_slots(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    mock_llm.set_reply(
        {"transcript": "嗯……", "command": None, "slots": {"query": "", "other": None}}
    )
    decision = LlmClient(cfg).decide_text("嗯")
    assert decision.command_id is None
    assert decision.slots == {}


def test_missing_api_key(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    cfg.llm.api_key = ""
    cfg.llm.api_key_env = "VC_TEST_MISSING_KEY"
    with pytest.raises(LlmError) as excinfo:
        LlmClient(cfg).decide_text("复制")
    assert "API Key" in str(excinfo.value)


def test_http_401_error_message(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    mock_llm.status = 401
    with pytest.raises(LlmError) as excinfo:
        LlmClient(cfg).decide_text("复制")
    message = str(excinfo.value)
    assert "401" in message
    assert "API Key" in message


def test_http_500_error_message(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    mock_llm.status = 500
    with pytest.raises(LlmError) as excinfo:
        LlmClient(cfg).decide_text("复制")
    assert "500" in str(excinfo.value)


def test_retries_without_response_format(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start())
    mock_llm.reject_response_format = True
    mock_llm.set_reply({"transcript": "复制", "command": "copy"})
    decision = LlmClient(cfg).decide_text("复制")
    assert decision.command_id == "copy"
    assert len(mock_llm.requests) == 2
    assert "response_format" in mock_llm.requests[0]["body"]
    assert "response_format" not in mock_llm.requests[1]["body"]


def test_saves_debug_audio(tmp_path: Path, mock_llm) -> None:
    cfg = _make_config(tmp_path, mock_llm.start(), save_audio_dir="debug-audio")
    mock_llm.set_reply({"transcript": "复制", "command": "copy"})
    LlmClient(cfg).decide_audio(b"\x00\x00" * 1600, 16000)
    files = list((tmp_path / "debug-audio").glob("*.wav"))
    assert len(files) == 1
    assert files[0].read_bytes()[:4] == b"RIFF"


def test_pcm_to_wav_bytes_header() -> None:
    data = pcm_to_wav_bytes(b"\x00\x00" * 480, 16000)
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    assert len(data) == 44 + 960
