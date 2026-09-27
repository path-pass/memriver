"""Contract tests for `memriver doctor` rendering and exit codes.

Most diagnostic policy is faked out here: these tests pin doctor.py's
CLI-facing contract (state -> message -> exit code, JSON/human shape) against
a stand-in memriver_core.bootstrap.build_services whose maintenance service's
diagnose() returns a prepared report. The few real-store tests pin the boundary end to end.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from memriver import doctor
from memriver.doctor import run_doctor
from memriver_core import StorageFailure, StoreNeedsUpgrade
from memriver_core.bootstrap import Services, build_services
from memriver_core.models import Create, DiagnosticFinding, DiagnosticsReport, PolicyHit
from memriver_core.settings import Settings

P = "aaaaaaaaaa"


@dataclass(frozen=True)
class DoctorRun:
    stdout: str
    stderr: str
    exit_code: int


def invoke_doctor(*, root: Path | None = None, json_output: bool = False,
                  stale_days: int = 90) -> DoctorRun:
    out, err = io.StringIO(), io.StringIO()
    exit_code = doctor.run_doctor(root=root, json_output=json_output,
                                  stale_days=stale_days, stdout=out, stderr=err)
    return DoctorRun(out.getvalue(), err.getvalue(), exit_code)


def _finding(kind: str = "unparsable") -> DiagnosticFinding:
    return DiagnosticFinding(
        kind=kind, memory_ids=(), project_ids=(P,),
        location_hints=("memories/bad.md",),
        reason="stored entry cannot be decoded",
        suggestion="repair or remove the stored entry")


class _FakeService:
    def __init__(self, report: DiagnosticsReport, calls: list) -> None:
        self._report = report
        self._calls = calls

    def diagnose(self, **kw) -> DiagnosticsReport:
        self._calls.append(kw["stale_days"])
        return self._report

    def scan_policy(self) -> list:
        return []


def _only_maintenance(maintenance_service) -> Services:
    # doctor needs the maintenance service alone: touching any other is an error
    return Services(memory=None, project=None, session=None, maintenance=maintenance_service)


def install_fake_diagnostics_service(monkeypatch, state: str, finding_count: int):
    """Stand in for build_services; returns (build_calls, diagnose_calls)."""
    report = DiagnosticsReport(
        state=state, findings=tuple(_finding() for _ in range(finding_count)))
    build_calls: list = []
    diagnose_calls: list = []

    def fake_build(settings, *, root=None, home=None):
        build_calls.append((settings, root))
        return _only_maintenance(_FakeService(report, diagnose_calls))

    monkeypatch.setattr("memriver_core.bootstrap.build_services", fake_build)
    return build_calls, diagnose_calls


def install_fake_diagnostics_service_for_findings(monkeypatch, state: str, findings) -> None:
    """Like install_fake_diagnostics_service, but with caller-supplied findings
    instead of the generic ``_finding()`` stand-in."""
    report = DiagnosticsReport(state=state, findings=tuple(findings))

    def fake_build(settings, *, root=None, home=None):
        return _only_maintenance(_FakeService(report, []))

    monkeypatch.setattr("memriver_core.bootstrap.build_services", fake_build)


def install_raising_service(monkeypatch, exc: Exception):
    """The inspector fails inside .diagnose(), as the real StorageFailure does."""
    class _RaisingService:
        def diagnose(self, **kw):
            raise exc

    def fake_build(settings, *, root=None, home=None):
        return _only_maintenance(_RaisingService())

    monkeypatch.setattr("memriver_core.bootstrap.build_services", fake_build)


@pytest.mark.parametrize(
    ("state", "finding_count", "exit_code"),
    [
        ("uninitialized", 0, 0),
        ("empty", 0, 0),
        ("healthy", 0, 0),
        ("degraded", 1, 1),
    ],
)
def test_doctor_state_exit_contract(monkeypatch, state, finding_count, exit_code, tmp_path):
    build_calls, diagnose_calls = install_fake_diagnostics_service(monkeypatch, state,
                                                                   finding_count)
    result = invoke_doctor(root=tmp_path, stale_days=45)

    assert result.exit_code == exit_code
    assert result.stderr == ""
    # doctor calls only build_services(settings, root=settings.root)
    # .maintenance.diagnose(stale_days=stale_days) -- never a concrete inspector or
    # application module
    assert len(build_calls) == 1
    settings, root = build_calls[0]
    assert root == settings.root
    assert diagnose_calls == [45]


def test_inaccessible_store_is_path_free_exit_two(monkeypatch, tmp_path):
    install_raising_service(monkeypatch, StorageFailure())
    result = invoke_doctor(root=tmp_path / "private")

    assert result.exit_code == 2
    assert str(tmp_path) not in result.stderr
    assert result.stdout == ""
    assert result.stderr == "memriver doctor: memory store is inaccessible\n"


def test_huge_stale_days_against_a_missing_store_stays_uninitialized(tmp_path):
    """CLI-boundary regression against a REAL store, not the fake service: a
    `stale_days` cutoff so large it underflows datetime's representable range
    must not turn a store that was never initialized into a reported
    'inaccessible' (exit 2) -- it stays 'uninitialized' (exit 0), because the
    cutoff arithmetic, not the store, was the thing out of range."""
    result = invoke_doctor(root=tmp_path / "missing", stale_days=1_000_000)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == "store not initialized yet; run memriver install\n"


def test_an_invalid_env_setting_is_a_named_exit_two(monkeypatch, tmp_path):
    """`load_settings` is the one call doctor makes before the store is opened. A
    doctor that never read the store must not report findings (exit 1); the one
    stderr line names the variable -- never the value, never a traceback."""
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "not-a-number")
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "memriver: environment variable MEMRIVER_MAX_BODY_CHARS is invalid\n"
    assert "not-a-number" not in result.stderr


def test_an_invalid_settings_file_with_json_emits_the_named_error(monkeypatch, tmp_path):
    """`--json` callers parse stdout as JSON: the settings error is an error object
    naming the file and the field, beside the same stderr line."""
    (tmp_path / "settings.toml").write_text('max_body_chars = "not-a-number"\n',
                                            encoding="utf-8")
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path, json_output=True)

    assert result.exit_code == 2
    assert result.stderr == "memriver: settings.toml is invalid: field max_body_chars\n"
    assert "not-a-number" not in result.stdout
    assert json.loads(result.stdout) == {
        "error": "settings.toml is invalid: field max_body_chars"}


def test_inaccessible_store_with_json_emits_a_json_error_object(monkeypatch, tmp_path):
    install_raising_service(monkeypatch, StorageFailure())
    result = invoke_doctor(root=tmp_path / "private", json_output=True)

    assert result.exit_code == 2
    assert str(tmp_path) not in result.stdout
    assert result.stderr == "memriver doctor: memory store is inaccessible\n"
    assert json.loads(result.stdout) == {"error": "memory store is inaccessible"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_inaccessible_root_leaks_no_logging_line_to_real_stderr(tmp_path, capsys):
    """CLI-boundary regression against a REAL store, not the fake service:
    memriver_core's own stdlib logging (e.g. a skipped entry) must
    not slip onto the real process stderr alongside doctor's own output --
    logging.lastResort writes straight to sys.stderr, bypassing the `stderr`
    IO parameter entirely. A root with every permission removed cannot be
    inspected at all -- not even its settings.toml -- so it is exit 2 with the
    settings line, not a degraded store."""
    root = tmp_path / "store"
    root.mkdir()
    root.chmod(0o000)
    try:
        result = invoke_doctor(root=root)
    finally:
        root.chmod(0o700)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "memriver: settings.toml could not be read\n"
    assert str(root) not in result.stderr
    assert capsys.readouterr().err == ""


_EXPECTED_JSON = {
    "state": "degraded",
    "initialized": True,
    "findings": [{
        "kind": "unparsable",
        "memory_ids": [],
        "project_ids": [P],
        "location_hints": ["memories/bad.md"],
        "reason": "stored entry cannot be decoded",
        "suggestion": "repair or remove the stored entry",
    }],
    "policy_hits": [],
    "incomplete_changes": [],
    "projects": [],
}


def test_json_output_matches_the_stable_shape(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "degraded", 1)
    result = invoke_doctor(root=tmp_path, json_output=True)

    assert json.loads(result.stdout) == _EXPECTED_JSON


def test_json_output_keeps_arrays_when_empty(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path, json_output=True)

    assert json.loads(result.stdout) == {
        "state": "healthy", "initialized": True, "findings": [], "policy_hits": [],
        "incomplete_changes": [], "projects": [],
    }


def test_human_output_groups_by_kind_with_no_body_or_absolute_path(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "degraded", 1)
    result = invoke_doctor(root=tmp_path)

    assert "store has findings" in result.stdout
    assert "unparsable" in result.stdout
    assert f"- projects: {P}" in result.stdout
    assert "memories/bad.md" in result.stdout
    assert "stored entry cannot be decoded" in result.stdout
    assert "repair or remove the stored entry" in result.stdout
    assert "Memory.body" not in result.stdout
    assert str(tmp_path) not in result.stdout


def test_control_characters_in_a_finding_render_on_one_line_with_no_raw_escape(
        monkeypatch, tmp_path):
    """A store's project id/location strings come from directory and file
    names, which a user can hand-edit to contain a newline or an ANSI escape.
    Neither may forge a second finding line or emit a raw terminal control
    sequence in the human renderer -- the JSON renderer is already safe via
    json.dumps."""
    finding = DiagnosticFinding(
        kind="unparsable", memory_ids=(), project_ids=("evil\ninjected\x1b[31mRED",),
        location_hints=("memories/bad\x1b[31m.md",),
        reason="stored entry cannot be decoded",
        suggestion="repair or remove the stored entry")
    install_fake_diagnostics_service_for_findings(monkeypatch, "degraded", [finding])

    result = invoke_doctor(root=tmp_path)

    assert "\x1b" not in result.stdout
    lines = result.stdout.splitlines()
    assert len(lines) == 7  # the injected newline must not forge an extra line
    projects_line = next(l for l in lines if l.strip().startswith("- projects:"))
    assert "evil" in projects_line and "injected" in projects_line and "RED" in projects_line


@pytest.mark.parametrize("hostile", [
    "evil\u202edm.txt",   # a bidi override reorders what the terminal shows
    "bad\udc9b31m",       # a non-UTF-8 filename byte, surrogateescaped
    "two\u2028lines",     # a line separator some terminals break on
    "zero\u200bwidth",    # a zero-width space, invisible in the report
])
def test_visible_neutralises_every_invisible_character_class(hostile):
    """A store's names reach the human renderer as text, and `str` carries
    more than the C0/C1 code points: Unicode format controls (Cf) can reorder
    the line a terminal draws, and a filename byte no codec accepts arrives as
    a lone surrogate (Cs) that turns straight back into that raw byte when it
    is written to a surrogateescape stdout. Category, not a code-point list,
    is what covers all of them."""
    import unicodedata

    rendered = doctor._visible(hostile)

    assert all(unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
               for char in rendered)
    assert len(rendered) == len(hostile)


def _terminal_stdout() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """A stdout that behaves like a real terminal's: surrogateescape on the
    way out, which turns a lone surrogate straight back into the raw byte it
    stood for."""
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="utf-8", errors="surrogateescape",
                                 newline="")


def test_a_surrogateescaped_location_reaches_stdout_without_its_raw_byte(
        monkeypatch, tmp_path):
    """The renderer's own contract, pinned without needing a filesystem that
    will store the name: `\\udc9b` written to a surrogateescape stdout is the
    byte 0x9b, a C1 CSI introducer, so it has to be neutralised before it is
    written, not merely absent from the `str`."""
    finding = DiagnosticFinding(
        kind="unparsable", memory_ids=(), project_ids=(P,),
        location_hints=(os.fsdecode(b"memories/bad\x9b31m.md"),),
        reason="stored entry cannot be decoded",
        suggestion="repair or remove the stored entry")
    install_fake_diagnostics_service_for_findings(monkeypatch, "degraded", [finding])
    raw, out = _terminal_stdout()

    doctor.run_doctor(root=tmp_path, json_output=False, stale_days=90,
                      stdout=out, stderr=io.StringIO())
    out.flush()

    assert b"31m.md" in raw.getvalue()  # the location really was rendered
    assert b"\x9b" not in raw.getvalue()


def test_healthy_human_output_has_no_findings_section(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path)

    assert result.stdout == "store is healthy\n"


def test_an_uninitialized_store_names_the_command_to_run(tmp_path):
    result = invoke_doctor(root=tmp_path / "never-created")
    assert (result.exit_code, result.stdout) == \
        (0, "store not initialized yet; run memriver install\n")


def test_an_initialized_empty_store_is_empty(tmp_path):
    root = tmp_path / "store"
    build_services(Settings(root=root), root=root).project.ensure_global()
    result = invoke_doctor(root=root, json_output=True)
    report = json.loads(result.stdout)
    assert (report["state"], report["initialized"]) == ("empty", True)


def test_doctor_reads_the_store_only(monkeypatch, tmp_path):
    """[DEFERRED-4] No harness-configuration audit: doctor never looks under
    HOME beyond the explicit store root, never writes the database, and the
    JSON has exactly the documented keys."""
    store, _, _ = _real_store(tmp_path)
    sentinel_home = tmp_path / "sentinel-home"
    sentinel_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: sentinel_home)
    before = (store / "memriver.db").read_bytes()

    result = invoke_doctor(root=store, json_output=True)

    assert (store / "memriver.db").read_bytes() == before
    assert list(sentinel_home.iterdir()) == []
    assert set(json.loads(result.stdout)) == {"state", "initialized", "findings", "policy_hits",
                                              "incomplete_changes", "projects"}


def test_a_pre_release_store_is_degraded_and_says_it_is_not_initialized(tmp_path):
    root = tmp_path / "store"
    (root / "memories").mkdir(parents=True)
    result = invoke_doctor(root=root)
    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert lines[:2] == ["store has findings",
                         "note: store not initialized yet; run memriver install"]
    assert "legacy-layout:" in result.stdout
    assert not (root / "memriver.db").exists()


def _real_store(tmp_path):
    store, work = tmp_path / "mem", tmp_path / "work"
    work.mkdir()
    services = build_services(Settings(root=store), root=store)
    services.project.ensure_global()
    project = services.project.init_project("demo", services.project.plan_root(str(work)))
    return store, work, project


def test_doctor_lists_projects_with_directory_state_and_counts(tmp_path, capsys):
    store, work, project = _real_store(tmp_path)
    work.rmdir()
    code = run_doctor(root=store, json_output=False, stale_days=90,
                      stdout=sys.stdout, stderr=sys.stderr)
    out = capsys.readouterr().out
    assert code == 0                                   # an offline directory is not a finding
    assert f"{project.id} (demo): {work.resolve()} [missing]; 0 memories, 0 deleted" in out
    assert "global (no directory)" in out


def test_doctor_fails_on_a_re_pointed_directory(tmp_path, capsys):
    store, work, _ = _real_store(tmp_path)
    moved = tmp_path / "moved"
    work.rename(moved)
    work.symlink_to(moved)
    code = run_doctor(root=store, json_output=False, stale_days=90,
                      stdout=sys.stdout, stderr=sys.stderr)
    assert code == 1 and "non-canonical-root" in capsys.readouterr().out


def test_doctor_neutralises_an_injected_root(tmp_path, capsys):
    # a hostile name never reaches the renderer (row validation rejects it);
    # a root is only shape-checked, so newline and ESC arrive intact
    store, _, project = _real_store(tmp_path)
    hostile = f"{tmp_path}/evil\n  forged line\x1b[2J"
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute("UPDATE projects SET root = ? WHERE id = ?", (hostile, project.id))
    run_doctor(root=store, json_output=False, stale_days=90, stdout=sys.stdout,
               stderr=sys.stderr)
    out = capsys.readouterr().out
    assert f"{project.id} (demo): {tmp_path}/evil   forged line [2J" in out
    assert "\x1b" not in out
    assert not any(line.startswith("  forged") for line in out.splitlines())


def test_doctor_marks_an_unverifiable_directory(tmp_path, capsys, monkeypatch):
    store, work, project = _real_store(tmp_path)
    monkeypatch.setattr("memriver_core.repository.directories.root_state",
                        lambda root: "unverifiable")
    code = run_doctor(root=store, json_output=False, stale_days=90,
                      stdout=sys.stdout, stderr=sys.stderr)
    out = capsys.readouterr().out
    assert code == 1                                   # unverifiable-root is a finding
    assert f"{project.id} (demo): {work.resolve()} [unverifiable]; 0 memories, 0 deleted" in out


_SESSION_COLUMNS = ("harness, session_id, status, origin, project_id, candidate_id, "
                    "candidate_root, entry_cwd, branch, transcript_path, started_at, "
                    "last_active_at, ended_at, prompt_count, last_write_prompt_count, "
                    "last_nudge_prompt_count, first_prompt, recent_prompts")


def _plant_session(store: Path, **overrides: object) -> None:
    """A raw `sessions` row, bypassing the app's own checks -- a bare
    sqlite3 connection never turns PRAGMA foreign_keys on, and
    ignore_check_constraints lets a row invalid for other reasons past the
    table's CHECKs, the same way the other planted rows above do."""
    row: dict[str, object] = {
        "harness": "codex", "session_id": "s1", "status": "registered", "origin": "start",
        "project_id": None, "candidate_id": None, "candidate_root": None,
        "entry_cwd": "/tmp/x", "branch": None, "transcript_path": None,
        "started_at": "2026-09-24T00:00:00.000000Z",
        "last_active_at": "2026-09-24T00:00:00.000000Z", "ended_at": None,
        "prompt_count": 0, "last_write_prompt_count": 0, "last_nudge_prompt_count": 0,
        "first_prompt": None, "recent_prompts": "[]",
    }
    row.update(overrides)
    placeholders = ", ".join("?" for _ in _SESSION_COLUMNS.split(","))
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"INSERT INTO sessions ({_SESSION_COLUMNS}) VALUES ({placeholders})",
                     tuple(row.values()))


