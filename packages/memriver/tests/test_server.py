import pytest
from fastmcp import Client
from memriver.server import build_server
from memriver_core.config import Settings
from memriver_core.entry import Entry
from memriver_core.scope import project_slug
from memriver_core.store import MemoryStore

# a valid ULID shape, used for a hand-written (hand-edited) entry file
BAD_YAML_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _write_raw(root, name: str, text: str) -> None:
    d = root / "global" / "entries"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _seed_healthy(root) -> Entry:
    e = Entry.new(body="uv manages this workspace", type="project", scope="global",
                  source={"harness": "test", "method": "agent"})
    MemoryStore(root).write(e)
    return e


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def server(tmp_path, project):
    return build_server(root=tmp_path / "mem", project_dir=project)


async def test_write_then_index_and_search(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "本项目 python 包管理用 uv", "type": "project",
            "scope": "project", "harness": "claude-code"})).data
        assert "id" in r and r["scope"].startswith("project:demo-")
        idx = (await c.call_tool("memory_index", {})).data
        assert "python 包管理用 uv" in idx
        hits = (await c.call_tool("memory_search", {"query": "包管理"})).data
        assert hits[0]["id"] == r["id"]


async def test_write_secret_rejected(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "key AKIAIOSFODNN7EXAMPLE", "type": "project"})).data
        assert "error" in r and "AKIA" not in r["error"]


async def test_malformed_explicit_scope_returns_error_dict(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "traversal attempt", "type": "project",
            "scope": "project:../../etc"})).data
        assert "error" in r


async def test_blank_content_rejected(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "   ", "type": "project"})).data
        assert "error" in r
        idx = (await c.call_tool("memory_index", {})).data
        assert "no memories yet" in idx


async def test_nul_bytes_do_not_escape_as_tool_error(server):
    async with Client(server) as c:
        assert (await c.call_tool("memory_search", {"query": "a\x00b"})).data == []
        r = (await c.call_tool("memory_write", {
            "content": "x\x00y", "type": "project"})).data
        assert "id" in r


async def test_harness_with_secret_material_is_refused(tmp_path, project):
    # 'harness' lands verbatim in the frontmatter, so without validation it is a
    # gate-free channel for secrets or megabytes of text
    root = tmp_path / "mem"
    server = build_server(root=root, project_dir=project)
    secret = "ghp_" + "a" * 36
    async with Client(server) as c:
        for bad in (secret,                                  # a credential
                    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLE",  # outside the charset
                    "x" * 65,                                # over the size cap
                    ""):                                     # empty
            r = (await c.call_tool("memory_write", {
                "content": "ok", "type": "project", "harness": bad})).data
            assert "error" in r, bad
            # the rejected value is never echoed back to the caller
            assert not bad or bad not in r["error"]
            assert secret[:8] not in r["error"]

    assert list(root.glob("**/entries/*.md")) == []


async def test_valid_harness_still_accepted(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "harness identifiers may carry dots and dashes",
            "type": "project", "harness": "claude-code"})).data
        assert "id" in r


async def test_write_name_with_secret_material_is_refused(tmp_path, project):
    # 'name' is persisted verbatim as the filename + frontmatter id via
    # sanitize_name, which only lowercases/strips -- it does not scrub
    # secret-shaped content, so the gate must cover it like content/harness/
    # description
    root = tmp_path / "mem"
    server = build_server(root=root, project_dir=project)
    token = "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx"
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "ok", "type": "project", "name": token})).data
        assert "error" in r and token not in r["error"]

    assert list(root.glob("**/entries/*.md")) == []


async def test_write_with_name_uses_it(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "mise manages runtimes", "type": "user",
            "name": "Mise Runtimes", "scope": "global"})).data
        assert r["id"] == "mise-runtimes"


async def test_write_name_collision_refused_with_echo(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global"})
        out = (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n", "scope": "global"})).data
        assert "error" in out
        assert out["existing"]["snippet"] == "v1"
        assert out["existing"]["scope"] == "global"


async def test_global_write_refused_when_a_project_holds_the_name(tmp_path, project):
    # store._find/read resolve a global write's collision check across the
    # caller's own two scopes only; a global write for a name some OTHER
    # project already owns must still be refused, and must not echo that
    # project's content or type back to the caller
    root = tmp_path / "mem"
    foreign = Entry.new(body="foreign project secret plan", type="project",
                        scope="project:other-000000", id="n",
                        source={"harness": "test", "method": "agent"})
    path = MemoryStore(root).write(foreign)
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n", "scope": "global"})).data
        assert "error" in out
        assert "existing" not in out
        assert "secret plan" not in str(out) and "project" not in str(out.get("error", ""))

    assert path.read_bytes() == before
    assert list(root.glob("global/entries/*.md")) == []


async def test_write_refuses_to_clobber_a_hand_written_non_entry_file(tmp_path, project):
    # the collision check must fail closed on a file it cannot parse, not
    # treat the name as free and let store.write overwrite it
    root = tmp_path / "mem"
    _write_raw(root, "notes.md", "just some hand-written notes\n")
    path = root / "global" / "entries" / "notes.md"
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "notes", "scope": "global"})).data
        assert "error" in out

    assert path.read_bytes() == before


