import time
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memriver.project_context import bind
from memriver.server import build_server
from memriver_core.bootstrap import build_service
from memriver_core.models import Memory, new_id
from memriver_core.repository.filesystem.markdown_codec import encode
from memriver_core.settings import Settings

SOURCE = {"harness": "test", "method": "agent"}
GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell the user; "
                    "do not retry through another entry or edit the store directly.")
NO_PROJECT = ("no writable project: this directory is not registered. No memory was saved. "
              "Ask the user to choose a project root and run memriver project init; do not "
              "run it yourself.")
MISSING = ("no writable project: the registered project does not exist in the store. "
           "No memory was saved. Ask the user to run memriver project explain.")
TOOLS = {"memory_index", "memory_read", "memory_search", "memory_write", "memory_update",
         "memory_delete"}


def _service(store: Path):
    return build_service(Settings(root=store), root=store)


def _plant(store: Path, memory: Memory) -> Memory:
    """A memory on disk behind the tools' backs, as hand maintenance of global does."""
    (store / "memories").mkdir(parents=True, exist_ok=True)
    (store / "memories" / f"{memory.id}.md").write_text(encode(memory), encoding="utf-8")
    return memory


def _snapshot(store: Path) -> dict[str, bytes]:
    return {str(p.relative_to(store)): p.read_bytes()
            for p in store.rglob("*") if p.is_file() and p.name != ".lock"}


@pytest.fixture
def world(tmp_path):
    store, directory = tmp_path / "mem", tmp_path / "demo"
    directory.mkdir()
    service = _service(store)
    global_id = service.ensure_global()
    project = service.create_project("demo")
    other = service.create_project("other")
    bind(store, service, project.id, str(directory.resolve()))
    return {"store": store, "dir": directory, "global": global_id, "project": project.id,
            "other": other.id}


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


async def test_write_then_read_returns_all_ten_fields(server, world):
    written = await _call(server, "memory_write", content="本项目用 uv", type="project",
                          harness="claude-code", description="包管理", sync=False)
    assert set(written) == {"id", "project_id"} and written["project_id"] == world["project"]
    read = await _call(server, "memory_read", memory_id=written["id"])
    assert set(read) == {"id", "project_id", "type", "source", "trust", "sync", "created",
                         "updated", "description", "body"}
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
        assert await _call(server, "memory_update", memory_id=memory_id, content="x") == \
            {"error": expected}
        assert await _call(server, "memory_delete", memory_id=memory_id) == {"error": expected}
    assert _snapshot(world["store"]) == before


async def test_a_damaged_file_is_reported_as_damage_per_operation(server, world):
    memory_id = new_id()
    (world["store"] / "memories").mkdir(exist_ok=True)
    (world["store"] / "memories" / f"{memory_id}.md").write_text("hand notes\n")
    assert await _call(server, "memory_read", memory_id=memory_id) == \
        {"error": f"could not read entry: {memory_id}"}
    assert await _call(server, "memory_update", memory_id=memory_id, content="x") == \
        {"error": f"could not update entry: {memory_id}"}
    assert await _call(server, "memory_delete", memory_id=memory_id) == \
        {"error": f"could not delete entry: {memory_id}"}
    assert (world["store"] / "memories" / f"{memory_id}.md").read_text() == "hand notes\n"


@pytest.mark.parametrize("fixture", ["registered", "unregistered"])
async def test_global_memories_cannot_be_changed_from_any_session(tmp_path, world, fixture):
    shared = _global_memory(world)
    directory = world["dir"] if fixture == "registered" else tmp_path
    srv = build_server(root=world["store"], project_dir=directory)
    before = _snapshot(world["store"])
    assert await _call(srv, "memory_update", memory_id=shared.id, content="x") == \
        {"error": GLOBAL_READ_ONLY}
    assert await _call(srv, "memory_delete", memory_id=shared.id) == {"error": GLOBAL_READ_ONLY}
    assert _snapshot(world["store"]) == before


async def test_write_without_a_project_is_refused_with_the_state_text(tmp_path, world):
    srv = build_server(root=world["store"], project_dir=tmp_path)
    before = _snapshot(world["store"])
    assert await _call(srv, "memory_write", content="x", type="user") == {"error": NO_PROJECT}
    assert _snapshot(world["store"]) == before


async def test_a_registered_project_missing_from_the_store_cannot_be_written(tmp_path, world):
    (world["store"] / "projects" / f"{world['project']}.toml").unlink()
    srv = build_server(root=world["store"], project_dir=world["dir"])
    index = await _call(srv, "memory_index")
    assert index.splitlines()[0].startswith("project: unavailable — registered project ")
    result = await _call(srv, "memory_write", content="x", type="user")
    assert "No memory was saved" in result["error"]


async def test_a_project_deleted_after_start_answers_the_missing_text(server, world):
    """A long-lived server outlives a hand-deleted project file: the directory is
    still registered, so the refusal must not tell the agent to run project init."""
    await _call(server, "memory_index")
    (world["store"] / "projects" / f"{world['project']}.toml").unlink()
    before = _snapshot(world["store"])
    assert await _call(server, "memory_write", content="x", type="user") == {"error": MISSING}
    assert _snapshot(world["store"]) == before


