import contextlib
import io
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass

import pytest
from memriver import cli, hooks
from memriver.hooks import HookResult
from memriver.project_context import project_slug
from memriver.protocol_text import STOP_NUDGE

PROTOCOL_VERSION = "2025-06-18"


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "memriver.cli", *args],
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize("args", [["--version"], ["--root", "X", "--version"]])
def test_version_flag_survives_the_legacy_store_options(args):
    """`--version` reports and exits even alongside the bare-form store options."""
    out = _run_cli(*args)
    assert out.returncode == 0 and "0.1.0" in out.stdout


def test_top_level_help_lists_the_serve_and_hook_commands():
    out = _run_cli("--help")
    assert out.returncode == 0
    assert "serve" in out.stdout and "hook" in out.stdout


def test_serve_help_documents_project_dir_default():
    out = _run_cli("serve", "--help")
    assert out.returncode == 0
    assert "--project-dir" in out.stdout
    assert "current working directory" in " ".join(out.stdout.split())


@dataclass(frozen=True)
class CliRun:
    stdout: str
    stderr: str
    exit_code: int


def invoke_main(argv: list[str], stdin: str) -> CliRun:
    """Run main() in-process over captured streams, the way a harness pipes it."""
    out, err = io.StringIO(), io.StringIO()
    original_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exit_code = cli.main(argv)
    finally:
        sys.stdin = original_stdin
    return CliRun(out.getvalue(), err.getvalue(), exit_code)


def capture_dispatch(argv: list[str], monkeypatch):
    """Parse argv through main() with every handler stubbed; return the args."""
    seen: list = []

    def record(args) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(cli, "_serve", record)
    monkeypatch.setattr(cli, "_hook", record)
    assert cli.main(list(argv)) == 0
    return seen[0]


@pytest.mark.parametrize(
    "argv",
    [
        ["--root", "ROOT", "--project-dir", "PROJECT"],
        ["serve", "--root", "ROOT", "--project-dir", "PROJECT"],
    ],
)
def test_legacy_and_explicit_serve_parse_to_the_same_handler(argv, monkeypatch):
    assert capture_dispatch(argv, monkeypatch).command == "serve"


def test_hook_subcommand_writes_only_hook_result_streams(monkeypatch):
    result = invoke_main(["hook", "stop", "--harness", "codex"],
                         stdin='{"stop_hook_active": false}')
    assert json.loads(result.stdout) == {"decision": "block", "reason": STOP_NUDGE}
    assert result.stderr == ""
    assert result.exit_code == 0


