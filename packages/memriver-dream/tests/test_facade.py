"""The facade the umbrella imports: every name it uses, and the report records with
retention under the run lock."""

from __future__ import annotations

import memriver_dream
from memriver_core.models import new_id
from memriver_dream import RunRecord, find_run, recent_runs
from memriver_dream.lock import run_lock
from memriver_dream.store import DreamStore, RunRow

FACADE = {
    "run_dream", "recent_runs", "find_run", "RunRecord", "storable", "Executor",
    "ExecutorResult", "FailureKind", "Record", "Transcript", "TranscriptSource",
    "DreamSettings", "check_dream_table", "load_dream_settings",
    "DEFAULT_DREAM_REPORT_RETENTION_DAYS", "DEFAULT_DREAM_SCHEDULE_AT",
    "DEFAULT_DREAM_TTL_DAYS", "DREAM_DIRECTORY", "DREAM_KILL_GRACE_S",
    "DREAM_LAUNCH_AGENT_LABEL", "DREAM_LOG_FILENAME", "DREAM_REPORTS_DIRECTORY",
    "DREAM_TOOL_OUTPUT_CHARS",
}
OLD = "2000-01-01T04:00:00.000000Z"


def test_the_facade_exports_every_name_the_umbrella_uses():
    assert FACADE <= set(memriver_dream.__all__)
    assert all(hasattr(memriver_dream, name) for name in FACADE)


def test_find_run_returns_the_latest_or_the_named_run_with_its_report_path(world):
    first = world.run(now="2026-09-26T04:00:00.000000Z")
    second = world.run()
    assert find_run(world.root, None, retention_days=36500) == RunRecord(
        second.run_id, second.started_at, "manual", "completed",
        world.reports / second.report_file)
    assert find_run(world.root, first.run_id, retention_days=36500).run_id == first.run_id
    assert find_run(world.root, new_id(), retention_days=36500) is None


def test_recent_runs_lists_the_newest_first(world):
    first = world.run(now="2026-09-26T04:00:00.000000Z")
    second = world.run()
    assert [r.run_id for r in recent_runs(world.root, limit=10, retention_days=36500)] == [
        second.run_id, first.run_id]
    assert len(recent_runs(world.root, limit=1, retention_days=36500)) == 1


def test_a_running_row_is_interrupted_only_while_nobody_holds_the_lock(world):
    world.run()
    run_id = new_id()
    DreamStore(world.root / "dream" / "dream.db").start_run(
        RunRow(run_id, world.now, None, "manual", "running", f"{run_id}.txt"))
    assert find_run(world.root, run_id, retention_days=36500).status == "interrupted"
    with run_lock(world.root):                              # a live run holds it
        assert find_run(world.root, run_id, retention_days=36500).status == "running"


def test_retention_runs_only_while_no_run_holds_the_lock(world):
    old = world.run(now=OLD)
    with run_lock(world.root):
        assert [r.run_id for r in recent_runs(world.root, limit=10, retention_days=30)] == [
            old.run_id]
    assert recent_runs(world.root, limit=10, retention_days=30) == []
    assert not (world.reports / old.report_file).exists()
