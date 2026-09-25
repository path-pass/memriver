import asyncio
import contextlib
import copy
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memriver.hooks import HookResult, run_hook
from memriver.protocol_text import (
    INSTRUCTIONS,
    SESSION_INSTRUCTIONS,
    STOP_NUDGE,
    UNTRUSTED_DATA_NOTICE,
)
from memriver.server import build_server
from memriver_core.bootstrap import build_service
from memriver_core.models import Memory, SessionKey, new_id
from memriver_core.settings import Settings

SOURCE = {"harness": "test", "method": "agent"}
GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell the user; "
                    "do not retry through another entry or edit the store directly.")
NO_PROJECT = ("no writable project: this directory is not registered. No memory was saved. "
              "Ask the user to choose a project root and run memriver project init; do not "
              "run it yourself.")
REGISTERED_MISSING = ("no writable project: the registered project could not be found in the "
                      "store. No memory was saved. Ask the user to run memriver project explain.")
UNAVAILABLE = ("no writable project: the memory store could not be read. No memory "
               "was saved. Ask the user to run memriver doctor.")
STORE_UNREADABLE_HEADER = ("project: unavailable — the memory store could not be read; "
                           "ask the user to run memriver doctor")
TOOLS = {"memory_index", "memory_read", "memory_search", "memory_write", "memory_update",
         "memory_delete", "session_search", "session_confirm", "session_register"}
PENDING_HEADER = ("project: awaiting confirmation — this session is not registered; "
                  "ask the user, then call session_confirm")
UNIDENTIFIED_HEADER = ("project: none — this session is not registered with memriver: its "
                       "hooks may not be installed (memriver install) or trusted (Codex asks "
                       "on start), or the harness sent no session id; ask the user to fix "
                       "that, then start a new session")
SESSION_NONE_HEADER = ("project: none — this session was registered with no project, so global "
                       "is read-only; to save, ask the user to run memriver project init where "
                       "this session started, then call session_register")
SESSION_PROJECT_GONE_HEADER = ("project: unavailable — this session's project no longer exists "
                               "or became global, so global is read-only; to save, ask the "
                               "user to start a new session")
PENDING = ("this session is awaiting the user's confirmation; ask the user, then call "
           "session_confirm")
UNIDENTIFIED = ("this session is not registered with memriver: its hooks may not be installed "
                "(memriver install) or trusted (Codex asks on start), or the harness sent no "
                "session id; ask the user to fix that, then start a new session")
SESSION_NO_PROJECT = ("no writable project: this session was registered with no project. No "
                      "memory was saved. Ask the user to run memriver project init where this "
                      "session started, then call session_register; do not run memriver "
                      "project init yourself unless they ask.")
PENDING_NO_CANDIDATE = ("this session is not registered, and the directory it was first observed "
                        "in is not in any registered project; once the user has run memriver "
                        "project init covering this session's start directory, call "
                        "session_register")
NOTHING_TO_REGISTER = ("no registered project covers the directory this session started in; "
                       "nothing was registered")
COULD_NOT_REGISTER = "could not register this session; ask the user to run memriver doctor"
SESSION_PROJECT_GONE = ("no writable project: this session's project no longer exists or became "
                        "global. No memory was saved. Ask the user to start a new session.")
CANDIDATE_CHANGED = ("the proposed project changed since memriver proposed it; ask the user "
                     "to start a new session")
NOT_AVAILABLE = "session tools are not available for this harness registration"

# the `_meta` of real Codex tool calls: a root thread, a sub-agent it spawned,
# the root resumed, and a fork of it
CODEX_META = json.loads((Path(__file__).parent / "fixtures" / "codex_mcp_meta.json").read_text())
CODEX_ID = CODEX_META["root"]["x-codex-turn-metadata"]["session_id"]


def _service(store: Path):
    return build_service(Settings(root=store), root=store)


MEMORY_COLUMNS = ("id, project_id, type, source_harness, source_method, trust, sync, "
                  "description, body, created, updated, version, deleted_at")


def _plant(store: Path, memory: Memory) -> Memory:
    """A row written behind the stores' backs, as an outside writer would.

    A raw connection has foreign keys off (SQLite's default), so this can also
    plant an orphan. The database must already exist (ensure_global made it).
    """
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute(
            f"INSERT INTO memories ({MEMORY_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory.id, memory.project_id, memory.type, memory.source["harness"],
             memory.source["method"], memory.trust, int(memory.sync), memory.description,
             memory.body, memory.created, memory.updated, memory.version, memory.deleted_at))
    return memory


def _sql(store: Path, statement: str, *params) -> list[tuple]:
    """Run one statement behind the stores' backs (foreign keys off) and commit."""
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        return conn.execute(statement, params).fetchall()


def _corrupt(store: Path, memory_id: str) -> None:
    """A row memriver could not have written: the CHECK is bypassed on this connection."""
    # the pragma is per connection, so it cannot go through `_sql`
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE memories SET trust = 'odd' WHERE id = ?", (memory_id,))


def _snapshot(store: Path) -> dict[str, bytes]:
    return {str(p.relative_to(store)): p.read_bytes()
            for p in store.rglob("*") if p.is_file() and not p.name.endswith("-journal")}


@pytest.fixture
def world(tmp_path):
    store, directory, elsewhere = tmp_path / "mem", tmp_path / "demo", tmp_path / "other"
    directory.mkdir()
    elsewhere.mkdir()
    service = _service(store)
    global_id = service.ensure_global()
    project_id = service.init_project("demo", service.plan_root(str(directory))).id
    other_id = service.init_project("other", service.plan_root(str(elsewhere))).id
    return {"store": store, "dir": directory, "global": global_id, "project": project_id,
            "other": other_id}


