import os
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memriver.project_context import bind
from memriver.server import build_server
from memriver_core.config import Settings
from memriver_core.models import AccessContext, Memory, ProjectId, Scope
from memriver_core.repository.filesystem import FileMemoryRepository
from memriver_core.repository.filesystem.markdown_codec import encode

# a valid ULID shape, used for a hand-written (hand-edited) entry file
BAD_YAML_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

SOURCE = {"harness": "test", "method": "agent"}
GLOBAL = Scope.global_()
PROJECT = ProjectId("demo-0123456789abcdef")
GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell the user; "
                    "do not retry through another entry or edit the store directly.")


def _write_raw(root, name: str, text: str) -> None:
    d = root / "global" / "entries"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _path(root, memory: Memory) -> Path:
    scope_dir = ("global" if memory.scope.project_id is None
                 else f"projects/{memory.scope.project_id}")
    return root / scope_dir / "entries" / f"{memory.id}.md"


def _seed(root, memory: Memory) -> Path:
    """Put a project memory on disk through the repository, as the server would."""
    FileMemoryRepository(root).create(
        memory, AccessContext(project_id=memory.scope.project_id))
    return _path(root, memory)


def _plant_global(root: Path, memory: Memory) -> None:
    """Put a global memory on disk directly: the repository refuses to write one."""
    d = root / "global" / "entries"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{memory.id}.md").write_text(encode(memory), encoding="utf-8")


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file() and p.name != ".lock"}


def _seed_healthy(root) -> Memory:
    m = Memory.new(body="uv manages this workspace", type="project",
                   scope=Scope.global_(), source=SOURCE)
    _plant_global(root, m)
    return m


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "demo"
    d.mkdir()
    bind(tmp_path / "mem", PROJECT, str(d.resolve()), create=True)
    return d


@pytest.fixture
def server(tmp_path, project):
    return build_server(root=tmp_path / "mem", project_dir=project)


@pytest.fixture
def unregistered_server(tmp_path):
    d = tmp_path / "repo"
    (d / ".git").mkdir(parents=True)
    return build_server(root=tmp_path / "mem", project_dir=d)


@pytest.fixture
def degraded_server(tmp_path):
    bad = tmp_path / "mem" / "projects" / "bad-0123456789abcdef"
    bad.mkdir(parents=True)
    (bad / "project.toml").write_text("roots = [\n")
    return build_server(root=tmp_path / "mem", project_dir=tmp_path)


async def test_write_then_index_and_search(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "本项目 python 包管理用 uv", "type": "project",
            "harness": "claude-code"})).data
        assert "id" in r and r["scope"] == f"project:{PROJECT}"
        idx = (await c.call_tool("memory_index", {})).data
        assert "python 包管理用 uv" in idx
        hits = (await c.call_tool("memory_search", {"query": "包管理"})).data
        assert hits[0]["id"] == r["id"]


async def test_index_starts_with_the_project_header_even_when_empty(
        server, unregistered_server, degraded_server):
    async with Client(server) as c:
        idx = (await c.call_tool("memory_index", {})).data
    assert idx.splitlines()[0].startswith(f"project: {PROJECT} (root ")
    assert idx.splitlines()[1] == "(no memories yet)"
    async with Client(unregistered_server) as c:
        idx = (await c.call_tool("memory_index", {})).data
    assert idx.splitlines()[0] == ("project: none — global is read-only; "
                                   "ask the user to run memriver project init")
    async with Client(degraded_server) as c:
        idx = (await c.call_tool("memory_index", {})).data
    assert idx.startswith("project: unavailable — registry invalid "
                          "(projects/bad-0123456789abcdef/project.toml: ")


async def test_write_without_project_is_refused_with_fixed_text(
        unregistered_server, degraded_server, tmp_path):
    async with Client(unregistered_server) as c:
        r = (await c.call_tool("memory_write", {"content": "fact", "type": "project"})).data
    assert r == {"error": ("no writable project: this directory is not registered. No memory "
                           "was saved. Ask the user to choose a project root and run memriver "
                           "project init; do not run it yourself.")}
    async with Client(degraded_server) as c:
        r = (await c.call_tool("memory_write", {"content": "fact", "type": "project"})).data
    assert r == {"error": ("no writable project: the project registry is invalid. No memory "
                           "was saved. Ask the user to run memriver project explain.")}
    assert not (tmp_path / "mem" / "global").exists()


