"""memriver's executors as memriver dream's Executor: each Result as dream's
ExecutorResult, the name shown in the report and the harness recorded on every change,
the failure hints -- plus dream's argv pins, recorded at c005153, now through the
harness executors dream is given."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

from memriver.dream_plugin.adapter import FAILURE_HINTS, DreamExecutor
from memriver.dream_plugin.commands import DreamTable
from memriver.executor import FAILURE_KINDS, Executor, Result, make_executor
from memriver.executor.harness import ClaudeExecutor, CodexExecutor, Completed
from memriver.settings import DEFAULT_DREAM_SCHEDULE_AT, DREAM_SCRATCH_PREFIX
from memriver_dream.protocols import ExecutorResult, FailureKind

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
          "properties": {"summary": {"type": "string"}}}
BASE_ENV = {"HOME": "/home/u", "PATH": "/usr/bin:/bin", "MEMRIVER_ROOT": "/home/u/agent-memory"}


class Runner:
    """Records one call and answers with `completed` (or builds it from the call)."""

    def __init__(self, completed=None, on_call=None) -> None:
        self.completed, self.on_call, self.seen = completed, on_call, {}

    def __call__(self, argv, *, cwd, env, timeout_s, stdin_text):
        self.seen = {"argv": list(argv), "cwd": cwd, "timeout_s": timeout_s,
                     "stdin": stdin_text}
        return self.on_call(argv) if self.on_call is not None else self.completed


class Scripted(Executor):
    name = "codex"

    def __init__(self, result: Result) -> None:
        self.result, self.requests = result, []

    def run(self, *, system_prompt, prompt, schema, timeout_s):
        self.requests.append((system_prompt, prompt, schema, timeout_s))
        return self.result


def _ok(payload: dict) -> Completed:
    return Completed(0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": "", "structured_output": payload}), "")


def _dream(**table) -> DreamExecutor:
    return DreamExecutor(make_executor(DreamTable(**table), env=BASE_ENV,
                                       scratch_prefix=DREAM_SCRATCH_PREFIX))


def test_a_result_becomes_dreams_executor_result_and_the_request_passes_through():
    for result, expected in ((Result(value={"summary": "s"}), ExecutorResult(value={"summary": "s"})),
                             (Result(error="too-large"), ExecutorResult(error="too-large"))):
        inner = Scripted(result)
        assert DreamExecutor(inner).run(system_prompt="S", prompt="P", schema=SCHEMA,
                                        timeout_s=300) == expected
        assert inner.requests == [("S", "P", SCHEMA, 300)]


def test_the_name_in_the_report_and_the_harness_on_every_change():
    claude = _dream(executor="claude", executor_path="/a/claude")
    codex = _dream(executor="codex", executor_path="/a/codex")
    assert (claude.name, claude.harness, codex.name, codex.harness) == (
        "claude", "claude-code", "codex", "codex")


def test_every_failure_kind_of_an_executor_is_one_dream_knows():
    assert set(FAILURE_KINDS) <= set(get_args(FailureKind))


def test_the_failure_hints_keep_their_wording():
    assert FAILURE_HINTS == {
        "login": ("check the executor's login; for API-key, Bedrock or Vertex auth see "
                  "[dream] claude_settings / codex_overrides"),
        "quota": "the executor's usage limit was hit",
    }


def test_the_schedule_default_is_memrivers():
    assert DreamTable.model_fields["schedule_at"].default == DEFAULT_DREAM_SCHEDULE_AT == "04:00"


# dream's argv pins from c005153's memriver.executors tests, unchanged
def test_the_claude_arguments_hold_everything_but_the_prompt_which_goes_on_stdin():
    runner = Runner(_ok({"summary": "s"}))
    executor = DreamExecutor(ClaudeExecutor("/opt/bin/claude", env=BASE_ENV,
                                            scratch_prefix=DREAM_SCRATCH_PREFIX, runner=runner))
    result = executor.run(system_prompt="SYSTEM", prompt="PROMPT", schema=SCHEMA, timeout_s=300)
    assert result == ExecutorResult(value={"summary": "s"})
    assert runner.seen["argv"] == [
        "/opt/bin/claude", "-p", "--system-prompt", "SYSTEM", "--restricted",
        "--strict-mcp-config", "--tools", "", "--no-session-persistence", "--output-format",
        "json", "--json-schema", json.dumps(SCHEMA)]
    assert (runner.seen["stdin"], runner.seen["timeout_s"]) == ("PROMPT", 300)


def test_the_codex_arguments_files_stdin_and_answer():
    def answer(argv):
        files = Path(argv[argv.index("--output-schema") + 1]).parent
        assert json.loads((files / "schema.json").read_text()) == SCHEMA
        assert (files / "instructions.md").read_text() == "SYSTEM"
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"summary": "s"}))
        return Completed(0, "", "")

    runner = Runner(on_call=answer)
    executor = DreamExecutor(CodexExecutor("/opt/bin/codex", env=BASE_ENV,
                                           scratch_prefix=DREAM_SCRATCH_PREFIX, runner=runner))
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
    assert runner.seen["stdin"] == "PROMPT" and files != Path(runner.seen["cwd"])


def test_dream_runs_keep_their_own_scratch_directory_prefix():
    claude_runner = Runner(_ok({"summary": "s"}))
    ClaudeExecutor("/opt/bin/claude", env=BASE_ENV, scratch_prefix=DREAM_SCRATCH_PREFIX,
                   runner=claude_runner).run(system_prompt="s", prompt="p", schema=SCHEMA,
                                             timeout_s=5)

    def answer(argv):
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"summary": "s"}))
        return Completed(0, "", "")

    codex_runner = Runner(on_call=answer)
    CodexExecutor("/opt/bin/codex", env=BASE_ENV, scratch_prefix=DREAM_SCRATCH_PREFIX,
                  runner=codex_runner).run(system_prompt="s", prompt="p", schema=SCHEMA,
                                           timeout_s=5)
    argv = codex_runner.seen["argv"]
    names = [Path(claude_runner.seen["cwd"]).name, Path(codex_runner.seen["cwd"]).name,
             Path(argv[argv.index("-o") + 1]).parent.name]
    assert all(name.startswith("memriver-dream-") for name in names), names


def test_the_table_reaches_the_argv_through_the_factory():
    codex = make_executor(DreamTable(executor="codex", executor_path="/a/codex",
                                     codex_overrides={"model": "deployment-a"}),
                          env={}, scratch_prefix=DREAM_SCRATCH_PREFIX)
    argv = codex.argv(files=Path("/files"))
    start = argv.index("read-only") + 1
    assert argv[start:start + 3] == ["-c", 'model="deployment-a"', "--disable"]
    claude = make_executor(DreamTable(executor="claude", executor_path="/opt/bin/claude",
                                      claude_settings="/etc/memriver/auth.json"),
                           env=BASE_ENV, scratch_prefix=DREAM_SCRATCH_PREFIX)
    assert claude.argv(system_prompt="SYS", schema=SCHEMA)[-2:] == [
        "--settings", "/etc/memriver/auth.json"]
    plain = make_executor(DreamTable(executor="claude", executor_path="/opt/bin/claude"),
                          env=BASE_ENV, scratch_prefix=DREAM_SCRATCH_PREFIX)
    assert "--settings" not in plain.argv(system_prompt="SYS", schema=SCHEMA)
