import inspect
from pathlib import Path

import pytest
from memriver_core.application.errors import (
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

CTX = AccessContext(project_id=MINE)
OTHER_CTX = AccessContext(project_id=OTHER)


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "mem"


@pytest.fixture
def memory_repository(root) -> FileMemoryRepository:
    return FileMemoryRepository(root)


def _m(body="内容", type="project", scope=GLOBAL, id=None, description=""):
    return Memory.new(body=body, type=type, scope=scope, source=SOURCE, id=id,
                      description=description)


def _write_raw(root: Path, rel: str, text: str) -> Path:
    path = root / "global" / "entries" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
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

def test_create_writes_into_the_global_entries_directory(memory_repository, root):
    m = _m()
    memory_repository.create(m, CTX)
    path = _entry_path(root, m)
    assert path.exists()
    assert memory_repository.get(m.id, CTX) == m


def test_project_scope_gets_its_own_directory(memory_repository, root):
    m = _m(scope=Scope.project(ProjectId("demo-abc123")))
    memory_repository.create(m, AccessContext(project_id=ProjectId("demo-abc123")))
    assert (root / "projects" / "demo-abc123" / "entries" / f"{m.id}.md").exists()


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
                          "scope: global", "scope: nonsense"))
    with pytest.raises(MemoryNotFound):
        memory_repository.get("n", CTX)
    assert "nonsense" in path.read_text(encoding="utf-8")


def test_iter_visible_skips_a_file_whose_stored_scope_does_not_parse(memory_repository, root):
    mine = _m(body="mine stays visible")
    memory_repository.create(mine, CTX)
    _write_raw(root, "n.md",
               encode(_m(body="hand-edited", id="n")).replace(
                   "scope: global", "scope: nonsense"))
    assert {m.id for m in memory_repository.iter_visible(CTX)} == {mine.id}


def test_create_refuses_when_name_taken_by_an_unparsable_scope_file(memory_repository, root):
    # the name is taken by a file get reports as absent; the collision check
    # must still refuse it rather than clobbering a hand-edited file
    path = _write_raw(root, "n.md",
                      encode(_m(body="hand-edited", id="n")).replace(
                          "scope: global", "scope: nonsense"))
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
                      "scope: global\n"
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
    assert not (root / "global" / "entries" / "bar.md").exists()


def test_same_scope_collision_echoes_the_existing_memory(memory_repository):
    first = _m(body="v1", type="user", id="n", description="original cue")
    memory_repository.create(first, CTX)
    with pytest.raises(NameTaken) as err:
        memory_repository.create(_m(body="v2", type="user", id="n"), CTX)
    assert err.value.existing == first
    assert "already exists" in str(err.value)


def test_global_create_refused_when_a_foreign_project_holds_the_name(memory_repository, root):
    # a global name must never shadow, or claim, a name any project already
    # uses -- so a global create checks every scope in the store, and the
    # refusal must not echo that project's content or type back
    foreign = _m(body="foreign project secret plan", scope=Scope.project(OTHER),
                 id="n")
    memory_repository.create(foreign, OTHER_CTX)
    path = _entry_path(root, foreign)
    before = path.read_bytes()

    with pytest.raises(NameTaken) as err:
        memory_repository.create(_m(body="v2", type="user", id="n"), CTX)
    assert err.value.existing is None
    assert "used elsewhere in the store" in str(err.value)
    assert "secret plan" not in str(err.value)
    assert path.read_bytes() == before
    assert list(root.glob("global/entries/*.md")) == []


def test_project_create_ignores_a_foreign_projects_name(memory_repository, root):
    memory_repository.create(_m(body="a", scope=Scope.project(OTHER), id="shared-name"), OTHER_CTX)
    memory_repository.create(_m(body="b", scope=Scope.project(MINE), id="shared-name"), CTX)
    assert memory_repository.get("shared-name", CTX).body == "b"


# --- storage failures ---

def _assert_opaque(err: StorageFailure, root: Path) -> None:
    message = str(err)
    assert str(root) not in message
    assert "Errno" not in message and "No such file" not in message


def test_create_failure_raises_an_opaque_storage_failure(memory_repository, root):
    # a file where the entries directory belongs: mkdir fails with OSError
    (root / "global").mkdir(parents=True)
    (root / "global" / "entries").write_text("not a directory", encoding="utf-8")
    with pytest.raises(StorageFailure) as err:
        memory_repository.create(_m(id="n"), CTX)
    _assert_opaque(err.value, root)


def test_read_failure_raises_an_opaque_storage_failure(memory_repository, root):
    # a directory named like an entry file: reading it fails with OSError
    (root / "global" / "entries" / "n.md").mkdir(parents=True)
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