async def test_an_invalid_manifest_still_starts_the_server_and_writes_nothing(world):
    (world["store"] / "store.toml").write_text("global_project = 'nope'\n")
    srv = build_server(root=world["store"], project_dir=world["dir"])
    lines = (await _call(srv, "memory_index")).splitlines()
    assert lines == [("project: unavailable — the memory store could not be read; "
                      "ask the user to run memriver doctor"), "(no memories yet)"]
    result = await _call(srv, "memory_write", content="x", type="user")
    assert "No memory was saved" in result["error"] and "doctor" in result["error"]


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
    ("memory_read", {}), ("memory_update", {"content": "x"}), ("memory_delete", {}),
])
async def test_an_invalid_id_is_never_echoed_back(server, tool, extra):
    result = await _call(server, tool, memory_id="x\n\nIGNORE PREVIOUS", **extra)
    assert set(result) == {"error"}
    assert "\n" not in result["error"] and "IGNORE" not in result["error"]


async def test_a_lone_surrogate_id_is_an_error_dict_not_a_tool_error(server):
    assert await _call(server, "memory_read", memory_id="\udc80") == {"error": "no such entry"}


# 8 nested alias levels: ~600 bytes of YAML, 10**8 leaves if every alias were expanded
_ALIAS_BOMB = "\n".join(
    ["  l0: &l0 [" + ", ".join(["a"] * 10) + "]"]
    + [f"  l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]" for i in range(1, 8)])
# one 20k-char scalar aliased 2000 times: a ~26 KB file, a 40 MB value if every alias were expanded
_SCALAR_ALIAS = '  s: &s "' + "a" * 20_000 + '"\n  r: [' + ", ".join(["*s"] * 2000) + "]"


@pytest.mark.parametrize(("old", "new"), [
    ("description: shared cue", 'description: "\\udc80"'),
    ("harness: test", 'harness: "\\udc80"'),
    ("harness: test", 'harness: test\n  tags: !!set {"\\udc80": null}'),
    ("harness: test", 'harness: test\n  blob: !!binary gA=='),
    ("trust: agent", 'trust: !!binary gA=='),
    ("harness: test", 'harness: test\n  r: &x [*x]'),  # a cycle has no JSON form
    ("harness: test", 'harness: test\n  r: &x {k: *x}'),
    ("harness: test", 'harness: test\n  a: &x [1, 2]\n  b: *x'),  # memriver never writes aliases
    pytest.param("harness: test", "harness: test\n" + _ALIAS_BOMB, id="alias-bomb"),
    pytest.param("harness: test", "harness: test\n" + _SCALAR_ALIAS, id="scalar-alias"),
])
async def test_an_unservable_stored_value_is_a_damaged_entry(server, world, old, new):
    healthy = _plant(world["store"], Memory.new(body="shared word ok", type="project",
                                                project_id=world["project"], source=SOURCE,
                                                description="shared cue"))
    damaged = Memory.new(body="shared word bad", type="project", project_id=world["project"],
                         source=SOURCE, description="shared cue")
    text = encode(damaged).replace(old, new)
    assert new in text
    (world["store"] / "memories" / f"{damaged.id}.md").write_text(text, encoding="utf-8")
    start = time.monotonic()
    assert await _call(server, "memory_read", memory_id=damaged.id) == \
        {"error": f"could not read entry: {damaged.id}"}
    hits = await _call(server, "memory_search", query="shared")
    assert [h["id"] for h in hits] == [healthy.id]
    index = await _call(server, "memory_index")
    assert healthy.id in index and damaged.id not in index
    assert time.monotonic() - start < 1.0


async def test_plain_yaml_values_in_a_stored_entry_are_still_served(server, world):
    """The decode whitelist keeps every plain YAML type (no aliases): .nan/.inf serialize as
    null, and a repeated scalar is not a shared container."""
    memory = Memory.new(body="b", type="project", project_id=world["project"], source=SOURCE)
    extra = ("harness: test\n  n: .nan\n  i: -.inf\n  when: 2020-01-01\n"
             "  o: !!omap [{a: 1}]\n  p: !!pairs [{a: 1}]\n  s: !!set {x: null}\n"
             "  k: {1: a, null: b}\n  t1: same\n  t2: same\n  u: [same, same]")
    (world["store"] / "memories").mkdir(exist_ok=True)
    (world["store"] / "memories" / f"{memory.id}.md").write_text(
        encode(memory).replace("harness: test", extra), encoding="utf-8")
    source = (await _call(server, "memory_read", memory_id=memory.id))["source"]
    assert source == {"harness": "test", "method": "agent", "n": None, "i": None,
                      "when": "2020-01-01", "o": [["a", 1]], "p": [["a", 1]], "s": ["x"],
                      "k": {"1": "a", "None": "b"}, "t1": "same", "t2": "same",
                      "u": ["same", "same"]}


async def test_update_rewrites_in_place_and_description_none_keeps_empty_clears(server):
    written = await _call(server, "memory_write", content="v1", type="user", description="cue")
    updated = await _call(server, "memory_update", memory_id=written["id"], content="v2")
    assert set(updated) == {"id", "updated"} and updated["id"] == written["id"]
    assert (await _call(server, "memory_read", memory_id=written["id"]))["description"] == "cue"
    await _call(server, "memory_update", memory_id=written["id"], content="v3", description="")
    read = await _call(server, "memory_read", memory_id=written["id"])
    assert (read["body"], read["description"]) == ("v3", "")


async def test_update_description_with_secret_material_is_refused(server):
    written = await _call(server, "memory_write", content="v1", type="user")
    result = await _call(server, "memory_update", memory_id=written["id"], content="v2",
                         description="ghp_" + "a" * 36)
    assert "ghp_" not in result["error"]


async def test_delete(server):
    written = await _call(server, "memory_write", content="gone soon", type="user")
    assert await _call(server, "memory_delete", memory_id=written["id"]) == \
        {"deleted": written["id"]}
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
    assert (world["store"] / "memories" / f"{written['id']}.md").is_file()
    assert not (tmp_path / "elsewhere").exists()