def _send(proc, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _await_response(proc, message_id: int, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for response id={message_id}")
        if not select.select([proc.stdout], [], [], remaining)[0]:
            continue
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout before responding")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # ignore anything that is not a JSON-RPC frame
        if message.get("id") == message_id:
            return message


def _write_over_stdio(root, cwd, extra_args: list[str], content: str,
                      command: str | None = None) -> dict:
    """Run the CLI as a real stdio MCP server and call memory_write once."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "memriver.cli", *([command] if command else []),
         "--root", str(root), *extra_args],
        cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": PROTOCOL_VERSION,
                                "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}}})
        _await_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized",
                     "params": {}})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "memory_write",
                                "arguments": {"content": content, "type": "project",
                                              "scope": "project"}}})
        return _await_response(proc, 2)
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)


def _git_repo(tmp_path, name: str):
    git_repo = tmp_path / name
    (git_repo / ".git").mkdir(parents=True)
    return git_repo


def _entry_files(root, git_repo):
    return sorted((root / "projects" / project_slug(git_repo) / "entries").glob("*.md"))


def test_project_scope_follows_project_dir_not_cwd(tmp_path):
    """--project-dir decides project attribution even when cwd is elsewhere."""
    root = tmp_path / "mem"
    git_repo = _git_repo(tmp_path, "target-repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    response = _write_over_stdio(root, cwd=elsewhere,
                                 extra_args=["--project-dir", str(git_repo)],
                                 content="stored for the target repo")
    assert response["result"]["isError"] is False
    assert len(_entry_files(root, git_repo)) == 1


def test_project_scope_defaults_to_working_directory(tmp_path):
    """Without --project-dir the MCP client's working directory decides the scope."""
    root = tmp_path / "mem"
    git_repo = _git_repo(tmp_path, "cwd-repo")

    response = _write_over_stdio(root, cwd=git_repo, extra_args=[],
                                 content="stored for the cwd repo")
    assert response["result"]["isError"] is False
    assert len(_entry_files(root, git_repo)) == 1


def test_config_file_in_root_is_honoured_end_to_end(tmp_path):
    """<root>/config.toml tunes the running server, not just load_settings()."""
    root = tmp_path / "mem"
    root.mkdir()
    (root / "config.toml").write_text("max_body_chars = 10\n", encoding="utf-8")
    git_repo = _git_repo(tmp_path, "configured-repo")

    response = _write_over_stdio(root, cwd=git_repo, extra_args=[],
                                 content="x" * 11)
    assert response["result"]["isError"] is False  # tools report, never raise
    assert "too large" in json.dumps(response["result"])
    assert not _entry_files(root, git_repo)


def test_bad_env_value_reports_readably(tmp_path):
    """A bad MEMRIVER_* env var fails loudly, but not as a bare traceback."""
    env = {**os.environ, "MEMRIVER_MAX_BODY_CHARS": "abc"}
    out = subprocess.run([sys.executable, "-m", "memriver.cli",
                          "--root", str(tmp_path / "mem")],
                         capture_output=True, text=True, env=env, timeout=30, check=False)
    assert out.returncode != 0
    assert "Traceback" not in out.stderr
    assert "MEMRIVER_" in out.stderr and "max_body_chars" in out.stderr


def test_explicit_serve_starts_the_same_stdio_server(tmp_path):
    """`memriver serve` is an alias, not a second server."""
    root = tmp_path / "mem"
    repo = _git_repo(tmp_path, "explicit-serve")

    response = _write_over_stdio(root, cwd=repo, extra_args=[],
                                 content="served explicitly", command="serve")
    assert response["result"]["isError"] is False
    assert len(_entry_files(root, repo)) == 1


def test_hook_without_project_dir_keeps_the_payload_cwd_fallback_reachable(monkeypatch):
    """No --project-dir means None, not cwd: only then can the harness payload
    decide the project, which is the whole point of hooks._resolve_dir."""
    captured: dict = {}

    def fake_run_hook(event, harness, payload_text, **kwargs):
        captured.update(kwargs)
        return HookResult()

    monkeypatch.setattr(hooks, "run_hook", fake_run_hook)
    result = invoke_main(["hook", "session-start", "--harness", "claude-code"],
                         stdin="{}")
    assert captured["project_dir"] is None
    assert result.exit_code == 0


# a hook fires at every session start and every turn end; importing the MCP
# server stack there would tax the harness for a module it never calls
_LEAK_CHECK = """
leaked = sorted(m for m in sys.modules
                if m.startswith(("fastmcp", "mcp", "memriver.server")))
assert not leaked, leaked
"""


def _python_c(script: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", script], input=stdin,
                          capture_output=True, text=True, check=False)


def test_importing_the_cli_does_not_import_the_server_stack():
    out = _python_c("import sys, memriver.cli\n" + _LEAK_CHECK)
    assert out.returncode == 0, out.stderr


def test_running_a_hook_does_not_import_the_server_stack():
    out = _python_c("import sys\n"
                    "from memriver.cli import main\n"
                    "assert main(['hook', 'stop', '--harness', 'codex']) == 0\n"
                    + _LEAK_CHECK,
                    stdin='{"stop_hook_active": false}')
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["decision"] == "block"