async def test_write_with_scope_argument_fails_whole_call(server, tmp_path):
    before = _snapshot(tmp_path / "mem")
    async with Client(server) as c:
        with pytest.raises(ToolError):
            await c.call_tool("memory_write", {
                "content": "fact", "type": "project", "scope": "global"})
        idx = (await c.call_tool("memory_index", {})).data
    assert "(no memories yet)" in idx
    assert _snapshot(tmp_path / "mem") == before


@pytest.mark.parametrize("fixture", ["server", "unregistered_server", "degraded_server"])
async def test_update_and_delete_of_global_are_refused_in_every_context(
        request, fixture, tmp_path):
    srv = request.getfixturevalue(fixture)
    _plant_global(tmp_path / "mem",
                  Memory.new(body="drinks oolong", type="user", scope=GLOBAL,
                             source=SOURCE, id="tea"))
    before = _snapshot(tmp_path / "mem")
    async with Client(srv) as c:
        assert (await c.call_tool("memory_update", {
            "entry_id": "tea", "content": "x"})).data == {"error": GLOBAL_READ_ONLY}
        assert (await c.call_tool("memory_delete", {
            "entry_id": "tea"})).data == {"error": GLOBAL_READ_ONLY}
        assert (await c.call_tool("memory_read", {"entry_id": "tea"})).data["body"] == (
            "drinks oolong")
        hits = (await c.call_tool("memory_search", {"query": "oolong"})).data
        assert hits[0]["id"] == "tea"
    assert _snapshot(tmp_path / "mem") == before


async def test_same_name_global_and_project_delete_is_refused_and_project_copy_intact(
        server, tmp_path):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "project copy", "type": "project", "name": "tea"})
    _plant_global(tmp_path / "mem",
                  Memory.new(body="global copy", type="user", scope=GLOBAL,
                             source=SOURCE, id="tea"))
    project_file = tmp_path / "mem" / "projects" / PROJECT / "entries" / "tea.md"
    before = project_file.read_bytes()
    async with Client(server) as c:
        assert (await c.call_tool("memory_delete", {
            "entry_id": "tea"})).data == {"error": GLOBAL_READ_ONLY}
    assert project_file.read_bytes() == before


async def test_global_name_collision_has_no_existing_payload(server, tmp_path):
    _plant_global(tmp_path / "mem",
                  Memory.new(body="g", type="user", scope=GLOBAL, source=SOURCE, id="tea"))
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "fact", "type": "project", "name": "tea"})).data
    assert r == {"error": "name 'tea' is already used by a read-only global memory; "
                          "choose another name"}


async def test_write_secret_rejected(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "key AKIAIOSFODNN7EXAMPLE", "type": "project"})).data
        assert "error" in r and "AKIA" not in r["error"]


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
            "name": "Mise Runtimes"})).data
        assert r["id"] == "mise-runtimes"


async def test_write_name_collision_refused_with_echo(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {"content": "v1", "type": "user", "name": "n"})
        out = (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n"})).data
        assert "error" in out
        assert out["existing"]["snippet"] == "v1"
        assert out["existing"]["scope"] == f"project:{PROJECT}"


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
            "content": "v1", "type": "user", "name": "notes"})).data
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
            "content": "v1", "type": "user", "name": "source"})).data
        assert "error" in out

    assert path.read_bytes() == before


