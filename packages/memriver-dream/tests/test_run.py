"""run_dream: the lock, the run record, the safety re-scan and the phase order."""

from __future__ import annotations

import pytest
from memriver_core import StorageFailure
from memriver_core.models import now
from memriver_dream import run as run_module
from memriver_dream.lock import run_lock
from memriver_dream.run import run_dream

SECRET = "token ghp_" + "a" * 36


def test_phase_0_quarantines_secrets_even_with_no_executor_configured(world):
    in_project = world.plant(world.project.id, SECRET)
    in_global = world.plant(world.global_id, "fact", description=SECRET)
    clean = world.plant(world.project.id, "fact")
    report = run_dream(world.maintenance, None, None, world.store, None, now(),
                       log=world.lines.append)
    secrets = report.phases["secrets"].as_json()
    assert report.status == "completed"
    assert secrets["outcomes"] == {"quarantined": 2} and secrets["items"] == []
    changes = world.maintenance.changes_of_run(report.run_id)
    assert sorted(c.rows[0].id for c in changes) == sorted([in_project, in_global])
    assert {c.kind for c in changes} == {"secret"}
    for phase in ("summarize", "consolidate", "retire"):
        assert report.phases[phase].as_json()["outcomes"] == {"not-configured": 1}
    assert world.service.show(clean).deleted_at is None
    recorded = world.maintenance.run(report.run_id)
    assert (recorded.status, recorded.executor, recorded.trigger) == ("completed", None,
                                                                      "manual")
    assert recorded.report["secrets"]["done"] == 2
    # the log and the stored report name ids and the rule, never the text
    assert all("ghp_" not in line for line in world.lines)
    assert "ghp_" not in str(recorded.report)


def test_a_held_lock_records_a_skipped_run_and_leaves_the_live_one_alone(world):
    live = world.maintenance.start_run("schedule", "fake", now())
    with run_lock(world.store) as held:
        assert held
        report = run_dream(world.maintenance, world.executor, world.transcripts,
                           world.store, world.dream, now())
    assert report.status == "skipped"
    assert world.maintenance.run(report.run_id).status == "skipped"
    assert world.maintenance.run(live).status == "running"


def test_a_run_left_running_by_a_killed_process_is_marked_failed_by_the_next(world):
    killed = world.maintenance.start_run("schedule", "fake", now())
    run_dream(world.maintenance, None, None, world.store, None, now())
    assert world.maintenance.run(killed).status == "failed"


def test_model_phases_run_in_order_with_the_run_context(world, monkeypatch):
    seen: list[tuple] = []
    for name in run_module.MODEL_PHASES:
        monkeypatch.setitem(run_module._PHASES, name,
                            lambda run, phase, _n=name: (seen.append((_n, run.run_id)),
                                                         phase.record("ok")))
    report = run_dream(world.maintenance, world.executor, world.transcripts, world.store,
                       world.dream, now(), trigger="schedule",
                       phases=("consolidate", "summarize"))
    assert seen == [("consolidate", report.run_id), ("summarize", report.run_id)]
    assert world.maintenance.run(report.run_id).trigger == "schedule"


def test_a_store_failure_marks_the_run_failed_and_propagates(world, monkeypatch):
    def broken(run, phase):
        raise StorageFailure

    monkeypatch.setitem(run_module._PHASES, "summarize", broken)
    with pytest.raises(StorageFailure):
        run_dream(world.maintenance, world.executor, world.transcripts, world.store,
                  world.dream, now(), phases=("summarize",))
    (recorded,) = world.maintenance.runs(1)
    assert recorded.status == "failed"


def test_phase_0_runs_before_the_model_phases(world, monkeypatch):
    secret_id = world.plant(world.project.id, SECRET)
    seen: dict = {}

    def check_quarantined(run, phase):
        seen["deleted_at"] = world.service.show(secret_id, include_deleted=True).deleted_at
        phase.record("ok")

    monkeypatch.setitem(run_module._PHASES, "summarize", check_quarantined)
    run_dream(world.maintenance, world.executor, world.transcripts, world.store,
              world.dream, now(), phases=("summarize",))
    assert seen["deleted_at"] is not None


def test_a_secret_scan_interrupted_after_a_partial_commit_stores_an_unknown_report(
    world, monkeypatch):
    first = world.plant(world.project.id, SECRET)
    second = world.plant(world.project.id, SECRET)
    store = world.maintenance._maintenance_store
    original_quarantine = store.quarantine
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise StorageFailure
        return original_quarantine(*args, **kwargs)

    monkeypatch.setattr(store, "quarantine", flaky)
    with pytest.raises(StorageFailure):
        run_dream(world.maintenance, None, None, world.store, None, now())
    (recorded,) = world.maintenance.runs(1)
    assert recorded.status == "failed"
    assert recorded.report == {}
    changes = world.maintenance.changes_of_run(recorded.run_id)
    assert len(changes) == 1
    assert changes[0].rows[0].id in (first, second)