@pytest.fixture(autouse=True)
def no_claude_code_session(monkeypatch):
    """A test run inside Claude Code inherits its session id."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def server(world):
    return build_server(root=world["store"], project_dir=world["dir"])


def _global_memory(world, body="uv manages python", description=""):
    return _plant(world["store"], Memory.new(body=body, type="user", project_id=world["global"],
                                            source=SOURCE, description=description))


async def _call(server, tool, *, meta=None, **arguments):
    async with Client(server) as c:
        return (await c.call_tool(tool, arguments, meta=meta)).data


async def _error(server, tool, *, meta=None, **arguments) -> str:
    """Call `tool`, assert it fails as an MCP tool error, and return its message."""
    async with Client(server) as c:
        result = await c.call_tool(tool, arguments, raise_on_error=False, meta=meta)
    assert result.is_error is True
    assert len(result.content) == 1
    return result.content[0].text


async def test_failures_are_mcp_tool_errors_successes_are_not(server, world):
    """The MCP-level contract the six tools promise: a failure is `isError`
    with the client message as its only text content, a success carries the
    same data it always did with `isError` false."""
    unknown = new_id()
    shared = _global_memory(world)
    written = await _call(server, "memory_write", content="v1", type="user")
    async with Client(server) as c:
        missing = await c.call_tool("memory_read", {"memory_id": unknown}, raise_on_error=False)
        assert missing.is_error is True
        assert len(missing.content) == 1
        assert missing.content[0].text == f"no such entry: {unknown}"

        refused = await c.call_tool(
            "memory_update", {"memory_id": shared.id, "expected_version": 1, "content": "x"},
            raise_on_error=False)
        assert refused.is_error is True
        assert refused.content[0].text == GLOBAL_READ_ONLY

        ok = await c.call_tool("memory_read", {"memory_id": written["id"]}, raise_on_error=False)
        assert ok.is_error is False
        assert set(ok.data) == {"id", "project_id", "type", "source", "trust", "sync",
                                "created", "updated", "description", "body", "version"}


async def test_the_tool_list_is_the_nine_tools_and_no_dream(server):
    async with Client(server) as c:
        assert {t.name for t in await c.list_tools()} == TOOLS


async def test_write_then_read_returns_the_eleven_agent_fields(server, world):
    written = await _call(server, "memory_write", content="本项目用 uv", type="project",
                          description="包管理", sync=False)
    assert set(written) == {"id", "project_id"} and written["project_id"] == world["project"]
    read = await _call(server, "memory_read", memory_id=written["id"])
    assert set(read) == {"id", "project_id", "type", "source", "trust", "sync", "created",
                         "updated", "description", "body", "version"}
    assert "deleted_at" not in read
    assert (read["body"], read["description"], read["sync"], read["trust"]) == \
        ("本项目用 uv", "包管理", False, "agent")
    assert read["source"] == {"harness": "unknown", "method": "agent"}


async def test_index_shows_the_header_then_project_then_global_entries(server, world):
    shared = _global_memory(world, description="global cue")
    mine = await _call(server, "memory_write", content="mine", type="project",
                       description="project cue")
    lines = (await _call(server, "memory_index")).splitlines()
    assert lines[0].startswith(f"project: demo [{world['project']}] (root ")
    assert lines[1].startswith(f"- [project] {mine['id']}: project cue (")
    assert lines[2].startswith(f"- [user, global] {shared.id}: global cue (")


async def test_an_empty_index_still_has_its_header(server, world):
    lines = (await _call(server, "memory_index")).splitlines()
    assert lines[0].startswith("project: demo [") and lines[1] == "(no memories yet)"


async def test_search_is_two_single_project_searches_project_first(server, world):
    _global_memory(world, body="shared word in global")
    mine = await _call(server, "memory_write", content="shared word here", type="project")
    _plant(world["store"], Memory.new(body="shared word elsewhere", type="user",
                                      project_id=world["other"], source=SOURCE))
    hits = await _call(server, "memory_search", query="shared")
    assert [h["collection"] for h in hits] == ["project", "global"]
    assert hits[0]["id"] == mine["id"]
    assert set(hits[0]) == {"id", "collection", "type", "description", "snippet"}


async def test_search_limit_caps_the_whole_answer_project_first(world):
    for i in range(3):
        _global_memory(world, body=f"word g{i}")
    srv = build_server(root=world["store"], project_dir=world["dir"])
    for i in range(3):
        await _call(srv, "memory_write", content=f"word p{i}", type="project")
    two = await _call(srv, "memory_search", query="word", limit=2)
    assert [h["collection"] for h in two] == ["project", "project"]
    five = await _call(srv, "memory_search", query="word", limit=5)
    assert [h["collection"] for h in five] == ["project"] * 3 + ["global"] * 2


async def test_search_default_limit_is_one_total_budget(world):
    for i in range(4):
        _global_memory(world, body=f"word g{i}")
    srv = build_server(root=world["store"], project_dir=world["dir"])
    for i in range(4):
        await _call(srv, "memory_write", content=f"word p{i}", type="project")
    assert len(await _call(srv, "memory_search", query="word")) == 5     # the default, not 10


async def test_no_tool_offers_a_scope_or_name_argument(server):
    async with Client(server) as c:
        for tool in await c.list_tools():
            assert "scope" not in tool.inputSchema.get("properties", {})
            assert "name" not in tool.inputSchema.get("properties", {})
        with pytest.raises(ToolError):
            await c.call_tool("memory_write", {"content": "x", "type": "user", "name": "n"})


async def test_a_foreign_id_and_an_unknown_id_answer_identically(server, world):
    foreign = _plant(world["store"], Memory.new(body="secret", type="user",
                                                project_id=world["other"], source=SOURCE))
    before = _snapshot(world["store"])
    for memory_id, expected in ((foreign.id, f"no such entry: {foreign.id}"),
                                (unknown := new_id(), f"no such entry: {unknown}"),
                                ("not-an-id", "no such entry")):
        assert await _error(server, "memory_read", memory_id=memory_id) == expected
        assert await _error(server, "memory_update", memory_id=memory_id, expected_version=1,
                            content="x") == expected
        assert await _error(server, "memory_delete", memory_id=memory_id,
                            expected_version=1) == expected
    assert _snapshot(world["store"]) == before


async def test_a_damaged_row_is_reported_as_damage_per_operation(server, world):
    memory_id = _plant(world["store"], Memory.new(body="hand notes", type="project",
                                                  project_id=world["project"],
                                                  source=SOURCE)).id
    _corrupt(world["store"], memory_id)
    before = _snapshot(world["store"])
    assert await _error(server, "memory_read", memory_id=memory_id) == \
        f"could not read entry: {memory_id}"
    assert await _error(server, "memory_update", memory_id=memory_id, expected_version=1,
                        content="x") == f"could not update entry: {memory_id}"
    assert await _error(server, "memory_delete", memory_id=memory_id,
                        expected_version=1) == f"could not delete entry: {memory_id}"
    assert _snapshot(world["store"]) == before


@pytest.mark.parametrize("fixture", ["registered", "unregistered"])
async def test_global_memories_cannot_be_changed_from_any_session(tmp_path, world, fixture):
    shared = _global_memory(world)
    directory = world["dir"] if fixture == "registered" else tmp_path
    srv = build_server(root=world["store"], project_dir=directory)
    before = _snapshot(world["store"])
    assert await _error(srv, "memory_update", memory_id=shared.id, expected_version=1,
                        content="x") == GLOBAL_READ_ONLY
    assert await _error(srv, "memory_delete", memory_id=shared.id,
                        expected_version=1) == GLOBAL_READ_ONLY
    assert _snapshot(world["store"]) == before


async def test_write_without_a_project_is_refused_with_the_state_text(tmp_path, world):
    srv = build_server(root=world["store"], project_dir=tmp_path)
    before = _snapshot(world["store"])
    assert await _error(srv, "memory_write", content="x", type="user") == NO_PROJECT
    assert _snapshot(world["store"]) == before


async def test_a_project_deleted_after_start_answers_the_missing_text(server, world):
    """A long-lived server outlives a project removed from the store behind its
    back: the session is still registered, so the refusal must not tell the agent
    to run project init."""
    await _call(server, "memory_index")
    _sql(world["store"], "DELETE FROM projects WHERE id = ?", world["project"])
    before = _snapshot(world["store"])
    assert await _error(server, "memory_write", content="x", type="user") == REGISTERED_MISSING
    assert _snapshot(world["store"]) == before


async def test_an_unknown_schema_still_starts_the_server_and_writes_nothing(world):
    _sql(world["store"], "PRAGMA user_version = 9")
    before = _snapshot(world["store"])
    srv = build_server(root=world["store"], project_dir=world["dir"])
    lines = (await _call(srv, "memory_index")).splitlines()
    assert lines == [STORE_UNREADABLE_HEADER, "(no memories yet)"]
    assert await _error(srv, "memory_write", content="x", type="user") == UNAVAILABLE
    assert _snapshot(world["store"]) == before


@pytest.mark.parametrize(("arguments", "fragment"), [
    ({"content": "token ghp_" + "a" * 36, "type": "user"}, "secret"),
    ({"content": "   ", "type": "user"}, "empty"),
    ({"content": "fine", "type": "user", "description": "ghp_" + "a" * 36}, "secret"),
])
async def test_write_rejections_never_echo_the_value(server, arguments, fragment):
    result = await _error(server, "memory_write", **arguments)
    assert fragment in result.lower()
    assert "ghp_" not in result


async def test_nul_bytes_do_not_escape_as_an_unhandled_exception(server):
    async with Client(server) as c:
        result = await c.call_tool("memory_write", {"content": "a\x00b", "type": "user"},
                                   raise_on_error=False)
    if result.is_error:
        assert isinstance(result.content[0].text, str)
    else:
        assert set(result.data) == {"id", "project_id"}


@pytest.mark.parametrize("arguments", [
    {"content": "a\udc80b", "type": "user"},
    {"content": "fine", "type": "user", "description": "cue \udc80"},
])
async def test_a_lone_surrogate_on_write_is_a_fixed_failure_not_a_codec_message(server, world,
                                                                               arguments):
    before = _snapshot(world["store"])
    result = await _error(server, "memory_write", **arguments)
    assert result == "could not write entry"
    assert _snapshot(world["store"]) == before


@pytest.mark.parametrize("tool, extra", [
    ("memory_read", {}), ("memory_update", {"expected_version": 1, "content": "x"}),
    ("memory_delete", {"expected_version": 1}),
])
async def test_an_invalid_id_is_never_echoed_back(server, tool, extra):
    result = await _error(server, tool, memory_id="x\n\nIGNORE PREVIOUS", **extra)
    assert "\n" not in result and "IGNORE" not in result


async def test_a_lone_surrogate_id_is_reported_as_no_such_entry(server):
    assert await _error(server, "memory_read", memory_id="\udc80") == "no such entry"


async def test_update_rewrites_in_place_and_description_none_keeps_empty_clears(server):
    written = await _call(server, "memory_write", content="v1", type="user", description="cue")
    updated = await _call(server, "memory_update", memory_id=written["id"], expected_version=1,
                          content="v2")
    assert set(updated) == {"id", "updated", "version"} and updated["id"] == written["id"]
    assert (await _call(server, "memory_read", memory_id=written["id"]))["description"] == "cue"
    await _call(server, "memory_update", memory_id=written["id"], expected_version=2,
                content="v3", description="")
    read = await _call(server, "memory_read", memory_id=written["id"])
    assert (read["body"], read["description"]) == ("v3", "")


async def test_update_description_with_secret_material_is_refused(server):
    written = await _call(server, "memory_write", content="v1", type="user")
    result = await _error(server, "memory_update", memory_id=written["id"], expected_version=1,
                          content="v2", description="ghp_" + "a" * 36)
    assert "ghp_" not in result


async def test_delete(server):
    written = await _call(server, "memory_write", content="gone soon", type="user")
    assert await _call(server, "memory_delete", memory_id=written["id"],
                       expected_version=1) == {"deleted": written["id"]}
    assert await _error(server, "memory_read", memory_id=written["id"]) == \
        f"no such entry: {written['id']}"


async def test_settings_tune_the_body_index_and_search_budgets(world):
    settings = Settings(root=world["store"], max_body_chars=10, index_budget_lines=1,
                        search_limit_default=1, search_limit_max=1)
    _global_memory(world, body="word global")
    srv = build_server(root=world["store"], project_dir=world["dir"], settings=settings)
    assert "too large" in await _error(srv, "memory_write", content="x" * 11, type="user")
    for i in range(2):
        await _call(srv, "memory_write", content=f"word {i}", type="user")
    index = await _call(srv, "memory_index")
    assert index.splitlines()[-1] == "… (2 more entries omitted; use memory_search)"
    # max 1 caps the whole answer: one project hit, global gets nothing
    hits = await _call(srv, "memory_search", query="word", limit=50)
    assert [h["collection"] for h in hits] == ["project"]


async def test_the_index_tool_describes_its_first_line_in_the_agents_terms(server):
    async with Client(server) as c:
        tool = next(t for t in await c.list_tools() if t.name == "memory_index")
    assert " ".join(tool.description.split()) == (
        "The current project on the first line, then a compact index of its memories "
        "followed by global's.")


async def test_search_limit_stays_a_plain_integer_in_the_tool_schema(server):
    async with Client(server) as c:
        tool = next(t for t in await c.list_tools() if t.name == "memory_search")
    limit = tool.inputSchema["properties"]["limit"]
    assert {"type": "integer"} in limit.get("anyOf", [limit])


async def test_explicit_root_wins_over_the_settings_root(tmp_path, world):
    srv = build_server(root=world["store"], project_dir=world["dir"],
                       settings=Settings(root=tmp_path / "elsewhere"))
    written = await _call(srv, "memory_write", content="here", type="user")
    assert _sql(world["store"], "SELECT id FROM memories WHERE id = ?", written["id"]) == \
        [(written["id"],)]
    assert not (tmp_path / "elsewhere").exists()


async def test_update_and_delete_need_the_version_memory_read_returned(server):
    written = await _call(server, "memory_write", content="v1", type="project")
    read = await _call(server, "memory_read", memory_id=written["id"])
    assert read["version"] == 1
    updated = await _call(server, "memory_update", memory_id=written["id"],
                          expected_version=1, content="v2")
    assert updated["version"] == 2
    stale = await _error(server, "memory_update", memory_id=written["id"],
                         expected_version=1, content="lost")
    assert stale == (f"entry {written['id']} changed since you read it; no change "
                     "was made. Call memory_read again and retry with its version.")
    assert (await _call(server, "memory_read", memory_id=written["id"]))["body"] == "v2"


async def test_a_deleted_memory_is_indistinguishable_from_an_absent_one(server):
    written = await _call(server, "memory_write", content="gone soon", type="project")
    assert await _call(server, "memory_delete", memory_id=written["id"],
                       expected_version=1) == {"deleted": written["id"]}
    absent = new_id()
    for memory_id in (written["id"], absent):
        expected = f"no such entry: {memory_id}"
        assert await _error(server, "memory_read", memory_id=memory_id) == expected
        assert await _error(server, "memory_update", memory_id=memory_id, expected_version=2,
                            content="x") == expected
        assert await _error(server, "memory_delete", memory_id=memory_id,
                            expected_version=2) == expected
    # the realistic retry names the version memory_read returned before the
    # delete: it must not reveal that the entry changed, only that it is absent
    gone = written["id"]
    assert await _error(server, "memory_update", memory_id=gone, expected_version=1,
                        content="x") == f"no such entry: {gone}"
    assert await _error(server, "memory_delete", memory_id=gone,
                        expected_version=1) == f"no such entry: {gone}"
    assert await _call(server, "memory_search", query="gone soon") == []


async def test_no_protocol_field_or_fixed_copy_reveals_deletion_state(server):
    from memriver import server as server_module
    from memriver.protocol_text import (
        COMPACT_PREFIX,
        COMPACT_RESCUE_SUFFIX,
        INSTRUCTIONS,
        PROTOCOL_BLOCK,
        SESSION_START_PREFIX,
        STOP_NUDGE,
        UNTRUSTED_DATA_NOTICE,
    )
    from memriver_core import MemoryNotFound, StorageFailure, VersionConflict

    async with Client(server) as c:
        tools = await c.list_tools()
    for tool in tools:
        assert "deleted_at" not in str(tool.inputSchema)
        assert "soft" not in (tool.description or "").lower()
    assert "soft" not in INSTRUCTIONS.lower() and "deleted_at" not in INSTRUCTIONS
    memory_id = new_id()
    mapped = [server_module._map_error(operation, err, memory_id=memory_id)
              for operation, err in (
                  ("read", MemoryNotFound(memory_id)), ("update", MemoryNotFound(memory_id)),
                  ("delete", MemoryNotFound(memory_id)), ("read", StorageFailure()),
                  ("update", StorageFailure()), ("delete", StorageFailure()),
                  ("update", VersionConflict(memory_id)),
                  ("delete", VersionConflict(memory_id)))]
    copy = [server_module._GLOBAL_READ_ONLY, server_module._COULD_NOT_READ_STORE,
            *server_module._NO_PROJECT.values(), *server_module._SESSION_NO_PROJECT.values(),
            STOP_NUDGE, UNTRUSTED_DATA_NOTICE,
            PROTOCOL_BLOCK, SESSION_START_PREFIX, COMPACT_PREFIX, COMPACT_RESCUE_SUFFIX,
            *mapped]
    assert not any("soft" in text.lower() or "deleted" in text.lower() for text in copy)


async def test_stored_user_text_about_deletion_is_returned_verbatim(server):
    text = "our schema has a deleted_at column for soft deletion"
    written = await _call(server, "memory_write", content=text, type="project")
    assert (await _call(server, "memory_read", memory_id=written["id"]))["body"] == text


async def test_no_tool_reaches_the_management_reads_or_hard_delete(world, monkeypatch):
    from memriver import server as server_module

    real_build = server_module.build_service
    seen: list[str] = []

    def spying_build(settings, *, root):
        service = real_build(settings, root=root)
        for name in ("show", "list_memories", "search_all", "delete_global"):
            monkeypatch.setattr(service, name, lambda *a, _n=name, **k: seen.append(_n))
        real_delete = service.delete

        def delete(*args, **kwargs):
            seen.append(f"hard={kwargs.get('hard', False)}")
            return real_delete(*args, **kwargs)

        monkeypatch.setattr(service, "delete", delete)
        return service

    monkeypatch.setattr(server_module, "build_service", spying_build)
    server = build_server(root=world["store"], project_dir=world["dir"])
    written = await _call(server, "memory_write", content="c", type="project")
    for tool, arguments in (("memory_index", {}), ("memory_search", {"query": "c"}),
                            ("memory_read", {"memory_id": written["id"]}),
                            ("memory_update", {"memory_id": written["id"],
                                               "expected_version": 1, "content": "d"}),
                            ("memory_delete", {"memory_id": written["id"],
                                               "expected_version": 2})):
        await _call(server, tool, **arguments)
    assert seen == ["hard=False"]


def _hold_the_write_lock(db_path: Path, hold_seconds: float, ready: threading.Event) -> None:
    """Take SQLite's write lock on another connection and sit on it a while,
    the way a concurrent writer (another process, another session) would."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("BEGIN IMMEDIATE")
    ready.set()
    time.sleep(hold_seconds)
    conn.rollback()
    conn.close()


