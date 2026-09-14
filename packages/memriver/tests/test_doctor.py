"""Contract tests for `memriver doctor` rendering and exit codes.

All diagnostic policy is faked out here: these tests only pin doctor.py's
CLI-facing contract (state -> message -> exit code, JSON/human shape) against
a stand-in memriver_core.bootstrap.build_diagnostics_service, never against a
real store.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from memriver import doctor
from memriver_core import StorageFailure
from memriver_core.models import DiagnosticFinding, DiagnosticsReport, ProjectId, Scope


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
        kind=kind, memory_ids=(), scopes=(Scope.global_(),),
        location_hints=("global/entries/bad.md",),
        reason="stored entry cannot be decoded",
        suggestion="repair or remove the stored entry")


class _FakeDiagnosticsService:
    def __init__(self, report: DiagnosticsReport, calls: list) -> None:
        self._report = report
        self._calls = calls

    def run(self, *, stale_days: int) -> DiagnosticsReport:
        self._calls.append(stale_days)
        return self._report


def install_fake_diagnostics_service(monkeypatch, state: str, finding_count: int):
    """Stand in for build_diagnostics_service; returns (build_calls, run_calls)."""
    report = DiagnosticsReport(
        state=state, findings=tuple(_finding() for _ in range(finding_count)))
    build_calls: list = []
    run_calls: list = []

    def fake_build(settings, *, root=None):
        build_calls.append((settings, root))
        return _FakeDiagnosticsService(report, run_calls)

    monkeypatch.setattr("memriver_core.bootstrap.build_diagnostics_service", fake_build)
    return build_calls, run_calls


def install_fake_diagnostics_service_for_findings(monkeypatch, state: str, findings) -> None:
    """Like install_fake_diagnostics_service, but with caller-supplied findings
    instead of the generic ``_finding()`` stand-in."""
    report = DiagnosticsReport(state=state, findings=tuple(findings))

    def fake_build(settings, *, root=None):
        return _FakeDiagnosticsService(report, [])

    monkeypatch.setattr("memriver_core.bootstrap.build_diagnostics_service", fake_build)


def install_raising_service(monkeypatch, exc: Exception):
    """The inspector fails inside .run(), as the real StorageFailure does."""
    class _RaisingService:
        def run(self, *, stale_days: int):
            raise exc

    def fake_build(settings, *, root=None):
        return _RaisingService()

    monkeypatch.setattr("memriver_core.bootstrap.build_diagnostics_service", fake_build)


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
    build_calls, run_calls = install_fake_diagnostics_service(monkeypatch, state, finding_count)
    result = invoke_doctor(root=tmp_path, stale_days=45)

    assert result.exit_code == exit_code
    assert result.stderr == ""
    # doctor calls only build_diagnostics_service(settings, root=settings.root)
    # .run(stale_days=stale_days) -- never a concrete inspector or application module
    assert len(build_calls) == 1
    settings, root = build_calls[0]
    assert root == settings.root
    assert run_calls == [45]


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
    assert result.stdout == "store not initialized yet\n"


def test_an_invalid_env_setting_is_the_same_path_free_exit_two(monkeypatch, tmp_path):
    """`load_settings` is the one call doctor makes before the store is opened,
    and the env layer's ValidationError is deliberately not swallowed there: it
    echoes the offending value and, as a traceback, absolute source paths.
    Neither may reach a terminal, and a doctor that never read the store must
    not report findings (exit 1) either."""
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "not-a-number")
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "memriver doctor: memory store is inaccessible\n"
    assert "not-a-number" not in result.stderr


def test_an_invalid_env_setting_with_json_still_emits_a_json_error_object(monkeypatch,
                                                                          tmp_path):
    """Same failure as above, but `--json` callers parse stdout as JSON and get
    nothing today: a script piping `memriver doctor --json` cannot tell an
    inaccessible store from a hang. The stderr line and exit code are
    unchanged; stdout gets a machine-readable error object instead of silence."""
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "not-a-number")
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path, json_output=True)

    assert result.exit_code == 2
    assert result.stderr == "memriver doctor: memory store is inaccessible\n"
    assert "not-a-number" not in result.stdout
    assert json.loads(result.stdout) == {"error": "memory store is inaccessible"}


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
    memriver_core's own stdlib logging (e.g. an unreadable config.toml) must
    not slip onto the real process stderr alongside doctor's one promised
    path-free line -- logging.lastResort writes straight to sys.stderr,
    bypassing the `stderr` IO parameter entirely."""
    root = tmp_path / "store"
    root.mkdir()
    root.chmod(0o000)
    try:
        result = invoke_doctor(root=root)
    finally:
        root.chmod(0o700)

    assert result.exit_code == 2
    assert result.stderr == "memriver doctor: memory store is inaccessible\n"
    assert str(root) not in result.stderr
    assert capsys.readouterr().err == ""


