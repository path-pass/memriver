import inspect
import os
import re
import stat
from datetime import datetime
from pathlib import Path

import pytest
from memriver_core.application.errors import (
    GlobalReadOnly,
    InvalidScope,
    MemoryNotFound,
    NameTaken,
    StorageFailure,
    UnreadableMemory,
)
from memriver_core.models import AccessContext, Memory, ProjectId, Scope
from memriver_core.repository.filesystem import FileMemoryRepository
from memriver_core.repository.filesystem.markdown_codec import encode
from memriver_core.repository.protocol import MemoryRepository

SOURCE = {"harness": "test", "session": "s", "method": "explicit"}

MINE = ProjectId("mine-000000")
OTHER = ProjectId("other-000000")
GLOBAL = Scope.global_()
MINE_SCOPE = Scope.project(MINE)
# CTX's own entries directory, and the scope line encode() writes into them
MINE_ENTRIES = Path("projects") / MINE / "entries"
MINE_SCOPE_LINE = f"scope: {MINE_SCOPE.to_storage()}"

CTX = AccessContext(project_id=MINE)
OTHER_CTX = AccessContext(project_id=OTHER)


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "mem"


@pytest.fixture
def memory_repository(root) -> FileMemoryRepository:
    return FileMemoryRepository(root)


def _m(body="内容", type="project", scope=MINE_SCOPE, id=None, description=""):
    return Memory.new(body=body, type=type, scope=scope, source=SOURCE, id=id,
                      description=description)


def _write_raw(root: Path, rel: str, text: str) -> Path:
    """Plant a file in CTX's project entries directory, bypassing the repository."""
    path = root / MINE_ENTRIES / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _plant_global(root: Path, memory: Memory) -> Path:
    """Write a global entry behind the repository's back.

    Global is read-only through the port, so a test that needs a global entry
    on disk cannot create one -- it writes the document itself, exactly as a
    hand-editing user (or a future reviewed maintenance step) would.
    """
    path = root / "global" / "entries" / f"{memory.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encode(memory), encoding="utf-8")
    return path


def _entry_path(root: Path, memory: Memory) -> Path:
    scope_dir = ("global" if memory.scope.project_id is None
                 else f"projects/{memory.scope.project_id}")
    return root / scope_dir / "entries" / f"{memory.id}.md"


def test_signatures_match_the_repository_protocol(memory_repository):
    for name in MemoryRepository.__protocol_attrs__:
        port = inspect.signature(getattr(MemoryRepository, name))
        port = port.replace(parameters=list(port.parameters.values())[1:])
        assert inspect.signature(getattr(memory_repository, name)) == port, name


# --- storage layout ---

def test_create_of_global_is_refused_and_creates_no_global_directory(memory_repository, root):
    # the refusal has to land before any directory work: a store that grew an
    # empty global/entries on every rejected write would advertise a write path
    # the port does not have
    with pytest.raises(GlobalReadOnly):
        memory_repository.create(_m(scope=GLOBAL), CTX)
    assert not (root / "global").exists()


def test_create_writes_into_the_project_entries_directory(memory_repository, root):
    m = _m()
    memory_repository.create(m, CTX)
    path = _entry_path(root, m)
    assert path.exists()
    assert memory_repository.get(m.id, CTX) == m


def test_project_scope_gets_its_own_directory(memory_repository, root):
    m = _m(scope=Scope.project(ProjectId("demo-abc123")))
    memory_repository.create(m, AccessContext(project_id=ProjectId("demo-abc123")))
    assert (root / "projects" / "demo-abc123" / "entries" / f"{m.id}.md").exists()