def test_doctor_reports_an_invalid_session_row_and_a_dangling_candidate_id(tmp_path, capsys):
    store, _, _ = _real_store(tmp_path)
    _plant_session(store, harness="codex", session_id="bad-row", status="odd")
    _plant_session(store, harness="claude-code", session_id="pending-1", status="pending",
                   origin="first-seen", candidate_id="zzzzzzzzzz", candidate_root="/z")

    code = run_doctor(root=store, json_output=False, stale_days=90,
                      stdout=sys.stdout, stderr=sys.stderr)
    out = capsys.readouterr().out

    assert code == 1
    assert "invalid-row:" in out
    assert "sessions/codex/bad-row" in out
    assert "session-orphan:" in out
    assert "sessions/claude-code/pending-1" in out
    assert "session refers to a project that does not exist" in out


def test_doctor_json_reports_session_findings(tmp_path):
    store, _, _ = _real_store(tmp_path)
    _plant_session(store, harness="codex", session_id="pending-2", status="pending",
                   origin="first-seen", candidate_id="zzzzzzzzzz", candidate_root="/z")

    out = io.StringIO()
    code = run_doctor(root=store, json_output=True, stale_days=90, stdout=out, stderr=io.StringIO())
    report = json.loads(out.getvalue())

    assert code == 1
    kinds = {f["kind"] for f in report["findings"]}
    assert "session-orphan" in kinds
    orphan = next(f for f in report["findings"] if f["kind"] == "session-orphan")
    assert orphan["location_hints"] == ["sessions/codex/pending-2"]