async def test_memory_write_does_not_block_the_event_loop(world):
    """memory_write's SQLite write can wait up to BUSY_TIMEOUT_MS for another
    writer's lock; a plain `def` tool runs that wait in a worker thread, so a
    10ms heartbeat on the event loop must keep ticking through it. Before the
    fix (an `async def` tool with no await) the whole wait ran inline and
    stalled every other request for as long as it lasted."""
    server = build_server(root=world["store"], project_dir=world["dir"])
    db_path = world["store"] / "memriver.db"
    assert db_path.exists(), "the world fixture must have created the store database"

    hold_seconds = 0.5
    ready = threading.Event()
    holder = threading.Thread(target=_hold_the_write_lock, args=(db_path, hold_seconds, ready))
    holder.start()
    call_elapsed = 0.0
    try:
        if not ready.wait(2):
            pytest.skip("could not deterministically acquire the write lock on this platform")

        stalls: list[float] = []
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                start = time.monotonic()
                await asyncio.sleep(0.01)
                stalls.append(time.monotonic() - start)

        beat = asyncio.ensure_future(heartbeat())
        try:
            async with Client(server) as c:
                call_start = time.monotonic()
                await c.call_tool("memory_write", {"content": "x", "type": "user"})
                call_elapsed = time.monotonic() - call_start
        finally:
            stop.set()
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
    finally:
        holder.join()

    if call_elapsed < 0.25:
        pytest.skip(f"memory_write only took {call_elapsed:.3f}s -- it did not actually wait "
                    "on the lock (client setup likely outlasted the hold), so this run proves "
                    "nothing either way")

    assert stalls, "the heartbeat never got a chance to run"
    # generous margin: the tool wait is ~0.5s; a blocked event loop would show
    # a single ~0.5s gap, a free one never exceeds a couple of scheduler ticks
    assert max(stalls) < 0.2, f"the event loop stalled for {max(stalls):.3f}s"


