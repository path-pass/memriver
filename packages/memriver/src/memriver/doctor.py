"""Rendering and CLI wiring for `memriver doctor`.

Every diagnostic rule lives in memriver_core, reached through
``MaintenanceService.diagnose`` -- the projects section included -- and
``MaintenanceService.scan_policy``, whose hits (memory, version, rule id, never
the text) doctor lists, as it lists the changes a hard delete left incomplete
(not undoable; not a finding). This module owns exit codes, fixed state messages, and
JSON/human rendering; it never opens the store itself, and [DEFERRED-4] performs
no harness-configuration audit (see spec S10). It also states the content
classifier in one line (never calling a model).
"""

from __future__ import annotations

from typing import IO, TYPE_CHECKING

from .core_logging import quiet_core_logging
from .project_context import visible

if TYPE_CHECKING:
    from pathlib import Path

    from memriver_core.models import DiagnosticFinding, DiagnosticsReport, PolicyHit

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
_POLICY_SCAN_INCOMPLETE_NOTE = "policy scan did not complete; content policy hits may be missing"

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


def _policy_hit_to_dict(hit: PolicyHit) -> dict:
    return {"memory_id": hit.memory_id, "version": hit.version, "rule_id": hit.rule_id,
            "current": hit.current}


def _render_json(report: DiagnosticsReport, hits: list[PolicyHit], scan_incomplete: bool,
                 classifier: str, stdout: IO[str]) -> None:
    import json

    payload = {
        "state": report.state,
        "initialized": report.initialized,
        "findings": [_finding_to_dict(f) for f in report.findings],
        "policy_hits": [_policy_hit_to_dict(hit) for hit in hits],
        "incomplete_changes": list(report.incomplete_changes),
        "projects": [_project_to_dict(p) for p in report.projects],
        "classifier": classifier,
    }
    if scan_incomplete:
        payload["policy_scan"] = "incomplete"
    stdout.write(json.dumps(payload, indent=2) + "\n")


def _render_policy_hits(hits: list[PolicyHit], scan_incomplete: bool, stdout: IO[str]) -> None:
    if hits:
        stdout.write("\ncontent policy hits:\n")
        for hit in hits:
            where = "current" if hit.current else "history"
            stdout.write(f"  - {_visible(hit.memory_id)} v{hit.version} ({where}): "
                         f"{_visible(hit.rule_id)}\n")
        stdout.write("    suggestion: inspect with memriver history ID; memriver delete ID "
                     "--hard removes every version\n")
    if scan_incomplete:
        stdout.write(f"\n{_POLICY_SCAN_INCOMPLETE_NOTE}\n")


def _render_incomplete_changes(report: DiagnosticsReport, stdout: IO[str]) -> None:
    # a legal hard delete's consequence, reported as a fact: never a finding, never
    # part of the state or the exit code
    if not report.incomplete_changes:
        return
    stdout.write("\nincomplete changes (a hard delete removed part of them; they cannot be "
                 "undone):\n")
    stdout.write("".join(f"  - {_visible(change_id)}\n"
                         for change_id in report.incomplete_changes))


def _render_human(report: DiagnosticsReport, hits: list[PolicyHit], scan_incomplete: bool,
                  classifier: str, stdout: IO[str]) -> None:
    stdout.write(_STATE_MESSAGES[report.state] + "\n")
    if not report.initialized and report.state != "uninitialized":
        stdout.write(_NOT_INITIALIZED_NOTE + "\n")
    stdout.write(f"classifier: {classifier}\n")
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
    _render_policy_hits(hits, scan_incomplete, stdout)
    _render_incomplete_changes(report, stdout)
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
    from memriver_core import StoreNeedsUpgrade
    from memriver_core.bootstrap import build_services
    from memriver_core.settings import SettingsError, load_settings

    from .classifier_plugin import classifier_state

    def _unsupported(err: StoreNeedsUpgrade) -> int:
        # spec §9: a store below the schema this memriver needs is refused
        # like every other command does it -- exit 1, the one hint, and
        # nothing else: whatever diagnose() already found is not shown
        # alongside a refusal every other entry point gives the same way
        from .views import unsupported_store

        hint = unsupported_store(err)
        stderr.write(f"memriver doctor: {hint}\n")
        if json_output:
            import json

            stdout.write(json.dumps({"error": hint}) + "\n")
        return 1

    try:
        with quiet_core_logging():
            settings = load_settings(root_override=root)
            classifier = classifier_state(settings.root)
            maintenance = build_services(settings, root=settings.root).maintenance
            report = maintenance.diagnose(stale_days=stale_days)
    except Exception as err:  # noqa: BLE001 - see below
        if isinstance(err, StoreNeedsUpgrade):
            return _unsupported(err)
        # Everything from here to the report is "reading the store": a
        # StorageFailure, but also the settings load. Whatever the reason, exit 2
        # is the one honest answer -- exit 1 would claim findings doctor never
        # looked for -- and the reason itself stays out of stderr: a traceback
        # carries the absolute source paths. An unusable settings.toml or
        # MEMRIVER_* value is named instead (file and field, never the value):
        # the user can act on that line.
        settings_error = isinstance(err, SettingsError)
        stderr.write(f"memriver: {err}\n" if settings_error else _INACCESSIBLE_MESSAGE + "\n")
        if json_output:
            import json

            reason = str(err) if settings_error else _INACCESSIBLE_JSON_ERROR
            stdout.write(json.dumps({"error": reason}) + "\n")
        return 2
    # the diagnosis above is kept whatever happens next: a policy scan is a second,
    # independent read of the store (inspector.py's own walk never opens the
    # database the way the serving read path does), and its own failure never
    # erases findings diagnose() already proved. A store that is not there is
    # never created just to be scanned.
    hits: list[PolicyHit] = []
    scan_incomplete = False
    if report.initialized:
        try:
            with quiet_core_logging():
                hits = maintenance.scan_policy()
        except StoreNeedsUpgrade as err:
            # the same schema this memriver cannot read at all: the scan's own
            # read hit it even where diagnose()'s walk of the raw file did not
            return _unsupported(err)
        except Exception:  # noqa: BLE001 - the diagnosis stays; only the scan is incomplete
            scan_incomplete = True
    if json_output:
        _render_json(report, hits, scan_incomplete, classifier, stdout)
    else:
        _render_human(report, hits, scan_incomplete, classifier, stdout)
    # a policy hit, or a scan that could not finish, is worth acting on whatever
    # the store's own state
    return max(_EXIT_CODES[report.state], 1 if hits else 0, 1 if scan_incomplete else 0)