class _PolicyMaintenance:
    """A maintenance service whose diagnose() and scan_policy() give prepared answers;
    hits None means scanning at all is a failure."""

    def __init__(self, report: DiagnosticsReport, hits) -> None:
        self._report, self._hits = report, hits

    def diagnose(self, **kw) -> DiagnosticsReport:
        return self._report

    def scan_policy(self) -> list:
        if self._hits is None:
            raise AssertionError("a store that is not initialized is never scanned")
        return self._hits


def install_policy_hits(monkeypatch, hits, *, state="healthy", initialized=True) -> None:
    maintenance = _PolicyMaintenance(
        DiagnosticsReport(state=state, findings=(), initialized=initialized), hits)
    monkeypatch.setattr("memriver_core.bootstrap.build_services",
                        lambda settings, *, root=None, home=None:
                        SimpleNamespace(maintenance=maintenance))


# §8.2: doctor lists scan_policy() hits -- id, version, rule id, never the text
def test_policy_hits_are_listed_by_version_and_rule_and_make_exit_one(monkeypatch, tmp_path):
    install_policy_hits(monkeypatch, [PolicyHit("mmmmmmmmmm", 2, "github-pat", True),
                                      PolicyHit("mmmmmmmmmm", 1, "memriver-instruction",
                                                False)])
    result = invoke_doctor(root=tmp_path)
    assert result.exit_code == 1
    assert result.stdout == (
        "store is healthy\n"
        "\ncontent policy hits:\n"
        "  - mmmmmmmmmm v2 (current): github-pat\n"
        "  - mmmmmmmmmm v1 (history): memriver-instruction\n"
        "    suggestion: inspect with memriver history ID; memriver delete ID --hard "
        "removes every version\n")