def test_created_directories_are_private_to_the_owner(memory_repository, root):
    # memory filenames are semantic now; a world/group-readable directory
    # lets other local users enumerate them by listing, even without read
    # access to the file contents themselves. Only the mode bits are read
    # here, and those are reported the same to every uid -- no skip for root.
    m = _m(scope=Scope.project(ProjectId("demo-abc123")))
    memory_repository.create(m, AccessContext(project_id=ProjectId("demo-abc123")))
    # the root itself is created by store_lock, the levels below it by
    # _mkdir_private; both have to lock the directory down
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    made_dirs = [p for p in root.glob("**/*") if p.is_dir()]
    assert made_dirs, "expected create() to have made at least one directory"
    for d in made_dirs:
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


@pytest.mark.parametrize("existing_mode", [0o755, 0o711])
def test_only_the_levels_a_write_creates_are_made_private(
        memory_repository, root, existing_mode):
    # the flip side of the guarantee above: a level that was already there is
    # the user's, not the store's. A shared checkout, or a root deliberately
    # relaxed (or locked down, as the permission tests below do), must survive
    # a write untouched -- only the descendants this write brings into
    # existence are forced to 0700.
    (root / "projects").mkdir(parents=True)
    (root / "projects").chmod(existing_mode)
    root.chmod(existing_mode)

    m = _m(scope=Scope.project(ProjectId("demo-abc123")))
    memory_repository.create(m, AccessContext(project_id=ProjectId("demo-abc123")))

    assert stat.S_IMODE(root.stat().st_mode) == existing_mode
    assert stat.S_IMODE((root / "projects").stat().st_mode) == existing_mode
    for created in (root / "projects" / "demo-abc123",
                    root / "projects" / "demo-abc123" / "entries"):
        assert stat.S_IMODE(created.stat().st_mode) == 0o700, created


def test_invalid_project_slug_rejected(memory_repository, root):
    # slugs reach the adapter as untrusted input; path traversal must be
    # rejected. The context carries the same evil slug here, so the scope
    # binding check lets it through and the path builder is the one that
    # has to refuse it.
    evil = Scope.project(ProjectId("../evil"))
    with pytest.raises(ValueError):
        memory_repository.create(_m(scope=evil), AccessContext(project_id=ProjectId("../evil")))
    assert list(root.glob("**/*.md")) == []
    # from an ordinary context the scope is not writable at all, so the
    # traversal never even reaches path construction
    with pytest.raises(InvalidScope):
        memory_repository.create(_m(scope=evil), CTX)
    assert list(root.glob("**/*.md")) == []


def test_create_rejects_path_traversal_id(memory_repository, root):
    # lookups validate ids with ID_RE before globbing; create must validate too,
    # or a core-API consumer's id="../../../outside" escapes the store root
    # when the entry path interpolates it straight into the filesystem path
    with pytest.raises(ValueError):
        memory_repository.create(_m(id="../../../outside"), CTX)
    assert list(root.glob("**/*.md")) == []


def test_get_rejects_malformed_ids(memory_repository):
    # memory ids are untrusted input; glob metacharacters and traversal must not resolve
    memory_repository.create(_m(), CTX)
    for bad in ("*", "../../../outside", ""):
        with pytest.raises(MemoryNotFound):
            memory_repository.get(bad, CTX)


def test_lookup_accepts_slug_and_ulid_shapes(memory_repository):
    memory_repository.create(_m(body="a", type="user", id="my-slug"), CTX)
    ulid_memory = _m(body="b", type="user")
    memory_repository.create(ulid_memory, CTX)
    assert memory_repository.get("my-slug", CTX).body == "a"
    assert memory_repository.get(ulid_memory.id, CTX).body == "b"
    with pytest.raises(MemoryNotFound):
        memory_repository.get("../escape", CTX)
    with pytest.raises(MemoryNotFound):
        memory_repository.get("Bad_Name", CTX)


# --- directory is truth ---