async def test_an_expected_refusal_logs_at_debug_not_error(world, caplog):
    """FastMCP itself logs every failed tool call at `ToolError.log_level`
    (`logger.log(e.log_level, ...)`, default ERROR), on the "fastmcp.server.server"
    logger (pytest's `catching_logs` attaches `caplog`'s handler straight to
    it, since `fastmcp`'s own `configure_logging` makes it non-propagating).
    A named core error such as `MemoryNotFound` is a routine agent mistake,
    not an operational problem, and must not print an ERROR line for it."""
    server = build_server(root=world["store"], project_dir=world["dir"])
    caplog.set_level(logging.DEBUG, logger="fastmcp.server.server")

    await _error(server, "memory_read", memory_id=new_id())

    fastmcp_records = [r for r in caplog.records if r.name == "fastmcp.server.server"]
    assert len(fastmcp_records) == 1
    assert fastmcp_records[0].levelno == logging.DEBUG
    assert not any(r.name == "memriver" for r in caplog.records)


async def test_an_unexpected_failure_logs_at_error_and_memriver_still_warns(
        world, monkeypatch, caplog):
    """An error outside `_NAMED_ERRORS` (and outside the write path's
    non-Unicode `ValueError` carve-out) is not routine: FastMCP's own ERROR
    line and memriver's WARNING must both still fire, unchanged."""
    from memriver import server as server_module
    from memriver_core import StorageFailure

    real_build_service = server_module.build_service

    def broken_build_service(settings, *, root):
        service = real_build_service(settings, root=root)
        monkeypatch.setattr(
            service, "read", lambda *a, **k: (_ for _ in ()).throw(StorageFailure()))
        return service

    monkeypatch.setattr(server_module, "build_service", broken_build_service)
    server = build_server(root=world["store"], project_dir=world["dir"])
    caplog.set_level(logging.DEBUG, logger="fastmcp.server.server")
    caplog.set_level(logging.DEBUG, logger="memriver")

    await _error(server, "memory_read", memory_id=new_id())

    fastmcp_records = [r for r in caplog.records if r.name == "fastmcp.server.server"]
    memriver_records = [r for r in caplog.records if r.name == "memriver"]
    assert len(fastmcp_records) == 1
    assert fastmcp_records[0].levelno == logging.ERROR
    assert len(memriver_records) == 1
    assert memriver_records[0].levelno == logging.WARNING
    assert "memory_read failed: StorageFailure" in memriver_records[0].message


