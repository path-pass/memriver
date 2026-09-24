"""Rendering and CLI wiring for `memriver doctor`.

Every diagnostic rule lives in memriver_core, reached through
``MemoryService.diagnose`` -- the projects section included. This module owns
exit codes, fixed state messages, and JSON/human rendering; it never opens the
store itself, and [DEFERRED-4] performs no harness-configuration audit (see
spec S10).
"""

from __future__ import annotations

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

# project ids, names, location hints and bound roots come from the store,
# which a user can hand-edit to contain a newline (forging a second finding
# line) or an ANSI escape (a raw terminal control sequence); the JSON
# renderer needs no such guard -- json.dumps already escapes both. The
# neutraliser itself is project_context.visible, shared with the project
# commands so that every management surface prints the same thing.
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


def _project_to_dict(project) -> dict:
    return {"id": project.id, "name": project.name, "root": project.root,
            "is_global": project.is_global, "root_state": project.root_state,
            "active_memories": project.active_memories,
            "deleted_memories": project.deleted_memories}


def _render_json(report: DiagnosticsReport, stdout: IO[str]) -> None:
    import json

    stdout.write(json.dumps({
        "state": report.state,
        "initialized": report.initialized,
        "findings": [_finding_to_dict(f) for f in report.findings],
        "projects": [_project_to_dict(p) for p in report.projects],
    }, indent=2) + "\n")


def _render_human(report: DiagnosticsReport, stdout: IO[str]) -> None:
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
    _render_projects_section(report, stdout)


def _render_projects_section(report: DiagnosticsReport, stdout: IO[str]) -> None:
    if not report.projects:
        return
    stdout.write("\nprojects:\n")
    for project in report.projects:
        where = ("global (no directory)" if project.is_global
                 else _visible(project.root) if project.root else "no directory")
        state = "" if project.root_state in ("ok", "unbound") else f" [{project.root_state}]"
        stdout.write(f"  {project.id} ({_visible(project.name)}): {where}{state}; "
                     f"{project.active_memories} memories, "
                     f"{project.deleted_memories} deleted\n")


def run_doctor(*, root: Path | None, json_output: bool, stale_days: int,
               stdout: IO[str], stderr: IO[str]) -> int:
    # imported here, not at module scope, to match the rest of the umbrella's
    # lazy-import convention for the memriver_core stack
    from memriver_core.bootstrap import build_service
    from memriver_core.settings import load_settings

    try:
        with quiet_core_logging():
            settings = load_settings(root_override=root)
            report = build_service(settings, root=settings.root).diagnose(stale_days=stale_days)
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
    if json_output:
        _render_json(report, stdout)
    else:
        _render_human(report, stdout)
    return _EXIT_CODES[report.state]