def test_policy_hits_in_json(monkeypatch, tmp_path):
    install_policy_hits(monkeypatch, [PolicyHit("mmmmmmmmmm", 1, "github-pat", False)])
    result = invoke_doctor(root=tmp_path, json_output=True)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["policy_hits"] == [
        {"memory_id": "mmmmmmmmmm", "version": 1, "rule_id": "github-pat", "current": False}]


def test_no_policy_hits_leave_the_output_and_exit_code_alone(monkeypatch, tmp_path):
    install_policy_hits(monkeypatch, [])
    result = invoke_doctor(root=tmp_path)
    assert (result.exit_code, result.stdout) == (0, "store is healthy\n")


def test_a_store_that_is_not_initialized_is_never_scanned(monkeypatch, tmp_path):
    install_policy_hits(monkeypatch, None, state="uninitialized", initialized=False)
    result = invoke_doctor(root=tmp_path)
    assert (result.exit_code, result.stdout) == (
        0, "store not initialized yet; run memriver install\n")


# spec §9: every command exits 1 with the one hint on a store below the schema this
# memriver needs
@pytest.mark.parametrize("json_output", [False, True])
def test_a_store_below_the_needed_schema_exits_one_with_the_hint(
        monkeypatch, tmp_path, json_output):
    class _OldStore:
        def diagnose(self, **kw):
            raise StoreNeedsUpgrade(3)

    monkeypatch.setattr("memriver_core.bootstrap.build_services",
                        lambda settings, *, root=None, home=None:
                        SimpleNamespace(maintenance=_OldStore()))
    result = invoke_doctor(root=tmp_path, json_output=json_output)
    hint = "the memory store is at schema version 3; this memriver needs schema version 4"
    assert (result.exit_code, result.stderr) == (1, f"memriver doctor: {hint}\n")
    assert result.stdout == (json.dumps({"error": hint}) + "\n" if json_output else "")