# --- session-routed mode (spec §7.1) and directory mode (§7.2) ---------------


def _codex_meta(session_id, base="root"):
    meta = copy.deepcopy(CODEX_META[base])
    meta["x-codex-turn-metadata"]["session_id"] = session_id
    return meta


def _start(world, key, directory, source="startup"):
    """Start a session the way the SessionStart hook does, through the service."""
    return _service(world["store"]).start_session(key, source=source,
                                                  entry_dir=str(directory),
                                                  transcript_path=None)


def _registered_header(world, directory) -> str:
    return _service(world["store"]).open_project_context(str(directory)).header


def _as_session(harness, session_id, monkeypatch):
    """The per-call routing input: `meta=` for Codex, the env var for Claude Code."""
    if harness == "codex":
        return _codex_meta(session_id)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    return None


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
async def test_a_session_answers_for_its_project_wherever_the_server_starts(
        world, monkeypatch, harness):
    """Spec acceptance 1: a session registered at A, a server started in B."""
    session_id = CODEX_ID if harness == "codex" else "claude-session-1"
    _start(world, SessionKey(harness, session_id), world["dir"])
    meta = _as_session(harness, session_id, monkeypatch)
    first = build_server(root=world["store"], project_dir=world["dir"].parent / "other",
                         harness=harness)
    index = await _call(first, "memory_index", meta=meta)
    assert index.splitlines()[0] == _registered_header(world, world["dir"])
    written = await _call(first, "memory_write", meta=meta, content="fact", type="project")
    assert written["project_id"] == world["project"]
    read = await _call(first, "memory_read", meta=meta, memory_id=written["id"])
    assert read["source"] == {"harness": harness, "method": "agent"}
    # a new server process for the same session answers the same
    second = build_server(root=world["store"], project_dir=world["dir"].parent / "other",
                          harness=harness)
    assert await _call(second, "memory_index", meta=meta) == \
        await _call(first, "memory_index", meta=meta)
    assert (await _call(second, "memory_read", meta=meta,
                        memory_id=written["id"]))["body"] == "fact"


async def test_codex_root_sub_agent_and_resume_route_to_one_row_and_a_fork_is_its_own(world):
    _start(world, SessionKey("codex", CODEX_ID), world["dir"])
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    header = _registered_header(world, world["dir"])
    for name in ("root", "sub_agent", "resume"):
        index = await _call(server, "memory_index", meta=CODEX_META[name])
        assert index.splitlines()[0] == header, name
    # the fork is a new session memriver has not seen: never the directory's project
    fork = await _call(server, "memory_index", meta=CODEX_META["fork"])
    assert fork.splitlines()[0] == UNIDENTIFIED_HEADER


def _nested_as_json_string():
    meta = copy.deepcopy(CODEX_META["root"])
    meta["x-codex-turn-metadata"] = json.dumps(meta["x-codex-turn-metadata"])
    return meta


def _without_nested():
    meta = copy.deepcopy(CODEX_META["root"])
    del meta["x-codex-turn-metadata"]
    return meta


@pytest.mark.parametrize("meta", [
    None,
    _nested_as_json_string(),
    _without_nested(),
    _codex_meta("has space"),
    _codex_meta(CODEX_ID + chr(0x202E)),
    _codex_meta(""),
    _codex_meta(12345),
], ids=["no-meta", "json-string", "no-nested", "space", "bidi", "empty", "not-a-string"])
async def test_a_codex_call_without_a_valid_session_id_is_unidentified(world, meta):
    # registered in the very directory the server starts in: never a fallback
    _start(world, SessionKey("codex", CODEX_ID), world["dir"])
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    assert (await _call(server, "memory_index", meta=meta)).splitlines()[0] == \
        UNIDENTIFIED_HEADER
    before = _snapshot(world["store"])
    assert await _error(server, "memory_write", meta=meta, content="x", type="user") == \
        UNIDENTIFIED
    assert await _error(server, "session_confirm", meta=meta) == UNIDENTIFIED
    assert await _error(server, "session_register", meta=meta) == UNIDENTIFIED
    assert await _call(server, "session_search", meta=meta) == []
    assert _snapshot(world["store"]) == before


async def test_claude_code_reads_its_session_id_per_call(world, monkeypatch):
    _start(world, SessionKey("claude-code", "s-demo"), world["dir"])
    server = build_server(root=world["store"], project_dir=world["dir"], harness="claude-code")
    assert (await _call(server, "memory_index")).splitlines()[0] == UNIDENTIFIED_HEADER
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-demo")
    assert (await _call(server, "memory_index")).splitlines()[0] == \
        _registered_header(world, world["dir"])


async def test_a_pending_session_reads_global_only_until_the_user_confirms(world):
    candidate = _plant(world["store"], Memory.new(body="candidate fact", type="project",
                                                  project_id=world["project"], source=SOURCE))
    shared = _global_memory(world, body="global fact")
    # a resumed session memriver never saw waits for the user
    assert _start(world, SessionKey("codex", CODEX_ID), world["dir"], source="resume").state \
        == "pending"
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    meta = CODEX_META["root"]
    index = await _call(server, "memory_index", meta=meta)
    assert index.splitlines()[0] == PENDING_HEADER
    assert candidate.id not in index and shared.id in index
    assert await _error(server, "memory_read", meta=meta, memory_id=candidate.id) == \
        f"no such entry: {candidate.id}"
    assert [h["id"] for h in await _call(server, "memory_search", meta=meta,
                                         query="fact")] == [shared.id]
    before = _snapshot(world["store"])
    assert await _error(server, "memory_write", meta=meta, content="x", type="user") == PENDING
    assert await _error(server, "memory_update", meta=meta, memory_id=candidate.id,
                        expected_version=1, content="x") == PENDING
    assert await _error(server, "memory_delete", meta=meta, memory_id=candidate.id,
                        expected_version=1) == PENDING
    assert await _call(server, "session_search", meta=meta) == []
    assert await _error(server, "session_register", meta=meta) == PENDING
    assert _snapshot(world["store"]) == before

    confirmed = await _call(server, "session_confirm", meta=meta)
    assert confirmed == {"header": _registered_header(world, world["dir"])}
    assert (await _call(server, "memory_read", meta=meta,
                        memory_id=candidate.id))["body"] == "candidate fact"
    written = await _call(server, "memory_write", meta=meta, content="now", type="project")
    assert written["project_id"] == world["project"]
    # confirming again is a no-op
    assert await _call(server, "session_confirm", meta=meta) == confirmed


