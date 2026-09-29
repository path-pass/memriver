"""The Executor interface and the two harness executors built on the runner: a Result
for every run, the caller's scratch-directory prefix, the argv methods, and
make_executor building an executor from the executor keys -- over a fake process
runner or a local stand-in, never a real harness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from memriver.executor import FAILURE_KINDS, Executor, Result, make_executor
from memriver.executor.harness import ClaudeExecutor, CodexExecutor, Completed
from memriver.settings import ExecutorSettings

SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
PREFIX = "memriver-test-"
PROVIDER = {"model_provider": "foundry", "model_providers.foundry.env_key": "FOUNDRY_API_KEY"}


class Runner:
    """Answers every call with `completed` -- for codex, after writing `answer` as the
    last message -- and records each call."""

    def __init__(self, completed: Completed, answer: dict | None = None) -> None:
        self.completed, self.answer, self.calls = completed, answer, []

    def __call__(self, argv, *, cwd, env, timeout_s, stdin_text):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "stdin": stdin_text})
        if self.answer is not None:
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(self.answer))
        return self.completed


def _claude_ok(payload: dict) -> Completed:
    return Completed(0, json.dumps({"is_error": False, "structured_output": payload}), "")


def _run(executor: Executor) -> Result:
    return executor.run(system_prompt="S", prompt="P", schema=SCHEMA, timeout_s=5)


def test_the_interface_is_one_abstract_run_and_one_set_of_failure_kinds():
    assert FAILURE_KINDS == ("start", "timeout", "login", "quota", "too-large", "exit",
                             "unparsable")
    with pytest.raises(TypeError):
        Executor()
    assert Result() == Result(value=None, error=None)


def test_a_claude_run_is_a_result_with_the_answer_or_the_kind():
    ok = ClaudeExecutor("/opt/bin/claude", env={}, scratch_prefix=PREFIX,
                        runner=Runner(_claude_ok({"summary": "s"})))
    assert ok.name == "claude" and _run(ok) == Result(value={"summary": "s"})
    failing = ClaudeExecutor("/opt/bin/claude", env={}, scratch_prefix=PREFIX,
                             runner=Runner(Completed(1, "", "Error: Not logged in")))
    assert _run(failing) == Result(error="login")


def test_a_codex_run_is_a_result_with_the_answer_or_the_kind():
    ok = CodexExecutor("/opt/bin/codex", env={}, scratch_prefix=PREFIX,
                       runner=Runner(Completed(0, "", ""), answer={"summary": "s"}))
    assert ok.name == "codex" and _run(ok) == Result(value={"summary": "s"})
    runner = Runner(Completed(0, "", ""))
    missing = CodexExecutor("/opt/bin/codex", env={}, scratch_prefix=PREFIX,
                            overrides=PROVIDER, runner=runner)
    assert _run(missing) == Result(error="login") and runner.calls == []


def test_every_run_names_its_temporary_directories_with_the_callers_prefix():
    claude_runner = Runner(_claude_ok({"summary": "s"}))
    _run(ClaudeExecutor("/opt/bin/claude", env={}, scratch_prefix=PREFIX, runner=claude_runner))
    codex_runner = Runner(Completed(0, "", ""), answer={"summary": "s"})
    _run(CodexExecutor("/opt/bin/codex", env={}, scratch_prefix=PREFIX, runner=codex_runner))
    argv = codex_runner.calls[0]["argv"]
    names = [claude_runner.calls[0]["cwd"].name, codex_runner.calls[0]["cwd"].name,
             Path(argv[argv.index("-o") + 1]).parent.name]
    assert all(name.startswith(PREFIX) for name in names), names


def test_the_argv_methods_build_what_a_run_sends():
    claude_runner = Runner(_claude_ok({"summary": "s"}))
    claude = ClaudeExecutor("/opt/bin/claude", env={}, scratch_prefix=PREFIX, model="haiku",
                            settings_path="/etc/memriver/auth.json", runner=claude_runner)
    _run(claude)
    assert claude_runner.calls[0]["argv"] == claude.argv(system_prompt="S", schema=SCHEMA)
    codex_runner = Runner(Completed(0, "", ""), answer={"summary": "s"})
    codex = CodexExecutor("/opt/bin/codex", env={"FOUNDRY_API_KEY": "set"},
                          scratch_prefix=PREFIX, model="gpt-5-mini", overrides=PROVIDER,
                          runner=codex_runner)
    _run(codex)
    argv = codex_runner.calls[0]["argv"]
    assert argv == codex.argv(files=Path(argv[argv.index("-o") + 1]).parent)


def test_codex_overrides_are_copied_when_the_executor_is_built():
    overrides = {"model": "a"}
    codex = CodexExecutor("/opt/bin/codex", env={}, scratch_prefix=PREFIX, overrides=overrides)
    overrides["model"] = "b"
    assert 'model="a"' in codex.argv(files=Path("/files"))


def test_make_executor_builds_claude_with_its_model_and_settings_file():
    executor = make_executor(ExecutorSettings(
        executor="claude", executor_path="/opt/bin/claude", model="haiku",
        claude_settings="/etc/memriver/auth.json"), env={}, scratch_prefix=PREFIX)
    assert isinstance(executor, ClaudeExecutor)
    assert executor.argv(system_prompt="S", schema=SCHEMA)[-4:] == [
        "--model", "haiku", "--settings", "/etc/memriver/auth.json"]


def test_make_executor_builds_codex_with_its_model_and_overrides():
    executor = make_executor(ExecutorSettings(
        executor="codex", executor_path="/opt/bin/codex", model="gpt-5-mini",
        codex_overrides={"model_provider": "foundry"}), env={}, scratch_prefix=PREFIX)
    assert isinstance(executor, CodexExecutor)
    argv = executor.argv(files=Path("/files"))
    start = argv.index("read-only") + 1
    assert argv[start:start + 5] == ["-c", 'model_provider="foundry"', "-c",
                                     'model="gpt-5-mini"', "--disable"]


def test_make_executor_names_the_runs_directories_with_the_callers_prefix(tmp_path):
    out = tmp_path / "cwd-name"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "sys.stdin.read()\n"
        "pathlib.Path(os.environ['OUT']).write_text(pathlib.Path.cwd().name)\n"
        "print(json.dumps({'is_error': False, 'structured_output': {'summary': 's'}}))\n",
        encoding="utf-8")
    script.chmod(0o755)
    executor = make_executor(ExecutorSettings(executor="claude", executor_path=str(script)),
                             env={"OUT": str(out)}, scratch_prefix=PREFIX)
    assert executor.run(system_prompt="S", prompt="P", schema=SCHEMA,
                        timeout_s=30) == Result(value={"summary": "s"})
    assert out.read_text().startswith(PREFIX)


def test_importing_the_layer_loads_neither_executor_module():
    probe = ("import sys, memriver.executor\n"
             "print(sorted(m for m in sys.modules\n"
             "             if m.startswith(('memriver.executor.', 'pydantic_ai'))))")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == "[]"
