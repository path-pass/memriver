import pytest
from fastmcp import Client
from memriver.server import build_server
from memriver_core.entry import Entry
from memriver_core.scope import project_slug
from memriver_core.store import MemoryStore

# valid ULID shapes, used for hand-written (hand-edited) entry files
BAD_YAML_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
BAD_TYPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


def _write_raw(root, name: str, text: str) -> None:
    d = root / "global" / "entries"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _seed_healthy(root) -> Entry:
    e = Entry.new(body="uv manages this workspace", type="fact", scope="global",
                  source={"harness": "test", "method": "agent"})
    MemoryStore(root).write(e)
    return e


def _hand_written_entry(entry_id: str, type: str) -> str:
    return (f"---\nid: {entry_id}\ntype: {type}\nscope: global\nsync: true\n"
            'created: "2026-01-01T00:00:00Z"\nupdated: "2026-01-01T00:00:00Z"\n'
            "source: {harness: manual, method: human}\ntrust: agent\n"
            "superseded_by: null\n---\nhand written entry\n")


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
            "content": "本项目 python 包管理用 uv", "type": "fact",
            "scope": "project", "harness": "claude-code"})).data
        assert "id" in r and r["scope"].startswith("project:demo-")
        idx = (await c.call_tool("memory_index", {})).data
        assert "python 包管理用 uv" in idx
        hits = (await c.call_tool("memory_search", {"query": "包管理"})).data
        assert hits[0]["id"] == r["id"]


async def test_write_secret_rejected(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "key AKIAIOSFODNN7EXAMPLE", "type": "fact"})).data
        assert "error" in r and "AKIA" not in r["error"]


async def test_malformed_explicit_scope_returns_error_dict(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "traversal attempt", "type": "fact",
            "scope": "project:../../etc"})).data
        assert "error" in r


async def test_blank_content_rejected(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "   ", "type": "fact"})).data
        assert "error" in r
        idx = (await c.call_tool("memory_index", {})).data
        assert "no memories yet" in idx


async def test_nul_bytes_do_not_escape_as_tool_error(server):
    async with Client(server) as c:
        assert (await c.call_tool("memory_search", {"query": "a\x00b"})).data == []
        r = (await c.call_tool("memory_write", {
            "content": "x\x00y", "type": "fact"})).data
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
                "content": "ok", "type": "fact", "harness": bad})).data
            assert "error" in r, bad
            # the rejected value is never echoed back to the caller
            assert not bad or bad not in r["error"]
            assert secret[:8] not in r["error"]

    assert list(root.glob("**/entries/*.md")) == []


async def test_valid_harness_still_accepted(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "harness identifiers may carry dots and dashes",
            "type": "fact", "harness": "claude-code"})).data
        assert "id" in r


async def test_update_supersedes(server):
    async with Client(server) as c:
        old = (await c.call_tool("memory_write", {
            "content": "旧偏好：用英文回复", "type": "preference",
            "scope": "global"})).data
        new = (await c.call_tool("memory_update", {
            "entry_id": old["id"], "content": "新偏好：用中文回复"})).data
        read_old = (await c.call_tool("memory_read", {"entry_id": old["id"]})).data
        assert read_old["superseded_by"] == new["id"]
        hits = (await c.call_tool("memory_search", {"query": "用中文回复"})).data
        assert {h["id"] for h in hits} == {new["id"]}


def _seed_foreign(root) -> Entry:
    # store.read() globs every projects/* directory, so an id leaked from another
    # project must still be refused by the tools of the current project
    e = Entry.new(body="foreign project secret plan", type="fact",
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
        assert "error" in r and "scope" in r["error"]
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
        assert "error" in r and "scope" in r["error"]

    assert path.read_text(encoding="utf-8") == before
    assert MemoryStore(root).read(foreign.id).superseded_by is None
    files = list(root.glob("**/entries/*.md"))
    assert files == [path]  # no replacement entry was written anywhere


def _seed_misplaced(root):
    # a hand-edited file that stays under another project's directory but claims
    # the global scope: the frontmatter alone must not carry it across the
    # physical boundary that store.read() resolves ids through
    e = Entry.new(body="foreign project secret plan", type="fact",
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
            "content": "planted by a foreign scope", "type": "fact",
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
            "content": "explicit current scope is allowed", "type": "fact",
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


async def test_update_of_invalid_hand_edited_entry_returns_error_dict(tmp_path, project):
    root = tmp_path / "mem"
    _write_raw(root, f"{BAD_TYPE_ID}.md", _hand_written_entry(BAD_TYPE_ID, "task"))

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        r = (await c.call_tool("memory_update", {
            "entry_id": BAD_TYPE_ID, "content": "replacement"})).data
        assert "error" in r and "task" in r["error"]


async def test_update_of_superseded_entry_is_refused(server):
    async with Client(server) as c:
        first = (await c.call_tool("memory_write", {
            "content": "deploys run on friday", "type": "fact",
            "scope": "global"})).data
        second = (await c.call_tool("memory_update", {
            "entry_id": first["id"], "content": "deploys run on monday"})).data
        stale = (await c.call_tool("memory_update", {
            "entry_id": first["id"], "content": "deploys run on tuesday"})).data

        assert "error" in stale
        assert stale["superseded_by"] == second["id"]
        idx = (await c.call_tool("memory_index", {})).data
        assert idx.count("\n") == 0 and second["id"] in idx
        assert "tuesday" not in idx
