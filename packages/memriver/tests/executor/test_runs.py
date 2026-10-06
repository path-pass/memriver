"""The harness runner's runs against a fake process runner and local stand-in
processes, plus real process-group timeouts on a Python stand-in -- never a real
harness."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
from memriver.executor import FAILURE_KINDS
from memriver.executor.harness import (
    Completed,
    isolated_env,
    missing_env,
    override_args,
    run_claude,
    run_codex,
    run_process,
)
from memriver.settings import KILL_GRACE_S

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
          "properties": {"summary": {"type": "string"}}}
BASE_ENV = {"HOME": "/home/u", "PATH": "/usr/bin:/bin", "MEMRIVER_ROOT": "/home/u/agent-memory"}
PREFIX = "memriver-test-"
# text a harness may repeat in its output: never evidence of a failure kind
ECHO = ("please log in to the usage limit dashboard; 401 and 429 are fine here. Document "
        "the context window setting; there were too many tokens in the log.")
# 360,000 combining marks: 1,080,000 bytes of UTF-8 -- more than one argv element may
# hold on Linux, and than all of argv on macOS
HUGE_PROMPT = chr(0x20DD) * 360_000
DEEP = "[" * 200_000 + "]" * 200_000     # nested past the JSON parser's recursion limit
PROVIDER = {"model_provider": "foundry", "model": 'deploy "a"',
            "model_providers.foundry.env_key": "FOUNDRY_API_KEY",
            "model_providers.foundry.requires_openai_auth": False}


class Runner:
    """Records one call and answers with `completed` (or builds it from the call)."""

    def __init__(self, completed=None, on_call=None) -> None:
        self.completed, self.on_call, self.seen = completed, on_call, {}

    def __call__(self, argv, *, cwd, env, timeout_s, stdin_text):
        self.seen = {"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout_s": timeout_s,
                     "stdin": stdin_text, "cwd_entries": sorted(os.listdir(cwd)),
                     "store_exists": os.path.exists(env["MEMRIVER_ROOT"])}
        return self.on_call(argv) if self.on_call is not None else self.completed


def _claude(completed, *, env=BASE_ENV, **options):
    runner = Runner(completed)
    answer = run_claude("/opt/bin/claude", system_prompt="SYS", prompt="PROMPT", schema=SCHEMA,
                        timeout_s=60, env=env, scratch_prefix=PREFIX, runner=runner,
                        **options)
    return answer, runner


def _codex(on_call, *, env=BASE_ENV, **options):
    runner = Runner(on_call=on_call)
    answer = run_codex("/opt/bin/codex", system_prompt="SYS", prompt="PROMPT", schema=SCHEMA,
                       timeout_s=60, env=env, scratch_prefix=PREFIX, runner=runner, **options)
    return answer, runner


def _ok(payload: dict) -> Completed:
    return Completed(0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": "", "structured_output": payload}), "")


def _error_result(message: str, stderr: str = "") -> Completed:
    return Completed(1, json.dumps({"type": "result", "is_error": True, "result": message}),
                     stderr)


def _events(*events: dict) -> str:
    """Codex's --json output: one event per line."""
    return "\n".join(json.dumps(event) for event in events)


def _turn_failed(message: str) -> dict:
    return {"type": "turn.failed", "error": {"message": message}}


def _answering(payload):
    """A codex stand-in call that writes `payload` as its last message and exits 0."""
    def answer(argv):
        Path(argv[argv.index("-o") + 1]).write_text(
            payload if isinstance(payload, str) else json.dumps(payload))
        return Completed(0, "", "")
    return answer


def test_the_failure_kinds_are_one_fixed_set():
    assert FAILURE_KINDS == ("start", "timeout", "login", "quota", "too-large", "exit",
                             "unparsable")
    assert KILL_GRACE_S == 2


