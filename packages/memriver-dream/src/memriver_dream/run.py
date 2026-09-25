"""run_dream: one maintenance run -- the safety re-scan, then the model phases, recorded.

Per-item failures are counted and retried by the next run; a store failure
stops the run, which is then recorded as failed and re-raised.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from memriver_core.models import RunTrigger
from memriver_core.models import now as _now
from memriver_core.settings import DREAM_MAX_QUARANTINE_PER_RUN, Settings

from . import consolidate, summarize
from .lock import run_lock
from .protocols import Executor, Run, TranscriptSource
from .report import PhaseReport, RunReport

if TYPE_CHECKING:
    from memriver_core.bootstrap import MaintenanceService

MODEL_PHASES = ("summarize", "consolidate", "retire")
# phase name -> runner; each model phase module registers itself here
_PHASES: dict[str, Callable[[Run, PhaseReport], None]] = {"summarize": summarize.run,
                                                          "consolidate": consolidate.run}


def _discard(line: str) -> None:
    pass


def _secrets(maintenance: MaintenanceService, report: RunReport, now: str,
             log: Callable[[str], None]) -> None:
    """Phase 0: no model, no tokens -- a memory the current policy refuses goes first."""
    phase = report.phase("secrets")
    for change in maintenance.quarantine_secrets(report.run_id, now,
                                                 DREAM_MAX_QUARANTINE_PER_RUN):
        phase.record("quarantined")
        log(f"secrets {change.rows[0].id}: quarantined ({change.reason})")   # the rule id
    log(phase.summary_line("secrets"))


def run_dream(maintenance: MaintenanceService, executor: Executor | None,
              transcripts: TranscriptSource | None, config: Settings, now: str, *,
              trigger: RunTrigger = "manual", phases: Sequence[str] = MODEL_PHASES,
              log: Callable[[str], None] = _discard,
              clock: Callable[[], str] = _now) -> RunReport:
    executor_name = None if executor is None else executor.name
    with run_lock(config.root) as held:
        if not held:
            log("skipped: locked")
            return RunReport(run_id=maintenance.record_skipped_run(trigger, executor_name, now),
                             status="skipped")
        report = RunReport(run_id=maintenance.start_run(trigger, executor_name, now),
                           status="running")
        try:
            _secrets(maintenance, report, now, log)
            ready = executor is not None and transcripts is not None and config.dream is not None
            for name in phases:
                phase = report.phase(name)
                if ready:
                    _PHASES[name](Run(maintenance=maintenance, executor=executor,
                                      transcripts=transcripts, dream=config.dream, now=now,
                                      run_id=report.run_id, log=log), phase)
                else:
                    phase.record("not-configured")
                log(phase.summary_line(name))
        except BaseException:
            report.status = "failed"
            # counts are unknown once a phase breaks mid-way -- a change it already
            # committed may be missing from the local counters -- so an interrupted
            # run stores {} rather than a confident, possibly-false zero (spec §3.6a);
            # the change log itself still lists whatever was committed. The store may
            # be what failed: recording that must not hide the cause.
            with contextlib.suppress(Exception):
                maintenance.finish_run(report.run_id, "failed", {}, clock())
            raise
        report.status = "completed"
        maintenance.finish_run(report.run_id, "completed", report.as_json(), clock())
        return report