# spec §4.4: changes with fewer steps than step_count are listed as incomplete (not
# undoable) -- the consequence of a legal hard delete, so not a finding and not "degraded"
def test_a_change_a_hard_delete_cut_is_listed_as_incomplete_but_not_a_finding(tmp_path):
    store, work = tmp_path / "mem", tmp_path / "work"
    work.mkdir()
    services = build_services(Settings(root=store), root=store)
    services.project.ensure_global()
    project = services.project.init_project("demo", services.project.plan_root(str(work)))
    pair = services.memory.apply([Create(project.id, "project", "kept cue", "kept"),
                                  Create(project.id, "project", "doomed cue", "doomed")],
                                 changed_by="dream")
    doomed = pair.steps[1].memory_id
    plan = services.maintenance.plan_hard_delete(doomed)
    services.maintenance.hard_delete(doomed, expected=plan.expected)

    text = invoke_doctor(root=store)
    assert text.exit_code == 0
    assert text.stdout.startswith("store is healthy\n")
    assert ("\nincomplete changes (a hard delete removed part of them; they cannot be "
            f"undone):\n  - {pair.change_id}\n") in text.stdout

    report = json.loads(invoke_doctor(root=store, json_output=True).stdout)
    assert (report["state"], report["findings"]) == ("healthy", [])
    assert report["incomplete_changes"] == [pair.change_id]


