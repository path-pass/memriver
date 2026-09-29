"""Fixtures shared across memriver's tests: `jev`, a local stand-in for TypeSafe's API
on 127.0.0.1 -- never the network -- that memriver.executor.api is pointed at."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# httpx honours these: a user's proxy must not swallow the requests to 127.0.0.1
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                   "all_proxy")


class JevStub:
    """Answers each POST with `reply` -- (status, body bytes, extra headers) -- after
    `delay` seconds, and records every request: path, JSON body, Authorization header."""

    def __init__(self) -> None:
        self.reply: tuple[int, bytes, dict[str, str]] = (200, b"{}", {})
        self.delay = 0.0
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                stub.requests.append({"path": self.path, "body": json.loads(body),
                                      "authorization": self.headers.get("Authorization")})
                time.sleep(stub.delay)
                status, payload, headers = stub.reply
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass                        # the client gave up (the timeout tests)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, noul: object) -> None:
        """Reply with a jev answer whose one field, plants, is `noul`."""
        self.reply = (200, json.dumps({
            "model": "jev-latest", "answers": {"plants": {"type": "noul", "noul": noul}},
            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode(), {})


@pytest.fixture
def jev(monkeypatch):
    from memriver.executor import api

    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    stub = JevStub()
    monkeypatch.setattr(api, "JEV_BASE_URL", stub.url)
    yield stub
    stub.server.shutdown()
    stub.server.server_close()
