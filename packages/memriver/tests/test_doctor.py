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
from memriver_core.models import DiagnosticFinding, DiagnosticsReport, Scope


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


@pytest.mark.parametrize("json_output", [False, True])
def test_an_invalid_env_setting_is_the_same_path_free_exit_two(monkeypatch, tmp_path,
                                                               json_output):
    """`load_settings` is the one call doctor makes before the store is opened,
    and the env layer's ValidationError is deliberately not swallowed there: it
    echoes the offending value and, as a traceback, absolute source paths.
    Neither may reach a terminal, and a doctor that never read the store must
    not report findings (exit 1) either."""
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "not-a-number")
    install_fake_diagnostics_service(monkeypatch, "healthy", 0)
    result = invoke_doctor(root=tmp_path, json_output=json_output)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "memriver doctor: memory store is inaccessible\n"
    assert "not-a-number" not in result.stderr


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
