"""The harness executors: one clean headless run of the user's own `claude -p` or
`codex exec` per call, for memriver dream and the content classifier.

Each run gets the prompt on stdin (an argument could not hold it: the OS caps argument
lists well below a model's context), an empty temporary working directory named with
the caller's prefix, the caller's environment with MEMRIVER_ROOT pointed at a path that
does not exist (memriver's own hooks inside the run find no store and do nothing), and
a process group of its own, killed whole on timeout. Only the kind of a failure comes
back, never the output: it may repeat the text that was sent.

The claude run loads no user, project or local settings -- except, when the caller
names one, the single file `settings_path` points `--settings` at. That file's hooks,
MCP servers and environment do run inside the call, so it must hold authentication
only (apiKeyHelper, a Bedrock/Vertex env block): a hook in it would receive the text
being sent.
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

from ..settings import KILL_GRACE_S
from . import Executor, Result

# ponytail: failure kinds are read from the wording of the harness's own error fields
# (never from output that may repeat the text sent); an unknown wording is "exit".
# Extend the patterns when a harness changes its messages.
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
    # ponytail: communicate() buffers this call's whole stdout and stderr in memory (as
    # run_codex reads the whole last-message file), so a harness that floods either one
    # can grow this process without bound; upgrade path, if a harness is ever seen to
    # do that: a chunked read with a fixed cap per stream.
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", start_new_session=True)
    except OSError:
        # argument list too long, executable missing or not executable: a failure of
        # this call, never an exception out of the caller
        return Completed(None, "", "", started=False)
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)   # the harness and anything it started
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            # a process that left the group (its own setsid) still holds the pipes:
            # stop reading rather than wait for it
            process.stdout.close()
            process.stderr.close()
            process.wait()
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


def _unfinished(completed: Completed) -> str | None:
    """The failure of a run that never produced output: not started, or timed out."""
    if not completed.started:
        return "start"
    if completed.returncode is None:
        return "timeout"
    return None


def _failure(errors: list[str]) -> str:
    """The kind the harness's error text names. Quota and login come first, so a
    temporary failure is never taken for input too large for the model."""
    text = "\n".join(errors)
    for pattern, kind in ((_QUOTA, "quota"), (_LOGIN, "login"), (_TOO_LARGE, "too-large")):
        if pattern.search(text):
            return kind
    return "exit"


def _codex_errors(completed: Completed) -> list[str]:
    """Codex's error text: the messages of its --json error events and the stderr lines
    that are errors -- never agent or tool output, which may repeat the text sent."""
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


def _scratch_directory(prefix: str) -> tempfile.TemporaryDirectory:
    # a directory left behind is harmless; a cleanup error must not lose the result
    return tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)


def claude_argv(executable: str, *, system_prompt: str, schema: dict,
                model: str | None = None, settings_path: str | None = None) -> list[str]:
    # --system-prompt replaces the default system prompt; --tools "" and
    # --strict-mcp-config leave the model no tool and no MCP server; --restricted
    # ignores the user, project and local settings files, hooks included, but still
    # honours --settings: the one file the caller names there is loaded whole, its
    # hooks, MCP servers and environment included, so it must hold authentication
    # only (apiKeyHelper, a Bedrock/Vertex env block) -- never a hook that would see
    # the text sent; the prompt itself arrives on stdin. The optional flags follow
    # every fixed one.
    argv = [executable, "-p", "--system-prompt", system_prompt, "--restricted",
            "--strict-mcp-config", "--tools", "", "--no-session-persistence",
            "--output-format", "json", "--json-schema", json.dumps(schema)]
    if model is not None:
        argv += ["--model", model]
    if settings_path is not None:
        argv += ["--settings", settings_path]
    return argv


def codex_argv(executable: str, *, files: Path, model: str | None = None,
               overrides: Mapping[str, str | bool] | None = None) -> list[str]:
    # the global AGENTS.md is accepted; project docs are not read
    # (project_doc_max_bytes=0), the user's config.toml -- its MCP servers included --
    # is ignored, hooks and the built-in tools are switched off, --json puts the error
    # events on stdout, and "-" reads the prompt from stdin. The user's provider
    # overrides and model come first: a later -c of the same key wins, so every fixed
    # switch and value below wins a collision (and the model wins over an override's).
    chosen = [] if model is None else ["-c", f"model={json.dumps(model, ensure_ascii=False)}"]
    switches = [arg for feature in _CODEX_FEATURES_OFF for arg in ("--disable", feature)]
    return [executable, "exec", "--json", "--ephemeral", "--ignore-user-config",
            "--skip-git-repo-check", "--sandbox", "read-only",
            *override_args(overrides or {}), *chosen, *switches,
            "-c", 'web_search="disabled"',
            "--output-schema", str(files / "schema.json"),
            "-c", f"model_instructions_file={json.dumps(str(files / 'instructions.md'))}",
            "-c", "project_doc_max_bytes=0", "-o", str(files / "last-message.json"), "-"]


def run_claude(executable: str, *, system_prompt: str, prompt: str, schema: dict,
               timeout_s: int, env: Mapping[str, str], scratch_prefix: str,
               model: str | None = None, settings_path: str | None = None,
               runner: Runner = run_process) -> dict | str:
    """The answer object, or one of FAILURE_KINDS. The run's temporary directory is
    named with `scratch_prefix`."""
    argv = claude_argv(executable, system_prompt=system_prompt, schema=schema, model=model,
                       settings_path=settings_path)
    try:
        with _scratch_directory(scratch_prefix) as workdir:
            completed = runner(argv, cwd=Path(workdir), env=isolated_env(env, Path(workdir)),
                               timeout_s=timeout_s, stdin_text=prompt)
    except OSError:
        return "start"                          # no directory to run in
    unfinished = _unfinished(completed)
    if unfinished is not None:
        return unfinished
    try:
        result = json.loads(completed.stdout)
    except (ValueError, RecursionError):
        result = None
    is_error = isinstance(result, dict) and result.get("is_error") is True
    if completed.returncode != 0 or is_error:
        # an error result's message, else stderr (the harness's diagnostics, never the
        # prompt); never a model answer, which may repeat the prompt
        message = result.get("result") if is_error else None
        return _failure([message] if isinstance(message, str) else [completed.stderr])
    answer = result.get("structured_output") if isinstance(result, dict) else None
    return answer if isinstance(answer, dict) else "unparsable"


def run_codex(executable: str, *, system_prompt: str, prompt: str, schema: dict,
              timeout_s: int, env: Mapping[str, str], scratch_prefix: str,
              model: str | None = None,
              overrides: Mapping[str, str | bool] | None = None,
              runner: Runner = run_process) -> dict | str:
    """The answer object, or one of FAILURE_KINDS. The run's two temporary directories
    (the working directory and the schema, instructions and answer files) are named
    with `scratch_prefix`."""
    overrides = dict(overrides or {})
    if missing_env(overrides, env):
        return "login"                          # never the default provider instead
    try:
        with _scratch_directory(scratch_prefix) as workdir, \
                _scratch_directory(scratch_prefix) as files_dir:
            files = Path(files_dir)
            (files / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            (files / "instructions.md").write_text(system_prompt, encoding="utf-8")
            completed = runner(codex_argv(executable, files=files, model=model,
                                          overrides=overrides),
                               cwd=Path(workdir), env=isolated_env(env, Path(workdir)),
                               timeout_s=timeout_s, stdin_text=prompt)
            unfinished = _unfinished(completed)
            if unfinished is not None:
                return unfinished
            if completed.returncode != 0:
                return _failure(_codex_errors(completed))
            try:
                value = json.loads((files / "last-message.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                return "unparsable"
    except OSError:
        return "start"                          # its directories or files could not be made
    return value if isinstance(value, dict) else "unparsable"


def _result(answer: dict | str) -> Result:
    return Result(error=answer) if isinstance(answer, str) else Result(value=answer)


class ClaudeExecutor(Executor):
    name = "claude"

    def __init__(self, executable: str, *, env: Mapping[str, str], scratch_prefix: str,
                 model: str | None = None, settings_path: str | None = None,
                 runner: Runner = run_process) -> None:
        self._executable, self._env, self._prefix = executable, env, scratch_prefix
        self._model, self._settings_path, self._runner = model, settings_path, runner

    def argv(self, *, system_prompt: str, schema: dict) -> list[str]:
        return claude_argv(self._executable, system_prompt=system_prompt, schema=schema,
                           model=self._model, settings_path=self._settings_path)

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> Result:
        return _result(run_claude(
            self._executable, system_prompt=system_prompt, prompt=prompt, schema=schema,
            timeout_s=timeout_s, env=self._env, scratch_prefix=self._prefix,
            model=self._model, settings_path=self._settings_path, runner=self._runner))


class CodexExecutor(Executor):
    name = "codex"

    def __init__(self, executable: str, *, env: Mapping[str, str], scratch_prefix: str,
                 model: str | None = None, overrides: Mapping[str, str | bool] | None = None,
                 runner: Runner = run_process) -> None:
        self._executable, self._env, self._prefix = executable, env, scratch_prefix
        self._model, self._runner = model, runner
        self._overrides = dict(overrides or {})     # copied at construction (D8)

    def argv(self, *, files: Path) -> list[str]:
        return codex_argv(self._executable, files=files, model=self._model,
                          overrides=self._overrides)

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> Result:
        return _result(run_codex(
            self._executable, system_prompt=system_prompt, prompt=prompt, schema=schema,
            timeout_s=timeout_s, env=self._env, scratch_prefix=self._prefix,
            model=self._model, overrides=self._overrides, runner=self._runner))
