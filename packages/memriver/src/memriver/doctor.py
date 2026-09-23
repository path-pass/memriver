"""Rendering and CLI wiring for `memriver doctor`.

Every diagnostic rule lives in memriver_core, reached only through
memriver_core.bootstrap.build_diagnostics_service. This module owns exit
codes, fixed state messages, and JSON/human rendering. It also reads the
project registry directly (project_context.load_registry/root_integrity) to
report registered projects and stats each root to classify it -- read-only,
never a write or the store lock -- and [DEFERRED-4] performs no
harness-configuration audit (see spec S10).
"""

from __future__ import annotations

import os
import stat
from typing import IO, TYPE_CHECKING

from .core_logging import quiet_core_logging
from .project_context import visible

if TYPE_CHECKING:
    from pathlib import Path

    from memriver_core.models import DiagnosticFinding, DiagnosticsReport

# Fixed per spec S6.2; the inaccessible message is stderr-only and path-free.
_STATE_MESSAGES = {
    "uninitialized": "store not initialized yet; run memriver install",
    "empty": "store is initialized and empty",
    "healthy": "store is healthy",
    "degraded": "store has findings",
}
_NOT_INITIALIZED_NOTE = "note: store not initialized yet; run memriver install"
_INACCESSIBLE_MESSAGE = "memriver doctor: memory store is inaccessible"
# the --json counterpart of _INACCESSIBLE_MESSAGE, without the CLI prefix --
# this is a value read back by a script, not a line printed to a terminal
_INACCESSIBLE_JSON_ERROR = "memory store is inaccessible"
_EXIT_CODES = {"uninitialized": 0, "empty": 0, "healthy": 0, "degraded": 1}

# project ids, location hints and registry roots come from file and directory
# names in the store, which a user can hand-edit to contain a newline
# (forging a second finding line) or an ANSI escape (a raw terminal control
# sequence); the JSON renderer needs no such guard -- json.dumps already
# escapes both. The neutraliser itself is project_context.visible, shared
# with the project commands so that every management surface prints the same
# thing.
def _visible(value: str) -> str:
    return visible(value)


def _finding_to_dict(finding: DiagnosticFinding) -> dict:
    return {
        "kind": finding.kind,
        "memory_ids": list(finding.memory_ids),
        "project_ids": list(finding.project_ids),
        "location_hints": list(finding.location_hints),
        "reason": finding.reason,
        "suggestion": finding.suggestion,
    }


def _render_json(report: DiagnosticsReport, projects: dict, stdout: IO[str]) -> None:
    import json

    stdout.write(json.dumps({
        "state": report.state,
        "initialized": report.initialized,
        "findings": [_finding_to_dict(f) for f in report.findings],
        "projects": projects,
    }, indent=2) + "\n")


def _render_human(report: DiagnosticsReport, projects: dict, stdout: IO[str]) -> None:
    stdout.write(_STATE_MESSAGES[report.state] + "\n")
    if not report.initialized and report.state != "uninitialized":
        stdout.write(_NOT_INITIALIZED_NOTE + "\n")
    by_kind: dict[str, list[DiagnosticFinding]] = {}
    for finding in report.findings:
        by_kind.setdefault(finding.kind, []).append(finding)
    for kind in sorted(by_kind):
        stdout.write(f"\n{kind}:\n")
        for finding in by_kind[kind]:
            project_ids = ", ".join(_visible(pid) for pid in finding.project_ids)
            locations = ", ".join(_visible(hint) for hint in finding.location_hints)
            stdout.write(f"  - projects: {project_ids}\n")
            stdout.write(f"    locations: {locations}\n")
            stdout.write(f"    reason: {finding.reason}\n")
            stdout.write(f"    suggestion: {finding.suggestion}\n")
    _render_projects_section(projects, stdout)


def _render_projects_section(projects: dict, stdout: IO[str]) -> None:
    # a store that has never adopted the project registry has nothing here to
    # report; the section only appears once there is something to say.
    # Registry roots and directory names are hand-editable, same as the
    # findings above -- every registry-derived string goes through _visible()
    # so a root/location/reason/diagnostic can never forge an extra line. The
    # project id is the one exception, and it needs none: load_registry only
    # returns ids that match ID_RE.
    if not (projects["registered"] or projects["finding"] or projects["integrity"]):
        return
    stdout.write("\nprojects:\n")
    for project in projects["registered"]:
        n = project["roots"]
        label = _visible(project["name"]) if project["name"] is not None else "unknown project"
        stdout.write(f"  {project['id']} ({label}): {n} {'root' if n == 1 else 'roots'}\n")
        for root in project["missing_roots"]:
            stdout.write(f"    missing: {_visible(root)}\n")
        for root in project["unverifiable_roots"]:
            stdout.write(f"    unverifiable: {_visible(root)}\n")
    finding = projects["finding"]
    if finding is not None:
        stdout.write(f"  invalid: {_visible(finding['location'])}: {_visible(finding['reason'])}\n")
    if projects["integrity"] is not None:
        stdout.write(f"  integrity: {_visible(projects['integrity'])}\n")


def run_doctor(*, root: Path | None, json_output: bool, stale_days: int,
              stdout: IO[str], stderr: IO[str]) -> int:
    # imported here, not at module scope, to match the rest of the umbrella's
    # lazy-import convention for the memriver_core stack
    from memriver_core import ProjectNotFound, StorageFailure
    from memriver_core.bootstrap import build_diagnostics_service, build_service
    from memriver_core.settings import load_settings

    from .project_context import RegistryInvalid, load_registry, root_integrity

    def classify(root_path: str) -> str:
        try:
            return "ok" if stat.S_ISDIR(os.stat(root_path).st_mode) else "missing"
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unverifiable"

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
        if json_output:
            import json

            stdout.write(json.dumps({"error": _INACCESSIBLE_JSON_ERROR}) + "\n")
        return 2

    try:
        registry = load_registry(settings.root)
        service = build_service(settings, root=settings.root)

        def project_label(project_id: str) -> str | None:
            try:
                return service.read_project(project_id).name
            except (ProjectNotFound, StorageFailure):
                return None

        verdicts = {r: classify(r) for p in registry.projects for r in p.roots}
        projects = {
            "registered": [
                {"id": str(p.id), "name": project_label(str(p.id)), "roots": len(p.roots),
                 "missing_roots": [r for r in p.roots if verdicts[r] == "missing"],
                 "unverifiable_roots": [r for r in p.roots if verdicts[r] == "unverifiable"]}
                for p in registry.projects],
            "finding": None,
            # the same check the resolver runs: a server that is degraded
            # because a root was re-pointed must never meet a green doctor
            "integrity": root_integrity(registry),
        }
    except RegistryInvalid as err:
        projects = {"registered": [], "finding": {"location": err.location, "reason": err.reason},
                    "integrity": None}

    if json_output:
        _render_json(report, projects, stdout)
    else:
        _render_human(report, projects, stdout)
    finding = projects["finding"]
    return max(_EXIT_CODES[report.state], 1 if finding or projects["integrity"] else 0)
