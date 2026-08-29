import pytest
from fastmcp import Client
from memriver.config import Settings
from memriver.server import build_server
from memriver_core.entry import Entry
from memriver_core.index_fts import FtsIndex
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


async def test_peer_crash_recovery_reconciles_the_live_index(tmp_path, project):
    # a peer process may crash between the two writes of a supersede. This
    # server's next lock repairs the Markdown chain, but its long-lived index
    # connection kept the pre-crash picture until a restart: the old entry stayed
    # searchable as active and its replacement was missing entirely.
    root = tmp_path / "mem"
    server = build_server(root=root, project_dir=project)
    store = MemoryStore(root)

    async with Client(server) as c:
        old = (await c.call_tool("memory_write", {
            "content": "部署流程走 staging 环境", "type": "fact",
            "scope": "global"})).data
        anchor = (await c.call_tool("memory_write", {
            "content": "unrelated anchor entry", "type": "fact",
            "scope": "global"})).data

        # the crash state a peer would leave behind: replacement file on disk,
        # journal recorded, old entry not yet marked -- and the server untouched
        replacement = Entry.new(body="部署流程走 production 环境", type="fact",
                                scope="global",
                                source={"harness": "test", "method": "agent"})
        store.write(replacement)
        store._atomic_write(root / ".supersede.journal",
                            f"{old['id']} {replacement.id}")

        # any tool that takes the store lock runs recovery; use a different entry
        await c.call_tool("memory_update", {
            "entry_id": anchor["id"], "content": "unrelated anchor entry v2"})

        hits = (await c.call_tool("memory_search", {"query": "部署流程走"})).data
        assert {h["id"] for h in hits} == {replacement.id}
        read_old = (await c.call_tool("memory_read", {"entry_id": old["id"]})).data
        assert read_old["superseded_by"] == replacement.id


async def test_update_keeps_its_journal_until_the_index_transition_commits(
        tmp_path, project, monkeypatch):
    # the index file is shared by every peer process, so a crash between the
    # Markdown writes and the index transition used to leave it serving the old
    # entry with no journal left to replay: stale until some process rebuilt.
    root = tmp_path / "mem"
    server = build_server(root=root, project_dir=project)
    journal = root / ".supersede.journal"
    real = FtsIndex.mark_superseded
    failed: list[str] = []

    def flaky(self, entry_id: str) -> None:
        if not failed:
            failed.append(entry_id)
            raise RuntimeError("index commit failed")
        real(self, entry_id)

    async with Client(server) as c:
        old = (await c.call_tool("memory_write", {
            "content": "部署流程走 staging 环境", "type": "fact",
            "scope": "global"})).data
        anchor = (await c.call_tool("memory_write", {
            "content": "unrelated anchor entry", "type": "fact",
            "scope": "global"})).data

        monkeypatch.setattr(FtsIndex, "mark_superseded", flaky)
        new = (await c.call_tool("memory_update", {
            "entry_id": old["id"], "content": "部署流程走 production 环境"})).data
        assert new["supersedes"] == old["id"]  # tools never raise
        assert journal.exists()
        monkeypatch.undo()

        # any later locked operation replays the journal and retries the index
        await c.call_tool("memory_update", {
            "entry_id": anchor["id"], "content": "unrelated anchor entry v2"})
        assert not journal.exists()
        hits = (await c.call_tool("memory_search", {"query": "部署流程走"})).data
        assert {h["id"] for h in hits} == {new["id"]}


async def test_update_reports_an_outstanding_recovery_instead_of_overwriting_it(
        tmp_path, project, monkeypatch):
    # while the index transition of an earlier supersede is still owed, its
    # journal is the only record of it. A later update writes its own journal
    # over that record, so it must be refused -- as an error dict, never a raise.
    root = tmp_path / "mem"
    server = build_server(root=root, project_dir=project)
    journal = root / ".supersede.journal"

    def always_fails(self, entry_id: str) -> None:
        raise RuntimeError("index commit failed")

    async with Client(server) as c:
        old = (await c.call_tool("memory_write", {
            "content": "部署流程走 staging 环境", "type": "fact",
            "scope": "global"})).data
        anchor = (await c.call_tool("memory_write", {
            "content": "unrelated anchor entry", "type": "fact",
            "scope": "global"})).data

        monkeypatch.setattr(FtsIndex, "mark_superseded", always_fails)
        await c.call_tool("memory_update", {
            "entry_id": old["id"], "content": "部署流程走 production 环境"})
        assert journal.exists()
        pending = journal.read_text(encoding="utf-8")

        r = (await c.call_tool("memory_update", {
            "entry_id": anchor["id"], "content": "unrelated anchor entry v2"})).data
        assert "error" in r and "pending memory recovery" in r["error"]
        # the outstanding transition is still on disk and nothing new was written
        assert journal.read_text(encoding="utf-8") == pending
        assert (await c.call_tool(
            "memory_read", {"entry_id": anchor["id"]})).data["superseded_by"] is None


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


async def test_settings_tune_the_body_budget(tmp_path, project):
    server = build_server(root=tmp_path / "mem", project_dir=project,
                          settings=Settings(max_body_chars=10))
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "x" * 11, "type": "fact", "scope": "global"})).data
        assert "error" in r and "too large" in r["error"]
        ok = (await c.call_tool("memory_write", {
            "content": "short", "type": "fact", "scope": "global"})).data
        assert "id" in ok


async def test_settings_tune_the_index_budget(tmp_path, project):
    server = build_server(root=tmp_path / "mem", project_dir=project,
                          settings=Settings(index_budget_lines=1))
    async with Client(server) as c:
        for i in range(3):
            await c.call_tool("memory_write", {
                "content": f"budget entry number {i}", "type": "fact",
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
                "content": f"shared keyword body number {i}", "type": "fact",
                "scope": "global"})
        assert len((await c.call_tool("memory_search", {"query": "keyword"})).data) == 1
        assert len((await c.call_tool("memory_search",
                                      {"query": "keyword", "limit": 10})).data) == 2


async def test_search_limit_stays_a_plain_integer_in_the_tool_schema(server):
    async with Client(server) as c:
        schema = {t.name: t.inputSchema for t in await c.list_tools()}["memory_search"]
        limit = schema["properties"]["limit"]
        assert limit.get("type") == "integer" or {"type": "integer"} in limit.get("anyOf", [])