def test_a_claude_run_gets_the_prompt_on_stdin_an_empty_directory_and_no_store():
    answer, runner = _claude(_ok({"summary": "s"}))
    assert answer == {"summary": "s"}
    seen = runner.seen
    assert (seen["stdin"], seen["timeout_s"], seen["cwd_entries"]) == ("PROMPT", 60, [])
    assert "PROMPT" not in seen["argv"]
    assert seen["env"]["MEMRIVER_ROOT"].startswith(str(seen["cwd"]))
    assert seen["store_exists"] is False
    assert {k: v for k, v in seen["env"].items() if k != "MEMRIVER_ROOT"} == {
        "HOME": "/home/u", "PATH": "/usr/bin:/bin"}
    assert Path(seen["cwd"]).name.startswith(PREFIX)
    assert not Path(seen["cwd"]).exists()                    # removed afterwards


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(None, "", "", started=False), "start"),
    (Completed(None, "", ""), "timeout"),
    (_error_result("Prompt is too long"), "too-large"),
    (_error_result("Invalid API key · Please run /login"), "login"),
    (Completed(1, "", "Error: Not logged in · Please run /login"), "login"),
    (_error_result("Claude AI usage limit reached"), "quota"),
    (Completed(1, "", "Error: Claude usage limit reached"), "quota"),
    (Completed(2, "", "something else broke"), "exit"),
    (Completed(0, "not json", ""), "unparsable"),
    (Completed(0, json.dumps({"type": "result", "is_error": False, "result": "text"}), ""),
     "unparsable"),
    (Completed(0, json.dumps({"is_error": False, "structured_output": "text"}), ""),
     "unparsable"),
    (Completed(0, DEEP, ""), "unparsable"),
])
def test_each_claude_failure_is_its_kind_and_carries_no_output(completed, kind):
    assert _claude(completed)[0] == kind


@pytest.mark.parametrize(("message", "kind"), [
    ("Claude AI usage limit reached", "quota"),
    ("Invalid API key · Please run /login", "login"),
    ("API Error: 500 Internal server error", "exit"),
])
def test_claude_kinds_come_from_the_error_result_never_from_echoed_text(message, kind):
    assert _claude(_error_result(message, stderr=ECHO))[0] == kind


def test_an_answer_that_echoes_the_text_is_still_the_answer():
    completed = Completed(0, json.dumps({"is_error": False, "result": ECHO,
                                         "structured_output": {"summary": "s"}}), "")
    assert _claude(completed)[0] == {"summary": "s"}


@pytest.mark.parametrize("message", [
    "Failed to update the catalog in the workspace",
    "Could not post to the blog in time",
])
def test_login_wording_needs_a_whole_word_so_catalog_in_or_blog_in_is_not_a_login(message):
    assert _claude(_error_result(message))[0] == "exit"


@pytest.mark.parametrize(("message", "kind"), [
    ("rate limit reached: prompt is too long", "quota"),
    ("not logged in; context window unknown", "login"),
])
def test_quota_and_login_come_before_too_large(message, kind):
    assert _claude(_error_result(message))[0] == kind


def test_a_codex_run_writes_its_files_apart_from_the_empty_working_directory():
    def answer(argv):
        files = Path(argv[argv.index("--output-schema") + 1]).parent
        assert json.loads((files / "schema.json").read_text()) == SCHEMA
        assert (files / "instructions.md").read_text() == "SYS"
        (files / "last-message.json").write_text(json.dumps({"summary": "s"}))
        return Completed(0, "", "")

    result, runner = _codex(answer)
    assert result == {"summary": "s"}
    seen = runner.seen
    files = Path(seen["argv"][seen["argv"].index("-o") + 1]).parent
    assert seen["stdin"] == "PROMPT" and seen["cwd_entries"] == []
    assert seen["store_exists"] is False and files != Path(seen["cwd"])
    assert files.name.startswith(PREFIX) and Path(seen["cwd"]).name.startswith(PREFIX)
    assert not files.exists() and not Path(seen["cwd"]).exists()


