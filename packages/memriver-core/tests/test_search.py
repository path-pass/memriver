from memriver_core.entry import Entry
from memriver_core.search import search_entries
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


def test_snippet_truncated(tmp_path):
    store = MemoryStore(tmp_path)
    store.write(Entry.new(body="x" * 200, type="user", scope="global",
                          source={}, id="long"))
    hits = search_entries(store, "xxx", scopes=["global"], limit=5)
    assert len(hits[0].snippet) <= 61
