import inspect

from memriver_core.entry import Entry
from memriver_core.search import review_queue, search_entries
from memriver_core.store import MemoryStore


def _seed(tmp_path):
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="mise manages every runtime on this machine",
                          type="user", scope="global", source={},
                          id="mise-runtime-management"))
    store.write(Entry.new(body="项目使用 uv workspace 管理依赖",
                          type="project", scope="global", source={}, id="uv-layout"))
    return store


def test_matches_body_case_insensitive(tmp_path):
    hits = search_entries(_seed(tmp_path), "MISE", scopes=["global"], limit=5)
    assert [h.id for h in hits] == ["mise-runtime-management"]


def test_matches_cjk_substring(tmp_path):
    hits = search_entries(_seed(tmp_path), "依赖", scopes=["global"], limit=5)
    assert [h.id for h in hits] == ["uv-layout"]


def test_matches_entry_name(tmp_path):
    hits = search_entries(_seed(tmp_path), "uv-layout", scopes=["global"], limit=5)
    assert [h.id for h in hits] == ["uv-layout"]


def test_empty_and_nul_queries(tmp_path):
    store = _seed(tmp_path)
    assert search_entries(store, "", scopes=["global"], limit=5) == []
    assert search_entries(store, "\x00", scopes=["global"], limit=5) == []


def test_limit_clamped(tmp_path):
    store = _seed(tmp_path)
    assert len(search_entries(store, "m", scopes=["global"], limit=-1)) == 1
    assert search_entries(store, "m", scopes=["global"], limit=10 ** 9)


def test_matches_description(tmp_path):
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="unrelated body text", type="user",
                          scope="global", source={}, id="cue-entry",
                          description="a distinctive recall cue"))
    hits = search_entries(store, "distinctive", scopes=["global"], limit=5)
    assert [h.id for h in hits] == ["cue-entry"]


def test_snippet_truncated(tmp_path):
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="x" * 200, type="user", scope="global",
                          source={}, id="long"))
    hits = search_entries(store, "xxx", scopes=["global"], limit=5)
    assert len(hits[0].snippet) <= 61


# --- review_queue ---

_MAX_BATCH = inspect.signature(review_queue).parameters["max_limit"].default


def _seed_aged(tmp_path, scope="global"):
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
    store, _entries = _seed_aged(tmp_path)
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
    store, _ = _seed_aged(tmp_path)
    hits = review_queue(store, scopes=["global"], limit=0)
    assert len(hits) == 1
    assert hits[0].id == "entry-0"


def test_limit_clamped_down_to_max(tmp_path):
    store = MemoryStore(tmp_path)
    for i in range(_MAX_BATCH + 5):
        e = Entry.new(body=f"fact {i}", type="project", scope="global",
                     source={}, id=f"entry-{i:02d}")
        e.updated = f"2026-01-{i + 1:02d}T00:00:00Z"
        store.write(e)
    hits = review_queue(store, scopes=["global"], limit=10 ** 9)
    assert len(hits) == _MAX_BATCH


def test_scope_filtering(tmp_path):
    store, _ = _seed_aged(tmp_path, scope="global")
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