_ECHOED = {"type": "item.completed", "item": {"type": "agent_message", "text": ECHO}}


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(None, "", "", started=False), "start"),
    (Completed(None, "", ""), "timeout"),
    (Completed(1, _events(_turn_failed("context_length_exceeded")), ""), "too-large"),
    (Completed(1, _events({"type": "error", "message": "401 Unauthorized"}), ""), "login"),
    (Completed(1, "", "ERROR: 401 Unauthorized"), "login"),       # failed before any event
    (Completed(1, _events(_turn_failed("You've hit your usage limit")), ""), "quota"),
    (Completed(1, _events(_turn_failed("429 Too Many Requests")), ""), "quota"),
    (Completed(1, "", "boom"), "exit"),
    (Completed(1, "", "error: something else"), "exit"),
    (Completed(0, "", ""), "unparsable"),                          # no last-message file
])
def test_each_codex_failure_is_its_kind(completed, kind):
    assert _codex(lambda argv: completed)[0] == kind


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(1, "", ECHO + "\nERROR: 429 quota exceeded"), "quota"),
    (Completed(1, _events(_ECHOED, _turn_failed("401 Unauthorized")), ""), "login"),
    (Completed(1, _events(_ECHOED, {"type": "error", "message": "stream disconnected"}),
               ECHO), "exit"),
])
def test_codex_kinds_come_from_error_events_never_from_echoed_text(completed, kind):
    assert _codex(lambda argv: completed)[0] == kind


@pytest.mark.parametrize("payload", [["not", "an", "object"], DEEP, "not json"])
def test_a_codex_last_message_that_is_no_object_is_unparsable(payload):
    assert _codex(_answering(payload))[0] == "unparsable"


@pytest.mark.parametrize("env", [BASE_ENV, {**BASE_ENV, "FOUNDRY_API_KEY": ""}])
def test_a_missing_or_empty_provider_variable_is_a_login_failure_and_starts_nothing(env):
    result, runner = _codex(_answering({"summary": "s"}), env=env, overrides=PROVIDER)
    assert result == "login" and runner.seen == {}


def test_the_provider_overrides_and_the_model_reach_the_codex_argv():
    result, runner = _codex(_answering({"summary": "s"}),
                            env={**BASE_ENV, "FOUNDRY_API_KEY": "set"}, overrides=PROVIDER,
                            model="gpt-5-mini")
    assert result == {"summary": "s"}
    assert 'model_provider="foundry"' in runner.seen["argv"]
    assert 'model="gpt-5-mini"' in runner.seen["argv"]


def test_a_run_whose_temporary_directory_cannot_be_made_is_a_start_failure(tmp_path,
                                                                           monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "missing"))
    claude, claude_runner = _claude(_ok({"summary": "s"}))
    codex, codex_runner = _codex(_answering({"summary": "s"}))
    assert (claude, codex) == ("start", "start")
    assert claude_runner.seen == {} and codex_runner.seen == {}


def test_the_environment_and_override_helpers():
    base = dict(BASE_ENV)
    assert isolated_env(base, Path("/scratch/work")) == {
        **BASE_ENV, "MEMRIVER_ROOT": "/scratch/work/no-memriver-store"}
    assert base == BASE_ENV                                  # a copy, never the caller's
    assert override_args(PROVIDER) == [
        "-c", 'model_provider="foundry"', "-c", 'model="deploy \\"a\\""',
        "-c", 'model_providers.foundry.env_key="FOUNDRY_API_KEY"',
        "-c", "model_providers.foundry.requires_openai_auth=false"]
    assert missing_env(PROVIDER, {}) == ["FOUNDRY_API_KEY"]
    assert missing_env(PROVIDER, {"FOUNDRY_API_KEY": "set"}) == []
    assert missing_env({"model_providers.a.env_key": "B",
                        "model_providers.b.env_key": "A"}, {}) == ["A", "B"]


# what a run started inside a Claude Code or Codex session inherits from it
SESSION_MARKERS = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                   "CLAUDE_CODE_SSE_PORT": "53111", "CODEX_SANDBOX": "seatbelt",
                   "CODEX_SANDBOX_NETWORK_DISABLED": "1"}
# authentication and provider settings: never markers
KEPT = {"ANTHROPIC_API_KEY": "synthetic", "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1", "AWS_PROFILE": "work", "CODEX_HOME": "/home/u/.codex",
        "FOUNDRY_API_KEY": "set", "CLAUDE_CONFIG_DIR": "/home/u/.claude-alt"}


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_run_drops_the_calling_harness_session_markers_and_keeps_the_rest(harness):
    env = {**BASE_ENV, **SESSION_MARKERS, **KEPT}
    if harness == "claude":
        _, runner = _claude(_ok({"summary": "s"}), env=env)
        _, plain = _claude(_ok({"summary": "s"}))
        assert runner.seen["argv"] == plain.seen["argv"]          # argv unchanged
    else:
        _, runner = _codex(_answering({"summary": "s"}), env=env, overrides=PROVIDER)
    child = runner.seen["env"]
    # every marker gone, everything else as given (MEMRIVER_ROOT replaced, as before)
    assert child == {**BASE_ENV, **KEPT, "MEMRIVER_ROOT": child["MEMRIVER_ROOT"]}
    assert child["MEMRIVER_ROOT"] != BASE_ENV["MEMRIVER_ROOT"]
    assert env == {**BASE_ENV, **SESSION_MARKERS, **KEPT}          # the caller's, untouched


