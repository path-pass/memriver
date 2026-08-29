import threading

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


def test_supersede_of_superseded_entry_raises(store):
    # the server pre-checks this, but a second process can pass that pre-check
    # before the first writes: the store itself must be the final guard
    a = _e(); store.write(a)
    b = _e(body="v2"); store.supersede(a.id, b)
    c = _e(body="v3")
    with pytest.raises(ValueError, match=b.id):
        store.supersede(a.id, c)
    assert not (store.root / "global" / "entries" / f"{c.id}.md").exists()


def test_concurrent_supersede_has_exactly_one_winner(store):
    # both threads clear the (nonexistent) pre-check together; the file lock plus
    # the in-lock re-read must let exactly one of them win
    a = _e(); store.write(a)
    barrier = threading.Barrier(2)
    results: list[Exception | None] = []
    lock = threading.Lock()

    def attempt() -> None:
        new = _e(body="rival")
        barrier.wait()
        try:
            store.supersede(a.id, new)
            outcome: Exception | None = None
        except Exception as err:
            outcome = err
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert len(results) == 2
    winners = [r for r in results if r is None]
    losers = [r for r in results if isinstance(r, ValueError)]
    assert len(winners) == 1 and len(losers) == 1


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