async def test_write_refuses_clobber_when_name_equals_missing_frontmatter_key(
        tmp_path, project):
    # Entry.from_markdown looks up frontmatter keys by name (m["source"], ...);
    # a hand-written file missing exactly the key that happens to match the
    # proposed entry name used to raise a bare KeyError indistinguishable from
    # "name not found" and get silently clobbered
    root = tmp_path / "mem"
    _write_raw(root, "source.md", "---\n"
               "id: source\n"
               "type: user\n"
               "scope: global\n"
               "sync: true\n"
               "created: 2026-08-29T10:00:00Z\n"
               "updated: 2026-08-29T10:00:00Z\n"
               "trust: agent\n"
               "---\n\n"
               "hand-written, missing the source: key\n")
    path = root / "global" / "entries" / "source.md"
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "source", "scope": "global"})).data
        assert "error" in out

    assert path.read_bytes() == before


async def test_write_refuses_when_name_taken_by_scope_mismatched_file(tmp_path, project):
    # store.read() treats a file whose frontmatter scope contradicts its
    # directory as EntryNotFound (directory is truth), so a collision check
    # built only on read() would conclude the name is free and let
    # store.write atomically replace a file the user may have hand-edited
    root = tmp_path / "mem"
    mismatched = Entry.new(body="hand-edited, wrong scope for its directory",
                           type="user", scope="project:elsewhere-000000", id="n",
                           source={"harness": "test", "method": "agent"})
    _write_raw(root, "n.md", mismatched.to_markdown())
    path = root / "global" / "entries" / "n.md"
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global"})).data
        assert "error" in out

    assert path.read_bytes() == before


async def test_update_rewrites_in_place(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global"})
        await c.call_tool("memory_update", {"entry_id": "n", "content": "v2"})
        r = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert r["body"] == "v2" and r["id"] == "n"
        idx = (await c.call_tool("memory_index", {})).data
        assert idx.count("n:") == 1


async def test_write_persists_description(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global",
            "description": "a one-line recall cue"})).data
        assert "id" in r
        read = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert read.get("description") == "a one-line recall cue"
        idx = (await c.call_tool("memory_index", {})).data
        assert "a one-line recall cue" in idx


async def test_write_collision_echo_carries_description(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global",
            "description": "original cue"})
        out = (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n", "scope": "global"})).data
        assert out["existing"]["description"] == "original cue"


async def test_write_description_with_secret_material_is_refused(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "ok", "type": "project",
            "description": "key AKIAIOSFODNN7EXAMPLE"})).data
        assert "error" in r and "AKIA" not in r["error"]
        idx = (await c.call_tool("memory_index", {})).data
        assert "no memories yet" in idx


async def test_update_description_none_preserves_string_replaces_empty_clears(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global",
            "description": "original cue"})

        await c.call_tool("memory_update", {"entry_id": "n", "content": "v2"})
        r = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert r["description"] == "original cue"

        await c.call_tool("memory_update", {
            "entry_id": "n", "content": "v3", "description": "new cue"})
        r = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert r["description"] == "new cue"

        await c.call_tool("memory_update", {
            "entry_id": "n", "content": "v4", "description": ""})
        r = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert r["description"] == ""


async def test_update_description_with_secret_material_is_refused(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global"})
        r = (await c.call_tool("memory_update", {
            "entry_id": "n", "content": "v2",
            "description": "key AKIAIOSFODNN7EXAMPLE"})).data
        assert "error" in r and "AKIA" not in r["error"]


async def test_delete(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n", "scope": "global"})
        out = (await c.call_tool("memory_delete", {"entry_id": "n"})).data
        assert out == {"deleted": "n"}
        r = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert "error" in r


async def test_unnamed_write_falls_back_to_ulid(server):
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "scope": "global"})).data
        assert len(out["id"]) == 26


def _seed_foreign(root) -> Entry:
    # store.read() globs every projects/* directory, so an id leaked from another
    # project must still be refused by the tools of the current project
    e = Entry.new(body="foreign project secret plan", type="project",
                  scope="project:other-000000",
                  source={"harness": "test", "method": "agent"})
    MemoryStore(root).write(e)
    return e


