from memriver_core.entry import Entry
from memriver_core.index_fts import FtsIndex

SOURCE = {"harness": "test", "session": "s", "method": "agent"}


def _e(body, scope="global", type="fact"):
    return Entry.new(body=body, type=type, scope=scope, source=SOURCE)


def _idx(tmp_path):
    return FtsIndex(tmp_path / ".derived" / "index.sqlite")


def test_add_and_search(tmp_path):
    idx = _idx(tmp_path)
    e = _e("python 包管理用 uv，不用 pip")
    idx.add(e)
    hits = idx.search("包管理", scopes=["global"], limit=5)
    assert hits and hits[0].id == e.id and hits[0].type == "fact"


def test_scope_filter(tmp_path):
    idx = _idx(tmp_path)
    idx.add(_e("全局记忆条目", scope="global"))
    p = _e("项目记忆条目", scope="project:p-000000")
    idx.add(p)
    hits = idx.search("记忆条目", scopes=["project:p-000000"], limit=10)
    assert {h.id for h in hits} == {p.id}


def test_superseded_excluded(tmp_path):
    idx = _idx(tmp_path)
    e = _e("旧的教训内容")
    idx.add(e)
    assert idx.search("教训内容", scopes=["global"], limit=5)  # prove it is findable first
    idx.mark_superseded(e.id)
    assert idx.search("教训内容", scopes=["global"], limit=5) == []


def test_rebuild_from_store(tmp_path, store):
    e = _e("重建后应可检索的内容")
    store.write(e)
    idx = _idx(tmp_path)
    idx.rebuild(store)
    assert idx.search("重建", scopes=["global"], limit=5)[0].id == e.id
