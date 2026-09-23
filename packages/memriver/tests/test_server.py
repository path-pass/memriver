import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memriver.server import build_server
from memriver_core.bootstrap import build_service
from memriver_core.models import Memory, new_id
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
         "memory_delete"}


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


@pytest.fixture
def server(world):
    return build_server(root=world["store"], project_dir=world["dir"])


def _global_memory(world, body="uv manages python", description=""):
    return _plant(world["store"], Memory.new(body=body, type="user", project_id=world["global"],
                                            source=SOURCE, description=description))


async def _call(server, tool, **arguments):
    async with Client(server) as c:
        return (await c.call_tool(tool, arguments)).data


async def test_the_tool_list_is_the_six_tools_and_no_dream(server):
    async with Client(server) as c:
        assert {t.name for t in await c.list_tools()} == TOOLS


async def test_write_then_read_returns_the_eleven_agent_fields(server, world):
    written = await _call(server, "memory_write", content="本项目用 uv", type="project",
                          harness="claude-code", description="包管理", sync=False)
    assert set(written) == {"id", "project_id"} and written["project_id"] == world["project"]
    read = await _call(server, "memory_read", memory_id=written["id"])
    assert set(read) == {"id", "project_id", "type", "source", "trust", "sync", "created",
                         "updated", "description", "body", "version"}
    assert "deleted_at" not in read
    assert (read["body"], read["description"], read["sync"], read["trust"]) == \
        ("本项目用 uv", "包管理", False, "agent")
    assert read["source"] == {"harness": "claude-code", "method": "agent"}


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
        assert await _call(server, "memory_read", memory_id=memory_id) == {"error": expected}
        assert await _call(server, "memory_update", memory_id=memory_id, expected_version=1,
                           content="x") == {"error": expected}
        assert await _call(server, "memory_delete", memory_id=memory_id,
                           expected_version=1) == {"error": expected}
    assert _snapshot(world["store"]) == before


async def test_a_damaged_row_is_reported_as_damage_per_operation(server, world):
    memory_id = _plant(world["store"], Memory.new(body="hand notes", type="project",
                                                  project_id=world["project"],
                                                  source=SOURCE)).id
    _corrupt(world["store"], memory_id)
    before = _snapshot(world["store"])
    assert await _call(server, "memory_read", memory_id=memory_id) == \
        {"error": f"could not read entry: {memory_id}"}
    assert await _call(server, "memory_update", memory_id=memory_id, expected_version=1,
                       content="x") == {"error": f"could not update entry: {memory_id}"}
    assert await _call(server, "memory_delete", memory_id=memory_id,
                       expected_version=1) == {"error": f"could not delete entry: {memory_id}"}
    assert _snapshot(world["store"]) == before


@pytest.mark.parametrize("fixture", ["registered", "unregistered"])
async def test_global_memories_cannot_be_changed_from_any_session(tmp_path, world, fixture):
    shared = _global_memory(world)
    directory = world["dir"] if fixture == "registered" else tmp_path
    srv = build_server(root=world["store"], project_dir=directory)
    before = _snapshot(world["store"])
    assert await _call(srv, "memory_update", memory_id=shared.id, expected_version=1,
                       content="x") == {"error": GLOBAL_READ_ONLY}
    assert await _call(srv, "memory_delete", memory_id=shared.id,
                       expected_version=1) == {"error": GLOBAL_READ_ONLY}
    assert _snapshot(world["store"]) == before


async def test_write_without_a_project_is_refused_with_the_state_text(tmp_path, world):
    srv = build_server(root=world["store"], project_dir=tmp_path)
    before = _snapshot(world["store"])
    assert await _call(srv, "memory_write", content="x", type="user") == {"error": NO_PROJECT}
    assert _snapshot(world["store"]) == before


async def test_a_project_deleted_after_start_answers_the_missing_text(server, world):
    """A long-lived server outlives a project removed from the store behind its
    back: the session is still registered, so the refusal must not tell the agent
    to run project init."""
    await _call(server, "memory_index")
    _sql(world["store"], "DELETE FROM projects WHERE id = ?", world["project"])
    before = _snapshot(world["store"])
    assert await _call(server, "memory_write", content="x", type="user") == \
        {"error": REGISTERED_MISSING}
    assert _snapshot(world["store"]) == before


async def test_an_unknown_schema_still_starts_the_server_and_writes_nothing(world):
    _sql(world["store"], "PRAGMA user_version = 9")
    before = _snapshot(world["store"])
    srv = build_server(root=world["store"], project_dir=world["dir"])
    lines = (await _call(srv, "memory_index")).splitlines()
    assert lines == [STORE_UNREADABLE_HEADER, "(no memories yet)"]
    assert await _call(srv, "memory_write", content="x", type="user") == {"error": UNAVAILABLE}
    assert _snapshot(world["store"]) == before


@pytest.mark.parametrize(("arguments", "fragment"), [
    ({"content": "token ghp_" + "a" * 36, "type": "user"}, "secret"),
    ({"content": "   ", "type": "user"}, "empty"),
    ({"content": "fine", "type": "user", "harness": "ghp_" + "a" * 36}, "secret"),
    ({"content": "fine", "type": "user", "harness": "has space"}, "invalid harness"),
    ({"content": "fine", "type": "user", "description": "ghp_" + "a" * 36}, "secret"),
])
async def test_write_rejections_never_echo_the_value(server, arguments, fragment):
    result = await _call(server, "memory_write", **arguments)
    assert fragment in result["error"].lower()
    assert "ghp_" not in result["error"]