async def test_read_outside_scope_is_refused(tmp_path, project):
    root = tmp_path / "mem"
    foreign = _seed_foreign(root)

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_read", {"entry_id": foreign.id})).data
        assert "error" in r and "no such entry" in r["error"]
        assert "secret plan" not in str(r)


async def test_update_outside_scope_is_refused(tmp_path, project):
    root = tmp_path / "mem"
    foreign = _seed_foreign(root)
    path = root / "projects" / "other-000000" / "entries" / f"{foreign.id}.md"
    before = path.read_text(encoding="utf-8")

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_update", {
            "entry_id": foreign.id, "content": "hijacked"})).data
        assert "error" in r and "no such entry" in r["error"]

    assert path.read_text(encoding="utf-8") == before
    files = list(root.glob("**/entries/*.md"))
    assert files == [path]  # no replacement entry was written anywhere


async def test_delete_outside_scope_is_refused(tmp_path, project):
    root = tmp_path / "mem"
    foreign = _seed_foreign(root)
    path = root / "projects" / "other-000000" / "entries" / f"{foreign.id}.md"
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_delete", {"entry_id": foreign.id})).data
        assert "error" in r

    assert path.read_bytes() == before


def _seed_misplaced(root):
    # a hand-edited file that stays under another project's directory but claims
    # the global scope: the frontmatter alone must not carry it across the
    # physical boundary that store.read() resolves ids through
    e = Entry.new(body="foreign project secret plan", type="project",
                  scope="project:other-000000",
                  source={"harness": "test", "method": "agent"})
    path = MemoryStore(root).write(e)
    e.scope = "global"
    path.write_text(e.to_markdown(), encoding="utf-8")
    return e, path


async def test_read_of_misplaced_entry_is_refused(tmp_path, project):
    root = tmp_path / "mem"
    misplaced, _ = _seed_misplaced(root)

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_read", {"entry_id": misplaced.id})).data
        assert "error" in r
        assert "secret plan" not in str(r)


async def test_update_of_misplaced_entry_is_refused(tmp_path, project):
    root = tmp_path / "mem"
    misplaced, path = _seed_misplaced(root)
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_update", {
            "entry_id": misplaced.id, "content": "hijacked"})).data
        assert "error" in r

    assert path.read_bytes() == before
    files = list(root.glob("**/entries/*.md"))
    assert files == [path]  # no replacement entry was written anywhere


async def test_write_to_foreign_project_scope_is_refused(tmp_path, project):
    # 'project:<other-slug>' passes resolve_scope untouched, so without an
    # explicit guard memory_write would seed another project's directory
    root = tmp_path / "mem"
    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "planted by a foreign scope", "type": "project",
            "scope": "project:other-000000"})).data
        assert "error" in r and "scope" in r["error"]
        idx = (await c.call_tool("memory_index", {})).data
        assert "no memories yet" in idx

    assert list(root.glob("**/entries/*.md")) == []


async def test_write_with_current_project_explicit_scope_succeeds(tmp_path, project):
    # the explicit form of the *current* project is inside the boundary
    root = tmp_path / "mem"
    server = build_server(root=root, project_dir=project)
    scope = f"project:{project_slug(project)}"
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "explicit current scope is allowed", "type": "project",
            "scope": scope})).data
        assert r.get("scope") == scope and "id" in r
        idx = (await c.call_tool("memory_index", {})).data
        assert r["id"] in idx


async def test_unreadable_entry_files_are_skipped_at_startup(tmp_path, project):
    root = tmp_path / "mem"
    healthy = _seed_healthy(root)
    _write_raw(root, "notes.md", "just some hand-written notes\n")
    _write_raw(root, f"{BAD_YAML_ID}.md", "---\nid: [unclosed\n---\nbody\n")

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        idx = (await c.call_tool("memory_index", {})).data
        assert healthy.id in idx
        assert "hand-written notes" not in idx and "unclosed" not in idx
        hits = (await c.call_tool("memory_search", {"query": "workspace"})).data
        assert [h["id"] for h in hits] == [healthy.id]


async def test_read_unreadable_entry_returns_error_dict(tmp_path, project):
    root = tmp_path / "mem"
    _write_raw(root, f"{BAD_YAML_ID}.md", "---\nid: [unclosed\n---\nbody\n")

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_read", {"entry_id": BAD_YAML_ID})).data
        assert "error" in r and "unreadable" in r["error"]
        u = (await c.call_tool("memory_update", {
            "entry_id": BAD_YAML_ID, "content": "replacement"})).data
        assert "error" in u and "unreadable" in u["error"]