# --- a policy scan failure never erases the diagnosis ----------------------------

# when the file or schema cannot be read safely the policy scan fails; the
# diagnosis's finding is kept, the scan is reported incomplete, and the generic
# "inaccessible" fallback never replaces it
def test_doctor_keeps_the_diagnosis_when_the_schema_is_unrecognized(tmp_path):
    store = tmp_path / "mem"
    build_services(Settings(root=store), root=store).project.ensure_global()
    with closing(sqlite3.connect(store / "memriver.db")) as conn:
        conn.execute("PRAGMA user_version = 99")
    result = invoke_doctor(root=store, json_output=True)
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert "unknown-schema" in {f["kind"] for f in report["findings"]}


def test_doctor_keeps_the_diagnosis_when_the_database_is_a_symlink(tmp_path):
    store = tmp_path / "mem"
    build_services(Settings(root=store), root=store).project.ensure_global()
    real = store / "memriver.db"
    target = tmp_path / "outside.db"
    real.rename(target)
    real.symlink_to(target)
    result = invoke_doctor(root=store, json_output=True)
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert "unsafe-database" in {f["kind"] for f in report["findings"]}


# scan_policy() failing for any other reason still keeps the diagnosis already run;
# only the policy scan itself is reported as incomplete
def test_an_injected_policy_scan_failure_keeps_the_diagnosis_and_marks_it_incomplete(
        monkeypatch, tmp_path):
    class _FlakyScan:
        def diagnose(self, **kw) -> DiagnosticsReport:
            return DiagnosticsReport(state="healthy", findings=(), initialized=True)

        def scan_policy(self) -> list:
            raise StorageFailure

    monkeypatch.setattr("memriver_core.bootstrap.build_services",
                        lambda settings, *, root=None, home=None:
                        SimpleNamespace(maintenance=_FlakyScan()))

    result = invoke_doctor(root=tmp_path, json_output=True)
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert (report["state"], report["findings"]) == ("healthy", [])
    assert report["policy_scan"] == "incomplete"

    text_result = invoke_doctor(root=tmp_path)
    assert text_result.exit_code == 1
    assert text_result.stdout.startswith("store is healthy\n")
    assert "policy scan did not complete" in text_result.stdout


