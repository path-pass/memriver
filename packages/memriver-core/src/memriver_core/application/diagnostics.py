"""Backend-neutral diagnostics policy over a `StoreInspector`.

`DiagnosticsService` owns the checks no backend should have to reimplement --
staleness, near-duplicate bodies, global-vs-project shadowing -- and maps
backend-reported findings into the same neutral shape. It never touches a
file, a table, or any other storage detail; that all lives behind
`StoreInspector`.
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
        InspectedMemory,
        StoreFinding,
        StoreReport,
    )
    from memriver_core.repository.inspection_protocol import StoreInspector

# Fixed, client-safe wording per backend finding kind (the concrete kinds a
# filesystem-style inspector reports today; an unrecognized future kind still
# gets a safe generic suggestion rather than crashing the umbrella check).
_BACKEND_SUGGESTIONS = {
    "unreadable-file": "restore read access to the entry, or remove it",
    "unparsable": "fix or remove the entry so it decodes as a memory",
    "scope-directory-mismatch": "move the entry to the location matching its stored scope",
    "id-stem-mismatch": "rename the entry so its location matches its stored id",
    "unaddressable-id": "rename the entry to an id the memory API can address",
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
        scopes=(finding.scope,) if finding.scope else (),
        location_hints=(finding.location_hint,),
        reason=finding.reason,
        suggestion=_BACKEND_SUGGESTIONS.get(finding.kind, _DEFAULT_BACKEND_SUGGESTION),
    )


def _staleness_findings(report: StoreReport, now_dt: datetime,
                        stale_days: int) -> list[DiagnosticFinding]:
    cutoff = now_dt - timedelta(days=stale_days)
    findings: list[DiagnosticFinding] = []
    for entry in report.entries:
        memory = entry.memory
        try:
            updated_dt = _timestamp(memory.updated)
        except ValueError:
            findings.append(DiagnosticFinding(
                kind="invalid-updated",
                memory_ids=(memory.id,),
                scopes=(memory.scope,),
                location_hints=(entry.location_hint,),
                reason="stored 'updated' value is not a valid timezone-aware timestamp",
                suggestion="fix or remove the malformed 'updated' timestamp",
            ))
            continue
        if updated_dt < cutoff:
            findings.append(DiagnosticFinding(
                kind="stale",
                memory_ids=(memory.id,),
                scopes=(memory.scope,),
                location_hints=(entry.location_hint,),
                reason=f"not updated in over {stale_days} days",
                suggestion="review and refresh, or delete, this memory",
            ))
    return findings


def _duplicate_findings(report: StoreReport,
                        jaccard_threshold: float) -> list[DiagnosticFinding]:
    ordered = sorted(
        report.entries,
        key=lambda e: (e.memory.scope.to_storage(), e.memory.id, e.location_hint),
    )
    grams = [(entry, _trigrams(entry.memory.body)) for entry in ordered]
    findings: list[DiagnosticFinding] = []
    # ponytail: O(n^2) pairwise scan over the whole store, no comparison cap
    # (spec DEFERRED-3 -- local-store scale keeps this sub-second). Re-enter
    # with time-bounding or MinHash if team-scale stores make it slow.
    for (entry_a, grams_a), (entry_b, grams_b) in combinations(grams, 2):
        if not grams_a or not grams_b:
            continue
        jaccard = len(grams_a & grams_b) / len(grams_a | grams_b)
        if jaccard >= jaccard_threshold:
            findings.append(DiagnosticFinding(
                kind="near-duplicate",
                memory_ids=(entry_a.memory.id, entry_b.memory.id),
                scopes=(entry_a.memory.scope, entry_b.memory.scope),
                location_hints=(entry_a.location_hint, entry_b.location_hint),
                reason=f"bodies are {jaccard:.0%} similar (>= {jaccard_threshold:.0%} threshold)",
                suggestion="merge or remove the near-duplicate memory",
            ))
    return findings


def _shadowing_findings(report: StoreReport) -> list[DiagnosticFinding]:
    by_id: dict[str, list[InspectedMemory]] = {}
    for entry in report.entries:
        by_id.setdefault(entry.memory.id, []).append(entry)
    findings: list[DiagnosticFinding] = []
    for memory_id, entries in sorted(by_id.items()):
        has_global = any(e.memory.scope.project_id is None for e in entries)
        has_project = any(e.memory.scope.project_id is not None for e in entries)
        if not (has_global and has_project):
            continue
        ordered = sorted(entries, key=lambda e: (e.memory.scope.to_storage(), e.location_hint))
        findings.append(DiagnosticFinding(
            kind="shadowing",
            memory_ids=(memory_id,),
            scopes=tuple(e.memory.scope for e in ordered),
            location_hints=tuple(e.location_hint for e in ordered),
            reason="same id exists in both a global scope and a project scope",
            suggestion="rename one entry, or confirm the shadowing is intentional",
        ))
    return findings


def _derive_state(report: StoreReport,
                  findings: list[DiagnosticFinding]) -> DiagnosticsState:
    if not report.initialized:
        return "uninitialized"
    if findings:
        return "degraded"
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
        findings.extend(_shadowing_findings(report))

        return DiagnosticsReport(state=_derive_state(report, findings),
                                 findings=tuple(findings))
