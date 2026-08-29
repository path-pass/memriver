import json
import os
import select
import subprocess
import sys
import time

from memriver_core.scope import project_slug

PROTOCOL_VERSION = "2025-06-18"


def test_version_flag():
    out = subprocess.run([sys.executable, "-m", "memriver.cli", "--version"],
                         capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "0.1.0" in out.stdout


def test_help_documents_project_dir_default():
    out = subprocess.run([sys.executable, "-m", "memriver.cli", "--help"],
                         capture_output=True, text=True, check=False)
    assert out.returncode == 0
    assert "--project-dir" in out.stdout
    assert "current working directory" in " ".join(out.stdout.split())


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


def _write_over_stdio(root, cwd, extra_args: list[str], content: str) -> dict:
    """Run the CLI as a real stdio MCP server and call memory_write once."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "memriver.cli", "--root", str(root), *extra_args],
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
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _entry_files(root, repo):
    return sorted((root / "projects" / project_slug(repo) / "entries").glob("*.md"))


def test_project_scope_follows_project_dir_not_cwd(tmp_path):
    """--project-dir decides project attribution even when cwd is elsewhere."""
    root = tmp_path / "mem"
    repo = _git_repo(tmp_path, "target-repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    response = _write_over_stdio(root, cwd=elsewhere,
                                 extra_args=["--project-dir", str(repo)],
                                 content="stored for the target repo")
    assert response["result"]["isError"] is False
    assert len(_entry_files(root, repo)) == 1


def test_project_scope_defaults_to_working_directory(tmp_path):
    """Without --project-dir the MCP client's working directory decides the scope."""
    root = tmp_path / "mem"
    repo = _git_repo(tmp_path, "cwd-repo")

    response = _write_over_stdio(root, cwd=repo, extra_args=[],
                                 content="stored for the cwd repo")
    assert response["result"]["isError"] is False
    assert len(_entry_files(root, repo)) == 1


def test_config_file_in_root_is_honoured_end_to_end(tmp_path):
    """<root>/config.toml tunes the running server, not just load_settings()."""
    root = tmp_path / "mem"
    root.mkdir()
    (root / "config.toml").write_text("max_body_chars = 10\n", encoding="utf-8")
    repo = _git_repo(tmp_path, "configured-repo")

    response = _write_over_stdio(root, cwd=repo, extra_args=[],
                                 content="x" * 11)
    assert response["result"]["isError"] is False  # tools report, never raise
    assert "too large" in json.dumps(response["result"])
    assert not _entry_files(root, repo)


def test_bad_env_value_reports_readably(tmp_path):
    """A bad MEMRIVER_* env var fails loudly, but not as a bare traceback."""
    env = {**os.environ, "MEMRIVER_MAX_BODY_CHARS": "abc"}
    out = subprocess.run([sys.executable, "-m", "memriver.cli",
                          "--root", str(tmp_path / "mem")],
                         capture_output=True, text=True, env=env, timeout=30, check=False)
    assert out.returncode != 0
    assert "Traceback" not in out.stderr
    assert "MEMRIVER_" in out.stderr and "max_body_chars" in out.stderr
