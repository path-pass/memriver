"""Executors against a fake process runner and local stand-in processes, plus one real
process-group timeout -- never a real harness."""

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
from memriver.executors import (
    ClaudeExecutor,
    CodexExecutor,
    Completed,
    make_executor,
    run_process,
)
from memriver_core.settings import DREAM_KILL_GRACE_S, DreamSettings
from memriver_dream.protocols import ExecutorResult

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
          "properties": {"summary": {"type": "string"}}}
BASE_ENV = {"HOME": "/home/u", "PATH": "/usr/bin:/bin", "MEMRIVER_ROOT": "/home/u/agent-memory"}
# prompt text a harness may repeat in its output: never evidence of a failure kind
ECHO = "Document the context window setting; there were too many tokens in the log."
# 360,000 combining marks: 90,000 estimated tokens, 1,080,000 bytes of UTF-8 -- more
# than one argv element may hold on Linux, and than all of argv on macOS
HUGE_PROMPT = chr(0x20DD) * 360_000


class Runner:
    """Records one call and answers with `completed` (or builds it from the call)."""

    def __init__(self, completed=None, on_call=None) -> None:
        self.completed, self.on_call, self.seen = completed, on_call, {}

    def __call__(self, argv, *, cwd, env, timeout_s, stdin_text):
        self.seen = {"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout_s": timeout_s,
                     "stdin": stdin_text, "cwd_entries": sorted(os.listdir(cwd)),
                     "store_exists": os.path.exists(env["MEMRIVER_ROOT"])}
        if self.on_call is not None:
            return self.on_call(argv)
        return self.completed


def _claude(completed) -> tuple[ClaudeExecutor, Runner]:
    runner = Runner(completed)
    return ClaudeExecutor("/opt/bin/claude", env=BASE_ENV, runner=runner), runner


def _ok(payload: dict) -> Completed:
    return Completed(0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": "", "structured_output": payload}), "")


def test_the_claude_arguments_hold_everything_but_the_prompt_which_goes_on_stdin():
    executor, runner = _claude(_ok({"summary": "s"}))
    result = executor.run(system_prompt="SYSTEM", prompt="PROMPT", schema=SCHEMA, timeout_s=300)
    assert result == ExecutorResult(value={"summary": "s"})
    assert runner.seen["argv"] == [
        "/opt/bin/claude", "-p", "--system-prompt", "SYSTEM", "--restricted",
        "--strict-mcp-config", "--tools", "", "--no-session-persistence", "--output-format",
        "json", "--json-schema", json.dumps(SCHEMA)]
    assert (runner.seen["stdin"], runner.seen["timeout_s"]) == ("PROMPT", 300)


def test_a_run_gets_an_empty_directory_and_the_environment_with_no_memriver_store():
    executor, runner = _claude(_ok({"summary": "s"}))
    executor.run(system_prompt="s", prompt="p", schema=SCHEMA, timeout_s=5)
    env = runner.seen["env"]
    assert runner.seen["cwd_entries"] == []
    assert (env["HOME"], env["PATH"]) == (BASE_ENV["HOME"], BASE_ENV["PATH"])
    assert env["MEMRIVER_ROOT"] != BASE_ENV["MEMRIVER_ROOT"]
    assert env["MEMRIVER_ROOT"].startswith(str(runner.seen["cwd"]))
    assert runner.seen["store_exists"] is False
    assert not Path(runner.seen["cwd"]).exists()             # removed afterwards


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(None, "", ""), "timeout"),
    (Completed(None, "", "", started=False), "start"),
    (Completed(1, json.dumps({"type": "result", "is_error": True,
                              "result": "Prompt is too long"}), ""), "too-large"),
    (Completed(1, json.dumps({"type": "result", "is_error": True,
                              "result": "Invalid API key · Please run /login"}), ""), "login"),
    (Completed(1, "", "Error: Claude usage limit reached"), "quota"),
    (Completed(2, "", "something else broke"), "exit"),
    (Completed(0, "not json", ""), "unparsable"),
    (Completed(0, json.dumps({"type": "result", "is_error": False, "result": "text"}), ""),
     "unparsable"),
])
def test_each_claude_failure_maps_to_its_kind_and_carries_no_content(completed, kind):
    executor, _ = _claude(completed)
    assert executor.run(system_prompt="s", prompt="SECRET PROMPT", schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error=kind)


@pytest.mark.parametrize(("message", "kind"), [
    ("Claude AI usage limit reached", "quota"),
    ("Invalid API key · Please run /login", "login"),
    ("API Error: 500 Internal server error", "exit"),
])
def test_claude_kinds_come_from_the_error_result_never_from_echoed_prompt_text(message, kind):
    completed = Completed(1, json.dumps({"type": "result", "is_error": True,
                                         "result": message}), ECHO)
    executor, _ = _claude(completed)
    assert executor.run(system_prompt="s", prompt=ECHO, schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error=kind)


