"""`SqliteSessionStore`: one row per harness session, every change one short transaction."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from contextlib import closing

import pytest
from memriver_core.models import PromptEntry, Session, SessionKey
from memriver_core.models.errors import ProjectUnavailable, StorageFailure
from memriver_core.repository.sqlite import SqliteSessionStore
from memriver_core.repository.sqlite.database import DATABASE_FILENAME, Database

PROJECT = "pppppppppp"
OTHER = "qqqqqqqqqq"
PROJECT_ROOT = "/work/app"
KEY = SessionKey("claude-code", "session-1")


def _at(second: int) -> str:
    return f"2026-09-24T10:{second // 60:02d}:{second % 60:02d}.000000Z"


def _session(key: SessionKey = KEY, **fields) -> Session:
    values = {
        "key": key, "status": "registered", "origin": "start", "project_id": PROJECT,
        "candidate_id": None, "candidate_root": None, "entry_cwd": "/work/app/src",
        "branch": "main", "transcript_path": None, "started_at": _at(0),
        "last_active_at": _at(0), "ended_at": None, "prompt_count": 0,
        "last_write_prompt_count": 0, "last_nudge_prompt_count": 0, "first_prompt": None,
        "recent_prompts": ()}
    return Session(**(values | fields))


def _pending(key: SessionKey = KEY, **fields) -> Session:
    values = {"status": "pending", "origin": "first-seen", "project_id": None,
              "candidate_id": PROJECT, "candidate_root": PROJECT_ROOT}
    return _session(key, **(values | fields))


def _prompt(second: int, text: str = "fix the build") -> PromptEntry:
    return PromptEntry(_at(second), text=text)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "store"


@pytest.fixture
def initialized(root):
    """A v2 store holding two projects, no global."""
    with Database(root, busy_timeout_ms=5000).write() as conn:
        conn.execute("INSERT INTO projects (id, name, root) VALUES (?, 'app', ?)",
                     (PROJECT, PROJECT_ROOT))
        conn.execute("INSERT INTO projects (id, name, root) VALUES (?, 'other', '/work/other')",
                     (OTHER,))
    return root


def _store(root) -> SqliteSessionStore:
    return SqliteSessionStore(root, busy_timeout_ms=5000)


@pytest.fixture
def session_store(initialized):
    return _store(initialized)


def _raw(root, sql: str, params: tuple = ()) -> list[tuple]:
    with closing(sqlite3.connect(root / DATABASE_FILENAME)) as conn, conn:
        return conn.execute(sql, params).fetchall()


def _run_together(count: int, work) -> list:
    """`work(index)` from `count` threads released at once; their results in order.

    A thread that raises leaves its result None, which the caller's asserts catch.
    """
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def run(index: int) -> None:
        barrier.wait()
        results[index] = work(index)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


# --- get / register ---

def test_get_answers_none_for_an_unknown_key(session_store):
    assert session_store.get(KEY) is None


def test_get_on_an_absent_store_is_none_and_creates_nothing(root):
    assert _store(root).get(KEY) is None
    assert not root.exists()


def test_store_exists_on_an_absent_store_is_false_and_creates_nothing(root):
    assert _store(root).store_exists() is False
    assert not root.exists()


def test_store_exists_on_an_initialized_store_is_true(session_store):
    assert session_store.store_exists() is True


def test_register_stores_the_row_and_returns_it(session_store):
    session = _session(transcript_path="/tmp/t.jsonl")
    assert session_store.register(session) == session
    assert session_store.get(KEY) == session


def test_register_never_overwrites_an_existing_row(session_store):
    first = session_store.register(_session())
    second = session_store.register(_session(project_id=OTHER, entry_cwd="/elsewhere",
                                             branch="dev", origin="first-seen"))
    assert second == first
    assert session_store.get(KEY) == first


@pytest.mark.parametrize("shared", [True, False])
def test_concurrent_registers_of_one_key_agree_on_the_stored_row(initialized, shared):
    stores = [_store(initialized)] * 2 if shared else [_store(initialized), _store(initialized)]
    candidates = [_session(), _session(project_id=OTHER, entry_cwd="/work/other")]
    results = _run_together(2, lambda index: stores[index].register(candidates[index]))
    assert results[0] == results[1]
    assert results[0] in candidates
    assert _raw(initialized, "SELECT count(*) FROM sessions") == [(1,)]


def test_registering_a_row_that_would_not_read_back_is_a_value_error_and_writes_nothing(
        session_store, initialized):
    with pytest.raises(ValueError):
        session_store.register(_session(started_at="yesterday"))
    assert _raw(initialized, "SELECT count(*) FROM sessions") == [(0,)]


# --- touch ---

def test_touch_moves_last_active_at_forward_only(session_store):
    session_store.register(_session(last_active_at=_at(10)))
    assert session_store.touch(KEY, _at(20)).last_active_at == _at(20)
    assert session_store.touch(KEY, _at(5)).last_active_at == _at(20)


def test_touch_replaces_the_transcript_path_only_when_one_is_given(session_store):
    session_store.register(_session(transcript_path="/tmp/a.jsonl"))
    assert session_store.touch(KEY, _at(1)).transcript_path == "/tmp/a.jsonl"
    assert session_store.touch(KEY, _at(2), transcript_path="/tmp/b.jsonl").transcript_path \
        == "/tmp/b.jsonl"
    assert session_store.get(KEY).transcript_path == "/tmp/b.jsonl"


def test_touch_of_an_unknown_key_is_none_and_writes_nothing(session_store, initialized):
    assert session_store.touch(KEY, _at(1)) is None
    assert _raw(initialized, "SELECT count(*) FROM sessions") == [(0,)]


# --- add_prompt ---

def test_the_first_prompt_inserts_the_seed_and_says_so(session_store):
    session, created = session_store.add_prompt(KEY, _prompt(5), seed=_pending(),
                                                keep_recent=5)
    assert created is True
    assert (session.status, session.prompt_count) == ("pending", 1)
    assert session.first_prompt == _prompt(5)
    assert session.recent_prompts == (_prompt(5),)
    assert session.last_active_at == _at(5)
    assert session_store.get(KEY) == session


def test_a_prompt_on_an_existing_row_ignores_the_seed(session_store):
    registered = session_store.register(_session())
    session, created = session_store.add_prompt(KEY, _prompt(5), seed=_pending(),
                                                keep_recent=5)
    assert created is False
    assert (session.status, session.project_id) == (registered.status, registered.project_id)


def test_eight_prompts_keep_the_first_and_the_last_five(session_store):
    for second in range(1, 9):
        session, _ = session_store.add_prompt(KEY, _prompt(second, f"prompt {second}"),
                                              seed=_pending(), keep_recent=5)
    assert session.prompt_count == 8
    assert session.first_prompt == _prompt(1, "prompt 1")
    assert session.recent_prompts == tuple(_prompt(s, f"prompt {s}") for s in range(4, 9))
    assert session_store.get(KEY) == session


def test_an_omitted_prompt_is_counted_and_kept_without_text(session_store):
    omitted = PromptEntry(_at(3), omitted="secret")
    session, _ = session_store.add_prompt(KEY, omitted, seed=_pending(), keep_recent=5)
    assert session.first_prompt == omitted and session.recent_prompts == (omitted,)


def test_an_earlier_prompt_never_moves_last_active_at_back(session_store):
    session_store.register(_session(last_active_at=_at(30)))
    session, _ = session_store.add_prompt(KEY, _prompt(10), seed=_pending(), keep_recent=5)
    assert session.last_active_at == _at(30)


def test_prompt_json_is_stored_compact_and_unescaped(session_store, initialized):
    session_store.add_prompt(KEY, _prompt(1, "修复构建"), seed=_pending(), keep_recent=5)
    first, recent = _raw(initialized, "SELECT first_prompt, recent_prompts FROM sessions")[0]
    assert first == '{"at":"2026-09-24T10:00:01.000000Z","text":"修复构建"}'
    assert recent == f"[{first}]"


@pytest.mark.parametrize("shared", [True, False])
def test_concurrent_first_prompts_create_one_row_and_exactly_one_reports_it(initialized,
                                                                            shared):
    stores = [_store(initialized)] * 2 if shared else [_store(initialized), _store(initialized)]
    results = _run_together(2, lambda index: stores[index].add_prompt(
        KEY, _prompt(index + 1), seed=_pending(), keep_recent=5))
    assert sorted(created for _, created in results) == [False, True]
    assert _raw(initialized, "SELECT count(*), prompt_count FROM sessions") == [(1, 2)]


# --- end / mark_saved ---

def test_end_records_ended_at_and_touches_last_active_at(session_store):
    session_store.register(_session(last_active_at=_at(10)))
    assert session_store.end(KEY, _at(40)) is None
    session = session_store.get(KEY)
    assert (session.ended_at, session.last_active_at) == (_at(40), _at(40))


def test_end_of_an_unknown_key_writes_nothing(session_store, initialized):
    session_store.end(KEY, _at(1))
    assert _raw(initialized, "SELECT count(*) FROM sessions") == [(0,)]


def test_mark_saved_moves_the_write_watermark_to_the_prompt_count(session_store):
    session_store.register(_session())
    for second in range(3):
        session_store.add_prompt(KEY, _prompt(second), seed=_pending(), keep_recent=5)
    assert session_store.mark_saved(KEY) is None
    assert session_store.get(KEY).last_write_prompt_count == 3


# --- nudge_if_due ---

def _prompts_until(session_store, count: int) -> None:
    current = session_store.get(KEY).prompt_count
    for second in range(current + 1, count + 1):
        session_store.add_prompt(KEY, _prompt(second), seed=_pending(), keep_recent=5)


def test_nudges_come_after_min_prompts_and_then_every_interval(session_store):
    session_store.register(_session())
    answers = {}
    for count in range(4, 11):
        _prompts_until(session_store, count)
        answers[count] = session_store.nudge_if_due(KEY, _at(100 + count), min_prompts=5,
                                                    interval=5)
    assert answers == {4: False, 5: True, 6: False, 7: False, 8: False, 9: False, 10: True}
    session_store.mark_saved(KEY)
    answers = {}
    for count in range(11, 16):
        _prompts_until(session_store, count)
        answers[count] = session_store.nudge_if_due(KEY, _at(100 + count), min_prompts=5,
                                                    interval=5)
    assert answers == {11: False, 12: False, 13: False, 14: False, 15: True}
    assert session_store.get(KEY).last_nudge_prompt_count == 15


def test_every_nudge_check_touches_a_registered_row(session_store):
    session_store.register(_session())
    for second in (200, 201, 202):
        session_store.nudge_if_due(KEY, _at(second), min_prompts=5, interval=5)
        assert session_store.get(KEY).last_active_at == _at(second)


def test_a_pending_row_is_never_nudged_or_touched(session_store):
    session_store.add_prompt(KEY, _prompt(1), seed=_pending(), keep_recent=5)
    _prompts_until(session_store, 6)
    before = session_store.get(KEY)
    assert session_store.nudge_if_due(KEY, _at(300), min_prompts=5, interval=5) is False
    assert session_store.get(KEY) == before


def test_a_session_without_a_project_is_never_nudged(session_store):
    session_store.register(_session(project_id=None))
    _prompts_until(session_store, 6)
    assert session_store.nudge_if_due(KEY, _at(300), min_prompts=5, interval=5) is False
    assert session_store.get(KEY).last_active_at == _at(300)


def test_an_unknown_key_is_never_nudged(session_store, initialized):
    assert session_store.nudge_if_due(KEY, _at(1), min_prompts=5, interval=5) is False
    assert _raw(initialized, "SELECT count(*) FROM sessions") == [(0,)]


@pytest.mark.parametrize("shared", [True, False])
def test_two_concurrent_checks_at_one_watermark_nudge_at_most_once(initialized, shared):
    _store(initialized).register(_session())
    _prompts_until(_store(initialized), 5)
    stores = [_store(initialized)] * 2 if shared else [_store(initialized), _store(initialized)]
    results = _run_together(2, lambda index: stores[index].nudge_if_due(
        KEY, _at(400 + index), min_prompts=5, interval=5))
    assert sorted(results) == [False, True]


# --- confirm ---

def test_confirm_binds_a_pending_row_to_its_unchanged_candidate(session_store):
    session_store.register(_pending())
    confirmed = session_store.confirm(KEY)
    assert (confirmed.status, confirmed.project_id) == ("registered", PROJECT)
    assert session_store.get(KEY) == confirmed


def test_confirm_without_a_candidate_registers_the_row_with_no_project(session_store):
    session_store.register(_pending(candidate_id=None, candidate_root=None))
    confirmed = session_store.confirm(KEY)
    assert (confirmed.status, confirmed.project_id) == ("registered", None)


@pytest.mark.parametrize("change", [
    "UPDATE projects SET root = '/work/moved' WHERE id = 'pppppppppp'",
    "UPDATE projects SET is_global = 1 WHERE id = 'pppppppppp'",
    "DELETE FROM projects WHERE id = 'pppppppppp'",
])
def test_confirm_refuses_a_candidate_that_changed_and_leaves_the_row(session_store,
                                                                     initialized, change):
    before = session_store.register(_pending())
    # CHECK constraints off only for the planted change: a global project with
    # a root isolates the is_global test from the root comparison
    with closing(sqlite3.connect(initialized / DATABASE_FILENAME)) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(change)
    with pytest.raises(ProjectUnavailable) as exc_info:
        session_store.confirm(KEY)
    assert exc_info.value.reason == "candidate-changed"
    assert session_store.get(KEY) == before


def test_confirming_a_registered_row_returns_it_unchanged(session_store):
    session_store.register(_pending())
    confirmed = session_store.confirm(KEY)
    assert session_store.confirm(KEY) == confirmed
    registered = session_store.register(_session(SessionKey("codex", "other"),
                                                 project_id=OTHER))
    assert session_store.confirm(registered.key) == registered


def test_confirm_of_an_unknown_key_is_none(session_store):
    assert session_store.confirm(KEY) is None


# --- assign_project ---

def test_assign_project_binds_a_registered_row_with_no_project(session_store):
    before = session_store.register(_session(project_id=None))
    assigned = session_store.assign_project(KEY, PROJECT)
    assert assigned == dataclasses.replace(before, project_id=PROJECT)
    assert session_store.get(KEY) == assigned


def test_assign_project_registers_a_pending_row_without_a_candidate(session_store):
    before = session_store.register(_pending(candidate_id=None, candidate_root=None))
    assigned = session_store.assign_project(KEY, PROJECT)
    assert assigned == dataclasses.replace(before, status="registered", project_id=PROJECT)
    assert assigned.origin == "first-seen"


@pytest.mark.parametrize("row", [_session(project_id=OTHER), _pending()],
                         ids=["has-a-project", "pending-with-a-candidate"])
def test_assign_project_leaves_a_row_with_a_project_or_candidate_unchanged(session_store, row):
    before = session_store.register(row)
    assert session_store.assign_project(KEY, PROJECT) == before
    assert session_store.get(KEY) == before


def test_assign_project_of_an_unknown_key_is_none(session_store, initialized):
    assert session_store.assign_project(KEY, PROJECT) is None
    assert _raw(initialized, "SELECT count(*) FROM sessions") == [(0,)]


# --- search ---

def _search_world(session_store):
    session_store.register(_session(SessionKey("codex", "old"), last_active_at=_at(10),
                                    branch="feature/Login"))
    session_store.register(_session(SessionKey("codex", "new"), last_active_at=_at(30),
                                    entry_cwd="/work/app/LOGIN"))
    session_store.register(_session(SessionKey("codex", "elsewhere"), project_id=OTHER,
                                    last_active_at=_at(20), entry_cwd="/work/other/login"))
    session_store.register(_pending(SessionKey("codex", "pending"), last_active_at=_at(40),
                                    entry_cwd="/work/app/login"))
    session_store.add_prompt(SessionKey("codex", "prompted"), _prompt(25, "Fix the LOGIN form"),
                             seed=_session(SessionKey("codex", "prompted"), branch=None,
                                           entry_cwd="/work/app"), keep_recent=5)


def _ids(sessions) -> list[str]:
    return [session.key.session_id for session in sessions]


def test_search_in_a_project_sees_only_its_registered_rows_newest_first(session_store):
    _search_world(session_store)
    assert _ids(session_store.search(PROJECT, "login", 10)) == ["new", "prompted", "old"]


def test_search_across_all_projects_sees_every_row(session_store):
    _search_world(session_store)
    assert _ids(session_store.search(None, "LOGIN", 10)) == [
        "pending", "new", "prompted", "elsewhere", "old"]


def test_search_honours_the_limit_and_the_query(session_store):
    _search_world(session_store)
    assert _ids(session_store.search(None, "login", 2)) == ["pending", "new"]
    assert session_store.search(None, "no such thing", 10) == []


def test_search_on_an_absent_store_is_empty_and_creates_nothing(root):
    assert _store(root).search(None, "x", 10) == []
    assert not root.exists()


@pytest.mark.parametrize("column, value", [
    ("recent_prompts", '[ ]'),                                    # not the compact form
    ("recent_prompts", "not json"),
    ("first_prompt", '{"at":"2026-09-24T10:00:01.000000Z"}'),     # neither text nor omitted
    ("started_at", "yesterday"),
    ("recent_prompts", "[" * 100000 + "]" * 100000),              # nested past the recursion limit
])
def test_a_bad_row_is_skipped_by_search_and_fails_a_direct_get(session_store, initialized,
                                                               column, value):
    session_store.register(_session(SessionKey("codex", "good"), entry_cwd="/work/app/x"))
    session_store.register(_session(entry_cwd="/work/app/x"))
    _raw(initialized, f"UPDATE sessions SET {column} = ? WHERE session_id = ?",
         (value, KEY.session_id))
    assert _ids(session_store.search(PROJECT, "/work/app/x", 10)) == ["good"]
    with pytest.raises(StorageFailure):
        session_store.get(KEY)


# --- an absent store ---

def test_every_write_to_an_absent_store_is_a_no_op_that_creates_nothing(root):
    session_store = _store(root)
    assert session_store.register(_session()) is None
    assert session_store.touch(KEY, _at(1)) is None
    assert session_store.add_prompt(KEY, _prompt(1), seed=_pending(), keep_recent=5) is None
    assert session_store.end(KEY, _at(1)) is None
    assert session_store.nudge_if_due(KEY, _at(1), min_prompts=5, interval=5) is False
    assert session_store.mark_saved(KEY) is None
    assert session_store.confirm(KEY) is None
    assert session_store.assign_project(KEY, PROJECT) is None
    assert not root.exists()


def test_a_store_removed_after_the_existence_check_is_still_a_no_op(root, monkeypatch):
    session_store = _store(root)
    database = session_store._database
    real_exists = database.exists
    calls = iter([True])
    monkeypatch.setattr(database, "exists", lambda: next(calls, None) or real_exists())
    assert session_store.register(_session()) is None
    assert not root.exists()



def test_a_bad_row_fails_a_write_too_and_is_left_as_it_was(session_store, initialized):
    session_store.register(_session())
    _raw(initialized, "UPDATE sessions SET recent_prompts = 'not json'")
    with pytest.raises(StorageFailure):
        session_store.touch(KEY, _at(50))
    assert _raw(initialized, "SELECT recent_prompts, last_active_at FROM sessions") == [
        ("not json", _at(0))]


@pytest.mark.parametrize("call", [
    lambda store: store.touch(KEY, "yesterday"),
    lambda store: store.end(KEY, "2026-09-24"),
    lambda store: store.nudge_if_due(KEY, "", min_prompts=5, interval=5),
    lambda store: store.add_prompt(KEY, _prompt(1), seed=_pending(SessionKey("codex", "x")),
                                   keep_recent=5),
    lambda store: store.add_prompt(KEY, _prompt(1), seed=_pending(), keep_recent=0),
])
def test_a_malformed_argument_is_a_value_error_and_writes_nothing(session_store, initialized,
                                                                  call):
    before = session_store.register(_session(last_active_at=_at(10)))
    with pytest.raises(ValueError):
        call(session_store)
    assert session_store.get(KEY) == before