def test_a_broken_entries_layout_is_inaccessible_not_empty(tmp_path):
    """CLI-boundary regression against a REAL store: `global/entries` occupied
    by a regular file is a store nothing can be written to, so doctor must say
    inaccessible and exit 2 -- not report it as initialized and empty."""
    root = tmp_path / "store"
    (root / "global").mkdir(parents=True)
    (root / "global" / "entries").write_text("not a directory", encoding="utf-8")

    result = invoke_doctor(root=root)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "memriver doctor: memory store is inaccessible\n"
    assert str(root) not in result.stderr


_EXPECTED_JSON = {
    "state": "degraded",
    "findings": [{
        "kind": "unparsable",
        "memory_ids": [],
        "scopes": ["global"],
        "location_hints": ["global/entries/bad.md"],
        "reason": "stored entry cannot be decoded",
        "suggestion": "repair or remove the stored entry",
    }],
}


def test_json_output_matches_the_stable_shape(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "degraded", 1)
    result = invoke_doctor(root=tmp_path, json_output=True)

    assert json.loads(result.stdout) == _EXPECTED_JSON


def test_json_output_keeps_arrays_when_empty(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path, json_output=True)

    assert json.loads(result.stdout) == {"state": "healthy", "findings": []}


def test_human_output_groups_by_kind_with_no_body_or_absolute_path(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "degraded", 1)
    result = invoke_doctor(root=tmp_path)

    assert "store has findings" in result.stdout
    assert "unparsable" in result.stdout
    assert "global" in result.stdout
    assert "global/entries/bad.md" in result.stdout
    assert "stored entry cannot be decoded" in result.stdout
    assert "repair or remove the stored entry" in result.stdout
    assert "Memory.body" not in result.stdout
    assert str(tmp_path) not in result.stdout


def test_control_characters_in_a_finding_render_on_one_line_with_no_raw_escape(
        monkeypatch, tmp_path):
    """A store's scope/location strings come from directory and file names,
    which a user can hand-edit to contain a newline or an ANSI escape. Neither
    may forge a second finding line or emit a raw terminal control sequence in
    the human renderer -- the JSON renderer is already safe via json.dumps."""
    malicious_scope = Scope.project(ProjectId("evil\ninjected\x1b[31mRED"))
    finding = DiagnosticFinding(
        kind="unparsable", memory_ids=(), scopes=(malicious_scope,),
        location_hints=("global/entries/bad\x1b[31m.md",),
        reason="stored entry cannot be decoded",
        suggestion="repair or remove the stored entry")
    install_fake_diagnostics_service_for_findings(monkeypatch, "degraded", [finding])

    result = invoke_doctor(root=tmp_path)

    assert "\x1b" not in result.stdout
    lines = result.stdout.splitlines()
    assert len(lines) == 7  # the injected newline must not forge an extra line
    scopes_line = next(l for l in lines if l.strip().startswith("- scopes:"))
    assert "evil" in scopes_line and "injected" in scopes_line and "RED" in scopes_line


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
        kind="unparsable", memory_ids=(), scopes=(Scope.global_(),),
        location_hints=(os.fsdecode(b"global/entries/bad\x9b31m.md"),),
        reason="stored entry cannot be decoded",
        suggestion="repair or remove the stored entry")
    install_fake_diagnostics_service_for_findings(monkeypatch, "degraded", [finding])
    raw, out = _terminal_stdout()

    doctor.run_doctor(root=tmp_path, json_output=False, stale_days=90,
                      stdout=out, stderr=io.StringIO())
    out.flush()

    assert b"31m.md" in raw.getvalue()  # the location really was rendered
    assert b"\x9b" not in raw.getvalue()


def test_a_non_utf8_filename_in_a_real_store_renders_without_its_raw_byte(tmp_path):
    """The same, end to end through a real store, so the decode boundary is
    the filesystem's rather than a literal in this file. Filesystems that
    enforce UTF-8 names (APFS does) cannot hold the file at all, and there is
    nothing to render there."""
    entries = tmp_path / "global" / "entries"
    entries.mkdir(parents=True)
    hostile = entries / os.fsdecode(b"bad\x9b31m.md")
    try:
        hostile.write_text("not a memory at all", encoding="utf-8")
    except OSError:
        pytest.skip("this filesystem rejects filenames that are not valid UTF-8")

    raw, out = _terminal_stdout()
    exit_code = doctor.run_doctor(root=tmp_path, json_output=False, stale_days=90,
                                  stdout=out, stderr=io.StringIO())
    out.flush()

    assert exit_code == 1  # the store is degraded, so the name really is rendered
    assert b"31m.md" in raw.getvalue()
    assert b"\x9b" not in raw.getvalue()


def test_healthy_human_output_has_no_findings_section(monkeypatch, tmp_path):
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path)

    assert result.stdout == "store is healthy\n"


def test_doctor_reads_the_store_only(monkeypatch, tmp_path):
    """[DEFERRED-4] No harness-configuration audit: doctor never looks under
    HOME beyond the explicit store root, and the JSON has exactly the two
    documented keys."""
    sentinel_home = tmp_path / "sentinel-home"
    sentinel_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: sentinel_home)
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)

    result = invoke_doctor(root=tmp_path / "store", json_output=True)

    assert list(sentinel_home.iterdir()) == []
    assert set(json.loads(result.stdout)) == {"state", "findings"}
