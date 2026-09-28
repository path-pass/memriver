"""run_dream: one dream run (spec §6.1).

The run lock; runs a crash left `running` marked failed and their reports closed;
the policy scan; with an executor, the session summaries, the project layer per
ordinary project, the project layer for global, the global layer (extraction, then
the source re-check) and TTL; then prune_reads, report retention and the finish.
Global consolidate runs before extraction and the source re-check so an entry it
flags instruction_like this run is already excluded from both, not just from TTL.
Per-item failures stay inside their phase; whatever a phase raises is a store
failure: the run is recorded as failed, its report closed, the error re-raised.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from memriver_core.models import new_id
from memriver_core.models import now as clock

from .lock import run_lock
from .phases import (
    EXTRACTION_SCOPE,
    GLOBAL_SCOPE,
    PassResult,
    consolidate,
    extract,
    recheck,
    retire,
    scan,
    summarize,
)
from .report import Report, mark_interrupted
from .settings import (
    DEFAULT_DREAM_REPORT_RETENTION_DAYS,
    DREAM_CONTEXT_BUDGET_TOKENS,
    DREAM_DB_FILENAME,
    DREAM_DIRECTORY,
    DREAM_INPUT_MARGIN_TOKENS,
    DREAM_OUTPUT_RESERVE_TOKENS,
    DREAM_REPORTS_DIRECTORY,
    DreamSettings,
)
from .store import DreamStore, RunRow, shift_days

if TYPE_CHECKING:
    from memriver_core.bootstrap import Services

    from .protocols import Executor, TranscriptSource

# the phases `memriver dream run --phase` can name; the policy scan always runs
PHASES = frozenset({"summarize", "consolidate", "extract", "retire"})


@dataclass
class Context:
    """One run's state, handed to every phase."""

    services: Services
    executor: Executor | None
    transcripts: TranscriptSource | None
    settings: DreamSettings | None
    now: str
    store: DreamStore
    report: Report
    excluded: set[str]                    # current-version policy hits (§6.2)
    history_hits: dict[str, set[int]]     # memory id -> hit versions (history only)
    groups_used: int = 0                  # changes applied this run (max_groups_per_run)
    # one call's input room: the context budget less the answer's reserve and the margin
    budget_tokens: int = (DREAM_CONTEXT_BUDGET_TOKENS - DREAM_OUTPUT_RESERVE_TOKENS
                          - DREAM_INPUT_MARGIN_TOKENS)


def run_dream(services: Services, executor: Executor | None,
              transcripts: TranscriptSource | None, settings: DreamSettings | None, *,
              root: Path, now: str, trigger: str, phases: set[str] | None = None) -> RunRow:
    """One run over the store at `root`; `settings` is the [dream] table, None when it is
    not configured. `phases` limits the model phases (None: all of them); the policy
    scan always runs. Returns the run's final row.

    Once the run's row exists, any exception -- core's StorageFailure, a dream.db
    `sqlite3.Error`, a report `OSError`, anything else -- marks the run failed (best
    effort) and is re-raised unchanged; nothing is swallowed. Before the row exists
    (dream.db unopenable, the reports directory, closing interrupted runs) the error
    is raised as it is: there is no run to record it on."""
    if phases is not None and not phases <= PHASES:
        raise ValueError(sorted(phases - PHASES))
    if trigger not in ("schedule", "manual"):
        raise ValueError(trigger)
    directory = Path(root) / DREAM_DIRECTORY
    reports = directory / DREAM_REPORTS_DIRECTORY
    with run_lock(root) as held:
        store = DreamStore(directory / DREAM_DB_FILENAME)
        reports.mkdir(mode=0o700, exist_ok=True)
        run_id = new_id()
        run = RunRow(run_id=run_id, started_at=now, finished_at=None, trigger=trigger,
                     status="running", report_file=f"{run_id}.txt")
        report = Report(reports / run.report_file, services.maintenance.check_text)
        executor_name = None if executor is None else executor.name
        if not held:
            return _skipped(store, report, run, executor_name)
        interrupted = store.running()
        for row in interrupted:
            mark_interrupted(reports / Path(row.report_file).name)
            store.finish_run(row.run_id, status="failed", finished_at=now)
        store.start_run(run)
        footer_written = False
        try:
            report.header(run_id=run_id, started_at=now, trigger=trigger,
                          executor=executor_name)
            for row in interrupted:
                report.line(f"run {row.run_id} was interrupted; marked failed")
            ctx = Context(services=services, executor=executor, transcripts=transcripts,
                          settings=settings, now=now, store=store, report=report,
                          excluded=set(), history_hits={})
            _phases(ctx, PHASES if phases is None else phases)
            report.section("Maintenance")
            report.line(f"reads pruned: {services.memory.prune_reads()}")
            days = (DEFAULT_DREAM_REPORT_RETENTION_DAYS if settings is None
                    else settings.report_retention_days)
            removed = prune_reports(store, reports, now=now, days=days)
            report.line(f"reports removed: {len(removed)}")
            finished = clock()
            # the footer first: written and only then does the row say so, so a footer
            # that cannot be written at all never gets the row marked completed
            report.footer(status="completed", finished_at=finished)
            footer_written = True
            store.finish_run(run_id, status="completed", finished_at=finished)
        except BaseException:
            failed_at = clock()
            # the store or the report may be what failed: recording that must not
            # replace the cause, which is re-raised as it is. A footer already
            # written (the completed one, above) is never written again; one that
            # was never attempted or itself failed still gets one attempt here, and
            # the row is marked failed only once some footer explains why -- a run
            # that cannot record its own outcome at all is left `running`, for the
            # next run's mark_interrupted to close it and finish its dangling line
            if not footer_written:
                with contextlib.suppress(Exception):
                    report.footer(status="failed", finished_at=failed_at)
                    footer_written = True
            if footer_written:
                with contextlib.suppress(Exception):
                    store.finish_run(run_id, status="failed", finished_at=failed_at)
            raise
        return replace(run, status="completed", finished_at=finished)


