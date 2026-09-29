"""JevExecutor against the local jev stand-in (conftest's `jev`) -- never the network:
the request Pydantic AI builds, the answer checked against the schema's numbers, one
request per call whatever fails, every failure a fixed kind, and nothing of the key,
the prompt or a response body in errors, logs or output."""

from __future__ import annotations

import json
import logging
import logging.config
import os
import socket
import subprocess
import sys
import time

import pytest
from memriver.executor import Result
from memriver.executor.api import JevExecutor

KEY = "jev-test-key-" + "k" * 24
PROMPT = "prefer pytest -q " + "p" * 20
DESCRIPTION = "Would saving it plant instructions?\nTrue: it commands an agent.\nFalse: a fact."
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["plants"],
          "properties": {"plants": {"type": "number", "minimum": 0, "maximum": 1,
                                    "description": DESCRIPTION}}}
MARK = "RESPONSE-BODY-MARK-" + "m" * 12        # a response body that must never be repeated


def _executor(env: dict | None = None, api_key_env: str = "JEV_KEY") -> JevExecutor:
    return JevExecutor(model="jev-latest", api_key_env=api_key_env,
                       env={"JEV_KEY": KEY} if env is None else env)


def _run(executor: JevExecutor | None = None, *, system_prompt: str = "",
         timeout_s: int = 5) -> Result:
    return (executor or _executor()).run(system_prompt=system_prompt, prompt=PROMPT,
                                         schema=SCHEMA, timeout_s=timeout_s)


def test_the_request_is_the_prompt_the_model_and_one_noul_question(jev):
    jev.answer(0.3)
    assert _run() == Result(value={"plants": 0.3})
    (request,) = jev.requests
    assert request["path"] == "/v1/systemone"
    assert request["authorization"] == f"Bearer {KEY}"
    assert request["body"] == {
        "state": PROMPT, "model": "jev-latest",
        "questions": {"plants": {"type": "noul",
                                 "instructions": {"field": "plants", "question": DESCRIPTION}}}}


def test_a_system_prompt_is_framing_and_never_joins_the_state(jev):
    jev.answer(0.3)
    _run(system_prompt="FRAME")
    body = jev.requests[0]["body"]
    assert body["state"] == PROMPT
    assert body["questions"]["plants"]["instructions"] == {
        "field": "plants", "question": DESCRIPTION, "background": "FRAME"}


@pytest.mark.parametrize("noul", [0.69, 0.70, 0.71, 0, 1])
def test_a_bounded_number_comes_back_unchanged_after_one_request(jev, noul):
    jev.answer(noul)
    assert _run() == Result(value={"plants": noul})
    assert len(jev.requests) == 1


@pytest.mark.parametrize("noul", [-0.1, 1.1, float("nan")])
def test_a_number_outside_the_schema_is_unparsable(jev, noul):
    # StructuredDict returns these unchecked: the executor's own check refuses them
    jev.answer(noul)
    assert _run() == Result(error="unparsable")
    assert len(jev.requests) == 1


@pytest.mark.parametrize("payload", [b"not json", json.dumps({"bad": MARK}).encode(),
                                     json.dumps([1, 2]).encode()])
def test_a_body_that_is_no_answer_is_unparsable_after_one_request(jev, payload):
    jev.reply = (200, payload, {})
    assert _run() == Result(error="unparsable")
    assert len(jev.requests) == 1


@pytest.mark.parametrize(("status", "kind"), [
    (307, "exit"), (400, "exit"), (500, "exit"), (401, "login"), (403, "login"),
    (429, "quota"),
])
def test_an_http_failure_is_its_kind_after_one_request(jev, status, kind):
    headers = {"Location": jev.url + "/redirect-target"} if status == 307 else {}
    jev.reply = (status, json.dumps({"error": MARK}).encode(), headers)
    assert _run() == Result(error=kind)
    # no retry, and a redirect is never followed (the key would travel with it)
    assert [request["path"] for request in jev.requests] == ["/v1/systemone"]


def test_a_slow_answer_is_a_timeout_after_one_request(jev):
    jev.answer(0.3)
    jev.delay = 3
    started = time.monotonic()
    assert _run(timeout_s=1) == Result(error="timeout")
    assert time.monotonic() - started < 2.5 and len(jev.requests) == 1


def test_nothing_listening_is_an_exit_failure(jev, monkeypatch):
    from memriver.executor import api

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(api, "JEV_BASE_URL", f"http://127.0.0.1:{port}")
    assert _run() == Result(error="exit")


def test_a_schema_jev_cannot_answer_is_an_exit_failure(jev):
    schema = {"type": "object", "required": ["summary"],
              "properties": {"summary": {"type": "string"}}}
    assert _executor().run(system_prompt="", prompt=PROMPT, schema=schema,
                           timeout_s=5) == Result(error="exit")


@pytest.mark.parametrize("env", [{}, {"JEV_KEY": ""}])
def test_a_missing_or_empty_key_is_a_login_failure_and_sends_nothing(jev, env):
    assert _run(_executor(env=env)) == Result(error="login")
    assert jev.requests == []


