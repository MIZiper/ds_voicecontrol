"""本地唤醒词（关键词检测）相关测试：拼音转换、关键词文件、模型文件选择。"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_control.kws import (
    KwsError,
    ensure_keywords_file,
    find_model_files,
    keywords_content,
    load_token_set,
    syllable_to_tokens,
    word_to_tokens,
)

# 真实模型的 token 表里包含这些（拼音声母 + 带声调韵母）
TOKENS = {
    "<blk>",
    "<sos/eos>",
    "<unk>",
    "x",
    "iǎo",
    "zh",
    "ù",
    "sh",
    "ǒu",
    "n",
    "ǐ",
    "h",
    "ǎo",
}


def _fake_model_dir(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "tokens.txt").write_text(
        "\n".join(f"{token} {index}" for index, token in enumerate(sorted(TOKENS))) + "\n",
        encoding="utf-8",
    )
    for name in (
        "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "encoder-epoch-99-avg-1-chunk-16-left-64.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    ):
        (model / name).write_bytes(b"x")
    return model


def test_syllable_split() -> None:
    assert syllable_to_tokens("xiǎo") == ["x", "iǎo"]
    assert syllable_to_tokens("zhù") == ["zh", "ù"]
    assert syllable_to_tokens("shǒu") == ["sh", "ǒu"]
    assert syllable_to_tokens("ér") == ["ér"]  # 零声母整体作为一个 token


def test_word_to_tokens_for_default_wake_words() -> None:
    assert word_to_tokens("小助手") == ["x", "iǎo", "zh", "ù", "sh", "ǒu"]
    assert word_to_tokens("你好小助手") == [
        "n",
        "ǐ",
        "h",
        "ǎo",
        "x",
        "iǎo",
        "zh",
        "ù",
        "sh",
        "ǒu",
    ]


def test_word_to_tokens_rejects_non_chinese() -> None:
    with pytest.raises(KwsError):
        word_to_tokens("computer")


def test_load_token_set(tmp_path: Path) -> None:
    model = _fake_model_dir(tmp_path)
    tokens = load_token_set(model)
    assert "iǎo" in tokens and "<blk>" in tokens


def test_keywords_content_matches_sherpa_format(tmp_path: Path) -> None:
    model = _fake_model_dir(tmp_path)
    content = keywords_content(model, ["小助手", "你好小助手"])
    lines = content.strip().splitlines()
    assert lines[0] == "x iǎo zh ù sh ǒu @小助手"
    assert lines[1] == "n ǐ h ǎo x iǎo zh ù sh ǒu @你好小助手"
    assert not any(line.startswith("#") for line in lines), "sherpa 的关键词文件不能有注释"


def test_keywords_content_reports_unknown_tokens(tmp_path: Path) -> None:
    model = _fake_model_dir(tmp_path)
    with pytest.raises(KwsError) as excinfo:
        keywords_content(model, ["龘龘龘龘"])
    assert "token" in str(excinfo.value)


def test_ensure_keywords_file_writes_once_and_updates(tmp_path: Path) -> None:
    model = _fake_model_dir(tmp_path)
    target = tmp_path / "wake-keywords.txt"
    path = ensure_keywords_file(model, ["小助手"], target)
    first = path.read_text(encoding="utf-8")
    assert "@小助手" in first
    # 内容不变时不再重复写（mtime 不变）
    stamp = path.stat().st_mtime_ns
    ensure_keywords_file(model, ["小助手"], target)
    assert path.stat().st_mtime_ns == stamp
    # 唤醒词变了要重写
    ensure_keywords_file(model, ["小助手", "你好小助手"], target)
    assert "你好小助手" in path.read_text(encoding="utf-8")


def test_find_model_files_prefers_int8_epoch12(tmp_path: Path) -> None:
    model = _fake_model_dir(tmp_path)
    encoder, decoder, joiner = find_model_files(model)
    assert encoder.name.endswith(".int8.onnx")
    assert "epoch-12" in encoder.name
    assert decoder.name.startswith("decoder-")
    assert joiner.name.startswith("joiner-")


def test_find_model_files_missing(tmp_path: Path) -> None:
    with pytest.raises(KwsError):
        find_model_files(tmp_path / "nope")