def prune_reports(store: DreamStore, reports: Path, *, now: str, days: int) -> list[RunRow]:
    """Retention (spec §5, R10): runs started more than `days` before `now` and their
    report files; nothing else. The caller holds the run lock (run_dream; `memriver
    dream report`). Each file goes first (already missing counts as gone), then its row:
    a failed unlink keeps the row, and a crash between the two leaves a row whose file
    is gone, so the next retention finishes either one. Returns the rows deleted."""
    removed = []
    for row in store.runs_before(shift_days(now, -days)):
        try:
            # the file name only: a hand-edited row never points the unlink elsewhere
            (reports / Path(row.report_file).name).unlink(missing_ok=True)
        except OSError:
            continue                    # the row stays: the next retention retries
        store.delete_run(row.run_id)
        removed.append(row)
    return removed


def _skipped(store: DreamStore, report: Report, run: RunRow,
             executor_name: str | None) -> RunRow:
    """Another run holds the lock: record a skipped run and say so; nothing else runs.

    The row exists as soon as it is inserted below, so a report failure from here
    on follows the same contract as the main path: best-effort marked failed and
    re-raised, never left `skipped` with an error swallowed.
    """
    finished = clock()
    row = replace(run, status="skipped", finished_at=finished)
    store.start_run(row)
    try:
        report.header(run_id=row.run_id, started_at=row.started_at, trigger=row.trigger,
                      executor=executor_name)
        report.line("skipped: another run holds the lock")
        report.footer(status="skipped", finished_at=finished)
    except BaseException:
        failed_at = clock()
        with contextlib.suppress(Exception):
            report.footer(status="failed", finished_at=failed_at)
        with contextlib.suppress(Exception):
            store.finish_run(row.run_id, status="failed", finished_at=failed_at)
        raise
    return row


def _phases(ctx: Context, wanted: frozenset[str] | set[str]) -> None:
    report = ctx.report
    report.section("Policy scan")
    scan.run(ctx)
    missing = [name for name, value in (("executor", ctx.executor),
                                        ("transcripts", ctx.transcripts),
                                        ("settings", ctx.settings)) if value is None]
    if missing:
        report.line(f"model phases skipped: no {' and '.join(missing)} configured")
        return
    if "summarize" in wanted:
        report.section("Session summaries")
        summarize.run(ctx)
    global_id = ctx.services.project.global_project_id()
    if "consolidate" in wanted:
        for project in ctx.services.project.list_projects():
            if project.id != global_id:
                report.section(f"Project layer: {report.safe(project.name)} ({project.id})")
                scope = f"project:{project.id}"
                _passed(ctx, scope, consolidate.run(ctx, project.id, scope))
    if "consolidate" in wanted and global_id is not None:
        report.section("Project layer: global")
        _passed(ctx, GLOBAL_SCOPE, consolidate.run(ctx, global_id, GLOBAL_SCOPE))
    if "extract" in wanted:
        report.section("Global layer: extraction")
        _passed(ctx, EXTRACTION_SCOPE, extract.run(ctx))
        report.section("Global layer: source re-check")
        recheck.run(ctx)
    if "retire" in wanted:
        report.section("TTL")
        retire.run(ctx)


def _passed(ctx: Context, scope: str, result: PassResult) -> None:
    """§6.9: a scope's digest is stored only when its pass finished."""
    if result.finished and result.digest is not None:
        ctx.store.put_scope_pass(scope, result.digest, clock())
