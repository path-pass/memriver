"""Every entry point refuses a store below the schema this memriver needs (spec §9;
§10 item 13, umbrella part): hooks do nothing, MCP tools answer with the schema
mismatch, CLI commands exit 1 with that hint -- and none of them touches the file.

A store below the needed schema is a fresh store with PRAGMA user_version set back to
3: the version gate reads user_version alone.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client
from memriver import cli
from memriver.hooks import HookResult, run_hook
from memriver.server import build_server
from memriver_core.bootstrap import build_services
from memriver_core.settings import Settings

MCP_UNSUPPORTED_STORE = ("The memory store is at schema version 3, which this memriver "
                         "does not support; no change was made.")
CLI_UNSUPPORTED_STORE = "the memory store is at schema version 3; this memriver needs schema version 4"
SESSION_ID = "019a1b2c-3d4e-7f00-8a00-000000000001"


def _snapshot(store: Path) -> dict[str, bytes]:
    return {str(p.relative_to(store)): p.read_bytes()
            for p in store.rglob("*") if p.is_file() and not p.name.endswith("-journal")}


@pytest.fixture(autouse=True)
def no_harness_session(monkeypatch):
    """A test run inside Claude Code inherits its session and project directory."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


@pytest.fixture
def v3(tmp_path):
    store, work, other, home = (tmp_path / "mem", tmp_path / "work", tmp_path / "other",
                                tmp_path / "home")
    for directory in (work, other, home):
        directory.mkdir()
    services = build_services(Settings(root=store), root=store, home=home)
    services.project.ensure_global()
    services.project.init_project("demo", services.project.plan_root(str(work)))
    context = services.project.open_project_context(str(work))
    memory = services.memory.record(content="a fact", type="project", sync=True, harness="t",
                                    description="the cue", context=context)
    change_id = services.memory.versions(memory.id)[0].change.change_id
    with closing(sqlite3.connect(store / "memriver.db")) as conn:
        conn.execute("PRAGMA user_version = 3")
    return SimpleNamespace(store=store, work=work, other=other, home=home,
                           memory_id=memory.id, change_id=change_id, before=_snapshot(store))


async def _error(server, tool, *, meta=None, **arguments) -> str:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False, meta=meta)
    assert result.is_error is True
    return result.content[0].text


# spec §9: hooks do nothing -- no stdout, no stderr line, exit 0, nothing written
@pytest.mark.parametrize(("event", "payload"), [
    ("session-start", {"source": "startup"}),
    ("user-prompt-submit", {"prompt": "hello"}),
    ("stop", {"stop_hook_active": False}),
    ("session-end", {}),
    ("pre-tool-use", {"tool_use_id": "toolu_01A09q90qw90lq917835lq9"}),
])
def test_every_hook_does_nothing(v3, event, payload):
    payload = {"session_id": SESSION_ID, "cwd": str(v3.work), **payload}
    result = run_hook(event, "claude-code", json.dumps(payload), root=v3.store,
                      project_dir=None, cwd=v3.work)
    assert result == HookResult()
    assert _snapshot(v3.store) == v3.before


# spec §9: MCP tools answer with the schema mismatch (directory mode resolves at build time)
@pytest.mark.parametrize(("tool", "arguments"), [
    ("memory_index", {}),
    ("memory_search", {"query": "fact"}),
    ("memory_read", {"memory_id": "MEMORY"}),
    ("memory_write", {"content": "x", "type": "user"}),
    ("memory_update", {"memory_id": "MEMORY", "expected_version": 1, "content": "x"}),
    ("memory_delete", {"memory_id": "MEMORY", "expected_version": 1}),
])
async def test_every_memory_tool_answers_with_the_schema_mismatch(v3, tool, arguments):
    arguments = {key: v3.memory_id if value == "MEMORY" else value
                 for key, value in arguments.items()}
    server = build_server(root=v3.store, project_dir=v3.work)
    assert await _error(server, tool, **arguments) == MCP_UNSUPPORTED_STORE
    assert _snapshot(v3.store) == v3.before


@pytest.mark.parametrize(("tool", "arguments"), [
    ("memory_index", {}),
    ("memory_write", {"content": "x", "type": "user"}),
    ("session_search", {}),
    ("session_confirm", {}),
    ("session_register", {}),
])
async def test_session_mode_tools_answer_with_the_schema_mismatch(v3, tool, arguments):
    server = build_server(root=v3.store, project_dir=v3.work, harness="codex")
    meta = {"x-codex-turn-metadata": {"session_id": SESSION_ID}}
    assert await _error(server, tool, meta=meta, **arguments) == MCP_UNSUPPORTED_STORE
    assert _snapshot(v3.store) == v3.before


# spec §9: every other CLI command exits 1 with the hint
@pytest.mark.parametrize(("argv", "prefix"), [
    (["list"], "memriver"),
    (["show", "{memory}"], "memriver"),
    (["search", "fact"], "memriver"),
    (["sessions"], "memriver"),
    (["export", "{export}"], "memriver"),
    (["history", "{memory}"], "memriver"),
    (["restore", "{memory}", "--to", "1", "--yes"], "memriver"),
    (["undo", "{change}", "--yes"], "memriver"),
    (["delete", "{memory}", "--version", "1", "--yes"], "memriver"),
    (["delete", "{memory}", "--hard", "--yes"], "memriver"),
    (["delete", "{memory}", "--hard", "--dry-run"], "memriver"),
    (["delete", "{memory}", "--hard", "--confirm", "0123456789abcdef"], "memriver"),
    (["project", "explain", "--project-dir", "{work}"], "memriver"),
    (["project", "init", "{other}", "--yes"], "memriver"),
    (["doctor"], "memriver doctor"),
    (["doctor", "--json"], "memriver doctor"),
])
def test_every_command_exits_one_with_the_hint(v3, argv, prefix, capsys, monkeypatch):
    monkeypatch.chdir(v3.work)
    export = v3.store.parent / "export"
    args = [arg.format(memory=v3.memory_id, change=v3.change_id, export=export, work=v3.work,
                       other=v3.other) for arg in argv]
    assert cli.main([*args, "--root", str(v3.store)]) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines()[-1] == f"{prefix}: {CLI_UNSUPPORTED_STORE}"
    assert "Traceback" not in captured.err
    assert _snapshot(v3.store) == v3.before
    assert not export.exists()


def test_install_stops_at_the_hint_before_any_harness(v3, monkeypatch, capsys):
    monkeypatch.setenv("MEMRIVER_ROOT", str(v3.store))
    monkeypatch.setenv("HOME", str(v3.home))

    def never(*args, **kwargs):
        raise AssertionError("run_install must not run")

    monkeypatch.setattr("memriver.install.run_install", never)
    assert cli.main(["install", "--yes"]) == 1
    assert capsys.readouterr().err.splitlines()[-1] == f"memriver: {CLI_UNSUPPORTED_STORE}"
    assert list(v3.home.iterdir()) == []
    assert _snapshot(v3.store) == v3.before
