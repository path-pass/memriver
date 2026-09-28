"""The jev backend against a local HTTP stub on 127.0.0.1 -- never the network: the
request it sends, the decision at the threshold, and every failure as a fixed detail
that never carries the key or the response text."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from memriver_classifier.backends import JEV_QUESTION_V2, JevBackend, jev_opener
from memriver_core import Verdict

KEY = "jev-test-key-" + "k" * 24
TEXT = "prefer pytest -q"
# the production redirect policy (never follow), minus the user's proxies so the local
# stub is reached even behind a system proxy
NO_PROXY = jev_opener(urllib.request.ProxyHandler({}))


class Stub:
    """Answers each POST with `reply`: (status, body bytes, delay seconds[, headers])."""

    def __init__(self) -> None:
        self.reply = (200, b"{}", 0.0)
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                stub.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(self.rfile.read(length))})
                status, body, delay, *extra = stub.reply
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    for name, value in (extra[0] if extra else {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass                    # the client gave up (the timeout test)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, noul) -> None:
        self.reply = (200, json.dumps({"answers": {"plants": {"noul": noul}},
                                       "model": "jev-1.13.0"}).encode(), 0.0)


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.server.shutdown()
    server.server.server_close()


def _backend(stub, *, env=None, threshold=0.7, timeout_s=5.0, api_key_env="JEV_KEY"):
    return JevBackend(env={"JEV_KEY": KEY} if env is None else env, api_key_env=api_key_env,
                      model="jev-latest", threshold=threshold, timeout_s=timeout_s,
                      url=stub.url, opener=NO_PROXY)


def test_the_request_carries_the_text_the_model_the_question_and_the_key(stub):
    stub.answer(0.08)
    assert _backend(stub).check(TEXT) is None
    (request,) = stub.requests
    assert request["path"] == "/v1/systemone"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert request["body"] == {"state": TEXT, "model": "jev-latest",
                               "questions": {"plants": JEV_QUESTION_V2}}


def test_the_question_is_the_calibrated_noul_with_both_criteria():
    assert JEV_QUESTION_V2["type"] == "noul"
    assert JEV_QUESTION_V2["instructions"].startswith("The state is a note a coding agent")
    assert JEV_QUESTION_V2["criteria"]["true"].endswith("or running untrusted code.")
    assert JEV_QUESTION_V2["criteria"]["false"].endswith(
        "asks for the user's confirmation is false.")


@pytest.mark.parametrize(("noul", "verdict"), [
    (0.97, Verdict("unsafe")),
    (0.7, Verdict("unsafe")),                  # at the threshold: blocked
    (0.69, None),                              # low confidence: allowed
    (0, None),
    (1, Verdict("unsafe")),
])
def test_a_score_at_or_above_the_threshold_blocks(stub, noul, verdict):
    stub.answer(noul)
    assert _backend(stub).check(TEXT) == verdict


def test_the_threshold_is_the_configured_one(stub):
    stub.answer(0.89)
    assert _backend(stub, threshold=0.9).check(TEXT) is None
    assert _backend(stub).score(TEXT) == 0.89


def test_a_missing_key_is_unavailable_and_sends_nothing(stub):
    assert _backend(stub, env={}).check(TEXT) == Verdict("unavailable", detail="no-key")
    assert _backend(stub, env={"JEV_KEY": ""}).check(TEXT) == Verdict("unavailable",
                                                                      detail="no-key")
    assert stub.requests == []


def test_the_key_is_read_at_call_time(stub):
    env: dict[str, str] = {}
    backend = _backend(stub, env=env)
    assert backend.check(TEXT) == Verdict("unavailable", detail="no-key")
    env["JEV_KEY"] = KEY
    stub.answer(0.1)
    assert backend.check(TEXT) is None


@pytest.mark.parametrize(("reply", "detail"), [
    ((401, json.dumps({"error": f"bad key {KEY}"}).encode(), 0.0), "http-401"),
    ((500, b"boom", 0.0), "http-500"),
    ((200, b"not json", 0.0), "unparsable"),
    ((200, b"{}", 0.0), "unparsable"),
    ((200, json.dumps({"answers": {"plants": {"noul": "0.9"}}}).encode(), 0.0), "unparsable"),
    ((200, json.dumps({"answers": {"plants": {"noul": True}}}).encode(), 0.0), "unparsable"),
    ((200, json.dumps({"answers": {"plants": {"noul": 1.5}}}).encode(), 0.0), "unparsable"),
    ((200, b'{"answers": {"plants": {"noul": NaN}}}', 0.0), "unparsable"),
    ((200, json.dumps([1, 2]).encode(), 0.0), "unparsable"),
])
def test_every_bad_answer_is_unavailable_with_a_fixed_detail(stub, reply, detail):
    stub.reply = reply
    verdict = _backend(stub).check(TEXT)
    assert verdict == Verdict("unavailable", detail=detail)


def test_a_slow_answer_is_a_timeout(stub):
    stub.answer(0.1)
    stub.reply = (*stub.reply[:2], 1.0)
    assert _backend(stub, timeout_s=0.2).check(TEXT) == Verdict("unavailable", detail="timeout")


def test_nothing_listening_is_unreachable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    backend = JevBackend(env={"JEV_KEY": KEY}, api_key_env="JEV_KEY", model="jev-latest",
                         threshold=0.7, timeout_s=2, url=f"http://127.0.0.1:{port}/v1/systemone",
                         opener=NO_PROXY)
    assert backend.check(TEXT) == Verdict("unavailable", detail="unreachable")


@pytest.mark.parametrize("status", [302, 303])
def test_a_redirect_is_never_followed_so_the_key_never_leaves(stub, status):
    # a 3xx to another origin (a second local stub on another port) must not be
    # followed: no second request, so the Authorization header never reaches it, and
    # the write is refused as unavailable with the fixed status detail
    other = Stub()
    try:
        other.answer(0.01)                              # would be "allowed" if reached
        stub.reply = (status, b"", 0.0, {"Location": other.url})
        assert _backend(stub).check(TEXT) == Verdict("unavailable", detail=f"http-{status}")
        assert len(stub.requests) == 1
        assert other.requests == []
    finally:
        other.server.shutdown()
        other.server.server_close()


def test_the_default_opener_uses_the_same_redirect_policy():
    backend = JevBackend(env={}, api_key_env="JEV_KEY", model="jev-latest", threshold=0.7,
                         timeout_s=5)
    redirects = [handler for handler in backend._opener.handlers
                 if isinstance(handler, urllib.request.HTTPRedirectHandler)]
    assert len(redirects) == 1
    assert redirects[0].redirect_request(None, None, 302, "Found", {}, "http://x/") is None


def test_the_key_and_the_text_never_reach_logs_or_output(stub, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    secret_text = "note " + "t" * 30
    for reply in ((200, json.dumps({"answers": {"plants": {"noul": 0.9}}}).encode(), 0.0),
                  (401, json.dumps({"echo": KEY}).encode(), 0.0), (200, b"garbage", 0.0)):
        stub.reply = reply
        verdict = _backend(stub).check(secret_text)
        assert KEY not in repr(verdict) and secret_text not in repr(verdict)
    out, err = capsys.readouterr()
    for text in (caplog.text, out, err):
        assert KEY not in text and secret_text not in text
