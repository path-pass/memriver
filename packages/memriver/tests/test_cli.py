import contextlib
import io
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from memriver import cli, hooks
from memriver.hooks import HookResult
from memriver.protocol_text import STOP_NUDGE
from memriver_core.bootstrap import build_service
from memriver_core.settings import Settings

PROTOCOL_VERSION = "2025-06-18"


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "memriver.cli", *args],
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize("args", [["--version"], ["--root", "X", "--version"]])
def test_version_flag_survives_the_legacy_store_options(args):
    """`--version` reports and exits even alongside the bare-form store options."""
    out = _run_cli(*args)
    assert out.returncode == 0 and "0.1.0" in out.stdout


def test_top_level_help_lists_the_serve_hook_and_install_commands():
    out = _run_cli("--help")
    assert out.returncode == 0
    assert "serve" in out.stdout and "hook" in out.stdout
    assert "install" in out.stdout and "doctor" in out.stdout


def test_doctor_rejects_a_non_positive_stale_days_without_a_traceback(tmp_path):
    out = _run_cli("doctor", "--root", str(tmp_path), "--stale-days", "0")
    assert out.returncode == 2
    assert "Traceback" not in out.stderr


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
    """Parse argv through main() with every handler stubbed; return the args.

    Each name gets its own recorder (not one shared stub), so a wiring bug --
    e.g. `show` bound to `_view_list` -- makes `args.handler is
    getattr(cli, handler)` fail instead of passing vacuously.
    """
    seen: list = []

    def make_recorder(name: str):
        def record(args) -> int:
            seen.append(args)
            return 0
        record.__name__ = name
        return record

    for name in ("_serve", "_hook", "_install", "_view_list", "_view_show", "_view_search",
                "_view_export", "_view_delete"):
        monkeypatch.setattr(cli, name, make_recorder(name))
    assert cli.main(list(argv)) == 0
    return seen[0]


def test_install_parses_its_selector_and_confirmation_flags(monkeypatch):
    args = capture_dispatch(["install", "--harness", "kiro", "--yes", "--dry-run"],
                            monkeypatch)
    assert (args.command, args.harness, args.yes, args.dry_run) == (
        "install", "kiro", True, True)


def test_install_without_a_selector_leaves_the_all_default(monkeypatch):
    args = capture_dispatch(["install"], monkeypatch)
    assert args.harness is None and args.yes is False and args.dry_run is False


def test_install_rejects_combining_harness_and_all():
    out = _run_cli("install", "--harness", "codex", "--all")
    assert out.returncode == 2
    assert "not allowed with" in out.stderr


@pytest.mark.parametrize(("argv", "handler", "expected"), [
    (["project", "init", "--yes", "--name", "Work"], "_project_init",
     {"directory": None, "yes": True, "root": None, "name": "Work"}),
    (["project", "adopt", "aaaaaaaaaa", "/d"], "_project_adopt",
     {"project_id": "aaaaaaaaaa", "directory": Path("/d"), "yes": False}),
    (["project", "unbind", "aaaaaaaaaa", "/d"], "_project_unbind",
     {"project_id": "aaaaaaaaaa", "directory": Path("/d"), "yes": False}),
    (["project", "explain", "--project-dir", "/d"], "_project_explain",
     {"project_dir": Path("/d"), "root": None}),
])
def test_project_subcommands_parse(argv, handler, expected):
    args = cli._build_parser().parse_args(argv)
    assert args.handler is getattr(cli, handler)
    assert {key: getattr(args, key) for key in expected} == expected


@pytest.mark.parametrize(("argv", "handler", "expected"), [
    (["list"], "_view_list", {"project": None}),
    (["list", "--project", "aaaaaaaaaa"], "_view_list", {"project": "aaaaaaaaaa"}),
    (["show", "mmmmmmmmmm"], "_view_show", {"memory_id": "mmmmmmmmmm", "deleted": False}),
    (["show", "mmmmmmmmmm", "--deleted"], "_view_show",
     {"memory_id": "mmmmmmmmmm", "deleted": True}),
    (["search", "query text"], "_view_search",
     {"query": "query text", "project": None, "limit": None}),
    (["search", "query text", "--project", "aaaaaaaaaa", "--limit", "3"], "_view_search",
     {"query": "query text", "project": "aaaaaaaaaa", "limit": 3}),
    (["export", "/tmp/out"], "_view_export", {"directory": Path("/tmp/out")}),
    (["delete", "mmmmmmmmmm", "--version", "2"], "_view_delete",
     {"memory_id": "mmmmmmmmmm", "version": 2, "hard": False, "yes": False}),
    (["delete", "mmmmmmmmmm", "--version", "2", "--hard", "--yes"], "_view_delete",
     {"memory_id": "mmmmmmmmmm", "version": 2, "hard": True, "yes": True}),
])
def test_view_subcommands_parse(argv, handler, expected, monkeypatch):
    args = capture_dispatch(argv, monkeypatch)
    assert args.handler is getattr(cli, handler)
    assert {key: getattr(args, key) for key in expected} == expected