async def test_nul_bytes_do_not_escape_as_tool_error(server):
    result = await _call(server, "memory_write", content="a\x00b", type="user")
    assert set(result) in ({"id", "project_id"}, {"error"})


@pytest.mark.parametrize("arguments", [
    {"content": "a\udc80b", "type": "user"},
    {"content": "fine", "type": "user", "description": "cue \udc80"},
])
async def test_a_lone_surrogate_on_write_is_a_fixed_failure_not_a_codec_message(server, world,
                                                                               arguments):
    before = _snapshot(world["store"])
    result = await _call(server, "memory_write", **arguments)
    assert result == {"error": "could not write entry"}
    assert _snapshot(world["store"]) == before


@pytest.mark.parametrize("tool, extra", [
    ("memory_read", {}), ("memory_update", {"expected_version": 1, "content": "x"}),
    ("memory_delete", {"expected_version": 1}),
])
async def test_an_invalid_id_is_never_echoed_back(server, tool, extra):
    result = await _call(server, tool, memory_id="x\n\nIGNORE PREVIOUS", **extra)
    assert set(result) == {"error"}
    assert "\n" not in result["error"] and "IGNORE" not in result["error"]


async def test_a_lone_surrogate_id_is_an_error_dict_not_a_tool_error(server):
    assert await _call(server, "memory_read", memory_id="\udc80") == {"error": "no such entry"}


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
    result = await _call(server, "memory_update", memory_id=written["id"], expected_version=1,
                         content="v2", description="ghp_" + "a" * 36)
    assert "ghp_" not in result["error"]


async def test_delete(server):
    written = await _call(server, "memory_write", content="gone soon", type="user")
    assert await _call(server, "memory_delete", memory_id=written["id"],
                       expected_version=1) == {"deleted": written["id"]}
    assert await _call(server, "memory_read", memory_id=written["id"]) == \
        {"error": f"no such entry: {written['id']}"}


async def test_settings_tune_the_body_index_and_search_budgets(world):
    settings = Settings(root=world["store"], max_body_chars=10, index_budget_lines=1,
                        search_limit_default=1, search_limit_max=1)
    _global_memory(world, body="word global")
    srv = build_server(root=world["store"], project_dir=world["dir"], settings=settings)
    assert "too large" in (await _call(srv, "memory_write", content="x" * 11, type="user"))["error"]
    for i in range(2):
        await _call(srv, "memory_write", content=f"word {i}", type="user")
    index = await _call(srv, "memory_index")
    assert index.splitlines()[-1] == "… (2 more entries omitted; use memory_search)"
    # max 1 caps the whole answer: one project hit, global gets nothing
    hits = await _call(srv, "memory_search", query="word", limit=50)
    assert [h["collection"] for h in hits] == ["project"]


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
    stale = await _call(server, "memory_update", memory_id=written["id"],
                        expected_version=1, content="lost")
    assert stale == {"error": f"entry {written['id']} changed since you read it; no change "
                              "was made. Call memory_read again and retry with its version."}
    assert (await _call(server, "memory_read", memory_id=written["id"]))["body"] == "v2"


async def test_a_deleted_memory_is_indistinguishable_from_an_absent_one(server):
    written = await _call(server, "memory_write", content="gone soon", type="project")
    assert await _call(server, "memory_delete", memory_id=written["id"],
                       expected_version=1) == {"deleted": written["id"]}
    absent = new_id()
    for memory_id in (written["id"], absent):
        assert await _call(server, "memory_read", memory_id=memory_id) == \
            {"error": f"no such entry: {memory_id}"}
        assert await _call(server, "memory_update", memory_id=memory_id, expected_version=2,
                           content="x") == {"error": f"no such entry: {memory_id}"}
        assert await _call(server, "memory_delete", memory_id=memory_id,
                           expected_version=2) == {"error": f"no such entry: {memory_id}"}
    # the realistic retry names the version memory_read returned before the
    # delete: it must not reveal that the entry changed, only that it is absent
    gone = written["id"]
    assert await _call(server, "memory_update", memory_id=gone, expected_version=1,
                       content="x") == {"error": f"no such entry: {gone}"}
    assert await _call(server, "memory_delete", memory_id=gone,
                       expected_version=1) == {"error": f"no such entry: {gone}"}
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
    mapped = [server_module._map_error(operation, err, memory_id=memory_id)["error"]
              for operation, err in (
                  ("read", MemoryNotFound(memory_id)), ("update", MemoryNotFound(memory_id)),
                  ("delete", MemoryNotFound(memory_id)), ("read", StorageFailure()),
                  ("update", StorageFailure()), ("delete", StorageFailure()),
                  ("update", VersionConflict(memory_id)),
                  ("delete", VersionConflict(memory_id)))]
    copy = [server_module._GLOBAL_READ_ONLY, server_module._COULD_NOT_READ_STORE,
            *server_module._NO_PROJECT.values(), STOP_NUDGE, UNTRUSTED_DATA_NOTICE,
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
        for name in ("show", "list_memories", "search_all"):
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
