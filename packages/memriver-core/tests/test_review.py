from memriver_core.entry import Entry
from memriver_core.review import MAX_REVIEW_BATCH, review_queue
from memriver_core.store import MemoryStore


def _seed(tmp_path, scope="global"):
    store = MemoryStore(tmp_path)
    entries = []
    for i, updated in enumerate(
            ["2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", "2026-08-01T00:00:00Z"]):
        e = Entry.new(body=f"fact {i}", type="project", scope=scope,
                     source={}, id=f"entry-{i}")
        e.updated = updated
        store.write(e)
        entries.append(e)
    return store, entries


def test_oldest_first_ordering(tmp_path):
    store, _entries = _seed(tmp_path)
    hits = review_queue(store, scopes=["global"], limit=3)
    assert [e.id for e in hits] == ["entry-0", "entry-1", "entry-2"]


def test_ties_broken_by_id(tmp_path):
    store = MemoryStore(tmp_path)
    for eid in ["b", "a", "c"]:
        e = Entry.new(body="tied", type="project", scope="global",
                     source={}, id=eid)
        e.updated = "2026-01-01T00:00:00Z"
        store.write(e)
    hits = review_queue(store, scopes=["global"], limit=3)
    assert [e.id for e in hits] == ["a", "b", "c"]


def test_limit_clamped_up_from_zero(tmp_path):
    store, _ = _seed(tmp_path)
    hits = review_queue(store, scopes=["global"], limit=0)
    assert len(hits) == 1
    assert hits[0].id == "entry-0"


def test_limit_clamped_down_to_max(tmp_path):
    store = MemoryStore(tmp_path)
    for i in range(MAX_REVIEW_BATCH + 5):
        e = Entry.new(body=f"fact {i}", type="project", scope="global",
                     source={}, id=f"entry-{i:02d}")
        e.updated = f"2026-01-{i + 1:02d}T00:00:00Z"
        store.write(e)
    hits = review_queue(store, scopes=["global"], limit=10 ** 9)
    assert len(hits) == MAX_REVIEW_BATCH


def test_scope_filtering(tmp_path):
    store, _ = _seed(tmp_path, scope="global")
    foreign = Entry.new(body="foreign", type="project",
                        scope="project:other-000000", source={}, id="foreign-entry")
    foreign.updated = "2020-01-01T00:00:00Z"
    store.write(foreign)

    hits = review_queue(store, scopes=["global"], limit=10)
    assert "foreign-entry" not in [e.id for e in hits]
    assert [e.id for e in hits] == ["entry-0", "entry-1", "entry-2"]


def test_empty_store_returns_empty_list(tmp_path):
    store = MemoryStore(tmp_path)
    assert review_queue(store, scopes=["global"], limit=5) == []