def test_project_without_a_subcommand_is_a_parser_error():
    out = _run_cli("project")
    assert out.returncode == 2
    assert "Traceback" not in out.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["--root", "ROOT", "--project-dir", "PROJECT"],
        ["serve", "--root", "ROOT", "--project-dir", "PROJECT"],
    ],
)
def test_legacy_and_explicit_serve_parse_to_the_same_handler(argv, monkeypatch):
    assert capture_dispatch(argv, monkeypatch).command == "serve"


def test_hook_subcommand_writes_only_hook_result_streams(tmp_path):
    """The Stop nudge fires inside a registered project, and only what the hook
    composed reaches stdout."""
    root = tmp_path / "mem"
    repo = _git_repo(tmp_path, "hook-repo")
    _register(root, repo)
    result = invoke_main(["hook", "stop", "--harness", "codex", "--root", str(root)],
                         stdin=json.dumps({"stop_hook_active": False, "cwd": str(repo)}))
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
                                "arguments": {"content": content,
                                              "type": "project"}}})
        return _await_response(proc, 2)
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)


def _git_repo(tmp_path, name: str):
    git_repo = tmp_path / name
    (git_repo / ".git").mkdir(parents=True)
    return git_repo


def _register(root, repo) -> str:
    """Create a project and bind the fixture repo, the way `memriver project init` would."""
    service = build_service(Settings(root=root), root=root)
    return service.init_project(repo.name, service.plan_root(str(repo))).id


def _active_memories(root, project_id) -> int:
    """That project's active memories, counted in the database."""
    with closing(sqlite3.connect(root / "memriver.db")) as conn:
        (count,) = conn.execute("SELECT count(*) FROM memories "
                                "WHERE project_id = ? AND deleted_at IS NULL",
                                (project_id,)).fetchone()
    return count