def test_get_refuses_memory_whose_frontmatter_scope_contradicts_its_directory(
        memory_repository, root):
    # _find resolves an id by globbing every project directory, so a direct get
    # would otherwise trust the file's own frontmatter: a hand-edited file
    # misplaced under another project could be read across the boundary
    misplaced = _m(body="foreign project secret plan", scope=Scope.project(OTHER))
    memory_repository.create(misplaced, OTHER_CTX)
    path = _entry_path(root, misplaced)
    misplaced.scope = Scope.global_()
    path.write_text(encode(misplaced), encoding="utf-8")

    with pytest.raises(MemoryNotFound):
        memory_repository.get(misplaced.id, OTHER_CTX)
    assert path.read_text(encoding="utf-8") == encode(misplaced)


def test_get_refuses_memory_whose_frontmatter_id_contradicts_its_filename(memory_repository, root):
    # the filename is the identity: a hand-edited frontmatter `id` that no
    # longer matches foo.md must not be trusted, or update_body would later
    # write under the declared id (bar.md) while foo.md sits untouched --
    # potentially clobbering an unrelated existing memory named "bar"
    mine = _m(body="original body")
    memory_repository.create(mine, CTX)
    path = _entry_path(root, mine)
    mine.id = "bar"
    path.write_text(encode(mine), encoding="utf-8")

    with pytest.raises(MemoryNotFound):
        memory_repository.get(path.stem, CTX)
    assert path.read_text(encoding="utf-8") == encode(mine)


def test_get_refuses_a_file_whose_stored_scope_does_not_parse_as_absent(memory_repository, root):
    # an ungrammatical scope string cannot equal any directory's scope, so it
    # is the same scope mismatch as above -- not found, not "unreadable"
    path = _write_raw(root, "n.md",
                      encode(_m(body="hand-edited", id="n")).replace(
                          MINE_SCOPE_LINE, "scope: nonsense"))
    with pytest.raises(MemoryNotFound):
        memory_repository.get("n", CTX)
    assert "nonsense" in path.read_text(encoding="utf-8")


def test_iter_visible_skips_a_file_whose_stored_scope_does_not_parse(memory_repository, root):
    mine = _m(body="mine stays visible")
    memory_repository.create(mine, CTX)
    _write_raw(root, "n.md",
               encode(_m(body="hand-edited", id="n")).replace(
                   MINE_SCOPE_LINE, "scope: nonsense"))
    assert {m.id for m in memory_repository.iter_visible(CTX)} == {mine.id}


def test_create_refuses_when_name_taken_by_an_unparsable_scope_file(memory_repository, root):
    # the name is taken by a file get reports as absent; the collision check
    # must still refuse it rather than clobbering a hand-edited file
    path = _write_raw(root, "n.md",
                      encode(_m(body="hand-edited", id="n")).replace(
                          MINE_SCOPE_LINE, "scope: nonsense"))
    before = path.read_bytes()
    with pytest.raises(UnreadableMemory):
        memory_repository.create(_m(body="v1", type="user", id="n"), CTX)
    assert path.read_bytes() == before


def test_get_refuses_an_undecodable_file_as_unreadable(memory_repository, root):
    _write_raw(root, "notes.md", "just some hand-written notes\n")
    with pytest.raises(UnreadableMemory):
        memory_repository.get("notes", CTX)


def test_iter_visible_skips_a_scope_mismatched_file(memory_repository, root):
    # entry files are hand-editable: retagging one under global/entries as a
    # foreign project scope used to leak its body into every project's index,
    # because the walk selects by directory and never rechecks the metadata
    mine = _m(body="mine stays visible")
    retagged = _m(body="foreign project secret plan")
    memory_repository.create(mine, CTX)
    memory_repository.create(retagged, CTX)
    path = _entry_path(root, retagged)
    retagged.scope = Scope.project(OTHER)
    path.write_text(encode(retagged), encoding="utf-8")

    assert {m.id for m in memory_repository.iter_visible(CTX)} == {mine.id}


def test_iter_visible_skips_an_id_mismatched_file(memory_repository, root):
    mine = _m(body="mine stays visible")
    retagged = _m(body="id no longer matches filename")
    memory_repository.create(mine, CTX)
    memory_repository.create(retagged, CTX)
    path = _entry_path(root, retagged)
    retagged.id = "some-other-id"
    path.write_text(encode(retagged), encoding="utf-8")

    assert {m.id for m in memory_repository.iter_visible(CTX)} == {mine.id}


