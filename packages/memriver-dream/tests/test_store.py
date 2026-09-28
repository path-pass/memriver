"""dream.db (spec §5): the five tables, the private file, upserts, run retention's
delete and the input digest (§6.9)."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import replace

import memriver_dream.store as store_module
import pytest
from memriver_dream.store import (
    _SCHEMA,
    DreamStore,
    ReviewRow,
    RunRow,
    SummaryRow,
    input_digest,
    shift_days,
)

T0 = "2026-09-27T04:00:00.000000Z"


def _store(tmp_path) -> DreamStore:
    return DreamStore(tmp_path / "dream" / "dream.db")


def _run(run_id: str, started_at: str, status: str = "completed") -> RunRow:
    finished = None if status == "running" else started_at
    return RunRow(run_id, started_at, finished, "schedule", status, f"{run_id}.txt")


def test_the_file_is_private_and_holds_the_five_tables(tmp_path):
    store = _store(tmp_path)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    with closing(sqlite3.connect(store.path)) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == {"runs", "ttl_reviews", "scope_passes", "source_checks",
                     "session_summaries"}
    store.put_scope_pass("global", "d1", T0)
    assert DreamStore(store.path).scope_digest("global") == "d1"     # reopening keeps rows


def test_a_fresh_store_is_stamped_with_the_current_schema_version(tmp_path):
    store = _store(tmp_path)
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_an_unstamped_dream_db_is_stamped_on_open_and_keeps_its_rows(tmp_path):
    path = tmp_path / "dream" / "dream.db"
    path.parent.mkdir(mode=0o700, parents=True)
    run = _run("run0000001", T0, "running")
    with closing(sqlite3.connect(path)) as conn, conn:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute("INSERT INTO runs (run_id, started_at, finished_at, trigger, status, "
                     "report_file) VALUES (?, ?, ?, ?, ?, ?)",
                     (run.run_id, run.started_at, run.finished_at, run.trigger,
                      run.status, run.report_file))
    store = DreamStore(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert store.run("run0000001") == run


def test_a_dream_db_with_an_unsupported_version_is_refused_untouched(tmp_path):
    path = tmp_path / "dream" / "dream.db"
    path.parent.mkdir(mode=0o700, parents=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA user_version = 2")
    with pytest.raises(sqlite3.DatabaseError):
        DreamStore(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == set()


def test_a_symlink_at_the_path_is_refused_not_followed(tmp_path):
    target = tmp_path / "elsewhere.db"
    target.touch()
    (tmp_path / "dream").mkdir()
    (tmp_path / "dream" / "dream.db").symlink_to(target)
    with pytest.raises(OSError):
        _store(tmp_path)
    assert target.stat().st_size == 0


def test_runs_are_started_finished_and_listed_newest_first(tmp_path):
    store = _store(tmp_path)
    first, second = _run("run0000001", T0, "running"), _run("run0000002", shift_days(T0, 1),
                                                            "running")
    store.start_run(first)
    store.start_run(second)
    assert store.running() == [first, second]
    later = shift_days(T0, 0.5)
    store.finish_run("run0000001", status="completed", finished_at=later)
    assert store.run("run0000001") == replace(first, status="completed", finished_at=later)
    assert store.running() == [second]
    assert [row.run_id for row in store.runs(10)] == ["run0000002", "run0000001"]
    assert [row.run_id for row in store.runs(1)] == ["run0000002"]
    assert store.run("missing000") is None


def test_the_trigger_and_status_are_checked(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        store.start_run(RunRow("run0000001", T0, None, "cron", "running", "run0000001.txt"))
    with pytest.raises(sqlite3.IntegrityError):
        store.start_run(RunRow("run0000001", T0, None, "manual", "done", "run0000001.txt"))


def test_old_runs_are_listed_then_deleted_one_by_one_touching_nothing_else(tmp_path):
    # §10 item 12: retention lists expired runs; each row goes only once its file is gone
    store = _store(tmp_path)
    old = _run("run0000001", shift_days(T0, -31))
    older = _run("run0000004", shift_days(T0, -35))
    stuck = _run("run0000002", shift_days(T0, -40), "running")    # closed by the next run first
    recent = _run("run0000003", shift_days(T0, -29))
    for row in (old, older, stuck, recent):
        store.start_run(row)
    ancient = shift_days(T0, -400)
    review = ReviewRow("aaaaaaaaaa", 1, "keep", 0, ancient, shift_days(ancient, 30), "in use")
    summary = SummaryRow("claude-code", "s1", ancient, ancient, "d", 3, True, "ok", None)
    store.put_review(review)
    store.put_scope_pass("global", "d", ancient)
    store.put_source_check("aaaaaaaaaa", "d", ancient)
    store.put_summary(summary)
    cutoff = shift_days(T0, -30)
    assert store.runs_before(cutoff) == [older, old]           # oldest first; nothing deleted
    assert len(store.runs(10)) == 4
    store.delete_run(older.run_id)
    assert store.runs_before(cutoff) == [old]
    assert store.runs(10) == [recent, old, stuck]
    assert store.review("aaaaaaaaaa") == review
    assert store.scope_digest("global") == "d"
    assert store.source_check("aaaaaaaaaa") == "d"
    assert store.summary("claude-code", "s1") == summary


def test_a_review_is_replaced_by_the_next(tmp_path):
    store = _store(tmp_path)
    first = ReviewRow("aaaaaaaaaa", 1, "uncertain", 1, T0, shift_days(T0, 30), "unclear")
    second = replace(first, memory_version=2, decision="keep", uncertain_streak=0)
    assert store.review("aaaaaaaaaa") is None
    store.put_review(first)
    assert store.review("aaaaaaaaaa") == first
    store.put_review(second)
    assert store.review("aaaaaaaaaa") == second
    with pytest.raises(sqlite3.IntegrityError):
        store.put_review(replace(first, decision="delete"))


def test_a_review_with_an_unverifiable_time_is_treated_as_absent(tmp_path):
    # a hand-edited or otherwise malformed next_review_at ("9999") must never be
    # compared as a raw string -- it never proves the review is not due yet
    store = _store(tmp_path)
    with closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute(
            "INSERT INTO ttl_reviews (memory_id, memory_version, decision, "
            "uncertain_streak, decided_at, next_review_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("aaaaaaaaaa", 1, "keep", 0, T0, "9999", "hand-edited"))
    assert store.review("aaaaaaaaaa") is None


def test_a_review_with_a_non_integer_streak_is_treated_as_absent(tmp_path):
    # the CHECK constraint (>= 0) lets a stored TEXT value like 'x' through (SQLite
    # compares TEXT as greater than any INTEGER); `1 + uncertain_streak` must never
    # be attempted on it
    store = _store(tmp_path)
    with closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute(
            "INSERT INTO ttl_reviews (memory_id, memory_version, decision, "
            "uncertain_streak, decided_at, next_review_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("bbbbbbbbbb", 1, "uncertain", "x", T0, shift_days(T0, 30), "hand-edited"))
    assert store.review("bbbbbbbbbb") is None


def test_scope_passes_and_source_checks_keep_the_last_digest(tmp_path):
    store = _store(tmp_path)
    assert store.scope_digest("project:aaaaaaaaaa") is None
    store.put_scope_pass("project:aaaaaaaaaa", "d1", T0)
    store.put_scope_pass("project:aaaaaaaaaa", "d2", shift_days(T0, 1))
    assert store.scope_digest("project:aaaaaaaaaa") == "d2"
    assert store.scope_digest("global") is None
    assert store.source_check("aaaaaaaaaa") is None
    store.put_source_check("aaaaaaaaaa", "c1", T0)
    store.put_source_check("aaaaaaaaaa", "c2", shift_days(T0, 1))
    assert store.source_check("aaaaaaaaaa") == "c2"


def test_a_summary_row_round_trips_its_checkpoint_and_flags(tmp_path):
    store = _store(tmp_path)
    pending = SummaryRow("claude-code", "s1", None, T0, None, None, None, "partial",
                         {"fingerprint": "f", "partials": ["p1", "中"]})
    store.put_summary(pending)
    assert store.summary("claude-code", "s1") == pending
    final = SummaryRow("claude-code", "s1", T0, T0, "digest", 12, False, "ok", None)
    store.put_summary(final)
    assert store.summary("claude-code", "s1") == final
    assert store.summary("codex", "s1") is None


def test_the_input_digest_is_order_free_and_moves_with_versions_and_prompt(monkeypatch):
    pairs = [("aaaaaaaaaa", 1), ("bbbbbbbbbb", 2)]
    base = input_digest(pairs)
    assert base == input_digest(reversed(pairs))
    assert len(base) == 64 and int(base, 16) >= 0
    assert base != input_digest([("aaaaaaaaaa", 1), ("bbbbbbbbbb", 3)])
    monkeypatch.setattr(store_module, "PROMPT_VERSION", "dream-next")
    assert base != input_digest(pairs)


def test_shift_days_keeps_the_fixed_width_form():
    assert shift_days(T0, -30) == "2026-08-28T04:00:00.000000Z"
    assert shift_days(T0, 0.5) == "2026-09-27T16:00:00.000000Z"
    # a year below 1000 keeps four digits
    assert shift_days("0005-01-01T00:00:00.000000Z", 1) == "0005-01-02T00:00:00.000000Z"
    assert shift_days(T0, -739_000) == "0003-06-05T04:00:00.000000Z"


@pytest.mark.parametrize("days", [3_000_000, 10**9, 10**12])
def test_shift_days_clamps_instead_of_overflowing(days):
    # any positive setting is valid (ttl_days, report_retention_days): a huge one
    # saturates at the earliest/latest timestamp, never an OverflowError mid-run
    assert shift_days(T0, days) == "9999-12-31T23:59:59.999999Z"
    assert shift_days(T0, -days) == "0001-01-01T00:00:00.000000Z"