def test_project_scope_follows_project_dir_not_cwd(tmp_path):
    """--project-dir decides project attribution even when cwd is elsewhere."""
    root = tmp_path / "mem"
    git_repo = _git_repo(tmp_path, "target-repo")
    project_id = _register(root, git_repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    response = _write_over_stdio(root, cwd=elsewhere,
                                 extra_args=["--project-dir", str(git_repo)],
                                 content="stored for the target repo")
    assert response["result"]["isError"] is False
    assert _active_memories(root, project_id) == 1


def test_project_scope_defaults_to_working_directory(tmp_path):
    """Without --project-dir the MCP client's working directory decides the scope."""
    root = tmp_path / "mem"
    git_repo = _git_repo(tmp_path, "cwd-repo")
    project_id = _register(root, git_repo)

    response = _write_over_stdio(root, cwd=git_repo, extra_args=[],
                                 content="stored for the cwd repo")
    assert response["result"]["isError"] is False
    assert _active_memories(root, project_id) == 1


def test_settings_file_in_root_is_honoured_end_to_end(tmp_path):
    """<root>/settings.toml tunes the running server, not just load_settings()."""
    root = tmp_path / "mem"
    root.mkdir()
    (root / "settings.toml").write_text("max_body_chars = 10\n", encoding="utf-8")
    git_repo = _git_repo(tmp_path, "configured-repo")
    project_id = _register(root, git_repo)

    response = _write_over_stdio(root, cwd=git_repo, extra_args=[],
                                 content="x" * 11)
    assert response["result"]["isError"] is False  # tools report, never raise
    assert "too large" in json.dumps(response["result"])
    assert _active_memories(root, project_id) == 0


def test_bad_env_value_reports_readably(tmp_path):
    """A bad MEMRIVER_* env var fails loudly, but not as a bare traceback."""
    env = {**os.environ, "MEMRIVER_MAX_BODY_CHARS": "abc"}
    out = subprocess.run([sys.executable, "-m", "memriver.cli",
                          "--root", str(tmp_path / "mem")],
                         capture_output=True, text=True, env=env, timeout=30, check=False)
    assert out.returncode != 0
    assert "Traceback" not in out.stderr
    assert "MEMRIVER_" in out.stderr and "max_body_chars" in out.stderr


def test_bad_env_value_leaves_doctor_a_path_free_exit_two(tmp_path):
    """The same bad env var that `serve` fails loudly on is, for doctor, a
    store it could not read: exit 2 and the one fixed line, never a traceback
    carrying source paths and the rejected value."""
    env = {**os.environ, "MEMRIVER_MAX_BODY_CHARS": "not-a-number"}
    out = subprocess.run([sys.executable, "-m", "memriver.cli", "doctor",
                          "--root", str(tmp_path / "mem")],
                         capture_output=True, text=True, env=env, timeout=30,
                         check=False)
    assert out.returncode == 2
    assert out.stdout == ""
    assert out.stderr == "memriver doctor: memory store is inaccessible\n"
    assert "not-a-number" not in out.stderr


@pytest.mark.parametrize(("event", "stderr"), [
    ("session-start", "memriver hook: invalid input\n"),
    ("stop", ""),
])
def test_undecodable_stdin_never_fails_the_hook(tmp_path, event, stderr):
    """`run_hook` never raises, but the stdin read happens before it is called:
    bytes that are not UTF-8 are malformed input, not a harness failure."""
    out = subprocess.run([sys.executable, "-m", "memriver.cli", "hook", event,
                          "--harness", "claude-code",
                          "--root", str(tmp_path / "mem")],
                         input=b'\xff\xfe{"source":"startup"}',
                         capture_output=True, timeout=30, check=False)
    assert out.returncode == 0
    assert out.stdout == b""
    assert out.stderr.decode() == stderr


def test_explicit_serve_starts_the_same_stdio_server(tmp_path):
    """`memriver serve` is an alias, not a second server."""
    root = tmp_path / "mem"
    repo = _git_repo(tmp_path, "explicit-serve")
    project_id = _register(root, repo)

    response = _write_over_stdio(root, cwd=repo, extra_args=[],
                                 content="served explicitly", command="serve")
    assert response["result"]["isError"] is False
    assert _active_memories(root, project_id) == 1


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


def test_running_a_hook_does_not_import_the_server_stack(tmp_path):
    root = tmp_path / "mem"
    repo = _git_repo(tmp_path, "leak-check-repo")
    _register(root, repo)
    out = _python_c("import sys\n"
                    "from memriver.cli import main\n"
                    f"assert main(['hook', 'stop', '--harness', 'codex', "
                    f"'--root', {str(root)!r}]) == 0\n"
                    + _LEAK_CHECK,
                    stdin=json.dumps({"stop_hook_active": False, "cwd": str(repo)}))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["decision"] == "block"


def test_install_harness_choices_match_the_installer(monkeypatch):
    """cli.py spells the four names itself to stay import-light; this pins them
    to the installer's own registry so the two can never drift."""
    from memriver.install import HARNESSES

    out = _run_cli("install", "--help")
    assert out.returncode == 0
    assert "{" + ",".join(HARNESSES) + "}" in out.stdout


def test_hook_harness_choices_match_the_literal():
    """The hook subcommand's --harness choices are pinned to hooks.Harness the
    same way install's are pinned to install.HARNESSES, so the two can never
    silently drift apart."""
    from typing import get_args

    from memriver.hooks import Harness

    out = _run_cli("hook", "--help")
    assert out.returncode == 0
    assert "{" + ",".join(get_args(Harness)) + "}" in out.stdout


@contextlib.contextmanager
def isolated_memriver_loggers():
    """Hand `_configure_logging` a clean slate and give it back afterwards.

    Its handler captures whichever `sys.stderr` is current when it is created,
    which in a process that only ever calls `main()` once is the right one --
    but across tests it would be a previous test's captured stream.
    """
    import logging

    named = [logging.getLogger(name) for name in ("memriver", "memriver_core")]
    saved = [(logger.handlers[:], logger.propagate, logger.level)
             for logger in named]
    for logger in named:
        logger.handlers.clear()
    try:
        yield named
    finally:
        for logger, (handlers, propagate, level) in zip(named, saved, strict=True):
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.setLevel(level)


def test_loader_warnings_go_to_stderr_even_behind_a_stdout_root_handler(
        tmp_path, capsys):
    """`logging.basicConfig` is a documented no-op once the root logger has a
    handler. A process that embeds `main()` and has already configured logging
    to stdout would therefore push memriver_core's loader warnings into the
    very stream the MCP and hook protocols own. The two memriver loggers get
    their own stderr handler and stop propagating, so where the root logger
    points stops mattering."""
    import logging

    root_handler = logging.StreamHandler(sys.stdout)
    logging.getLogger().addHandler(root_handler)
    try:
        with isolated_memriver_loggers():
            assert cli.main(["doctor", "--root", str(tmp_path)]) == 0
            logging.getLogger("memriver_core.settings").warning(
                "settings.toml could not be read")
    finally:
        logging.getLogger().removeHandler(root_handler)

    captured = capsys.readouterr()
    assert "settings.toml could not be read" not in captured.out
    assert "settings.toml could not be read" in captured.err


def test_configuring_the_memriver_loggers_twice_does_not_stack_handlers():
    """`main()` is an ordinary callable, and calling it twice in one process
    must not double every warning line."""
    with isolated_memriver_loggers() as named:
        cli._configure_logging()
        cli._configure_logging()

        assert [len(logger.handlers) for logger in named] == [1, 1]


def test_configure_logging_replaces_a_preattached_stdout_handler(tmp_path, capsys):
    """An embedding process may already have attached its own
    `StreamHandler(sys.stdout)` to `memriver_core` before memriver's own
    `main()` runs. `_configure_logging` only added a handler when the logger
    had none, so that pre-existing stdout handler survived -- and with
    propagation off, a loader warning went out over it, straight into the
    JSON-RPC/hook stdout stream. Configuring must replace it, not add
    alongside it."""
    import logging

    with isolated_memriver_loggers() as named:
        for logger in named:
            logger.addHandler(logging.StreamHandler(sys.stdout))

        assert cli.main(["doctor", "--root", str(tmp_path)]) == 0
        logging.getLogger("memriver_core.settings").warning(
            "settings.toml could not be read")

    captured = capsys.readouterr()
    assert "settings.toml could not be read" not in captured.out
    assert "settings.toml could not be read" in captured.err


def test_configure_logging_replaces_a_preattached_null_handler(tmp_path, capsys):
    """A `NullHandler` pre-attached by an embedding process must not survive
    configuration either: with propagation off, it would otherwise be the
    only handler on the logger, and every warning is swallowed instead of
    reaching stderr."""
    import logging

    with isolated_memriver_loggers() as named:
        for logger in named:
            logger.addHandler(logging.NullHandler())

        assert cli.main(["doctor", "--root", str(tmp_path)]) == 0
        logging.getLogger("memriver_core.settings").warning(
            "settings.toml could not be read")

    captured = capsys.readouterr()
    assert "settings.toml could not be read" not in captured.out
    assert "settings.toml could not be read" in captured.err


# --- install: the CLI hands the store step to the installer -----------------

def test_store_step_is_none_once_global_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "store"))
    build_service(Settings(root=tmp_path / "store"), root=tmp_path / "store").ensure_global()
    assert cli._store_step() is None


