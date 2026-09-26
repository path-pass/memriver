"""Backend-neutral diagnostics policy over a `StoreInspector`.

`DiagnosticsService` owns the checks no backend should have to reimplement --
staleness, near-duplicate bodies -- and maps backend-reported findings into
the same neutral shape. It never touches a file, a table, or any other
storage detail; that all lives behind `StoreInspector`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import TYPE_CHECKING

from memriver_core.models import DiagnosticFinding, DiagnosticsReport
from memriver_core.models import now as _default_now

if TYPE_CHECKING:
    from memriver_core.models import (
        DiagnosticsState,
        StoreFinding,
        StoreReport,
    )
    from memriver_core.repository.inspection_protocol import StoreInspector

# Fixed, client-safe wording per backend finding kind (the concrete kinds the
# SQLite inspector reports today; an unrecognized future kind still gets a
# safe generic suggestion rather than crashing the umbrella check).
_BACKEND_SUGGESTIONS = {
    "unknown-schema": "restore the database from a backup made by this memriver version",
    "unsafe-database": "replace it with the real database file",
    "integrity": "restore the database from a backup",
    "orphan": "restore the missing project from a backup; until then the memory stays hidden",
    "invalid-row": "fix or remove the row",
    "non-canonical-root": "bind the real directory with memriver project unbind and adopt",
    "unverifiable-root": "restore access to the directory, then run memriver doctor again",
    "root-conflict": "unbind one of them with memriver project unbind",
    "legacy-layout": "migrate it, or remove it once migrated",
    "session-orphan": ("restore the missing project from a backup; until then the session's "
                       "project is unreachable"),
}
_DEFAULT_BACKEND_SUGGESTION = "inspect this entry manually; its finding kind is unrecognized"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _trigrams(body: str) -> frozenset[str]:
    normalized = " ".join(body.lower().split())
    return frozenset(normalized[i:i + 3] for i in range(len(normalized) - 2))


def _map_backend_finding(finding: StoreFinding) -> DiagnosticFinding:
    return DiagnosticFinding(
        kind=finding.kind,
        memory_ids=(finding.memory_id,) if finding.memory_id else (),
        project_ids=(finding.project_id,) if finding.project_id else (),
        location_hints=(finding.location_hint,),
        reason=finding.reason,
        suggestion=_BACKEND_SUGGESTIONS.get(finding.kind, _DEFAULT_BACKEND_SUGGESTION),
    )


def _stale_cutoff(now_dt: datetime, stale_days: int) -> datetime:
    try:
        return now_dt - timedelta(days=stale_days)
    except OverflowError:
        # the cutoff underflows datetime's representable range (either
        # `timedelta(days=...)` itself overflows, or the subtraction pushes
        # past `datetime.min`). datetime.min is already earlier than any
        # representable timestamp, so nothing can be staler than it -- no new
        # setting, no product cap, just the honest bound.
        return datetime.min.replace(tzinfo=UTC)


def _staleness_findings(report: StoreReport, now_dt: datetime,
                        stale_days: int) -> list[DiagnosticFinding]:
    cutoff = _stale_cutoff(now_dt, stale_days)
    findings: list[DiagnosticFinding] = []
    for entry in report.entries:
        memory = entry.memory
        try:
            updated_dt = _timestamp(memory.updated)
        except (ValueError, OverflowError):
            findings.append(DiagnosticFinding(
                kind="invalid-updated",
                memory_ids=(memory.id,),
                project_ids=(memory.project_id,),
                location_hints=(entry.location_hint,),
                reason="stored 'updated' value is not a valid timezone-aware timestamp",
                suggestion="fix or remove the malformed 'updated' timestamp",
            ))
            continue
        if updated_dt < cutoff:
            findings.append(DiagnosticFinding(
                kind="stale",
                memory_ids=(memory.id,),
                project_ids=(memory.project_id,),
                location_hints=(entry.location_hint,),
                reason=f"not updated in over {stale_days} days",
                suggestion="review and refresh, or delete, this memory",
            ))
    return findings


def _duplicate_findings(report: StoreReport,
                        jaccard_threshold: float) -> list[DiagnosticFinding]:
    ordered = sorted(
        report.entries,
        key=lambda e: (e.memory.project_id, e.memory.id, e.location_hint),
    )
    grams = [(entry, _trigrams(entry.memory.body)) for entry in ordered]
    findings: list[DiagnosticFinding] = []
    # ponytail: O(n^2) pairwise scan over the whole store, no comparison cap
    # (spec DEFERRED-3 -- local-store scale keeps this sub-second). Re-enter
    # with time-bounding or MinHash if team-scale stores make it slow.
    for (entry_a, grams_a), (entry_b, grams_b) in combinations(grams, 2):
        if not grams_a or not grams_b:
            continue
        id_a, id_b = entry_a.memory.id, entry_b.memory.id
        if (id_a, id_b) in report.sources or (id_b, id_a) in report.sources:
            # dream deliberately keeps a source until the TTL review retires
            # it, so it and what it derived are expected to still read alike
            continue
        jaccard = len(grams_a & grams_b) / len(grams_a | grams_b)
        if jaccard >= jaccard_threshold:
            findings.append(DiagnosticFinding(
                kind="near-duplicate",
                memory_ids=(entry_a.memory.id, entry_b.memory.id),
                project_ids=(entry_a.memory.project_id, entry_b.memory.project_id),
                location_hints=(entry_a.location_hint, entry_b.location_hint),
                reason=f"bodies are {jaccard:.0%} similar (>= {jaccard_threshold:.0%} threshold)",
                suggestion="merge or remove the near-duplicate memory",
            ))
    return findings


def _derive_state(report: StoreReport,
                  findings: list[DiagnosticFinding]) -> DiagnosticsState:
    # a finding outranks everything: a pre-release or damaged store must never
    # read as merely "not initialized yet"
    if findings:
        return "degraded"
    if not report.initialized:
        return "uninitialized"
    if not report.entries:
        return "empty"
    return "healthy"


class DiagnosticsService:
    def __init__(self, inspector: StoreInspector) -> None:
        self._inspector = inspector

    def run(self, *, now: str | None = None, stale_days: int = 90,
            jaccard_threshold: float = 0.6) -> DiagnosticsReport:
        if stale_days <= 0:
            raise ValueError("stale_days must be a positive number of days")
        if not (0 < jaccard_threshold <= 1):
            raise ValueError("jaccard_threshold must be in the range (0, 1]")
        # a malformed `now` is a caller error and must fail before the
        # inspector is ever asked to walk the store
        now_dt = _timestamp(now if now is not None else _default_now())

        report = self._inspector.inspect()

        findings = [_map_backend_finding(f) for f in report.findings]
        findings.extend(_staleness_findings(report, now_dt, stale_days))
        findings.extend(_duplicate_findings(report, jaccard_threshold))

        return DiagnosticsReport(state=_derive_state(report, findings),
                                 findings=tuple(findings), initialized=report.initialized,
                                 projects=report.projects)
