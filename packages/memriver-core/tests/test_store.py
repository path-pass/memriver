import threading

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


def test_exists(tmp_path):
    store = MemoryStore(tmp_path)
    assert not store.exists("n", scopes=["global"])
    store.write(Entry.new(body="b", type="user", scope="global", source={}, id="n"))
    assert store.exists("n", scopes=["global"])


def test_update_body_serializes_concurrent_writers(tmp_path):
    # each thread opens .lock separately, so flock genuinely serializes the two
    # in-process calls; without it a torn write could corrupt the file or leave
    # a stray temp file behind instead of exactly one clean entry
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="base", type="user", scope="global", source={}, id="n"))
    barrier = threading.Barrier(2)
    markers = ["marker-A", "marker-B"]
    errors: list[Exception] = []

    def attempt(marker: str) -> None:
        barrier.wait()
        try:
            store.update_body("n", marker, scopes=["global"])
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=attempt, args=(m,)) for m in markers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert errors == []
    assert store.read("n").body in markers
    assert len(list(store.iter_entries(scopes=["global"]))) == 1