def test_store_step_for_an_uninitialized_store_creates_global_only_when_applied(
        monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "store"))
    step = cli._store_step()
    assert "memory store (required): create the global project in" in step.summary
    assert not (tmp_path / "store" / "memriver.db").exists()    # building it writes nothing
    line = step.apply()
    global_id = build_service(Settings(root=tmp_path / "store"),
                              root=tmp_path / "store").global_project_id()
    assert line == f"memory store: ready (global project {global_id})"


def test_install_passes_the_step_and_the_real_tty_state(monkeypatch, tmp_path):
    import memriver.install as install_module

    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "store"))
    seen: dict = {}

    def fake_run_install(*args, **kwargs):
        seen.update(kwargs)
        kwargs["stderr"].write("install-error-channel\n")
        return 0

    monkeypatch.setattr(install_module, "run_install", fake_run_install)
    result = invoke_main(["install", "--harness", "codex"], stdin="y\n")
    assert result.exit_code == 0
    assert result.stderr == "install-error-channel\n" and result.stdout == ""
    assert seen["store_step"] is not None
    assert seen["stdin_is_tty"] is False        # invoke_main pipes stdin


def test_install_with_an_unreadable_store_stops_before_touching_any_harness(
        monkeypatch, tmp_path):
    import memriver.install as install_module

    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "memriver.db").write_bytes(b"not a database")
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "store"))
    ran: list = []
    monkeypatch.setattr(install_module, "run_install", lambda *a, **kw: ran.append(1) or 0)
    result = invoke_main(["install", "--harness", "codex", "--yes"], stdin="")
    assert result.exit_code == 1 and ran == []
    assert result.stderr == ("memriver install: the memory store could not be read; "
                             "run memriver doctor\n")