async def test_a_changed_candidate_is_refused_and_the_row_stays_pending(world):
    _start(world, SessionKey("codex", CODEX_ID), world["dir"], source="resume")
    _sql(world["store"], "UPDATE projects SET root = ? WHERE id = ?",
         str(world["dir"].parent / "moved"), world["project"])
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    meta = CODEX_META["root"]
    assert await _error(server, "session_confirm", meta=meta) == CANDIDATE_CHANGED
    assert (await _call(server, "memory_index", meta=meta)).splitlines()[0] == PENDING_HEADER


def _session_with_no_project(world, key):
    unregistered = world["dir"].parent / "unregistered"
    unregistered.mkdir()
    _start(world, key, unregistered)


def _session_whose_project_is_gone(world, key):
    _start(world, key, world["dir"])
    _sql(world["store"], "UPDATE sessions SET project_id = 'zzzzzzzzzz'")


def _bad_session_row(world, key):
    _start(world, key, world["dir"])
    _sql(world["store"], "UPDATE sessions SET recent_prompts = '{}'")


def _shorten_busy_wait(monkeypatch):
    """memriver's busy wait, shortened; the stores read it when they are built."""
    from memriver_core import bootstrap

    monkeypatch.setattr(bootstrap, "BUSY_TIMEOUT_MS", 50)