def test_iter_visible_skips_an_undecodable_file(memory_repository, root):
    # the store is hand-editable and users may drop their own notes next to
    # entries: one broken file must never break a traversal
    mine = _m(body="mine stays visible")
    memory_repository.create(mine, CTX)
    _write_raw(root, "notes.md", "just some hand-written notes\n")
    assert {m.id for m in memory_repository.iter_visible(CTX)} == {mine.id}


def test_update_body_refuses_when_id_contradicts_filename(memory_repository, root):
    mine = _m(body="original body")
    memory_repository.create(mine, CTX)
    path = _entry_path(root, mine)
    mine.id = "bar"
    path.write_text(encode(mine), encoding="utf-8")

    with pytest.raises(MemoryNotFound):
        memory_repository.update_body(path.stem, "hijacked", CTX)
    assert not (path.parent / "bar.md").exists()
    assert path.read_text(encoding="utf-8") == encode(mine)


def test_delete_refuses_a_hand_written_non_entry_file(memory_repository, root):
    # a hand-written note whose filename happens to match the slug shape must
    # not be unlinked just because its name resolves: delete has to decode it
    # like get does, and refuse instead of deleting an unparseable file
    path = _write_raw(root, "notes.md", "just some hand-written notes\n")
    with pytest.raises(UnreadableMemory):
        memory_repository.delete("notes", CTX)
    assert path.read_text(encoding="utf-8") == "just some hand-written notes\n"


# --- create collisions against files on disk ---

def test_create_refuses_to_clobber_a_hand_written_non_entry_file(memory_repository, root):
    # the collision check must fail closed on a file it cannot decode, not
    # treat the name as free and let the atomic write overwrite it
    path = _write_raw(root, "notes.md", "just some hand-written notes\n")
    before = path.read_bytes()
    with pytest.raises(UnreadableMemory):
        memory_repository.create(_m(body="v1", type="user", id="notes"), CTX)
    assert path.read_bytes() == before


def test_create_refuses_clobber_when_name_equals_missing_frontmatter_key(memory_repository, root):
    # decode looks up frontmatter keys by name (m["source"], ...); a
    # hand-written file missing exactly the key that happens to match the
    # proposed name used to raise a bare KeyError indistinguishable from
    # "name not found" and get silently clobbered
    path = _write_raw(root, "source.md", "---\n"
                      "id: source\n"
                      "type: user\n"
                      f"{MINE_SCOPE_LINE}\n"
                      "sync: true\n"
                      "created: 2026-08-29T10:00:00Z\n"
                      "updated: 2026-08-29T10:00:00Z\n"
                      "trust: agent\n"
                      "---\n\n"
                      "hand-written, missing the source: key\n")
    before = path.read_bytes()
    with pytest.raises(UnreadableMemory):
        memory_repository.create(_m(body="v1", type="user", id="source"), CTX)
    assert path.read_bytes() == before


def test_create_refuses_when_name_taken_by_scope_mismatched_file(memory_repository, root):
    # get treats a file whose frontmatter scope contradicts its directory as
    # missing (directory is truth), so a collision check built only on get
    # would conclude the name is free and let the atomic write replace a file
    # the user may have hand-edited
    mismatched = _m(body="hand-edited, wrong scope for its directory", type="user",
                    scope=Scope.project(ProjectId("elsewhere-000000")), id="n")
    path = _write_raw(root, "n.md", encode(mismatched))
    before = path.read_bytes()
    with pytest.raises(UnreadableMemory):
        memory_repository.create(_m(body="v1", type="user", id="n"), CTX)
    assert path.read_bytes() == before