def test_only_the_listed_names_and_the_codex_sandbox_prefix_are_markers():
    # exact, case-sensitive names; CODEX_SANDBOX as a prefix only
    env = isolated_env({"CLAUDECODE_X": "1", "XCLAUDECODE": "1", "claudecode": "1",
                        "CODEX_SANDBOXED_TOOL": "1", "MY_CODEX_SANDBOX": "1",
                        "CLAUDE_CODE_ENTRYPOINT_V2": "1"}, Path("/scratch/work"))
    assert set(env) == {"CLAUDECODE_X", "XCLAUDECODE", "claudecode", "MY_CODEX_SANDBOX",
                        "CLAUDE_CODE_ENTRYPOINT_V2", "MEMRIVER_ROOT"}


def _stand_in(tmp_path: Path, body: str) -> Path:
    """A local executable standing in for a harness."""
    script = tmp_path / "stand-in"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(0o755)
    return script


def test_a_huge_unicode_prompt_reaches_a_claude_stand_in_intact_on_stdin(tmp_path):
    script = _stand_in(tmp_path, """
        import json, sys
        data = sys.stdin.buffer.read().decode("utf-8")
        print(json.dumps({"type": "result", "is_error": False,
                          "structured_output": {"summary": str(len(data))}}))
    """)
    assert run_claude(str(script), system_prompt="s", prompt=HUGE_PROMPT, schema=SCHEMA,
                      timeout_s=60, env=dict(os.environ),
                      scratch_prefix=PREFIX) == {"summary": "360000"}


def test_a_huge_unicode_prompt_reaches_a_codex_stand_in_intact_on_stdin(tmp_path):
    script = _stand_in(tmp_path, """
        import json, sys
        data = sys.stdin.buffer.read().decode("utf-8")
        out = sys.argv[sys.argv.index("-o") + 1]
        open(out, "w").write(json.dumps({"summary": str(len(data))}))
    """)
    assert run_codex(str(script), system_prompt="s", prompt=HUGE_PROMPT, schema=SCHEMA,
                     timeout_s=60, env=dict(os.environ),
                     scratch_prefix=PREFIX) == {"summary": "360000"}


def test_a_process_that_cannot_start_is_a_start_failure(tmp_path):
    not_executable = tmp_path / "plain-file"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    for executable in (str(tmp_path / "missing"), str(not_executable)):
        assert run_claude(executable, system_prompt="s", prompt="p", schema=SCHEMA,
                          timeout_s=5, env=dict(os.environ), scratch_prefix=PREFIX) == "start"
    assert run_process([str(tmp_path / "missing")], cwd=tmp_path, env={}, timeout_s=5,
                       stdin_text="") == Completed(None, "", "", started=False)
    # an argument list too long for the OS: E2BIG, reported, never raised
    too_long = run_process(["/bin/echo", "x" * 3_000_000], cwd=tmp_path, env=dict(os.environ),
                           timeout_s=5, stdin_text="")
    assert too_long.started is False


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
        pytest.fail("the grandchild outlived the timeout")


def test_a_timeout_returns_even_when_a_process_that_left_the_group_holds_the_pipes(tmp_path):
    pid_file = tmp_path / "escaped.pid"
    script = tmp_path / "escape.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(60)
    """), encoding="utf-8")
    started = time.monotonic()
    try:
        completed = run_process([sys.executable, str(script)], cwd=tmp_path,
                                env=dict(os.environ), timeout_s=1, stdin_text="")
        elapsed = time.monotonic() - started
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert completed == Completed(None, "", "")
    assert elapsed < 1 + KILL_GRACE_S + 3