async def test_delete_of_non_entry_file_is_refused(tmp_path, project):
    # a hand-written note whose name happens to match the slug shape must not
    # be unlinked by memory_delete just because its name resolves
    root = tmp_path / "mem"
    _write_raw(root, "notes.md", "just some hand-written notes\n")
    path = root / "global" / "entries" / "notes.md"

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_delete", {"entry_id": "notes"})).data
        assert "error" in r
        # the OS/parse error is never echoed back to the caller
        assert str(root) not in r["error"]

    assert path.read_text(encoding="utf-8") == "just some hand-written notes\n"


async def test_settings_tune_the_body_budget(tmp_path, project):
    server = build_server(root=tmp_path / "mem", project_dir=project,
                          settings=Settings(max_body_chars=10))
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "x" * 11, "type": "project", "scope": "global"})).data
        assert "error" in r and "too large" in r["error"]
        ok = (await c.call_tool("memory_write", {
            "content": "short", "type": "project", "scope": "global"})).data
        assert "id" in ok


async def test_settings_tune_the_index_budget(tmp_path, project):
    server = build_server(root=tmp_path / "mem", project_dir=project,
                          settings=Settings(index_budget_lines=1))
    async with Client(server) as c:
        for i in range(3):
            await c.call_tool("memory_write", {
                "content": f"budget entry number {i}", "type": "project",
                "scope": "global"})
        idx = (await c.call_tool("memory_index", {})).data
        assert idx.count("\n") == 1  # one entry line + the omitted notice
        assert "2 more entries omitted" in idx


async def test_settings_tune_the_search_limits(tmp_path, project):
    server = build_server(root=tmp_path / "mem", project_dir=project,
                          settings=Settings(search_limit_default=1, search_limit_max=2))
    async with Client(server) as c:
        for i in range(3):
            await c.call_tool("memory_write", {
                "content": f"shared keyword body number {i}", "type": "project",
                "scope": "global"})
        assert len((await c.call_tool("memory_search", {"query": "keyword"})).data) == 1
        assert len((await c.call_tool("memory_search",
                                      {"query": "keyword", "limit": 10})).data) == 2


async def test_search_limit_stays_a_plain_integer_in_the_tool_schema(server):
    async with Client(server) as c:
        schema = {t.name: t.inputSchema for t in await c.list_tools()}["memory_search"]
        limit = schema["properties"]["limit"]
        assert limit.get("type") == "integer" or {"type": "integer"} in limit.get("anyOf", [])


def _seed_with_updated(root, entry_id, updated, scope="global"):
    e = Entry.new(body=f"body of {entry_id}", type="project", scope=scope,
                  source={"harness": "test", "method": "agent"}, id=entry_id,
                  description=f"description of {entry_id}")
    e.updated = updated
    MemoryStore(root).write(e)
    return e


async def test_dream_returns_oldest_entry_first_with_full_content(tmp_path, project):
    root = tmp_path / "mem"
    _seed_with_updated(root, "newer", "2026-06-01T00:00:00Z")
    _seed_with_updated(root, "older", "2026-01-01T00:00:00Z")

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_dream", {"limit": 2})).data
        ids = [e["id"] for e in out["entries"]]
        assert ids == ["older", "newer"]
        first = out["entries"][0]
        assert first["body"] == "body of older"
        assert first["description"] == "description of older"


async def test_dream_never_surfaces_entries_outside_current_project_scopes(
        tmp_path, project):
    root = tmp_path / "mem"
    _seed_with_updated(root, "global-entry", "2026-06-01T00:00:00Z")
    _seed_with_updated(root, "foreign-entry", "2026-01-01T00:00:00Z",
                       scope="project:other-000000")

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_dream", {"limit": 10})).data
        assert "foreign-entry" not in [e["id"] for e in out["entries"]]
        assert "global-entry" in [e["id"] for e in out["entries"]]


async def test_dream_confirm_is_touch_rotates_the_queue(tmp_path, project):
    # _now() is second-resolution, so seed distinct, clearly-ordered `updated`
    # values directly rather than relying on real-time writes to differ
    root = tmp_path / "mem"
    _seed_with_updated(root, "a", "2020-01-01T00:00:00Z")
    _seed_with_updated(root, "b", "2021-01-01T00:00:00Z")

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        first = (await c.call_tool("memory_dream", {"limit": 1})).data
        assert first["entries"][0]["id"] == "a"

        # confirming "a" is still true bumps its `updated` to the real current
        # time, which is far newer than either seeded timestamp
        await c.call_tool("memory_update", {"entry_id": "a", "content": "body of a"})

        second = (await c.call_tool("memory_dream", {"limit": 1})).data
        assert second["entries"][0]["id"] == "b"
