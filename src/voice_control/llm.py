"""命令理解：把一段音频（或一句文本）交给大模型，拿回「要执行哪条命令」。

设计要点：

* 走 **OpenAI 兼容接口**（默认阿里云百炼 DashScope），只要改 base_url/model 就能换服务；
* **音频直传**：把唤醒后那段语音按 ``input_audio`` 发过去，由模型自己听、自己选命令，
  本地不做整句识别；
* 模型只能从 ``[[rules]]`` 命令表里挑 ``id``，其他一律拒绝执行——这是安全边界；
* 模型同时返回它听到的原话（``transcript``），既方便排查，也能在模型没给命令时
  交给本地规则兜底匹配。
"""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import Config, Rule

SYSTEM_PROMPT = """\
你是一个语音命令解析器。用户会用{language}说一句口令，你要从下面的命令表里挑出一条要执行的命令。

命令表（格式：id | 说明 | 示例说法 | 需要的槽位）：
{catalog}

只输出一个 JSON 对象，不要输出任何解释文字、不要用代码块以外的文字：
{{"transcript": "你听到的原话", "command": "命令表里的 id，没有合适的命令时填 null", "slots": {{}}, "confidence": 0.0}}

规则：
1. command 必须是命令表里出现过的 id，绝对不要发明新的 id；没有合适的命令就填 null。
2. 口语化、同义、省略的说法都要尽量匹配，例如“帮我复制一下”对应 copy，“声音调小点”对应 volume_down。
3. slots 里只填命令表声明过的槽位；槽位内容保留用户的说法，不要翻译、不要改写、不要补全。
4. 如果用户在聊天、在问问题、或者只是叫了一声唤醒词，command 填 null。
5. transcript 要如实转写你听到的内容（可以去掉唤醒词）。
6. 你只负责选命令，不负责执行任何别的事情。"""


class LlmError(RuntimeError):
    """调用大模型失败。"""


# --------------------------------------------------------------------------- #
# 命令表
# --------------------------------------------------------------------------- #
@dataclass
class CommandCatalog:
    rules: list[Rule]

    @classmethod
    def from_config(cls, config: Config) -> "CommandCatalog":
        return cls(rules=list(config.enabled_rules()))

    def ids(self) -> list[str]:
        return [rule.id for rule in self.rules]

    def rule(self, command_id: str | None) -> Rule | None:
        if not command_id:
            return None
        wanted = str(command_id).strip()
        for rule in self.rules:
            if rule.id == wanted:
                return rule
        lowered = wanted.lower().replace(" ", "").replace("_", "").replace("-", "")
        for rule in self.rules:
            key = rule.id.lower().replace(" ", "").replace("_", "").replace("-", "")
            if key == lowered:
                return rule
        return None

    def prompt_block(self) -> str:
        lines: list[str] = []
        for rule in self.rules:
            examples = "、".join(f"“{phrase}”" for phrase in rule.phrases[:5]) or "-"
            slots = "、".join(rule.slots) if rule.slots else "-"
            description = rule.description or rule.name
            lines.append(f"{rule.id} | {description} | {examples} | {slots}")
        return "\n".join(lines) if lines else "（命令表为空）"

    def build_prompt(self, language: str = "中文", extra: str = "") -> str:
        prompt = SYSTEM_PROMPT.format(
            language="中文" if language.startswith("zh") else language,
            catalog=self.prompt_block(),
        )
        if extra.strip():
            prompt += "\n\n补充说明（来自用户配置）：\n" + extra.strip()
        return prompt


