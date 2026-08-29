import threading

import pytest
from memriver_core.entry import Entry
from memriver_core.index_fts import FtsIndex
from memriver_core.render import render_index

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


def test_recovery_completes_supersede_interrupted_between_writes(store):
    # a crash between "write the new entry" and "mark the old one" used to leave
    # both entries active forever; the journal lets the next lock finish the job
    a = _e(body="A"); store.write(a)
    b = _e(body="A v2"); store.write(b)  # the orphaned "new" entry
    journal = store.root / ".supersede.journal"
    store._atomic_write(journal, f"{a.id} {b.id}")

    with store.locked():
        pass

    assert store.read(a.id).superseded_by == b.id
    assert not journal.exists()
    assert {e.id for e in store.iter_entries(scopes=["global"])} == {b.id}


def test_recovery_keeps_old_active_when_new_entry_never_landed(store):
    # crash before the first write: the operation never happened, so the old
    # entry must stay active rather than point at a file that does not exist
    a = _e(body="A"); store.write(a)
    never_written = _e(body="A v2")
    journal = store.root / ".supersede.journal"
    store._atomic_write(journal, f"{a.id} {never_written.id}")

    with store.locked():
        pass

    assert store.read(a.id).superseded_by is None
    assert not journal.exists()
    assert {e.id for e in store.iter_entries(scopes=["global"])} == {a.id}


def test_recovery_discards_malformed_journal(store):
    a = _e(body="A"); store.write(a)
    journal = store.root / ".supersede.journal"
    store._atomic_write(journal, "not a journal")

    with store.locked():
        pass

    assert store.read(a.id).superseded_by is None
    assert not journal.exists()


def test_recovery_notifies_an_owner_holding_derived_state(store):
    # a peer process crashed mid-supersede, so this process repairs the chain --
    # but an owner that keeps a search index open across operations still holds
    # the pre-crash picture. The callback is how it learns to reconcile.
    a = _e(body="alpha fact"); store.write(a)
    b = _e(body="beta fact"); store.write(b)  # the orphaned "new" entry
    index = FtsIndex(store.root / ".derived" / "index.sqlite")
    index.add(a)  # only the old entry was ever indexed

    seen: list[tuple[str, str]] = []

    def reconcile(old_id: str, new_id: str) -> None:
        seen.append((old_id, new_id))
        index.mark_superseded(old_id)
        index.add(store.read(new_id))

    store.on_recover = reconcile
    store._atomic_write(store.root / ".supersede.journal", f"{a.id} {b.id}")

    with store.locked():
        pass

    assert seen == [(a.id, b.id)]
    assert [h.id for h in index.search("beta", scopes=["global"])] == [b.id]
    assert index.search("alpha", scopes=["global"]) == []


def test_recovery_does_not_notify_when_the_new_entry_never_landed(store):
    # nothing was recovered, so an owner's derived state is still correct;
    # firing here would make it index an entry that does not exist
    a = _e(body="A"); store.write(a)
    store._atomic_write(store.root / ".supersede.journal", f"{a.id} {_e(body='A v2').id}")
    seen: list[tuple[str, str]] = []
    store.on_recover = lambda old_id, new_id: seen.append((old_id, new_id))

    with store.locked():
        pass

    assert seen == []


def test_recovery_survives_a_broken_on_recover_callback(store):
    # the callback belongs to the owner and may fail on its own; recovery of the
    # Markdown chain must complete and the journal must still be cleared
    a = _e(body="A"); store.write(a)
    b = _e(body="A v2"); store.write(b)
    store._atomic_write(store.root / ".supersede.journal", f"{a.id} {b.id}")

    def boom(old_id: str, new_id: str) -> None:
        raise RuntimeError("owner is broken")

    store.on_recover = boom

    with store.locked():
        pass

    assert store.read(a.id).superseded_by == b.id
    assert not (store.root / ".supersede.journal").exists()


def test_supersede_removes_its_journal(store):
    a = _e(); store.write(a)
    store.supersede(a.id, _e(body="v2"))
    assert not (store.root / ".supersede.journal").exists()


def test_journal_is_not_visible_as_an_entry(store):
    a = _e(); store.write(a)
    store._atomic_write(store.root / ".supersede.journal", f"{a.id} {_e().id}")
    assert {e.id for e in store.iter_entries()} == {a.id}


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
    index_text = render_index(store, scopes=["global"])
    assert "secret plan" not in index_text and mine.id in index_text

    fts = FtsIndex(store.root / ".derived" / "index.sqlite")
    fts.rebuild(store)
    hits = fts.search("secret plan", scopes=["global", "project:other-000000"])
    assert hits == []
    assert [h.id for h in fts.search("stays visible", scopes=["global"])] == [mine.id]


def test_read_refuses_entry_whose_frontmatter_scope_contradicts_its_directory(store):
    # _find resolves an id by globbing every project directory, so a direct read
    # used to trust the file's own frontmatter: a hand-edited file misplaced
    # under another project could be read (and superseded) across the boundary
    misplaced = _e(body="foreign project secret plan", scope="project:other-000000")
    path = store.write(misplaced)
    misplaced.scope = "global"
    path.write_text(misplaced.to_markdown(), encoding="utf-8")

    with pytest.raises(KeyError):
        store.read(misplaced.id)
    assert path.read_text(encoding="utf-8") == misplaced.to_markdown()


def test_supersede_of_misplaced_entry_is_refused(store):
    # supersede reads first, so the same guard has to stop the rewrite too
    misplaced = _e(body="foreign project secret plan", scope="project:other-000000")
    path = store.write(misplaced)
    misplaced.scope = "global"
    path.write_text(misplaced.to_markdown(), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    replacement = _e(body="hijacked")
    with pytest.raises(KeyError):
        store.supersede(misplaced.id, replacement)
    assert path.read_text(encoding="utf-8") == before
    assert not (store.root / "global" / "entries" / f"{replacement.id}.md").exists()


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