async def test_write_refuses_when_name_taken_by_scope_mismatched_file(tmp_path, project):
    # store.read() treats a file whose frontmatter scope contradicts its
    # directory as EntryNotFound (directory is truth), so a collision check
    # built only on read() would conclude the name is free and let
    # store.write atomically replace a file the user may have hand-edited
    root = tmp_path / "mem"
    mismatched = Memory.new(body="hand-edited, wrong scope for its directory",
                            type="user",
                            scope=Scope.project(ProjectId("elsewhere-000000")),
                            id="n", source=SOURCE)
    _write_raw(root, "n.md", encode(mismatched))
    path = root / "global" / "entries" / "n.md"
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n"})).data
        assert "error" in out

    assert path.read_bytes() == before


async def test_write_refuses_when_name_taken_by_id_mismatched_file(tmp_path, project):
    # store.read() now treats a file whose frontmatter id contradicts its own
    # filename as EntryNotFound (filename is truth); the collision check must
    # still refuse the name rather than concluding it is free and letting
    # store.write create a second file (bar.md) while foo.md is untouched
    root = tmp_path / "mem"
    mismatched = Memory.new(body="hand-edited, id no longer matches filename",
                            type="user", scope=Scope.global_(), id="foo",
                            source=SOURCE)
    _plant_global(root, mismatched)
    path = root / "global" / "entries" / "foo.md"
    mismatched.id = "bar"
    path.write_text(encode(mismatched), encoding="utf-8")
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "foo"})).data
        assert "error" in out

    assert path.read_bytes() == before
    assert not (root / "global" / "entries" / "bar.md").exists()


async def test_update_rewrites_in_place(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {"content": "v1", "type": "user", "name": "n"})
        await c.call_tool("memory_update", {"entry_id": "n", "content": "v2"})
        r = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert r["body"] == "v2" and r["id"] == "n"
        idx = (await c.call_tool("memory_index", {})).data
        assert idx.count("n:") == 1


async def test_write_persists_description(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n",
            "description": "a one-line recall cue"})).data
        assert "id" in r
        read = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert read.get("description") == "a one-line recall cue"
        idx = (await c.call_tool("memory_index", {})).data
        assert "a one-line recall cue" in idx


async def test_write_collision_echo_carries_description(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n",
            "description": "original cue"})
        out = (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n"})).data
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
            "content": "v1", "type": "user", "name": "n",
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
        await c.call_tool("memory_write", {"content": "v1", "type": "user", "name": "n"})
        r = (await c.call_tool("memory_update", {
            "entry_id": "n", "content": "v2",
            "description": "key AKIAIOSFODNN7EXAMPLE"})).data
        assert "error" in r and "AKIA" not in r["error"]


async def test_delete(server):
    async with Client(server) as c:
        await c.call_tool("memory_write", {"content": "v1", "type": "user", "name": "n"})
        out = (await c.call_tool("memory_delete", {"entry_id": "n"})).data
        assert out == {"deleted": "n"}
        r = (await c.call_tool("memory_read", {"entry_id": "n"})).data
        assert "error" in r


async def test_unnamed_write_falls_back_to_ulid(server):
    async with Client(server) as c:
        out = (await c.call_tool("memory_write", {"content": "v1", "type": "user"})).data
        assert len(out["id"]) == 26


def _seed_foreign(root) -> Memory:
    # the repository resolves an id across every projects/* directory, so an id
    # leaked from another project must still be refused by the current project's
    # tools
    m = Memory.new(body="foreign project secret plan", type="project",
                   scope=Scope.project(ProjectId("other-000000")), source=SOURCE)
    _seed(root, m)
    return m


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
    # physical boundary the repository resolves ids through
    m = Memory.new(body="foreign project secret plan", type="project",
                   scope=Scope.project(ProjectId("other-000000")), source=SOURCE)
    path = _seed(root, m)
    m.scope = Scope.global_()
    path.write_text(encode(m), encoding="utf-8")
    return m, path


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
            "content": "x" * 11, "type": "project"})).data
        assert "error" in r and "too large" in r["error"]
        ok = (await c.call_tool("memory_write", {
            "content": "short", "type": "project"})).data
        assert "id" in ok


