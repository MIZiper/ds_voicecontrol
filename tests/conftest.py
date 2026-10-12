"""测试用的公共夹具：一个可编程的假大模型接口（OpenAI 兼容）。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest


class MockLlmServer:
    """在本地起一个 /v1/chat/completions，记录请求并返回可配置的回复。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply_content = '{"transcript": "复制", "command": "copy", "slots": {}, "confidence": 0.9}'
        self.status = 200
        self.reject_response_format = False
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 配置 -------------------------------------------------------------- #
    def set_reply(self, payload: dict[str, Any]) -> None:
        self.reply_content = json.dumps(payload, ensure_ascii=False)

    def set_raw_reply(self, text: str) -> None:
        self.reply_content = text

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self) -> str:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                body = json.loads(raw) if raw else {}
                server.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )
                if server.reject_response_format and "response_format" in body:
                    payload = json.dumps(
                        {"error": {"message": "response_format is not supported by this model"}}
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if server.status != 200:
                    payload = json.dumps({"error": {"message": f"mock status {server.status}"}}).encode()
                    self.send_response(server.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": server.reply_content},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:  # 静音
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


@pytest.fixture()
def mock_llm() -> MockLlmServer:
    server = MockLlmServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()