# every other degraded store -- a few bad rows, say -- still scans normally
def test_a_store_with_ordinary_findings_still_scans(monkeypatch, tmp_path):
    maintenance = _PolicyMaintenance(
        DiagnosticsReport(state="degraded", findings=(_finding("invalid-row"),),
                         initialized=True),
        [PolicyHit("mmmmmmmmmm", 1, "github-pat", True)])
    monkeypatch.setattr("memriver_core.bootstrap.build_services",
                        lambda settings, *, root=None, home=None:
                        SimpleNamespace(maintenance=maintenance))

    result = invoke_doctor(root=tmp_path, json_output=True)
    report = json.loads(result.stdout)
    assert report["state"] == "degraded"
    assert report["policy_hits"] == [
        {"memory_id": "mmmmmmmmmm", "version": 1, "rule_id": "github-pat", "current": True}]
    assert "policy_scan" not in report


def _damage_the_only_memorys_id(store, raw_id: bytes) -> None:
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute("UPDATE memory_versions SET memory_id = CAST(? AS TEXT)", (raw_id,))
        conn.execute("UPDATE memories SET id = CAST(? AS TEXT)", (raw_id,))


def _seed_a_policy_matching_memory(store, work):
    """A memory the write path would refuse, planted the way a history row already
    holding it looks (the content policy blocks it at write time, never at rest)."""
    services = build_services(Settings(root=store), root=store)
    context = services.project.open_project_context(str(work))
    services.memory.record(content="clean", type="project", sync=True, harness="t",
                           description="cue", context=context)
    secret = "aws key AKIAIOSFODNN7EXAMPLE ok"
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        conn.execute("UPDATE memory_versions SET body = ?", (secret,))
        conn.execute("UPDATE memories SET body = ?", (secret,))


# a policy-matching row whose id is invalid UTF-8 never becomes a PolicyHit --
# doctor --json succeeds and the inspector's own invalid-row finding is present
def test_doctor_json_survives_an_undecodable_policy_hit_id(tmp_path):
    store, work, _ = _real_store(tmp_path)
    _seed_a_policy_matching_memory(store, work)
    _damage_the_only_memorys_id(store, b"\x80123456789")

    result = invoke_doctor(root=store, json_output=True)
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["policy_hits"] == []
    assert "invalid-row" in {f["kind"] for f in report["findings"]}


# a policy-matching row whose id carries ESC and a newline never reaches the human
# renderer as a PolicyHit; no raw control character escapes into the terminal
def test_doctor_human_output_has_no_raw_control_characters_from_a_policy_hit_id(tmp_path):
    store, work, _ = _real_store(tmp_path)
    _seed_a_policy_matching_memory(store, work)
    _damage_the_only_memorys_id(store, b"\x1b[31m\nABCD")

    result = invoke_doctor(root=store)
    assert result.exit_code == 1
    assert "\x1b" not in result.stdout
