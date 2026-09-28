"""One clean headless harness run per classification: `claude -p` or `codex exec`.

Each run gets the prompt on stdin, an empty temporary working directory, the caller's
environment with MEMRIVER_ROOT pointed at a path that does not exist (memriver's own
hooks inside the run find no store and do nothing), the isolation switches memriver
dream's executors use, and a process group of its own, killed whole on timeout. Only
the kind of a failure comes back, never the output: it may repeat the memory text.

The runner, the environment, the failure wording and the argv are a copy of the
umbrella's memriver.executors: memriver dream is required and this package is
optional, so neither may depend on the other. A fix to one is checked against the
other.
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

from .settings import KILL_GRACE_S

# ponytail: failure kinds are read from the wording of the harness's own error fields
# (never from output that may repeat the memory text); an unknown wording is "exit".
# Extend the patterns when a harness changes its messages.
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
    # ponytail: communicate() buffers the whole stdout and stderr in memory; a
    # classification answer is a few lines, so no cap until a harness floods either
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", start_new_session=True)
    except OSError:
        # executable missing or not executable: a failure of this call, never an
        # exception out of the classifier
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
            # a process that left the group still holds the pipes: stop reading
            process.stdout.close()
            process.stderr.close()
            process.wait()
        return Completed(None, "", "")
    return Completed(process.returncode, stdout, stderr)


def override_args(overrides: Mapping[str, str | bool]) -> list[str]:
    """`-c key=value` for each provider override (already whitelisted by the settings)."""
    args: list[str] = []
    for key, value in overrides.items():
        literal = ("true" if value else "false") if isinstance(value, bool) \
            else json.dumps(value, ensure_ascii=False)
        args += ["-c", f"{key}={literal}"]
    return args


def missing_env(overrides: Mapping[str, str | bool], env: Mapping[str, str]) -> list[str]:
    """The variables the provider overrides name that `env` lacks."""
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
    """The kind the harness's error text names; quota before login."""
    text = "\n".join(errors)
    if _QUOTA.search(text):
        return "quota"
    if _LOGIN.search(text):
        return "login"
    return "exit"


def _codex_errors(completed: Completed) -> list[str]:
    """Codex's error text: its --json error events and the stderr lines that are
    errors -- never agent output, which may repeat the memory text."""
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


def _scratch_directory() -> tempfile.TemporaryDirectory:
    # a directory left behind is harmless; a cleanup error must not lose the result
    return tempfile.TemporaryDirectory(prefix="memriver-classifier-", ignore_cleanup_errors=True)


def claude_argv(executable: str, *, system_prompt: str, schema: dict,
                model: str | None = None, settings_path: str | None = None) -> list[str]:
    # --system-prompt replaces the default system prompt; --tools "" and
    # --strict-mcp-config leave the model no tool and no MCP server; --restricted
    # ignores the user, project and local settings files, hooks included, and still
    # honours --settings (the auth file the user names); the prompt arrives on stdin
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
    # the user's provider overrides and model come first: a later -c of the same key
    # wins, so every fixed switch and value below wins a collision
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
               timeout_s: int, env: Mapping[str, str], model: str | None = None,
               settings_path: str | None = None, runner: Runner = run_process) -> dict | str:
    """The answer object, or the kind of failure."""
    argv = claude_argv(executable, system_prompt=system_prompt, schema=schema, model=model,
                       settings_path=settings_path)
    try:
        with _scratch_directory() as workdir:
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
        # an error result's message, else stderr; never a model answer
        message = result.get("result") if is_error else None
        return _failure([message] if isinstance(message, str) else [completed.stderr])
    answer = result.get("structured_output") if isinstance(result, dict) else None
    return answer if isinstance(answer, dict) else "unparsable"


def run_codex(executable: str, *, system_prompt: str, prompt: str, schema: dict,
              timeout_s: int, env: Mapping[str, str], model: str | None = None,
              overrides: Mapping[str, str | bool] | None = None,
              runner: Runner = run_process) -> dict | str:
    """The answer object, or the kind of failure."""
    overrides = dict(overrides or {})
    if missing_env(overrides, env):
        return "login"                          # never the default provider instead
    try:
        with _scratch_directory() as workdir, _scratch_directory() as files_dir:
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
