import threading

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


def test_rebuild_waits_for_the_store_lock(tmp_path, store):
    # rebuild must not snapshot entries while another process is halfway through
    # a supersede, so it takes the same cross-process lock supersede takes
    store.write(_e("串行化重建的内容"))
    db = tmp_path / ".derived" / "index.sqlite"
    done = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        # sqlite connections are bound to their creating thread
        try:
            FtsIndex(db).rebuild(store)
        except BaseException as err:  # surfaced through the assert below
            errors.append(err)
        finally:
            done.set()

    worker = threading.Thread(target=run)
    with store.locked():
        worker.start()
        assert not done.wait(0.5), "rebuild ran while the store lock was held"
    worker.join(timeout=10)
    assert done.is_set() and not errors, f"rebuild did not finish: {errors}"
    assert _idx(tmp_path).search("串行化", scopes=["global"], limit=5)


def test_rebuild_after_recovery_indexes_only_the_survivor(tmp_path, store):
    # the recovered supersede must not leave both versions searchable
    a = _e("重建幸存者内容 A")
    b = _e("重建幸存者内容 B")
    store.write(a); store.write(b)
    store._atomic_write(store.root / ".supersede.journal", f"{a.id} {b.id}")

    idx = _idx(tmp_path)
    idx.rebuild(store)

    assert {h.id for h in idx.search("幸存者", scopes=["global"], limit=10)} == {b.id}


def test_query_with_embedded_quotes_is_safe(tmp_path):
    idx = _idx(tmp_path)
    idx.add(_e("normal body about testing memory"))
    secret = _e("secret target body")
    idx.add(secret)
    # odd number of quotes must not raise
    assert idx.search('he said "hello', scopes=["global"], limit=5) == []
    # even quotes must not escape the phrase and match unrelated rows
    hits = idx.search('zzz" OR "secret', scopes=["global"], limit=5)
    assert secret.id not in {h.id for h in hits}


def test_fallback_wildcards_do_not_dump_table(tmp_path):
    idx = _idx(tmp_path)
    idx.add(_e("body one"))
    idx.add(_e("body two"))
    assert idx.search("%", scopes=["global"], limit=10) == []
    assert idx.search("_", scopes=["global"], limit=10) == []


def test_nul_bytes_in_query_do_not_raise(tmp_path):
    idx = _idx(tmp_path)
    idx.add(_e("body one"))
    # a NUL truncates the query string inside FTS5 and used to surface as
    # sqlite3.OperationalError, which no caller catches
    assert isinstance(idx.search("evil\x00query", scopes=["global"], limit=5), list)
    assert idx.search("\x00", scopes=["global"], limit=5) == []


def test_negative_limit_does_not_dump_table(tmp_path):
    # sqlite treats LIMIT -1 as unlimited, so an unclamped limit leaks the table
    idx = _idx(tmp_path)
    for i in range(3):
        idx.add(_e(f"shared keyword body number {i}"))
    assert len(idx.search("keyword", scopes=["global"], limit=-1)) == 1
    assert len(idx.search("keyword", scopes=["global"], limit=10)) == 3


def test_fallback_short_ascii_query_hits(tmp_path):
    idx = _idx(tmp_path)
    e = _e("uv is the package manager")
    idx.add(e)
    hits = idx.search("uv", scopes=["global"], limit=5)
    assert [h.id for h in hits] == [e.id]


def test_max_limit_is_tunable(tmp_path):
    # the umbrella package passes a configured ceiling; core keeps 50 as default
    idx = FtsIndex(tmp_path / ".derived" / "index.sqlite", max_limit=2)
    for i in range(3):
        idx.add(_e(f"shared keyword body number {i}"))
    assert len(idx.search("keyword", scopes=["global"], limit=10)) == 2
    # same database, default ceiling: the clamp is per-index, not per-schema
    assert len(_idx(tmp_path).search("keyword", scopes=["global"], limit=10)) == 3