async def test_settings_tune_the_index_budget(tmp_path, project):
    server = build_server(root=tmp_path / "mem", project_dir=project,
                          settings=Settings(index_budget_lines=1))
    async with Client(server) as c:
        for i in range(3):
            await c.call_tool("memory_write", {
                "content": f"budget entry number {i}", "type": "project"})
        idx = (await c.call_tool("memory_index", {})).data
        # the project header, one entry line, and the omitted notice
        assert idx.count("\n") == 2
        assert "2 more entries omitted" in idx


async def test_settings_tune_the_search_limits(tmp_path, project):
    server = build_server(root=tmp_path / "mem", project_dir=project,
                          settings=Settings(search_limit_default=1, search_limit_max=2))
    async with Client(server) as c:
        for i in range(3):
            await c.call_tool("memory_write", {
                "content": f"shared keyword body number {i}", "type": "project"})
        assert len((await c.call_tool("memory_search", {"query": "keyword"})).data) == 1
        assert len((await c.call_tool("memory_search",
                                      {"query": "keyword", "limit": 10})).data) == 2


async def test_search_limit_stays_a_plain_integer_in_the_tool_schema(server):
    async with Client(server) as c:
        schema = {t.name: t.inputSchema for t in await c.list_tools()}["memory_search"]
        limit = schema["properties"]["limit"]
        assert limit.get("type") == "integer" or {"type": "integer"} in limit.get("anyOf", [])


async def test_no_tool_offers_a_scope_argument(server):
    async with Client(server) as c:
        schemas = {t.name: t.inputSchema for t in await c.list_tools()}
    assert schemas and all("scope" not in s.get("properties", {}) for s in schemas.values())


def _seed_with_updated(root, entry_id, updated, scope=None):
    scope = Scope.project(PROJECT) if scope is None else scope
    m = Memory.new(body=f"body of {entry_id}", type="project", scope=scope,
                   source=SOURCE, id=entry_id,
                   description=f"description of {entry_id}")
    m.updated = updated
    if scope.project_id is None:
        _plant_global(root, m)
    else:
        _seed(root, m)
    return m


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


async def test_dream_is_project_only_and_names_the_project(
        server, unregistered_server, tmp_path):
    _plant_global(tmp_path / "mem",
                  Memory.new(body="g", type="user", scope=GLOBAL, source=SOURCE,
                             id="old-global"))
    async with Client(server) as c:
        await c.call_tool("memory_write", {
            "content": "p", "type": "project", "name": "p-entry"})
        r = (await c.call_tool("memory_dream", {"limit": 5})).data
    assert r["project"].startswith(f"project: {PROJECT} (root ")
    assert [e["id"] for e in r["entries"]] == ["p-entry"]
    async with Client(unregistered_server) as c:
        r = (await c.call_tool("memory_dream", {"limit": 5})).data
    assert r["entries"] == [] and r["project"].startswith("project: none")


async def test_dream_never_surfaces_entries_outside_current_project_scopes(
        tmp_path, project):
    root = tmp_path / "mem"
    _seed_with_updated(root, "project-entry", "2026-06-01T00:00:00Z")
    _seed_with_updated(root, "foreign-entry", "2026-01-01T00:00:00Z",
                       scope=Scope.project(ProjectId("other-000000")))

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        out = (await c.call_tool("memory_dream", {"limit": 10})).data
        assert [e["id"] for e in out["entries"]] == ["project-entry"]


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


async def test_explicit_root_wins_over_the_settings_root(tmp_path):
    # callers that already resolved the root (the CLI, the tests) must not have
    # it replaced by whatever the settings layer resolved
    explicit = tmp_path / "explicit"
    other = tmp_path / "other"
    d = tmp_path / "demo"
    d.mkdir()
    bind(explicit, PROJECT, str(d.resolve()), create=True)
    server = build_server(root=explicit, project_dir=d, settings=Settings(root=other))
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n"})).data
        assert "id" in r

    assert [p.name for p in explicit.glob("**/entries/*.md")] == ["n.md"]
    assert not other.exists()


