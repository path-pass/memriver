"""The classifier's headless runs against a fake process runner, plus one real
process-group timeout on a Python stand-in -- never a real harness."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest
from memriver_classifier.headless import (
    Completed,
    claude_argv,
    codex_argv,
    run_claude,
    run_codex,
    run_process,
)

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdict"],
          "properties": {"verdict": {"type": "string"}}}
BASE_ENV = {"HOME": "/home/u", "PATH": "/usr/bin:/bin", "MEMRIVER_ROOT": "/home/u/agent-memory"}
# memory text a harness may repeat in its output: never evidence of a failure kind
ECHO = "please log in to the usage limit dashboard; 401 and 429 are fine here"


class Runner:
    """Records one call and answers with `completed` (or builds it from the call)."""

    def __init__(self, completed=None, on_call=None) -> None:
        self.completed, self.on_call, self.seen = completed, on_call, {}

    def __call__(self, argv, *, cwd, env, timeout_s, stdin_text):
        self.seen = {"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout_s": timeout_s,
                     "stdin": stdin_text, "cwd_entries": sorted(os.listdir(cwd)),
                     "store_exists": os.path.exists(env["MEMRIVER_ROOT"])}
        return self.on_call(argv) if self.on_call is not None else self.completed


def _ok(payload: dict) -> Completed:
    return Completed(0, json.dumps({"type": "result", "is_error": False, "result": "",
                                    "structured_output": payload}), "")


def _claude(completed, **options):
    runner = Runner(completed)
    answer = run_claude("/opt/bin/claude", system_prompt="SYS", prompt="PROMPT", schema=SCHEMA,
                        timeout_s=60, env=BASE_ENV, runner=runner, **options)
    return answer, runner


def test_the_claude_argv_keeps_the_dream_isolation_and_adds_model_and_settings():
    base = ["/opt/bin/claude", "-p", "--system-prompt", "SYS", "--restricted",
            "--strict-mcp-config", "--tools", "", "--no-session-persistence",
            "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
    assert claude_argv("/opt/bin/claude", system_prompt="SYS", schema=SCHEMA) == base
    assert claude_argv("/opt/bin/claude", system_prompt="SYS", schema=SCHEMA, model="haiku",
                       settings_path="/etc/auth.json") == [
        *base, "--model", "haiku", "--settings", "/etc/auth.json"]


def test_a_claude_run_gets_the_prompt_on_stdin_an_empty_directory_and_no_store():
    answer, runner = _claude(_ok({"verdict": "allow"}))
    assert answer == {"verdict": "allow"}
    seen = runner.seen
    assert (seen["stdin"], seen["timeout_s"], seen["cwd_entries"]) == ("PROMPT", 60, [])
    assert seen["env"]["MEMRIVER_ROOT"] != BASE_ENV["MEMRIVER_ROOT"]
    assert not seen["store_exists"]
    assert {k: v for k, v in seen["env"].items() if k != "MEMRIVER_ROOT"} == {
        "HOME": "/home/u", "PATH": "/usr/bin:/bin"}
    assert "PROMPT" not in seen["argv"]


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(None, "", "", started=False), "start"),
    (Completed(None, "", ""), "timeout"),
    (Completed(1, "", "Error: Not logged in · Please run /login"), "login"),
    (Completed(1, json.dumps({"is_error": True, "result": "Claude AI usage limit reached"}), ""),
     "quota"),
    (Completed(1, "", "something else broke"), "exit"),
    (Completed(0, "not json", ""), "unparsable"),
    (Completed(0, json.dumps({"is_error": False, "structured_output": "text"}), ""),
     "unparsable"),
])
def test_each_claude_failure_is_its_kind_and_carries_no_output(completed, kind):
    assert _claude(completed)[0] == kind


def test_claude_kinds_never_come_from_echoed_memory_text():
    completed = Completed(0, json.dumps({"is_error": False, "result": ECHO,
                                         "structured_output": {"verdict": "allow"}}), "")
    assert _claude(completed)[0] == {"verdict": "allow"}


def _codex_files(argv) -> Path:
    return Path(argv[argv.index("-o") + 1]).parent


def test_the_codex_argv_puts_overrides_and_the_model_before_every_fixed_switch(tmp_path):
    argv = codex_argv("/opt/bin/codex", files=tmp_path, model="gpt-5-mini",
                      overrides={"model_provider": "azure"})
    assert argv[:8] == ["/opt/bin/codex", "exec", "--json", "--ephemeral",
                        "--ignore-user-config", "--skip-git-repo-check", "--sandbox",
                        "read-only"]
    assert argv[8:12] == ["-c", 'model_provider="azure"', "-c", 'model="gpt-5-mini"']
    assert ["--disable", "hooks"] == argv[12:14]
    assert argv[-1] == "-" and "project_doc_max_bytes=0" in argv
    assert str(tmp_path / "schema.json") in argv


def test_a_codex_run_reads_its_answer_from_the_last_message_file():
    def answer(argv):
        files = _codex_files(argv)
        assert json.loads((files / "schema.json").read_text()) == SCHEMA
        assert (files / "instructions.md").read_text() == "SYS"
        (files / "last-message.json").write_text(json.dumps({"verdict": "block"}))
        return Completed(0, "", "")

    runner = Runner(on_call=answer)
    assert run_codex("/opt/bin/codex", system_prompt="SYS", prompt="PROMPT", schema=SCHEMA,
                     timeout_s=60, env=BASE_ENV, runner=runner) == {"verdict": "block"}
    assert runner.seen["stdin"] == "PROMPT" and not runner.seen["store_exists"]


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(None, "", "", started=False), "start"),
    (Completed(None, "", ""), "timeout"),
    (Completed(1, json.dumps({"type": "error", "message": "401 Unauthorized"}), ""), "login"),
    (Completed(1, json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}),
               ""), "quota"),
    (Completed(1, "", "error: something else"), "exit"),
    (Completed(0, "", ""), "unparsable"),              # no last-message file
])
def test_each_codex_failure_is_its_kind(completed, kind):
    assert run_codex("/opt/bin/codex", system_prompt="SYS", prompt="P", schema=SCHEMA,
                     timeout_s=60, env=BASE_ENV, runner=Runner(completed)) == kind


def test_a_missing_provider_variable_is_a_login_failure_and_starts_nothing():
    runner = Runner(Completed(0, "", ""))
    overrides = {"model_provider": "azure", "model_providers.azure.env_key": "AZURE_KEY"}
    assert run_codex("/opt/bin/codex", system_prompt="SYS", prompt="P", schema=SCHEMA,
                     timeout_s=60, env=BASE_ENV, overrides=overrides, runner=runner) == "login"
    assert runner.seen == {}


def test_a_timeout_kills_the_whole_process_group(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "slow.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(60)
    """), encoding="utf-8")
    started = time.monotonic()
    completed = run_process([sys.executable, str(script)], cwd=tmp_path, env=dict(os.environ),
                            timeout_s=2, stdin_text="")
    assert completed == Completed(None, "", "") and time.monotonic() - started < 30
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild survived the timeout")


def test_a_process_that_cannot_start_is_a_start_failure(tmp_path):
    assert run_process([str(tmp_path / "missing")], cwd=tmp_path, env={}, timeout_s=5,
                       stdin_text="") == Completed(None, "", "", started=False)