@contextlib.contextmanager
def _store_locked(world):
    """Another process holding the write lock past memriver's (shortened) busy wait."""
    with closing(sqlite3.connect(world["store"] / "memriver.db",
                                 isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        try:
            yield
        finally:
            holder.execute("ROLLBACK")


@pytest.mark.parametrize(("case", "header", "refusal"), [
    ("no-project", SESSION_NONE_HEADER, SESSION_NO_PROJECT),
    ("project-gone", SESSION_PROJECT_GONE_HEADER, SESSION_PROJECT_GONE),
    ("bad-row", STORE_UNREADABLE_HEADER, UNAVAILABLE),
    ("lock-timeout", STORE_UNREADABLE_HEADER, UNAVAILABLE),
], ids=["no-project", "project-gone", "bad-row", "lock-timeout"])
async def test_a_session_without_a_usable_project_never_falls_back_to_the_server_directory(
        world, monkeypatch, case, header, refusal):
    """The server starts in a registered directory; the session's own state answers."""
    key = SessionKey("codex", CODEX_ID)
    {"no-project": _session_with_no_project,
     "project-gone": _session_whose_project_is_gone,
     "bad-row": _bad_session_row,
     "lock-timeout": lambda world, key: _start(world, key, world["dir"])}[case](world, key)
    if case == "lock-timeout":
        _shorten_busy_wait(monkeypatch)
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    meta = CODEX_META["root"]
    before = _snapshot(world["store"])
    with _store_locked(world) if case == "lock-timeout" \
            else contextlib.nullcontext():
        index = await _call(server, "memory_index", meta=meta)
        written = await _error(server, "memory_write", meta=meta, content="x", type="project")
    assert index.splitlines()[0] == header
    assert index.splitlines()[0] != _registered_header(world, world["dir"])
    assert written == refusal
    assert _snapshot(world["store"]) == before


@pytest.mark.parametrize("harness", [None, "cursor", "kiro"])
async def test_directory_mode_answers_for_its_start_directory(world, monkeypatch, harness):
    # a session registered elsewhere cannot steer a directory-mode server
    _start(world, SessionKey("claude-code", "elsewhere"), world["dir"].parent / "other")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "elsewhere")
    server = build_server(root=world["store"], project_dir=world["dir"], harness=harness)
    async with Client(server) as c:
        assert c.initialize_result.instructions == INSTRUCTIONS
    index = await _call(server, "memory_index", meta=_codex_meta("elsewhere"))
    assert index.splitlines()[0] == _registered_header(world, world["dir"])
    written = await _call(server, "memory_write", content="fact", type="project")
    assert written["project_id"] == world["project"]
    read = await _call(server, "memory_read", memory_id=written["id"])
    assert read["source"] == {"harness": harness or "unknown", "method": "agent"}
    assert await _error(server, "session_search") == NOT_AVAILABLE
    assert await _error(server, "session_confirm") == NOT_AVAILABLE
    assert await _error(server, "session_register") == NOT_AVAILABLE


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
async def test_hooks_and_server_share_the_save_watermark_end_to_end(world, monkeypatch, harness):
    """Real hooks count the prompts, a real MCP write through the session-routed
    server moves the watermark, and the Stop hook reads it: silent right after
    the save, a nudge once 5 more prompts have gone by."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    session_id = CODEX_ID if harness == "codex" else "claude-session-1"

    def hook(event, **payload):
        return run_hook(event, harness, json.dumps({"session_id": session_id} | payload),
                        root=world["store"], project_dir=None, cwd=world["dir"])

    def prompts(count):
        for number in range(count):
            assert hook("user-prompt-submit", cwd=str(world["dir"]),
                        prompt=f"task {number}") == HookResult()

    hook("session-start", cwd=str(world["dir"]), source="startup")
    prompts(5)
    meta = _as_session(harness, session_id, monkeypatch)
    server = build_server(root=world["store"], project_dir=world["dir"].parent / "other",
                          harness=harness)
    written = await _call(server, "memory_write", meta=meta, content="fact", type="project")
    assert written["project_id"] == world["project"]
    assert hook("stop", stop_hook_active=False) == HookResult()
    prompts(5)
    nudge = hook("stop", stop_hook_active=False)
    assert json.loads(nudge.stdout) == {"decision": "block", "reason": STOP_NUDGE}


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
async def test_session_mode_appends_the_session_instructions(world, harness):
    server = build_server(root=world["store"], project_dir=world["dir"], harness=harness)
    async with Client(server) as c:
        assert c.initialize_result.instructions == INSTRUCTIONS + "\n\n" + SESSION_INSTRUCTIONS


async def test_concurrent_calls_each_answer_for_their_own_session(world):
    _start(world, SessionKey("codex", "session-demo"), world["dir"])
    _start(world, SessionKey("codex", "session-other"), world["dir"].parent / "other")
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    expected = {"session-demo": _registered_header(world, world["dir"]),
                "session-other": _registered_header(world, world["dir"].parent / "other")}
    order = ["session-demo", "session-other"] * 8
    async with Client(server) as c:
        results = await asyncio.gather(*(
            c.call_tool("memory_index", {}, meta=_codex_meta(session_id))
            for session_id in order))
    assert [r.data.splitlines()[0] for r in results] == [expected[s] for s in order]


def _observe(world, key, directory, prompt):
    _service(world["store"]).observe_prompt(key, prompt=prompt, entry_dir=str(directory),
                                            transcript_path=None)


# --- Claude Code: the PreToolUse call mapping (spec U15) ---------------------------


def _claude_meta(call_id):
    return {"claudecode/toolUseId": call_id}


def _write_watermark(world, key) -> int:
    return next(s for s in _service(world["store"]).list_sessions()
                if s.key == key).last_write_prompt_count


async def test_a_mapped_claude_code_call_answers_for_the_session_that_made_it(world,
                                                                              monkeypatch):
    """Claude Code keeps one MCP server across /clear and an in-app /resume, so its
    environment names the startup session; the PreToolUse mapping names the current one."""
    other = world["dir"].parent / "other"
    startup, current = SessionKey("claude-code", "s1"), SessionKey("claude-code", "s2")
    _start(world, startup, world["dir"])
    _start(world, current, other, source="clear")
    for number in range(3):
        _observe(world, current, other, f"task {number}")
    _service(world["store"]).record_tool_call(current, "call-1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    server = build_server(root=world["store"], project_dir=world["dir"], harness="claude-code")

    index = await _call(server, "memory_index", meta=_claude_meta("call-1"))
    assert index.splitlines()[0] == _registered_header(world, other)
    written = await _call(server, "memory_write", meta=_claude_meta("call-1"),
                          content="fact", type="project")
    assert written["project_id"] == world["other"]
    assert _write_watermark(world, current) == 3
    assert _write_watermark(world, startup) == 0

    # an unknown call, or none named, falls back to the server's environment
    for meta in (_claude_meta("call-unknown"), None, {"claudecode/toolUseId": 17}):
        fallback = await _call(server, "memory_index", meta=meta)
        assert fallback.splitlines()[0] == _registered_header(world, world["dir"]), meta


async def test_a_codex_server_ignores_a_claude_code_call_id(world):
    _start(world, SessionKey("codex", CODEX_ID), world["dir"])
    _start(world, SessionKey("claude-code", "s2"), world["dir"].parent / "other")
    _service(world["store"]).record_tool_call(SessionKey("claude-code", "s2"), "call-1")
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    meta = _codex_meta(CODEX_ID) | _claude_meta("call-1")
    index = await _call(server, "memory_index", meta=meta)
    assert index.splitlines()[0] == _registered_header(world, world["dir"])


async def test_claude_code_follows_a_clear_through_real_hooks_end_to_end(world, monkeypatch):
    """Real hooks: start S1 in demo, /clear into S2 whose entry is other, S2's
    PreToolUse maps call-X; the one MCP server, still carrying S1 in its
    environment, answers call-X for S2, and S2's Stop is silent after the save
    while S1 -- whose watermark never moved -- is still nudged."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    other = world["dir"].parent / "other"

    def hook(event, session_id, **payload):
        return run_hook(event, "claude-code",
                        json.dumps({"session_id": session_id} | payload),
                        root=world["store"], project_dir=None, cwd=world["dir"])

    hook("session-start", "s1", cwd=str(world["dir"]), source="startup")
    for session_id, directory in (("s1", world["dir"]), ("s2", other)):
        if session_id == "s2":
            hook("session-start", "s2", cwd=str(directory), source="clear")
        for number in range(5):
            assert hook("user-prompt-submit", session_id, cwd=str(directory),
                        prompt=f"task {number}") == HookResult()
    assert hook("pre-tool-use", "s2", tool_use_id="call-X",
                tool_name="mcp__memriver__memory_write", cwd=str(other)) == HookResult()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    server = build_server(root=world["store"], project_dir=world["dir"], harness="claude-code")

    written = await _call(server, "memory_write", meta=_claude_meta("call-X"),
                          content="fact", type="project")
    assert written["project_id"] == world["other"]
    assert hook("stop", "s2", stop_hook_active=False) == HookResult()
    nudge = hook("stop", "s1", stop_hook_active=False)
    assert json.loads(nudge.stdout) == {"decision": "block", "reason": STOP_NUDGE}


async def test_session_search_is_limited_to_the_callers_project(world):
    other = world["dir"].parent / "other"
    me = SessionKey("codex", CODEX_ID)
    sibling = SessionKey("claude-code", "it's-$HOME")
    _start(world, me, world["dir"])
    _observe(world, me, world["dir"], "first: fix the login bug")
    _start(world, sibling, world["dir"])
    _observe(world, sibling, world["dir"], "tidy the login page")
    _observe(world, sibling, world["dir"], "token ghp_" + "a" * 36)
    _start(world, SessionKey("codex", "session-other"), other)
    _observe(world, SessionKey("codex", "session-other"), other, "login elsewhere")
    _start(world, SessionKey("codex", "session-pending"), world["dir"], source="resume")
    server = build_server(root=world["store"], project_dir=other, harness="codex")
    meta = CODEX_META["root"]

    found = await _call(server, "session_search", meta=meta)
    assert {(s["harness"], s["session_id"]) for s in found} == \
        {("codex", CODEX_ID), ("claude-code", "it's-$HOME")}
    item = next(s for s in found if s["harness"] == "claude-code")
    assert set(item) == {"harness", "session_id", "project", "branch", "entry_cwd",
                         "first_recorded", "last_active_at", "last_end_event_at",
                         "first_prompt", "recent_prompts", "resume_command"}
    assert item["project"] == world["project"]
    assert item["entry_cwd"] == str(world["dir"].resolve())
    assert item["last_end_event_at"] is None
    assert item["first_prompt"]["text"] == "tidy the login page"
    assert [p.get("text", p.get("omitted")) for p in item["recent_prompts"]] == \
        ["tidy the login page", "secret"]
    assert item["resume_command"] == "claude --resume 'it'\"'\"'s-$HOME'"
    mine = next(s for s in found if s["harness"] == "codex")
    assert mine["resume_command"] == f"codex resume {CODEX_ID}"

    assert [s["session_id"] for s in await _call(server, "session_search", meta=meta,
                                                 query="fix the login")] == [CODEX_ID]
    assert len(await _call(server, "session_search", meta=meta, limit=1)) == 1


async def test_session_search_marks_prompt_text_as_untrusted_data(world):
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    async with Client(server) as c:
        tool = next(t for t in await c.list_tools() if t.name == "session_search")
    assert UNTRUSTED_DATA_NOTICE in tool.description


async def test_no_tool_takes_a_harness_or_project_argument(world):
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    async with Client(server) as c:
        for tool in await c.list_tools():
            properties = tool.inputSchema.get("properties", {})
            assert not {"harness", "project", "project_id", "ctx"} & set(properties), tool.name


async def test_a_store_failure_in_the_session_tools_is_a_fixed_message(
        world, monkeypatch, caplog):
    from memriver import server as server_module
    from memriver_core import StorageFailure

    real_build_service = server_module.build_service

    def broken_build_service(settings, *, root):
        service = real_build_service(settings, root=root)
        for name in ("search_sessions", "confirm_session", "register_session"):
            monkeypatch.setattr(service, name,
                                lambda *a, **k: (_ for _ in ()).throw(StorageFailure()))
        return service

    monkeypatch.setattr(server_module, "build_service", broken_build_service)
    _start(world, SessionKey("codex", CODEX_ID), world["dir"])
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    caplog.set_level(logging.DEBUG, logger="memriver")
    meta = CODEX_META["root"]
    assert await _error(server, "session_search", meta=meta) == \
        "could not read the memory store"
    assert await _error(server, "session_confirm", meta=meta) == \
        "could not confirm this session; ask the user to run memriver doctor"
    assert await _error(server, "session_register", meta=meta) == COULD_NOT_REGISTER
    assert [r.message for r in caplog.records if r.name == "memriver"] == \
        ["memory_list failed: StorageFailure", "session_confirm failed: StorageFailure",
         "session_register failed: StorageFailure"]


# --- session_register (U14) ------------------------------------------------------


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
async def test_session_register_binds_the_project_inited_where_the_session_started(
        world, monkeypatch, harness):
    session_id = CODEX_ID if harness == "codex" else "claude-session-1"
    later = world["dir"].parent / "later"
    later.mkdir()
    _start(world, SessionKey(harness, session_id), later)
    meta = _as_session(harness, session_id, monkeypatch)
    server = build_server(root=world["store"], project_dir=world["dir"], harness=harness)
    assert await _error(server, "memory_write", meta=meta, content="x", type="project") == \
        SESSION_NO_PROJECT
    assert await _call(server, "session_register", meta=meta) == \
        {"header": SESSION_NONE_HEADER, "note": NOTHING_TO_REGISTER}

    service = _service(world["store"])
    project = service.init_project("later", service.plan_root(str(later)))
    registered = await _call(server, "session_register", meta=meta)
    assert registered == {"header": _registered_header(world, later)}
    assert f"[{project.id}]" in registered["header"]
    written = await _call(server, "memory_write", meta=meta, content="fact", type="project")
    assert written["project_id"] == project.id
    # registering again is a no-op
    assert await _call(server, "session_register", meta=meta) == registered


async def test_a_pending_session_without_a_candidate_is_pointed_at_session_register(world):
    later = world["dir"].parent / "later"
    later.mkdir()
    _start(world, SessionKey("codex", CODEX_ID), later, source="resume")
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    meta = CODEX_META["root"]
    refusal = await _error(server, "memory_write", meta=meta, content="x", type="project")
    assert refusal == PENDING_NO_CANDIDATE
    assert "session_confirm" not in refusal
    service = _service(world["store"])
    project = service.init_project("later", service.plan_root(str(later)))
    assert await _call(server, "session_register", meta=meta) == \
        {"header": _registered_header(world, later)}
    written = await _call(server, "memory_write", meta=meta, content="fact", type="project")
    assert written["project_id"] == project.id


# --- tolerant meta reading across fastmcp/mcp versions (Task 14 / R8) --------------


class _PydanticLikeMeta:
    """Stands in for the model fastmcp 3.4.7's mcp dependency hands over."""

    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


class _MetaThatRaisesOnDump:
    def model_dump(self):
        raise RuntimeError("boom")


@pytest.mark.parametrize("meta, expected", [
    ({"a": 1}, {"a": 1}),
    (_PydanticLikeMeta({"b": 2}), {"b": 2}),
    (None, {}),
    ("not-a-mapping", {}),
    (_MetaThatRaisesOnDump(), {}),
    (_PydanticLikeMeta([1, 2, 3]), {}),
    (_PydanticLikeMeta("not-a-mapping-either"), {}),
    (_PydanticLikeMeta(None), {}),
], ids=["mapping", "pydantic-like", "none", "non-mapping", "model-dump-raises",
        "model-dump-returns-list", "model-dump-returns-string", "model-dump-returns-none"])
def test_meta_as_dict_reads_any_shape_without_raising(meta, expected):
    from memriver.server import _meta_as_dict
    assert _meta_as_dict(meta) == expected


@pytest.fixture
def meta_delivered_as_plain_dict(monkeypatch):
    """fastmcp>=4's mcp dependency hands `ctx.request_context.meta` over as a
    plain dict; the locked fastmcp 3.4.7 still hands a pydantic model. Mutates
    the real RequestContext in place so a test server sees the newer shape."""
    from fastmcp import Context as FastMCPContext
    original = FastMCPContext.request_context.fget

    def as_plain_dict(self):
        request_context = original(self)
        if request_context is not None and hasattr(request_context.meta, "model_dump"):
            request_context.meta = request_context.meta.model_dump()
        return request_context

    monkeypatch.setattr(FastMCPContext, "request_context", property(as_plain_dict))


async def test_a_codex_call_still_routes_when_meta_arrives_as_a_plain_dict(
        world, meta_delivered_as_plain_dict):
    _start(world, SessionKey("codex", CODEX_ID), world["dir"])
    server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    index = await _call(server, "memory_index", meta=CODEX_META["root"])
    assert index.splitlines()[0] == _registered_header(world, world["dir"])


async def test_a_claude_code_call_id_still_routes_when_meta_arrives_as_a_plain_dict(
        world, monkeypatch, meta_delivered_as_plain_dict):
    other = world["dir"].parent / "other"
    startup, current = SessionKey("claude-code", "s1"), SessionKey("claude-code", "s2")
    _start(world, startup, world["dir"])
    _start(world, current, other, source="clear")
    _service(world["store"]).record_tool_call(current, "call-1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    server = build_server(root=world["store"], project_dir=world["dir"], harness="claude-code")
    index = await _call(server, "memory_index", meta=_claude_meta("call-1"))
    assert index.splitlines()[0] == _registered_header(world, other)


async def test_a_meta_value_of_the_wrong_shape_is_read_as_absent(world, monkeypatch):
    """A Mapping meta whose nested value has an unexpected shape must not raise
    a AttributeError into the tool call; it reads as if that value were absent
    (Task 14 fix round 1): Codex's nested turn metadata as a list falls back to
    unidentified, and Claude Code's call id as a number falls back to the
    server's environment session, exactly as an unmapped call id already does."""
    _start(world, SessionKey("codex", CODEX_ID), world["dir"])
    codex_server = build_server(root=world["store"], project_dir=world["dir"], harness="codex")
    nested_not_a_mapping = {"x-codex-turn-metadata": [1, 2, 3]}
    assert (await _call(codex_server, "memory_index", meta=nested_not_a_mapping)) \
        .splitlines()[0] == UNIDENTIFIED_HEADER

    _start(world, SessionKey("claude-code", "s-demo"), world["dir"])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-demo")
    claude_server = build_server(root=world["store"], project_dir=world["dir"],
                                 harness="claude-code")
    call_id_not_a_string = {"claudecode/toolUseId": 17}
    assert (await _call(claude_server, "memory_index", meta=call_id_not_a_string)) \
        .splitlines()[0] == _registered_header(world, world["dir"])


async def test_memory_read_records_the_server_harness_and_the_calling_session(world,
                                                                              monkeypatch):
    _start(world, SessionKey("claude-code", "s-read"), world["dir"])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-read")
    server = build_server(root=world["store"], project_dir=world["dir"], harness="claude-code")
    written = await _call(server, "memory_write", content="fact", type="project")
    await _call(server, "memory_read", memory_id=written["id"])
    assert _sql(world["store"], "SELECT memory_id, harness, session_id FROM memory_reads") == [
        (written["id"], "claude-code", "s-read")]


async def test_a_server_without_a_harness_records_reads_as_unknown(server, world):
    written = await _call(server, "memory_write", content="fact", type="project")
    await _call(server, "memory_read", memory_id=written["id"])
    assert _sql(world["store"], "SELECT harness, session_id FROM memory_reads") == [
        ("unknown", None)]
