"""run_dream (spec §6.1): the lock, interrupted runs, the phase order, the no-executor
path, the phases filter, digest storage (§6.9) and report retention (§5).

The model phases are replaced by recorders: their own behavior is tested with them.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import memriver_dream.report as report_module
import pytest
from memriver_core import StorageFailure
from memriver_core.models.changes import Update
from memriver_dream.changes import apply_group
from memriver_dream.lock import run_lock
from memriver_dream.phases import (
    PassResult,
    consolidate,
    extract,
    recheck,
    retire,
    summarize,
)
from memriver_dream.protocols import ExecutorResult
from memriver_dream.report import INTERRUPTED, WITHHELD, Report
from memriver_dream.run import prune_reports
from memriver_dream.store import DreamStore, ReviewRow, RunRow, SummaryRow, shift_days


@pytest.fixture
def calls(world, monkeypatch) -> list[tuple]:
    seen: list[tuple] = []

    def recorder(name: str):
        def phase(ctx, *args):
            seen.append((name, *args))
            return PassResult(finished=True)
        return phase

    for module in (summarize, consolidate, extract, recheck, retire):
        monkeypatch.setattr(module, "run", recorder(module.__name__.rpartition(".")[2]))
    monkeypatch.setattr(world.services.memory, "prune_reads",
                        lambda: seen.append(("prune_reads",)) or 0)
    return seen


def _store(world) -> DreamStore:
    return DreamStore(world.root / "dream" / "dream.db")


def test_the_phases_run_in_spec_order_projects_before_global(world, calls):
    other = world.root.parent / "other"
    other.mkdir()
    second = world.services.project.init_project(
        "second", world.services.project.plan_root(str(other)))
    row = world.run()
    assert (row.status, row.trigger, row.started_at) == ("completed", "manual", world.now)
    assert row.finished_at is not None
    demo, glob = world.project.id, world.global_id
    assert calls == [("summarize",),
                     ("consolidate", demo, f"project:{demo}"),
                     ("consolidate", second.id, f"project:{second.id}"),
                     ("consolidate", glob, "global"),
                     ("extract",), ("recheck",),
                     ("retire",), ("prune_reads",)]
    text = world.report_text(row)
    assert [line for line in text.splitlines() if line.startswith("== ")] == [
        "== Policy scan ==", "== Session summaries ==",
        f"== Project layer: demo ({demo}) ==", f"== Project layer: second ({second.id}) ==",
        "== Project layer: global ==", "== Global layer: extraction ==",
        "== Global layer: source re-check ==", "== TTL ==", "== Maintenance =="]
    assert text.startswith(f"memriver dream run {row.run_id}\nstarted: {world.now}\n"
                           "trigger: manual\nexecutor: fake\n")
    assert text.splitlines()[-2:] == ["status: completed", f"finished: {row.finished_at}"]
    assert _store(world).run(row.run_id) == row


@pytest.mark.parametrize("missing", ["executor", "transcripts", "settings"])
def test_without_an_executor_a_run_does_the_scan_and_the_upkeep_only(world, calls, missing):
    row = world.run(**{missing: None})
    assert row.status == "completed"
    assert calls == [("prune_reads",)]
    text = world.report_text(row)
    assert "== Policy scan ==\npolicy hits: 0; left out of model steps: 0\n" in text
    assert f"model phases skipped: no {missing} configured\n" in text
    assert "== Maintenance ==\nreads pruned: 0\nreports removed: 0\n" in text


def test_the_skipped_message_names_every_missing_piece(world, calls):
    row = world.run(executor=None, settings=None)
    assert "model phases skipped: no executor and settings configured\n" in world.report_text(row)


def test_an_unknown_trigger_is_refused_before_anything_runs(world, calls):
    with pytest.raises(ValueError):
        world.run(trigger="cron")
    assert calls == []
    assert not (world.root / "dream").exists()


def test_an_unknown_trigger_never_closes_a_stale_running_row(world, calls):
    store = _store(world)
    world.reports.mkdir(parents=True)
    crashed = RunRow("crashed005", shift_days(world.now, -1), None, "schedule", "running",
                     "crashed005.txt")
    store.start_run(crashed)
    with pytest.raises(ValueError):
        world.run(trigger="cron")
    assert store.run(crashed.run_id) == crashed


@pytest.mark.parametrize(("phases", "expected"), [
    (set(), []),
    ({"summarize"}, ["summarize"]),
    ({"extract"}, ["extract", "recheck"]),
    ({"retire"}, ["retire"]),
    ({"consolidate"}, ["consolidate", "consolidate"]),
])
def test_the_phases_filter_runs_only_the_named_phases(world, calls, phases, expected):
    world.run(phases=phases)
    assert [call[0] for call in calls] == [*expected, "prune_reads"]


def test_an_unknown_phase_is_refused_before_anything_runs(world, calls):
    with pytest.raises(ValueError):
        world.run(phases={"scan"})
    assert calls == []
    assert not (world.root / "dream").exists()


def test_a_held_lock_makes_a_skipped_run_that_runs_nothing(world, calls):
    with run_lock(world.root) as held:
        assert held
        row = world.run()
    assert (row.status, row.started_at) == ("skipped", world.now)
    assert row.finished_at is not None
    assert calls == []
    assert _store(world).run(row.run_id) == row
    text = world.report_text(row)
    assert "skipped: another run holds the lock\n" in text
    assert text.splitlines()[-2] == "status: skipped"


def test_a_lock_conflict_report_failure_is_marked_failed_too(world, calls, monkeypatch):
    # the skipped row already exists once inserted: a report failure from there on
    # follows the same "row exists -> best-effort failed, re-raise" contract as an
    # ordinary run, rather than leaving the row permanently `skipped`
    def broken_header(self, **fields):
        raise OSError("injected")

    monkeypatch.setattr(Report, "header", broken_header)
    with run_lock(world.root) as held:
        assert held
        with pytest.raises(OSError, match="^injected$"):
            world.run()
    (row,) = _store(world).runs(10)
    assert row.status == "failed" and row.finished_at is not None
    assert calls == []


def test_a_run_left_running_by_a_crash_is_marked_failed_and_its_line_unknown(world, calls):
    # §10 item 12: the dangling "applying" line is marked after a crash
    store = _store(world)
    world.reports.mkdir(parents=True)
    crashed = RunRow("crashed001", shift_days(world.now, -1), None, "schedule", "running",
                     "crashed001.txt")
    store.start_run(crashed)
    (world.reports / crashed.report_file).write_text(
        "memriver dream run crashed001\n\n== Project layer: demo ==\n"
        "applying merge aaaaaaaaaa bbbbbbbbbb (creates a memory)")
    row = world.run()
    assert row.status == "completed"
    assert store.run(crashed.run_id) == replace(crashed, status="failed",
                                                finished_at=world.now)
    assert (world.reports / crashed.report_file).read_text().endswith(
        "applying merge aaaaaaaaaa bbbbbbbbbb (creates a memory) -> outcome unknown — see "
        "memriver history aaaaaaaaaa; see memriver history bbbbbbbbbb; a created memory, "
        "if any, is not listed — see memriver list\n"
        f"\n{INTERRUPTED}\nstatus: failed\n")
    assert "run crashed001 was interrupted; marked failed\n" in world.report_text(row)


def test_a_crashed_runs_unreadable_report_still_lets_its_row_close(world, calls):
    # a directory (or any other unreadable path) at the old report's location must
    # not stop the stale `running` row from being closed on the next run
    store = _store(world)
    world.reports.mkdir(parents=True)
    crashed = RunRow("crashed002", shift_days(world.now, -1), None, "schedule", "running",
                     "crashed002.txt")
    store.start_run(crashed)
    (world.reports / crashed.report_file).mkdir()
    row = world.run()
    assert row.status == "completed"
    assert store.run(crashed.run_id) == replace(crashed, status="failed",
                                                finished_at=world.now)


def test_a_crashed_runs_unwritable_report_still_lets_its_row_close(world, calls):
    # an OSError while appending to the old report (made read-only) must not stop the
    # stale `running` row from being closed on the next run either
    store = _store(world)
    world.reports.mkdir(parents=True)
    crashed = RunRow("crashed004", shift_days(world.now, -1), None, "schedule", "running",
                     "crashed004.txt")
    store.start_run(crashed)
    report_path = world.reports / crashed.report_file
    report_path.write_text("memriver dream run crashed004\n\napplying merge aaaaaaaaaa")
    report_path.chmod(0o400)
    try:
        row = world.run()
    finally:
        report_path.chmod(0o600)
    assert row.status == "completed"
    assert store.run(crashed.run_id) == replace(crashed, status="failed",
                                                finished_at=world.now)


def test_a_scope_digest_is_stored_only_when_its_pass_finished(world, calls, monkeypatch):
    # §10 item 9 (digest storage rules): finished + digest stores; unfinished stores nothing
    store = _store(world)
    store.put_scope_pass("global", "before", world.now)
    project_scope = f"project:{world.project.id}"
    results = {project_scope: PassResult(finished=True, digest="d-project"),
               "global": PassResult(finished=False, digest="d-global")}
    monkeypatch.setattr(consolidate, "run", lambda ctx, project_id, scope: results[scope])
    monkeypatch.setattr(extract, "run", lambda ctx: PassResult(finished=True))
    world.run()
    assert store.scope_digest(project_scope) == "d-project"
    assert store.scope_digest("global") == "before"
    assert store.scope_digest("extraction") is None


def test_a_finished_extraction_stores_its_own_scope(world, calls, monkeypatch):
    monkeypatch.setattr(extract, "run", lambda ctx: PassResult(finished=True, digest="d-x"))
    world.run(phases={"extract"})
    assert _store(world).scope_digest("extraction") == "d-x"


def test_a_store_failure_fails_the_run_closes_its_report_and_frees_the_lock(
        world, calls, monkeypatch):
    def broken(ctx):
        raise StorageFailure

    monkeypatch.setattr(summarize, "run", broken)
    with pytest.raises(StorageFailure):
        world.run()
    (row,) = _store(world).runs(10)
    assert row.status == "failed" and row.finished_at is not None
    assert world.report_text(row).splitlines()[-2] == "status: failed"
    assert calls == []                  # nothing after the failing phase, not even upkeep
    with run_lock(world.root) as held:
        assert held


def _settings(world, retention):
    if retention == "unconfigured":
        return None
    if retention == "default":
        return world.dream
    return world.dream.model_copy(update={"report_retention_days": retention})


@pytest.mark.parametrize(("retention", "old_kept"), [
    ("default", False), ("unconfigured", False), (40, True)])
def test_retention_deletes_only_old_runs_and_their_report_files(world, calls, retention,
                                                                old_kept):
    # §10 item 12: retention deletes only report files and runs
    store = _store(world)
    world.reports.mkdir(parents=True)
    old_at, kept_at = shift_days(world.now, -31), shift_days(world.now, -29)
    old = RunRow("expired001", old_at, old_at, "schedule", "completed", "expired001.txt")
    kept = RunRow("recent0001", kept_at, kept_at, "schedule", "completed", "recent0001.txt")
    for row in (old, kept):
        store.start_run(row)
        (world.reports / row.report_file).write_text("report\n")
    (world.reports / "notes.txt").write_text("not a report\n")
    ancient = shift_days(world.now, -400)
    review = ReviewRow("aaaaaaaaaa", 1, "keep", 0, ancient, shift_days(ancient, 30), "in use")
    summary = SummaryRow("claude-code", "s1", ancient, ancient, "d", 3, True, "ok", None)
    store.put_review(review)
    store.put_scope_pass("global", "d", ancient)
    store.put_source_check("aaaaaaaaaa", "d", ancient)
    store.put_summary(summary)
    row = world.run(settings=_settings(world, retention))
    assert (store.run(old.run_id) is not None) is old_kept
    assert (world.reports / old.report_file).exists() is old_kept
    assert store.run(kept.run_id) == kept and (world.reports / kept.report_file).exists()
    assert (world.reports / "notes.txt").exists()
    assert (world.reports / row.report_file).exists()
    assert store.review("aaaaaaaaaa") == review
    assert store.scope_digest("global") == "d"
    assert store.source_check("aaaaaaaaaa") == "d"
    assert store.summary("claude-code", "s1") == summary
    assert f"reports removed: {0 if old_kept else 1}\n" in world.report_text(row)


def _expired(world, store, run_id: str) -> RunRow:
    at = shift_days(world.now, -31)
    row = RunRow(run_id, at, at, "schedule", "completed", f"{run_id}.txt")
    store.start_run(row)
    (world.reports / row.report_file).write_text("report\n")
    return row


def test_a_failed_unlink_keeps_its_row_for_the_next_retention(world, monkeypatch):
    # §10 item 12: each file goes before its row, so a failure leaves nothing orphaned
    store = _store(world)
    world.reports.mkdir(parents=True)
    stuck, gone = _expired(world, store, "stuckrun01"), _expired(world, store, "gonerun001")
    real_unlink = Path.unlink

    def unlink(path, missing_ok=False):
        if path.name == stuck.report_file:
            raise PermissionError
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert prune_reports(store, world.reports, now=world.now, days=30) == [gone]
    assert store.run(stuck.run_id) == stuck and (world.reports / stuck.report_file).exists()
    assert store.run(gone.run_id) is None and not (world.reports / gone.report_file).exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert prune_reports(store, world.reports, now=world.now, days=30) == [stuck]
    assert store.run(stuck.run_id) is None and not (world.reports / stuck.report_file).exists()


def test_a_crash_between_unlink_and_row_delete_is_finished_next_time(world, monkeypatch):
    store = _store(world)
    world.reports.mkdir(parents=True)
    row = _expired(world, store, "crashrun01")

    def crash(self, run_id):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(DreamStore, "delete_run", crash)
    with pytest.raises(sqlite3.OperationalError):
        prune_reports(store, world.reports, now=world.now, days=30)
    assert not (world.reports / row.report_file).exists()     # the file went first
    assert store.run(row.run_id) == row                       # the row is still there
    monkeypatch.undo()
    assert prune_reports(store, world.reports, now=world.now, days=30) == [row]
    assert store.run(row.run_id) is None


def test_a_run_that_cannot_record_its_own_failure_leaves_the_row_running(
        world, calls, monkeypatch):
    # every append after the change fails (e.g. a full disk): neither the completion
    # footer nor the failure footer can be written, so the row must stay `running`
    # for the next run's mark_interrupted to close it and finish the dangling line
    memory_id = world.create(world.project.id, "old body")
    real_append = report_module._append
    tripped = False

    def flaky(path, text):
        nonlocal tripped
        if tripped or text.startswith(" -> change"):
            tripped = True
            raise OSError("injected")
        real_append(path, text)

    monkeypatch.setattr(report_module, "_append", flaky)

    def broken(ctx, project_id, scope):
        apply_group(ctx, "rewrite", [memory_id],
                    [Update(memory_id=memory_id, expected_version=1, body="new body")])
        return PassResult(finished=True)   # unreachable: apply_group raises above

    monkeypatch.setattr(consolidate, "run", broken)
    with pytest.raises(OSError, match="^injected$"):
        world.run()
    store = _store(world)
    (row,) = store.runs(10)
    assert row.status == "running" and row.finished_at is None

    monkeypatch.setattr(report_module, "_append", real_append)
    monkeypatch.setattr(consolidate, "run",
                        lambda ctx, project_id, scope: PassResult(finished=True))
    second = world.run()
    assert second.status == "completed"
    assert store.run(row.run_id).status == "failed"
    text = (world.reports / row.report_file).read_text()
    assert text.endswith(
        f"applying rewrite {memory_id} -> outcome unknown — see memriver history "
        f"{memory_id}\n"
        f"\n{INTERRUPTED}\nstatus: failed\n")
    assert f"run {row.run_id} was interrupted; marked failed\n" in world.report_text(second)


def test_max_groups_per_run_is_shared_across_phases(world):
    # §10 item 9: groups_used lives on the shared Context, so a limit already reached
    # in one phase carries into a later one, not reset per phase
    a = world.create(world.project.id, "uv manages python")
    b = world.create(world.project.id, "python is managed with uv")
    victim = world.create(world.project.id, "the staging host is stage-3")
    aged = shift_days(world.now, -60)
    world.sql("UPDATE memories SET created = ?, updated = ?, last_read_at = ? WHERE id = ?",
              aged, aged, aged, victim)
    world.executor.replies = [
        {"judgments": [{"kind": "merge", "ids": [a, b], "id": "", "by": "",
                        "evidence_ids": [], "type": "project", "description": "python tooling",
                        "body": "Use uv to manage python.", "reason": "same fact"}]},
        {"decision": "delete", "reason": "no longer relevant", "evidence": []},
    ]
    limited = world.dream.model_copy(update={"max_groups_per_run": 1})
    row = world.run(phases={"consolidate", "retire"}, settings=limited)
    assert row.status == "completed"
    text = world.report_text(row)
    assert "applying merge" in text and " -> change" in text
    assert f"not applied (group limit): retire {victim}\n" in text


def _fail_header(monkeypatch):
    def header(self, **fields):
        raise OSError("injected")
    monkeypatch.setattr(Report, "header", header)
    return OSError


def _fail_scope_pass(monkeypatch):
    def put_scope_pass(self, scope, digest, at):
        raise sqlite3.OperationalError("injected")
    monkeypatch.setattr(DreamStore, "put_scope_pass", put_scope_pass)
    monkeypatch.setattr(extract, "run", lambda ctx: PassResult(finished=True, digest="d-x"))
    return sqlite3.OperationalError


def _fail_completed_finish(monkeypatch):
    real_finish = DreamStore.finish_run

    def finish_run(self, run_id, *, status, finished_at):
        if status == "completed":
            raise sqlite3.OperationalError("injected")
        real_finish(self, run_id, status=status, finished_at=finished_at)

    monkeypatch.setattr(DreamStore, "finish_run", finish_run)
    return sqlite3.OperationalError


@pytest.mark.parametrize("inject", [_fail_header, _fail_scope_pass, _fail_completed_finish])
def test_a_dream_store_or_report_error_after_the_row_exists_fails_the_run(
        world, calls, monkeypatch, inject):
    # the header write, a dream.db write in the middle, the final bookkeeping: each is
    # recorded as a failed run and re-raised unchanged, never swallowed
    error = inject(monkeypatch)
    with pytest.raises(error, match="^injected$"):
        world.run()
    (row,) = _store(world).runs(10)
    assert row.status == "failed" and row.finished_at is not None
    with run_lock(world.root) as held:
        assert held


def test_an_error_before_the_row_exists_is_raised_with_no_run_recorded(world, calls):
    (world.root / "dream").mkdir(mode=0o700)
    (world.root / "dream" / "dream.db").write_bytes(b"not a database, just bytes" * 8)
    with pytest.raises(sqlite3.DatabaseError):
        world.run()
    assert calls == []


def test_the_report_names_a_policy_hit_but_never_its_text(world, calls):   # §10 item 12
    memory_id = world.create(world.project.id, "plain body", description="plain cue")
    world.plant(memory_id, 1, body=world.secret)
    row = world.run()
    text = world.report_text(row)
    rule = world.services.maintenance.check_text(world.secret)
    assert (f"policy hit: {memory_id} v1 (current) rule {rule} — memriver delete "
            f"{memory_id} --hard\n") in text
    assert world.secret not in text and "ghp_" not in text and "plain body" not in text
    assert text.index("== Maintenance ==") < text.index("== Needs you ==")


def test_a_project_name_that_hits_the_policy_is_withheld_in_its_section_title(world):
    other = world.root.parent / "secret-project"
    other.mkdir()
    secret_project = world.services.project.init_project(
        world.secret, world.services.project.plan_root(str(other)))
    row = world.run()
    text = world.report_text(row)
    assert f"== Project layer: {WITHHELD} ({secret_project.id}) ==" in text
    assert world.secret not in text and "ghp_" not in text


def test_an_uncaught_apply_group_failure_still_marks_its_line_unknown_end_to_end(
        world, monkeypatch):
    # a failure inside apply_group that is neither BatchConflict nor ContentRejected
    # (here, a transient report I/O error after core already committed) is not a
    # per-item failure a phase can swallow: it propagates through run_dream's own
    # failure handling, which still closes the dangling line as "outcome unknown"
    # and marks the run failed
    memory_id = world.create(world.project.id, "old body")
    real_append = report_module._append

    def flaky(path, text):
        if text.startswith(" -> change"):
            raise OSError("injected")
        real_append(path, text)

    monkeypatch.setattr(report_module, "_append", flaky)

    def broken(ctx, project_id, scope):
        apply_group(ctx, "rewrite", [memory_id],
                    [Update(memory_id=memory_id, expected_version=1, body="new body")])
        return PassResult(finished=True)   # unreachable: apply_group raises above

    monkeypatch.setattr(consolidate, "run", broken)
    with pytest.raises(OSError, match="^injected$"):
        world.run()
    (row,) = _store(world).runs(10)
    assert row.status == "failed"
    text = world.report_text(row)
    assert (f"applying rewrite {memory_id} -> outcome unknown — see memriver history "
            f"{memory_id}\n") in text
    assert [version.version for version in world.services.memory.versions(memory_id)] == [1, 2]


def test_the_budget_setting_sizes_every_call_and_too_large_scopes_go_to_needs_you(world):
    world.create(world.project.id, "a project fact")
    room = 1                                   # 20_001 less the 20_000 reserve and margin
    row = world.run(settings=world.dream.model_copy(update={"context_budget_tokens": 20_001}))
    needs = world.report_text(row).split("== Needs you ==\n", 1)[1]
    for subject in (f"project:{world.project.id}", "extraction"):
        assert re.search(rf"^{subject}: input too large \(\d+/{room} tokens\); not processed "
                         r"— raise \[dream\] context_budget_tokens$", needs, re.MULTILINE), subject
    assert world.executor.calls == []


def test_the_default_budget_leaves_180k_tokens_of_input_room(world):
    assert world.context().budget_tokens == 200_000 - 20_000


LOGIN = ("executor fake: login failure — check the executor's login; for API-key, Bedrock "
         "or Vertex auth see [dream] claude_settings / codex_overrides")
QUOTA = "executor fake: quota failure — the executor's usage limit was hit"


def _second_project(world) -> str:
    directory = world.root.parent / "second"
    directory.mkdir()
    return world.services.project.init_project(
        "second", world.services.project.plan_root(str(directory))).id


def _needs(world, row) -> list[str]:
    text = world.report_text(row)
    return text.split("== Needs you ==\n", 1)[1].split("\n\nstatus:")[0].splitlines()


def test_the_first_login_failure_of_a_run_is_one_needs_you_line(world):
    world.create(world.project.id, "a demo fact")
    world.create(_second_project(world), "a second fact")
    world.executor.default = ExecutorResult(error="login")
    row = world.run()
    assert len(world.executor.calls) >= 3                   # two scopes and the extraction
    assert _needs(world, row).count(LOGIN) == 1
    assert row.status == "completed"


def test_login_and_quota_each_get_their_own_line_and_other_kinds_none(world):
    world.create(world.project.id, "a demo fact")
    world.create(_second_project(world), "a second fact")
    world.executor.replies = [ExecutorResult(error="quota"), ExecutorResult(error="timeout"),
                              ExecutorResult(error="login"), ExecutorResult(error="quota")]
    row = world.run()
    needs = _needs(world, row)
    assert (needs.count(LOGIN), needs.count(QUOTA)) == (1, 1)
    assert not [line for line in needs if "timeout" in line]
