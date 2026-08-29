import threading
import time

import pytest
from memriver_core.entry import Entry
from memriver_core.store import MemoryStore

SOURCE = {"harness": "test", "session": "s", "method": "explicit"}


def _e(body="内容", type="project", scope="global"):
    return Entry.new(body=body, type=type, scope=scope, source=SOURCE)


def test_write_and_read_roundtrip(store):
    e = _e()
    path = store.write(e)
    assert path.name == f"{e.id}.md" and "global/entries" in str(path)
    assert store.read(e.id) == e


def test_project_scope_directory(store):
    e = _e(scope="project:demo-abc123")
    path = store.write(e)
    assert "projects/demo-abc123/entries" in str(path)


def test_iter_filters_scope(store):
    a = _e(body="A"); b = _e(body="B", scope="project:p-000000")
    store.write(a); store.write(b)
    ids = {e.id for e in store.iter_entries(scopes=["global"])}
    assert ids == {a.id}
    all_ids = {e.id for e in store.iter_entries()}
    assert all_ids == {a.id, b.id}


def test_entry_whose_frontmatter_scope_contradicts_its_directory_is_skipped(store):
    # entry files are hand-editable: retagging one under global/entries as a
    # foreign project scope used to leak its body into every project's index,
    # because iter_entries selects by directory and never rechecks the metadata
    mine = _e(body="mine stays visible")
    retagged = _e(body="foreign project secret plan")
    store.write(mine)
    path = store.write(retagged)
    retagged.scope = "project:other-000000"
    path.write_text(retagged.to_markdown(), encoding="utf-8")

    assert {e.id for e in store.iter_entries(scopes=["global"])} == {mine.id}
    assert {e.id for e in store.iter_entries()} == {mine.id}


def test_read_refuses_entry_whose_frontmatter_scope_contradicts_its_directory(store):
    # _find resolves an id by globbing every project directory, so a direct read
    # used to trust the file's own frontmatter: a hand-edited file misplaced
    # under another project could be read across the boundary
    misplaced = _e(body="foreign project secret plan", scope="project:other-000000")
    path = store.write(misplaced)
    misplaced.scope = "global"
    path.write_text(misplaced.to_markdown(), encoding="utf-8")

    with pytest.raises(KeyError):
        store.read(misplaced.id)
    assert path.read_text(encoding="utf-8") == misplaced.to_markdown()


def test_read_missing_raises(store):
    with pytest.raises(KeyError):
        store.read("01UNKNOWNULID0000000000000")


def test_invalid_scope_slug_rejected(store):
    # scope slugs are untrusted input; path traversal must be rejected
    with pytest.raises(ValueError):
        store.write(_e(scope="project:../evil"))
    with pytest.raises(ValueError):
        store.write(_e(scope="project:"))


def test_read_rejects_malformed_ids(store):
    # entry ids are untrusted input; glob metacharacters and traversal must not resolve
    e = _e()
    store.write(e)
    for bad in ("*", "../../../outside", ""):
        with pytest.raises(KeyError):
            store.read(bad)


def test_read_scoped_to_given_scopes(tmp_path):
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="a", type="project", scope="project:alpha-000000",
                          source={}, id="shared-name"))
    store.write(Entry.new(body="b", type="project", scope="project:beta-000000",
                          source={}, id="shared-name"))
    e = store.read("shared-name", scopes=["project:beta-000000"])
    assert e.body == "b"
    with pytest.raises(KeyError):
        store.read("shared-name", scopes=["global"])


def test_find_accepts_slug_and_ulid_shapes(tmp_path):
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="a", type="user", scope="global",
                          source={}, id="my-slug"))
    ulid_entry = Entry.new(body="b", type="user", scope="global", source={})
    store.write(ulid_entry)
    assert store.read("my-slug").body == "a"
    assert store.read(ulid_entry.id).body == "b"
    with pytest.raises(KeyError):
        store.read("../escape")
    with pytest.raises(KeyError):
        store.read("Bad_Name")


def test_update_body_rewrites_in_place(tmp_path):
    store = MemoryStore(tmp_path)
    e = Entry.new(body="old", type="user", scope="global", source={}, id="n")
    store.write(e)
    updated = store.update_body("n", "new", scopes=["global"])
    assert updated.id == "n" and updated.body == "new"
    assert updated.updated >= e.updated
    again = store.read("n")
    assert again.body == "new" and again.created == e.created
    assert len(list(store.iter_entries(scopes=["global"]))) == 1


def test_delete_removes_file(tmp_path):
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="b", type="user", scope="global", source={}, id="n"))
    store.delete("n", scopes=["global"])
    with pytest.raises(KeyError):
        store.read("n")
    with pytest.raises(KeyError):
        store.delete("n", scopes=["global"])


def test_delete_refuses_a_hand_written_non_entry_file(tmp_path):
    # a hand-written note whose filename happens to match the slug shape must
    # not be unlinked just because its name resolves: delete() has to parse it
    # like read() does, and refuse instead of deleting an unparseable file
    store = MemoryStore(tmp_path)
    d = tmp_path / "global" / "entries"
    d.mkdir(parents=True)
    path = d / "notes.md"
    path.write_text("just some hand-written notes\n", encoding="utf-8")

    with pytest.raises(KeyError):
        store.delete("notes", scopes=["global"])
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "just some hand-written notes\n"


def test_exists(tmp_path):
    store = MemoryStore(tmp_path)
    assert not store.exists("n", scopes=["global"])
    store.write(Entry.new(body="b", type="user", scope="global", source={}, id="n"))
    assert store.exists("n", scopes=["global"])


def test_update_body_serializes_concurrent_writers(tmp_path):
    # instrument the critical section directly: end-state assertions alone
    # can't discriminate a missing lock, because _atomic_write's mkstemp +
    # os.replace already guarantees a single clean winner either way. Count
    # concurrent entries into _atomic_write instead -- with the store-wide
    # flock held for the whole read-modify-write, only one thread can ever
    # be inside it at a time.
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="base", type="user", scope="global", source={}, id="n"))
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    real_write = store._atomic_write

    def observed_write(path, text):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)  # widen the window so an unserialized peer overlaps
        try:
            real_write(path, text)
        finally:
            with counter_lock:
                active -= 1

    store._atomic_write = observed_write
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def attempt(marker: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.update_body("n", marker, scopes=["global"])
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=attempt, args=(m,)) for m in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert errors == []
    assert max_active == 1  # flock serialized the two critical sections
    assert store.read("n").body in {"a", "b"}