async def test_unknown_id_shape_reads_as_not_found(server):
    # ids that cannot be a filename never reach the filesystem; they answer
    # like any other absent name rather than leaking a ValueError
    async with Client(server) as c:
        for tool, args in (("memory_read", {}), ("memory_update", {"content": "x"}),
                           ("memory_delete", {})):
            r = (await c.call_tool(tool, {"entry_id": "Bad Name", **args})).data
            assert r == {"error": "no such entry: Bad Name"}, tool


async def test_unreadable_entry_maps_per_operation(tmp_path, project):
    root = tmp_path / "mem"
    _write_raw(root, f"{BAD_YAML_ID}.md", "---\nid: [unclosed\n---\nbody\n")
    _write_raw(root, "notes.md", "just some hand-written notes\n")

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        assert (await c.call_tool("memory_read", {"entry_id": BAD_YAML_ID})).data == {
            "error": f"unreadable entry file: {BAD_YAML_ID}"}
        assert (await c.call_tool("memory_update", {
            "entry_id": BAD_YAML_ID, "content": "replacement"})).data == {
            "error": f"unreadable entry file: {BAD_YAML_ID}"}
        assert (await c.call_tool("memory_delete", {"entry_id": "notes"})).data == {
            "error": "could not delete entry: notes"}
        assert (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "notes"})).data == {
            "error": "name 'notes' is taken by a file that is not a readable entry"}


@pytest.mark.parametrize("raw", ["nonsense", '""'])
async def test_unparsable_stored_scope_maps_per_operation(tmp_path, project, raw):
    # a hand-edited `scope:` outside the storage grammar cannot match the
    # directory the file sits in, so every tool answers as it does for any
    # other scope mismatch: the entry is absent, and its file stays untouched
    root = tmp_path / "mem"
    m = Memory.new(body="hand-edited scope", type="project", scope=GLOBAL,
                   source=SOURCE, id="n")
    _write_raw(root, "n.md", encode(m).replace("scope: global", f"scope: {raw}"))
    path = root / "global" / "entries" / "n.md"
    before = path.read_bytes()

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        assert (await c.call_tool("memory_read", {"entry_id": "n"})).data == {
            "error": "no such entry: n"}
        assert (await c.call_tool("memory_update", {
            "entry_id": "n", "content": "replacement"})).data == {
            "error": "no such entry: n"}
        assert (await c.call_tool("memory_delete", {"entry_id": "n"})).data == {
            "error": "no such entry: n"}
        # the name is still taken: the write must not replace the file
        assert (await c.call_tool("memory_write", {
            "content": "v1", "type": "user", "name": "n"})).data == {
            "error": "name 'n' is taken by a file that is not a readable entry"}
        # and it stays out of the index and search
        assert "hand-edited" not in (await c.call_tool("memory_index", {})).data
        assert (await c.call_tool("memory_search", {"query": "hand-edited"})).data == []

    assert path.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
async def test_storage_failure_maps_per_operation(tmp_path, project):
    root = tmp_path / "mem"
    _seed_with_updated(root, "n", "2026-01-01T00:00:00Z")
    entries = root / "projects" / PROJECT / "entries"
    path = entries / "n.md"

    server = build_server(root=root, project_dir=project)
    async with Client(server) as c:
        path.chmod(0o000)  # the file itself cannot be read
        try:
            assert (await c.call_tool("memory_read", {"entry_id": "n"})).data == {
                "error": "unreadable entry file: n"}
            # update's read-modify-write can fail on either half; a
            # StorageFailure is deliberately fieldless, so the transport
            # cannot tell this read-side failure from a write-side one and
            # reports the operation, not a diagnosis it cannot make
            assert (await c.call_tool("memory_update", {
                "entry_id": "n", "content": "v2"})).data == {
                "error": "could not update entry: n"}
        finally:
            path.chmod(0o644)

        entries.chmod(0o500)  # the directory cannot be written to
        try:
            assert (await c.call_tool("memory_write", {
                "content": "v1", "type": "user", "name": "fresh"})).data == {
                "error": "could not write entry"}
            assert (await c.call_tool("memory_delete", {"entry_id": "n"})).data == {
                "error": "could not delete entry: n"}
        finally:
            entries.chmod(0o755)
