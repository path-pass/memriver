"""The executors `memriver dream` calls: one headless harness run per call (spec §9.2).

Each run gets the prompt on stdin (an argument could not hold it: the OS caps
argument lists well below the token budget), an empty temporary working
directory, the user's environment with MEMRIVER_ROOT pointed at a path that
does not exist (memriver's own hooks inside the run then find no store and do
nothing), and a process group of its own, killed whole on timeout. Only the
kind of a failure comes back, never the output: it may repeat the material
that was sent.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from memriver_core.settings import DreamSettings
from memriver_dream.protocols import ExecutorResult

# ponytail: failure kinds are read from the wording of the harness's own error
# fields (never from output that may repeat the prompt); an unknown wording is
# "exit", which is retried like any other failure. Extend the patterns when a
# harness changes its messages.
_TOO_LARGE = re.compile(r"prompt is too long|input is too long|context[ _-]?length"
                        r"|context window|maximum context|too many tokens", re.IGNORECASE)
# whole words only: "catalog in" or "blog in" is no login failure
_LOGIN = re.compile(r"not logged in|\blog ?in\b|unauthori[sz]ed|authenticat|invalid api key"
                    r"|\b401\b", re.IGNORECASE)
_QUOTA = re.compile(r"usage limit|rate limit|quota|too many requests|\b429\b", re.IGNORECASE)
# switched off for every Codex run, in the user's own CODEX_HOME: hooks (ignoring
# config.toml does not stop them) and every built-in tool but request_user_input
_CODEX_FEATURES_OFF = ("hooks", "shell_tool", "unified_exec", "code_mode_host", "multi_agent",
                       "sleep_tool", "goals", "image_generation", "view_image", "plugins")


@dataclass(frozen=True)
class Completed:
    returncode: int | None      # None: killed on timeout, or never started
    stdout: str
    stderr: str
    started: bool = True


Runner = Callable[..., Completed]


def run_process(argv: list[str], *, cwd: Path, env: Mapping[str, str], timeout_s: int,
                stdin_text: str) -> Completed:
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", start_new_session=True)
    except OSError:
        # argument list too long, executable missing or not executable: a failure of
        # this call, retried next run -- never an exception out of the executor
        return Completed(None, "", "", started=False)
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)   # the harness and anything it started
        except ProcessLookupError:
            pass
        process.communicate()
        return Completed(None, "", "")
    return Completed(process.returncode, stdout, stderr)


def override_args(overrides: Mapping[str, str | bool]) -> list[str]:
    """`-c key=value` for each provider override (already whitelisted by the settings):
    a string as a TOML basic string, a boolean as true/false -- list elements, never
    shell text."""
    args: list[str] = []
    for key, value in overrides.items():
        literal = ("true" if value else "false") if isinstance(value, bool) \
            else json.dumps(value, ensure_ascii=False)
        args += ["-c", f"{key}={literal}"]
    return args


def missing_env(overrides: Mapping[str, str | bool], env: Mapping[str, str]) -> list[str]:
    """The variables the provider overrides name that `env` lacks: without them Codex
    would reach no provider, or the wrong one."""
    return sorted({value for key, value in overrides.items()
                   if key.endswith(".env_key") and isinstance(value, str)
                   and not env.get(value)})


def isolated_env(base: Mapping[str, str], workdir: Path) -> dict[str, str]:
    env = dict(base)
    env["MEMRIVER_ROOT"] = str(workdir / "no-memriver-store")      # never created
    return env


def _unfinished(completed: Completed) -> ExecutorResult | None:
    """The failure of a run that never produced output: not started, or timed out."""
    if not completed.started:
        return ExecutorResult(error="start")
    if completed.returncode is None:
        return ExecutorResult(error="timeout")
    return None


def _failure(errors: list[str]) -> ExecutorResult:
    """The kind the harness's error text names. Quota and login come first, so a
    temporary failure is never taken for input too large for the model."""
    text = "\n".join(errors)
    for pattern, kind in ((_QUOTA, "quota"), (_LOGIN, "login"), (_TOO_LARGE, "too-large")):
        if pattern.search(text):
            return ExecutorResult(error=kind)
    return ExecutorResult(error="exit")


def _codex_errors(completed: Completed) -> list[str]:
    """Codex's error text: the messages of its --json error events and the stderr lines
    that are errors -- never agent or tool output, which may repeat the prompt."""
    errors = [line for line in completed.stderr.splitlines()
              if line.lstrip().lower().startswith("error")]
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("type") not in ("error", "turn.failed"):
            continue
        detail = event.get("error") if event["type"] == "turn.failed" else event
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            errors.append(detail["message"])
    return errors


def _answer(value: object) -> ExecutorResult:
    return ExecutorResult(value=value) if isinstance(value, dict) \
        else ExecutorResult(error="unparsable")


class ClaudeExecutor:
    name = "claude"
    harness = "claude-code"

    def __init__(self, executable: str, *, env: Mapping[str, str],
                 runner: Runner = run_process) -> None:
        self._executable, self._env, self._runner = executable, env, runner

    def argv(self, *, system_prompt: str, schema: dict) -> list[str]:
        # --system-prompt replaces the default system prompt; --tools "" and
        # --strict-mcp-config leave the model no tool and no MCP server;
        # --restricted also ignores the user, project and local settings files,
        # hooks included; the prompt itself arrives on stdin
        return [self._executable, "-p", "--system-prompt", system_prompt, "--restricted",
                "--strict-mcp-config", "--tools", "", "--no-session-persistence",
                "--output-format", "json", "--json-schema", json.dumps(schema)]

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> ExecutorResult:
        with tempfile.TemporaryDirectory(prefix="memriver-dream-") as workdir:
            completed = self._runner(self.argv(system_prompt=system_prompt, schema=schema),
                                     cwd=Path(workdir), env=isolated_env(self._env, Path(workdir)),
                                     timeout_s=timeout_s, stdin_text=prompt)
        unfinished = _unfinished(completed)
        if unfinished is not None:
            return unfinished
        try:
            result = json.loads(completed.stdout)
        except (ValueError, RecursionError):
            result = None
        is_error = isinstance(result, dict) and result.get("is_error") is True
        if completed.returncode != 0 or is_error:
            # an error result's message, else stderr (Claude's diagnostics, never the
            # prompt); never a model answer, which may repeat the prompt
            message = result.get("result") if is_error else None
            return _failure([message] if isinstance(message, str) else [completed.stderr])
        return _answer(result.get("structured_output") if isinstance(result, dict) else None)


class CodexExecutor:
    name = "codex"
    harness = "codex"

    def __init__(self, executable: str, *, env: Mapping[str, str],
                 overrides: Mapping[str, str | bool] | None = None,
                 runner: Runner = run_process) -> None:
        self._executable, self._env, self._runner = executable, env, runner
        self._overrides = dict(overrides or {})

    def argv(self, *, files: Path) -> list[str]:
        # the global AGENTS.md is accepted; project docs are not read
        # (project_doc_max_bytes=0), the user's config.toml -- its MCP servers
        # included -- is ignored, hooks and the built-in tools are switched off,
        # --json puts the error events on stdout, and "-" reads the prompt from stdin
        # the user's provider overrides come first: a later -c of the same key wins, so
        # every fixed switch and value below would win a collision
        switches = [arg for feature in _CODEX_FEATURES_OFF for arg in ("--disable", feature)]
        return [self._executable, "exec", "--json", "--ephemeral", "--ignore-user-config",
                "--skip-git-repo-check", "--sandbox", "read-only",
                *override_args(self._overrides), *switches,
                "-c", 'web_search="disabled"',
                "--output-schema", str(files / "schema.json"),
                "-c", f"model_instructions_file={json.dumps(str(files / 'instructions.md'))}",
                "-c", "project_doc_max_bytes=0", "-o", str(files / "last-message.json"), "-"]

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> ExecutorResult:
        if missing_env(self._overrides, self._env):
            return ExecutorResult(error="login")    # never the default provider instead
        with tempfile.TemporaryDirectory(prefix="memriver-dream-") as workdir, \
                tempfile.TemporaryDirectory(prefix="memriver-dream-files-") as files_dir:
            files = Path(files_dir)
            (files / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (files / "instructions.md").write_text(system_prompt, encoding="utf-8")
            completed = self._runner(self.argv(files=files), cwd=Path(workdir),
                                     env=isolated_env(self._env, Path(workdir)),
                                     timeout_s=timeout_s, stdin_text=prompt)
            unfinished = _unfinished(completed)
            if unfinished is not None:
                return unfinished
            if completed.returncode != 0:
                return _failure(_codex_errors(completed))
            try:
                value = json.loads((files / "last-message.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                return ExecutorResult(error="unparsable")
        return _answer(value)


def make_executor(dream: DreamSettings, *,
                  env: Mapping[str, str]) -> ClaudeExecutor | CodexExecutor:
    if dream.executor == "claude":
        return ClaudeExecutor(dream.executor_path, env=env)
    return CodexExecutor(dream.executor_path, env=env, overrides=dream.codex_overrides)
