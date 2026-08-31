"""Rendering and CLI wiring for `memriver doctor`.

Every diagnostic rule lives in memriver_core, reached only through
memriver_core.bootstrap.build_diagnostics_service. This module owns exit
codes, fixed state messages, and JSON/human rendering -- it never touches the
store itself, and [DEFERRED-4] performs no harness-configuration audit (see
spec S10).
"""

from __future__ import annotations

from typing import IO, TYPE_CHECKING

from .core_logging import quiet_core_logging

if TYPE_CHECKING:
    from pathlib import Path

    from memriver_core.models import DiagnosticFinding, DiagnosticsReport

# Fixed per spec S6.2; the inaccessible message is stderr-only and path-free.
_STATE_MESSAGES = {
    "uninitialized": "store not initialized yet",
    "empty": "store is initialized and empty",
    "healthy": "store is healthy",
    "degraded": "store has findings",
}
_INACCESSIBLE_MESSAGE = "memriver doctor: memory store is inaccessible"
_EXIT_CODES = {"uninitialized": 0, "empty": 0, "healthy": 0, "degraded": 1}


def _finding_to_dict(finding: DiagnosticFinding) -> dict:
    return {
        "kind": finding.kind,
        "memory_ids": list(finding.memory_ids),
        "scopes": [scope.to_storage() for scope in finding.scopes],
        "location_hints": list(finding.location_hints),
        "reason": finding.reason,
        "suggestion": finding.suggestion,
    }


def _render_json(report: DiagnosticsReport, stdout: IO[str]) -> None:
    import json

    stdout.write(json.dumps({
        "state": report.state,
        "findings": [_finding_to_dict(f) for f in report.findings],
    }, indent=2) + "\n")


def _render_human(report: DiagnosticsReport, stdout: IO[str]) -> None:
    stdout.write(_STATE_MESSAGES[report.state] + "\n")
    by_kind: dict[str, list[DiagnosticFinding]] = {}
    for finding in report.findings:
        by_kind.setdefault(finding.kind, []).append(finding)
    for kind in sorted(by_kind):
        stdout.write(f"\n{kind}:\n")
        for finding in by_kind[kind]:
            scopes = ", ".join(scope.to_storage() for scope in finding.scopes)
            locations = ", ".join(finding.location_hints)
            stdout.write(f"  - scopes: {scopes}\n")
            stdout.write(f"    locations: {locations}\n")
            stdout.write(f"    reason: {finding.reason}\n")
            stdout.write(f"    suggestion: {finding.suggestion}\n")


def run_doctor(*, root: Path | None, json_output: bool, stale_days: int,
              stdout: IO[str], stderr: IO[str]) -> int:
    # imported here, not at module scope, to match the rest of the umbrella's
    # lazy-import convention for the memriver_core stack
    from memriver_core.bootstrap import build_diagnostics_service
    from memriver_core.config import load_settings

    try:
        with quiet_core_logging():
            settings = load_settings(root_override=root)
            report = build_diagnostics_service(
                settings, root=settings.root).run(stale_days=stale_days)
    except Exception:  # noqa: BLE001 - see below
        # Everything from here to the report is "reading the store": a
        # StorageFailure, but also the settings load, which does not swallow a
        # bad MEMRIVER_* value. Whatever the reason, exit 2 is the one honest
        # answer -- exit 1 would claim findings doctor never looked for -- and
        # the reason itself stays out of stderr: a pydantic error echoes the
        # rejected value, a traceback the absolute source paths.
        stderr.write(_INACCESSIBLE_MESSAGE + "\n")
        return 2

    if json_output:
        _render_json(report, stdout)
    else:
        _render_human(report, stdout)
    return _EXIT_CODES[report.state]