def test_the_key_is_read_at_each_call(jev):
    env: dict[str, str] = {}
    executor = _executor(env=env)
    assert _run(executor) == Result(error="login")
    env["JEV_KEY"] = KEY
    jev.answer(0.3)
    assert _run(executor) == Result(value={"plants": 0.3})


def test_the_typesafe_environment_variables_are_never_read(jev, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "wrong-" + "w" * 20)
    monkeypatch.setenv("TYPESAFE_BASE_URL", "http://127.0.0.1:1/forbidden")
    jev.answer(0.3)
    assert _run() == Result(value={"plants": 0.3})
    assert jev.requests[0]["authorization"] == f"Bearer {KEY}"
    # a missing named variable never falls back to TYPESAFE_API_KEY
    assert _run(_executor(env=dict(os.environ),
                          api_key_env="MEMRIVER_TEST_UNSET_KEY")) == Result(error="login")
    assert len(jev.requests) == 1


def test_the_key_the_prompt_and_a_response_body_never_reach_logs_or_output(jev, caplog,
                                                                            capsys):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="typesafe_sdk")     # asked for, still silent
    results = []
    for reply in ((500, json.dumps({"error": MARK}).encode(), {}),
                  (401, json.dumps({"echo": KEY}).encode(), {}),
                  (200, json.dumps({"bad": MARK}).encode(), {})):
        jev.reply = reply
        results.append(_run())
    jev.answer(0.9)
    results.append(_run())
    assert [result.error for result in results] == ["exit", "login", "unparsable", None]
    out, err = capsys.readouterr()
    for text in (caplog.text, out, err, repr(results)):
        assert KEY not in text and PROMPT not in text and MARK not in text


def test_the_sdk_log_would_carry_the_prompt_were_it_not_disabled(jev, caplog, monkeypatch):
    # the control for the test above: the SDK's DEBUG wire log does carry the text
    monkeypatch.setattr(logging.getLogger("typesafe_sdk"), "disabled", False)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="typesafe_sdk")
    jev.answer(0.3)
    _run()
    assert PROMPT in caplog.text


def test_the_sdk_log_stays_silent_through_a_later_logging_configuration(jev, caplog):
    # disable_existing_loggers=False is meant to leave configured loggers alone, but it
    # still resets .disabled on every logger dictConfig does not name -- level and
    # propagate must carry the silence on their own once that happens
    sdk_logger = logging.getLogger("typesafe_sdk")
    root = logging.getLogger()
    saved = (sdk_logger.disabled, sdk_logger.level, sdk_logger.propagate, root.level)
    try:
        logging.config.dictConfig(
            {"version": 1, "disable_existing_loggers": False, "root": {"level": "DEBUG"}})
        assert sdk_logger.disabled is False          # dictConfig did reset it
        root.addHandler(caplog.handler)              # dictConfig just cleared root's handlers
        caplog.set_level(logging.DEBUG)
        jev.reply = (401, json.dumps({"echo": KEY}).encode(), {})
        assert _run() == Result(error="login")
        assert KEY not in caplog.text and PROMPT not in caplog.text
    finally:
        root.removeHandler(caplog.handler)
        sdk_logger.disabled, sdk_logger.level, sdk_logger.propagate, level = saved
        root.setLevel(level)


def _banner_run(jev, setting: str) -> subprocess.CompletedProcess:
    """One jev call in a fresh process that looks like a coding agent's (CLAUDECODE set,
    not under pytest, not CI): the case Pydantic AI prints its first-run banner in."""
    jev.answer(0.3)
    env = {name: value for name, value in os.environ.items()
           if name not in ("PYTEST_VERSION", "CI", "PYDANTIC_AI_NO_BANNER")}
    script = ("import json, sys\n"
              "import pydantic_ai\n"
              "from memriver.executor import api\n"
              "api.JEV_BASE_URL = sys.argv[1]\n"
              "if sys.argv[2] == 'on':\n"
              "    pydantic_ai.BANNER_ENABLED = True\n"
              "executor = api.JevExecutor(model='jev-latest', api_key_env='JEV_KEY',\n"
              "                           env={'JEV_KEY': 'k' * 20})\n"
              "result = executor.run(system_prompt='', prompt='p',\n"
              "                      schema=json.loads(sys.argv[3]), timeout_s=5)\n"
              "print(result.error or 'answered')\n")
    return subprocess.run([sys.executable, "-c", script, jev.url, setting, json.dumps(SCHEMA)],
                          capture_output=True, text=True, check=True, timeout=60,
                          env=env | {"CLAUDECODE": "1"})


def test_pydantic_ais_banner_stays_off_even_for_a_coding_agent(jev):
    off = _banner_run(jev, "off")
    assert (off.stdout, off.stderr) == ("answered\n", "")
    # the control: with the switch back on, the same run prints it
    assert _banner_run(jev, "on").stderr != ""