def _codex(on_call) -> tuple[CodexExecutor, Runner]:
    runner = Runner(on_call=on_call)
    return CodexExecutor("/opt/bin/codex", env=BASE_ENV, runner=runner), runner


def test_the_codex_arguments_files_stdin_and_answer():
    def answer(argv):
        files = Path(argv[argv.index("--output-schema") + 1]).parent
        assert json.loads((files / "schema.json").read_text()) == SCHEMA
        assert (files / "instructions.md").read_text() == "SYSTEM"
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"summary": "s"}))
        return Completed(0, "", "")

    executor, runner = _codex(answer)
    assert executor.run(system_prompt="SYSTEM", prompt="PROMPT", schema=SCHEMA,
                        timeout_s=300) == ExecutorResult(value={"summary": "s"})
    argv = runner.seen["argv"]
    files = Path(argv[argv.index("--output-schema") + 1]).parent
    assert argv == [
        "/opt/bin/codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
        "--skip-git-repo-check", "--sandbox", "read-only",
        "--disable", "hooks", "--disable", "shell_tool", "--disable", "unified_exec",
        "--disable", "code_mode_host", "--disable", "multi_agent", "--disable", "sleep_tool",
        "--disable", "goals", "--disable", "image_generation", "--disable", "view_image",
        "--disable", "plugins", "-c", 'web_search="disabled"', "--output-schema",
        str(files / "schema.json"), "-c",
        f"model_instructions_file={json.dumps(str(files / 'instructions.md'))}", "-c",
        "project_doc_max_bytes=0", "-o", str(files / "last-message.json"), "-"]
    assert runner.seen["stdin"] == "PROMPT"
    assert runner.seen["cwd_entries"] == [] and files != Path(runner.seen["cwd"])


def _events(*events: dict) -> str:
    """Codex's --json output: one event per line."""
    return "\n".join(json.dumps(event) for event in events)


def _turn_failed(message: str) -> dict:
    return {"type": "turn.failed", "error": {"message": message}}


_ECHOED = {"type": "item.completed", "item": {"type": "agent_message", "text": ECHO}}


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(None, "", ""), "timeout"),
    (Completed(None, "", "", started=False), "start"),
    (Completed(1, _events(_turn_failed("context_length_exceeded")), ""), "too-large"),
    (Completed(1, _events({"type": "error", "message": "401 Unauthorized"}), ""), "login"),
    (Completed(1, _events(_turn_failed("You've hit your usage limit")), ""), "quota"),
    (Completed(1, "", "ERROR: 401 Unauthorized"), "login"),  # failed before any event
    (Completed(1, "", "boom"), "exit"),
    (Completed(0, "", ""), "unparsable"),                    # no last-message file
])
def test_each_codex_failure_maps_to_its_kind(completed, kind):
    executor, _ = _codex(lambda argv: completed)
    assert executor.run(system_prompt="s", prompt="p", schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error=kind)


@pytest.mark.parametrize(("completed", "kind"), [
    (Completed(1, "", ECHO + "\nERROR: 429 quota exceeded"), "quota"),
    (Completed(1, _events(_ECHOED, _turn_failed("401 Unauthorized")), ""), "login"),
    (Completed(1, _events(_ECHOED, {"type": "error", "message": "stream disconnected"}),
               ECHO), "exit"),
])
def test_codex_kinds_come_from_error_events_never_from_echoed_prompt_text(completed, kind):
    executor, _ = _codex(lambda argv: completed)
    assert executor.run(system_prompt="s", prompt=ECHO, schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error=kind)


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
    result = ClaudeExecutor(str(script), env=dict(os.environ)).run(
        system_prompt="s", prompt=HUGE_PROMPT, schema=SCHEMA, timeout_s=60)
    assert result == ExecutorResult(value={"summary": "360000"})


def test_a_huge_unicode_prompt_reaches_a_codex_stand_in_intact_on_stdin(tmp_path):
    script = _stand_in(tmp_path, """
        import json, sys
        data = sys.stdin.buffer.read().decode("utf-8")
        out = sys.argv[sys.argv.index("-o") + 1]
        open(out, "w").write(json.dumps({"summary": str(len(data))}))
    """)
    result = CodexExecutor(str(script), env=dict(os.environ)).run(
        system_prompt="s", prompt=HUGE_PROMPT, schema=SCHEMA, timeout_s=60)
    assert result == ExecutorResult(value={"summary": "360000"})