# --------------------------------------------------------------------------- #
# 决策结果
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    command_id: str | None = None
    slots: dict[str, str] = field(default_factory=dict)
    transcript: str = ""
    confidence: float | None = None
    model: str = ""
    latency_ms: float = 0.0
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)

    def rule(self, catalog: CommandCatalog) -> Rule | None:
        return catalog.rule(self.command_id)

    def slot_text(self, rule: Rule | None = None) -> str:
        """给动作模板里的 ``{text}`` 用的“主要内容”。"""
        if rule is not None:
            for name in rule.slots:
                value = self.slots.get(name)
                if value:
                    return value
        for value in self.slots.values():
            if value:
                return value
        return self.transcript

    def describe(self) -> str:
        parts = [f"命令={self.command_id or '（无）'}"]
        if self.slots:
            parts.append("槽位=" + json.dumps(self.slots, ensure_ascii=False))
        if self.transcript:
            parts.append(f"听到={self.transcript!r}")
        if self.confidence is not None:
            parts.append(f"置信度={self.confidence:.2f}")
        parts.append(f"耗时={self.latency_ms:.0f}ms")
        return "  ".join(parts)


# --------------------------------------------------------------------------- #
# JSON 解析
# --------------------------------------------------------------------------- #
def extract_json_object(text: str) -> dict[str, Any]:
    """从模型回复里抠出第一个 JSON 对象（容忍 ```json 围栏和多余文字）。"""
    if not text:
        raise LlmError("模型返回了空内容")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [line for line in cleaned.splitlines() if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError as exc:
            raise LlmError(f"模型返回的不是合法 JSON：{exc}\n原始内容：{cleaned[:400]}") from exc
    raise LlmError(f"模型返回里找不到 JSON：{cleaned[:400]}")


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class LlmClient:
    """OpenAI 兼容的 chat/completions 客户端（只用标准库，避免额外依赖）。"""

    def __init__(
        self,
        config: Config,
        *,
        catalog: CommandCatalog | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.catalog = catalog or CommandCatalog.from_config(config)
        self.log = log
        self._system_prompt = self.catalog.build_prompt(
            language=config.general.language, extra=config.llm.extra_prompt
        )
        self._response_format_supported = True

    # -- 对外接口 ---------------------------------------------------------- #
    def decide_audio(self, pcm: bytes, sample_rate: int = 16000) -> Decision:
        """把一段语音交给模型，返回它选定的命令。"""
        if not pcm:
            raise LlmError("没有音频可以发送")
        audio_b64 = base64.b64encode(pcm_to_wav_bytes(pcm, sample_rate)).decode("ascii")
        content: list[dict[str, Any]] = [
            {
                "type": "input_audio",
                "input_audio": {
                    "data": f"data:;base64,{audio_b64}",
                    "format": self.config.llm.audio_format or "wav",
                },
            },
            {"type": "text", "text": "请解析这段语音，并按格式返回 JSON。"},
        ]
        self._save_debug_audio(pcm, sample_rate)
        return self._request(content)

    def decide_text(self, text: str) -> Decision:
        """文本模式（`vc say` / `vc doctor` 用），方便在不说话的情况下调试命令表。"""
        content = [{"type": "text", "text": f"用户说：{text}"}]
        return self._request(content)

    # -- 内部 -------------------------------------------------------------- #
    def _request(self, content: list[dict[str, Any]]) -> Decision:
        started = time.monotonic()
        raw_text = self._chat(content)
        latency_ms = (time.monotonic() - started) * 1000
        data = extract_json_object(raw_text)
        decision = Decision(
            transcript=str(data.get("transcript") or "").strip(),
            model=self.config.llm.model,
            latency_ms=latency_ms,
            raw_text=raw_text,
        )
        confidence = data.get("confidence")
        if isinstance(confidence, (int, float)):
            decision.confidence = float(confidence)
        slots = data.get("slots") or {}
        if isinstance(slots, dict):
            decision.slots = {
                str(key): str(value).strip()
                for key, value in slots.items()
                if value is not None and str(value).strip()
            }
        elif slots:
            decision.notes.append(f"模型返回的 slots 不是对象，已忽略：{slots!r}")

        command_id = data.get("command")
        if command_id is None or (isinstance(command_id, str) and not command_id.strip()):
            decision.command_id = None
        elif not isinstance(command_id, str):
            decision.notes.append(f"模型返回的 command 不是字符串，已忽略：{command_id!r}")
            decision.command_id = None
        else:
            rule = self.catalog.rule(command_id)
            if rule is None:
                decision.notes.append(f"模型返回了命令表以外的 id “{command_id}”，已拒绝执行")
                decision.command_id = None
            else:
                decision.command_id = rule.id
        return decision

    def _chat(self, user_content: list[dict[str, Any]]) -> str:
        key = self.config.resolve_api_key()
        if not key:
            env_name = self.config.llm.api_key_env or "DASHSCOPE_API_KEY"
            raise LlmError(
                f"没有配置大模型 API Key。请设置环境变量 {env_name}，"
                "或把 key 写到配置的 llm.api_key（不推荐）。"
            )
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            return self._post(messages, key, use_json_format=True)
        except LlmError as exc:
            message = str(exc)
            if "response_format" in message and self._response_format_supported:
                # 有些模型/网关不支持 response_format，去掉它重试一次
                self._response_format_supported = False
                if self.log is not None:
                    self.log("该模型不支持 response_format=json_object，已自动去掉重试。")
                return self._post(messages, key, use_json_format=False)
            raise

    def _post(self, messages: list[dict[str, Any]], key: str, *, use_json_format: bool) -> str:
        body: dict[str, Any] = {
            "model": self.config.llm.model,
            "messages": messages,
            "temperature": float(self.config.llm.temperature),
            "stream": False,
        }
        want_json = bool(self.config.llm.response_format_json) and use_json_format
        if want_json and self._response_format_supported:
            body["response_format"] = {"type": "json_object"}

        url = self.config.llm.base_url.rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        timeout = float(self.config.llm.timeout_s) or 30.0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass
            raise LlmError(self._http_error_message(exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise LlmError(
                f"连接大模型失败：{exc.reason}\n"
                f"检查网络、llm.base_url（当前 {self.config.llm.base_url}）和代理设置。"
            ) from exc
        except TimeoutError as exc:
            raise LlmError(
                f"调用大模型超时（{timeout:.0f}s）。可以调大 llm.timeout_s，或换更快的模型。"
            ) from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LlmError(f"大模型返回的不是 JSON：{payload[:400]}") from exc
        if "error" in data and data.get("error"):
            raise LlmError(f"大模型返回错误：{json.dumps(data['error'], ensure_ascii=False)[:400]}")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"大模型返回结构异常：{payload[:400]}") from exc
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise LlmError(f"大模型没有返回文本内容：{payload[:400]}")
        return content

    def _http_error_message(self, code: int, detail: str) -> str:
        detail = (detail or "").strip()
        hint = {
            400: "请求被拒绝：常见原因是模型不支持音频输入（换 llm.model，例如 qwen-audio-turbo-latest / qwen3-omni-flash），或参数不被支持。",
            401: "API Key 无效或没带上，检查 llm.api_key_env 指向的环境变量。",
            403: "没有权限：确认该模型已在控制台开通，且 Key 属于对应地域。",
            404: f"接口地址不存在：检查 llm.base_url（当前 {self.config.llm.base_url}）和 llm.model。",
            429: "请求过于频繁或额度用尽，稍后再试。",
        }.get(code, "调用失败。")
        return f"大模型接口返回 HTTP {code}：{hint}\n服务端信息：{detail[:600]}"

    def _save_debug_audio(self, pcm: bytes, sample_rate: int) -> None:
        target = self.config.llm.save_audio_dir.strip()
        if not target:
            return
        directory = Path(target)
        if not directory.is_absolute():
            directory = self.config.base_dir / directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("utterance-%Y%m%d-%H%M%S.wav")
            (directory / name).write_bytes(pcm_to_wav_bytes(pcm, sample_rate))
        except OSError as exc:  # pragma: no cover
            if self.log is not None:
                self.log(f"保存调试音频失败：{exc}")