def test_create_refuses_when_name_taken_by_id_mismatched_file(memory_repository, root):
    # get treats a file whose frontmatter id contradicts its own filename as
    # missing (filename is truth); the collision check must still refuse the
    # name rather than concluding it is free and creating a second file
    # (bar.md) while foo.md is untouched
    mismatched = _m(body="hand-edited, id no longer matches filename", type="user",
                    id="foo")
    memory_repository.create(mismatched, CTX)
    path = _entry_path(root, mismatched)
    mismatched.id = "bar"
    path.write_text(encode(mismatched), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(UnreadableMemory):
        memory_repository.create(_m(body="v1", type="user", id="foo"), CTX)
    assert path.read_bytes() == before
    assert not (root / MINE_ENTRIES / "bar.md").exists()


def test_same_scope_collision_echoes_the_existing_memory(memory_repository):
    first = _m(body="v1", type="user", id="n", description="original cue")
    memory_repository.create(first, CTX)
    with pytest.raises(NameTaken) as err:
        memory_repository.create(_m(body="v2", type="user", id="n"), CTX)
    # fields only: the client-visible wording is the transport's to write
    assert err.value.memory_id == "n"
    assert err.value.existing == first


def test_project_create_refused_when_a_global_entry_holds_the_name(memory_repository, root):
    # global is read-only, not invisible: it is in every context's visible
    # scopes, so a hand-planted global name still reserves that name -- and the
    # refusal must leave both the global document and the project directory as
    # they were
    path = _plant_global(root, _m(body="curated global fact", type="user",
                                  scope=GLOBAL, id="n"))
    before = path.read_bytes()

    with pytest.raises(NameTaken) as err:
        memory_repository.create(_m(body="v2", type="user", id="n"), CTX)
    assert err.value.memory_id == "n"
    assert err.value.existing is not None and err.value.existing.scope == GLOBAL
    assert path.read_bytes() == before
    assert list(root.glob(f"{MINE_ENTRIES}/*.md")) == []


def test_project_create_ignores_a_foreign_projects_name(memory_repository, root):
    memory_repository.create(_m(body="a", scope=Scope.project(OTHER), id="shared-name"), OTHER_CTX)
    memory_repository.create(_m(body="b", scope=Scope.project(MINE), id="shared-name"), CTX)
    assert memory_repository.get("shared-name", CTX).body == "b"


# --- the global scope is read-only, down to the bytes ---

def _snapshot(storage_dir: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(storage_dir)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in storage_dir.rglob("*") if p.is_file() and p.name != ".lock"}


def test_global_refusals_leave_the_store_byte_identical(tmp_path):
    # the contract file can only see that the refusals raise; here the store is
    # inspectable, so "no change was made" is checked as the bytes and mtimes
    # it claims to be -- a rewrite with identical content would still fail this
    d = tmp_path / "global" / "entries"
    d.mkdir(parents=True)
    g = Memory.new(body="drinks oolong", type="user", scope=Scope.global_(), source=SOURCE,
                   id="tea")
    (d / "tea.md").write_text(encode(g), encoding="utf-8")
    memory_repository = FileMemoryRepository(tmp_path)
    ctx = AccessContext(project_id=ProjectId("mine-000000"))
    before = _snapshot(tmp_path)
    for call in (lambda: memory_repository.create(
                     Memory.new(body="x", type="user", scope=Scope.global_(), source=SOURCE,
                                id="new"), ctx),
                 lambda: memory_repository.update_body("tea", "x", ctx),
                 lambda: memory_repository.delete("tea", ctx)):
        with pytest.raises(GlobalReadOnly):
            call()
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "global" / "entries" / "new.md").exists()


# --- storage failures ---

def _assert_opaque(err: StorageFailure, root: Path) -> None:
    message = str(err)
    assert str(root) not in message
    assert "Errno" not in message and "No such file" not in message


def test_create_failure_raises_an_opaque_storage_failure(memory_repository, root):
    # a file where the entries directory belongs: mkdir fails with OSError
    (root / MINE_ENTRIES).parent.mkdir(parents=True)
    (root / MINE_ENTRIES).write_text("not a directory", encoding="utf-8")
    with pytest.raises(StorageFailure) as err:
        memory_repository.create(_m(id="n"), CTX)
    _assert_opaque(err.value, root)


