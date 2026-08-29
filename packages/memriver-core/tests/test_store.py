import pytest
from memriver_core.entry import Entry

SOURCE = {"harness": "test", "session": "s", "method": "explicit"}


def _e(body="内容", type="fact", scope="global"):
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


def test_iter_filters_scope_and_superseded(store):
    a = _e(body="A"); b = _e(body="B", scope="project:p-000000")
    store.write(a); store.write(b)
    ids = {e.id for e in store.iter_entries(scopes=["global"])}
    assert ids == {a.id}
    new = _e(body="A v2")
    store.supersede(a.id, new)
    active = {e.id for e in store.iter_entries(scopes=["global"])}
    assert active == {new.id}
    all_ids = {e.id for e in store.iter_entries(scopes=["global"], include_superseded=True)}
    assert all_ids == {a.id, new.id}


def test_supersede_marks_old(store):
    a = _e(); store.write(a)
    new = _e(body="v2")
    store.supersede(a.id, new)
    old = store.read(a.id)
    assert old.superseded_by == new.id and old.updated >= old.created


def test_read_missing_raises(store):
    with pytest.raises(KeyError):
        store.read("01UNKNOWNULID0000000000000")


def test_invalid_scope_slug_rejected(store):
    # scope slugs are untrusted input; path traversal must be rejected
    with pytest.raises(ValueError):
        store.write(_e(scope="project:../evil"))
    with pytest.raises(ValueError):
        store.write(_e(scope="project:"))