def test_a_process_that_cannot_start_is_a_start_failure(tmp_path):
    not_executable = tmp_path / "plain-file"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    for executable in (str(tmp_path / "missing"), str(not_executable)):
        assert ClaudeExecutor(executable, env=dict(os.environ)).run(
            system_prompt="s", prompt="p", schema=SCHEMA,
            timeout_s=5) == ExecutorResult(error="start")
    # an argument list too long for the OS: E2BIG, reported, never raised
    too_long = run_process(["/bin/echo", "x" * 3_000_000], cwd=tmp_path, env=dict(os.environ),
                           timeout_s=5, stdin_text="")
    assert too_long.started is False


def test_make_executor_picks_the_configured_one():
    claude = make_executor(DreamSettings(executor="claude", executor_path="/a/claude"), env={})
    codex = make_executor(DreamSettings(executor="codex", executor_path="/a/codex"), env={})
    assert (claude.name, claude.harness, codex.name, codex.harness) == (
        "claude", "claude-code", "codex", "codex")


PROVIDER = {"model_provider": "foundry", "model": 'deploy "a"',
            "model_providers.foundry.env_key": "FOUNDRY_API_KEY",
            "model_providers.foundry.requires_openai_auth": False}


def test_codex_provider_overrides_come_before_every_fixed_switch_and_value():
    executor = CodexExecutor("/opt/bin/codex", env={"FOUNDRY_API_KEY": "set"},
                             overrides=PROVIDER)
    argv = executor.argv(files=Path("/files"))
    start = argv.index("read-only") + 1
    assert argv[start:start + 8] == [
        "-c", 'model_provider="foundry"', "-c", 'model="deploy \\"a\\""',
        "-c", 'model_providers.foundry.env_key="FOUNDRY_API_KEY"',
        "-c", "model_providers.foundry.requires_openai_auth=false"]
    # memriver's fixed switches and values follow, so a same-key -c of theirs would win
    assert argv[start + 8:start + 10] == ["--disable", "hooks"]
    assert argv.index('web_search="disabled"') > start + 8
    assert argv.index("project_doc_max_bytes=0") > start + 8


def test_the_settings_overrides_reach_the_codex_argv_through_the_factory():
    dream = DreamSettings(executor="codex", executor_path="/a/codex",
                          codex_overrides={"model": "deployment-a"})
    argv = make_executor(dream, env={}).argv(files=Path("/files"))
    start = argv.index("read-only") + 1
    assert argv[start:start + 3] == ["-c", 'model="deployment-a"', "--disable"]


def test_a_missing_provider_variable_is_a_login_failure_and_starts_nothing():
    runner = Runner(Completed(0, "", ""))
    executor = CodexExecutor("/opt/bin/codex", env=BASE_ENV, overrides=PROVIDER,
                             runner=runner)
    assert executor.run(system_prompt="s", prompt="p", schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error="login")
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
        pytest.fail("the grandchild outlived the timeout")


@pytest.mark.parametrize("message", [
    "Failed to update the catalog in the workspace",
    "Could not post to the blog in time",
])
def test_login_wording_needs_a_whole_word_so_catalog_in_or_blog_in_is_not_a_login(message):
    completed = Completed(1, json.dumps({"type": "result", "is_error": True,
                                         "result": message}), "")
    executor, _ = _claude(completed)
    assert executor.run(system_prompt="s", prompt="p", schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error="exit")


def test_an_empty_provider_variable_is_a_login_failure_and_starts_nothing():
    runner = Runner(Completed(0, "", ""))
    executor = CodexExecutor("/opt/bin/codex", env={**BASE_ENV, "FOUNDRY_API_KEY": ""},
                             overrides=PROVIDER, runner=runner)
    assert executor.run(system_prompt="s", prompt="p", schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error="login")
    assert runner.seen == {}


DEEP = "[" * 200_000 + "]" * 200_000     # nested past the JSON parser's recursion limit


def test_output_nested_too_deep_to_parse_is_unparsable_never_an_exception():
    executor, _ = _claude(Completed(0, DEEP, ""))
    assert executor.run(system_prompt="s", prompt="p", schema=SCHEMA,
                        timeout_s=5) == ExecutorResult(error="unparsable")

    def answer(argv):
        Path(argv[argv.index("-o") + 1]).write_text(DEEP)
        return Completed(0, "", "")

    codex, _ = _codex(answer)
    assert codex.run(system_prompt="s", prompt="p", schema=SCHEMA,
                     timeout_s=5) == ExecutorResult(error="unparsable")


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
    assert elapsed < 1 + DREAM_KILL_GRACE_S + 3


def test_a_run_whose_temporary_directory_cannot_be_made_is_a_start_failure(tmp_path,
                                                                           monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "missing"))
    claude, claude_runner = _claude(_ok({"summary": "s"}))
    codex, codex_runner = _codex(lambda argv: Completed(0, "", ""))
    for executor in (claude, codex):
        assert executor.run(system_prompt="s", prompt="p", schema=SCHEMA,
                            timeout_s=5) == ExecutorResult(error="start")
    assert claude_runner.seen == {} and codex_runner.seen == {}