def test_read_failure_raises_an_opaque_storage_failure(memory_repository, root):
    # a directory named like an entry file: reading it fails with OSError
    (root / MINE_ENTRIES / "n.md").mkdir(parents=True)
    with pytest.raises(StorageFailure) as err:
        memory_repository.get("n", CTX)
    _assert_opaque(err.value, root)


def test_delete_failure_raises_an_opaque_storage_failure(memory_repository, root, monkeypatch):
    memory_repository.create(_m(id="n"), CTX)

    def boom(self, *args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", boom)
    with pytest.raises(StorageFailure) as err:
        memory_repository.delete("n", CTX)
    _assert_opaque(err.value, root)


# --- lock lifecycle failures (I-1) ---
#
# store_lock's own mkdir/open/flock happen before any repository-level
# try/except: a raw OSError there must not escape create/update_body/delete
# any more than one from _read/_write/unlink does.

def test_root_is_a_regular_file_raises_opaque_storage_failure(tmp_path):
    # store_lock's root.mkdir(exist_ok=True) raises FileExistsError when the
    # root path is already occupied by a plain file, not a directory
    root = tmp_path / "blocked"
    root.write_text("not a directory", encoding="utf-8")
    memory_repository = FileMemoryRepository(root)

    with pytest.raises(StorageFailure) as err:
        memory_repository.create(_m(id="n"), CTX)
    _assert_opaque(err.value, root)

    with pytest.raises(StorageFailure) as err:
        memory_repository.update_body("n", "v2", CTX)
    _assert_opaque(err.value, root)

    with pytest.raises(StorageFailure) as err:
        memory_repository.delete("n", CTX)
    _assert_opaque(err.value, root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_root_raises_opaque_storage_failure(memory_repository, root):
    # root exists but cannot be entered: store_lock's open(root / ".lock")
    # fails with PermissionError before create/update_body/delete ever reach
    # their own try/except
    memory_repository.create(_m(id="n"), CTX)
    root.chmod(0o000)
    try:
        with pytest.raises(StorageFailure) as err:
            memory_repository.create(_m(id="m"), CTX)
        _assert_opaque(err.value, root)

        with pytest.raises(StorageFailure) as err:
            memory_repository.update_body("n", "v2", CTX)
        _assert_opaque(err.value, root)

        with pytest.raises(StorageFailure) as err:
            memory_repository.delete("n", CTX)
        _assert_opaque(err.value, root)
    finally:
        root.chmod(0o755)


def test_flock_failure_raises_opaque_storage_failure(memory_repository, root, monkeypatch):
    import fcntl

    def boom(*args, **kwargs):
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(fcntl, "flock", boom)
    with pytest.raises(StorageFailure) as err:
        memory_repository.create(_m(id="n"), CTX)
    _assert_opaque(err.value, root)


def test_lock_lifecycle_failure_does_not_mask_domain_errors(memory_repository):
    # negative control: a plain lock (no injected OSError) still lets a real
    # collision raise NameTaken, not StorageFailure
    memory_repository.create(_m(body="v1", type="user", id="n"), CTX)
    with pytest.raises(NameTaken):
        memory_repository.create(_m(body="v2", type="user", id="n"), CTX)


# --- timestamp normalization / ordering (fix 3+4) ---

def test_project_slug_named_global_is_not_confused_with_the_global_scope(
        memory_repository, root):
    # `_dir_scope` checked parent.name == "global" before parent.parent.name
    # == "projects", so projects/global/entries misjudged itself as the
    # global scope directory -- a scope mismatch against its own frontmatter,
    # and the entry vanished from get()/iter_visible(). project_slug always
    # appends a -<6hex> suffix, so only the core API can hit this directly.
    project_global = ProjectId("global")
    ctx = AccessContext(project_id=project_global)
    m = _m(scope=Scope.project(project_global), id="proj-named-global")
    memory_repository.create(m, ctx)
    assert memory_repository.get("proj-named-global", ctx) == m
    assert m.id in [visible.id for visible in memory_repository.iter_visible(ctx)]
    assert (root / "projects" / "global" / "entries" / f"{m.id}.md").exists()


def test_a_store_root_named_projects_keeps_global_entries_addressable(tmp_path):
    # `_dir_scope` matched on basenames, so with a root like ~/projects the
    # global directory read as <root:projects>/global/entries and answered
    # Scope.project("global") -- every global entry then failed the
    # directory-is-truth check and disappeared from get()/iter_visible().
    root = tmp_path / "projects"
    repository = FileMemoryRepository(root)
    m = _m(scope=GLOBAL, id="global-under-projects-root")
    _plant_global(root, m)
    assert repository.get(m.id, CTX) == m
    assert [visible.id for visible in repository.iter_visible(CTX)] == [m.id]


def test_iter_visible_skips_entries_whose_id_fails_id_re(memory_repository, root, caplog):
    # iter_visible's stem check let a file like Bad_Name.md into
    # memory_index/search, while get()/update_body()/delete() already refuse
    # it via _find's ID_RE guard -- disagreeing about the same file's
    # existence. This file's own id/stem agree with each other (only ID_RE
    # rejects the shape), so this exercises a fresh gap, not the id-stem
    # mismatch case above.
    _write_raw(root, "Bad_Name.md", encode(_m(id="Bad_Name")))
    with caplog.at_level("WARNING"):
        visible_ids = [m.id for m in memory_repository.iter_visible(CTX)]
    assert "Bad_Name" not in visible_ids
    assert memory_repository.search("Bad_Name", CTX, 5) == []
    assert any("Bad_Name" in rec.message for rec in caplog.records)


def test_two_updates_within_one_clock_tick_still_advance_updated(
        memory_repository, monkeypatch):
    # a coarse clock, or two updates inside one tick, must not leave two
    # revisions sharing an `updated`: freshness ordering (search, dream) would
    # then fall back to the id tiebreak and report the older body as newer
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 29, 10, 0, 0, tzinfo=tz)

    monkeypatch.setattr("memriver_core.models.memory.datetime", FrozenClock)
    m = _m(id="ticking")
    memory_repository.create(m, CTX)

    first = memory_repository.update_body(m.id, "v2", CTX).updated
    second = memory_repository.update_body(m.id, "v3", CTX).updated
    assert m.updated < first < second
    # ...and each one is still the canonical form the sort key relies on
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", stamp)
               for stamp in (first, second))
    assert memory_repository.get(m.id, CTX).updated == second


def test_hand_edited_naive_timestamp_sorts_correctly_among_server_written(
        memory_repository, root):
    # a hand-edited, unquoted "updated:" timestamp parses as a naive
    # datetime; without canonicalizing it to the server's UTC "T...ffffffZ"
    # form, str()'s "YYYY-MM-DD HH:MM:SS" (space, no offset) can sort out of
    # place against the server's own microsecond-resolution strings
    old = _m(id="old", body="ordering fact old")
    old.updated = "2026-01-01T00:00:00.000000Z"
    memory_repository.create(old, CTX)

    _write_raw(root, "hand.md",
               "---\n"
               "id: hand\n"
               "type: user\n"
               f"{MINE_SCOPE_LINE}\n"
               "sync: true\n"
               "created: 2026-06-01T00:00:00\n"
               "updated: 2026-06-01T00:00:00\n"
               "source: {}\n"
               "trust: agent\n"
               "description: ''\n"
               "---\n\nordering fact hand\n")

    new = _m(id="new", body="ordering fact new")
    new.updated = "2026-08-01T00:00:00.000000Z"
    memory_repository.create(new, CTX)

    assert [h.id for h in memory_repository.search("ordering fact", CTX, 5)] == [
        "new", "hand", "old"]
