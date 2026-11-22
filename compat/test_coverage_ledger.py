"""compat/COVERAGE.md accounts for every Python test function (see its header)."""

from __future__ import annotations

from .gen_coverage import LEDGER, REPO, listing, parse, problems


def test_every_python_test_function_of_a_finished_phase_has_a_target():
    text = LEDGER.read_text()
    assert sorted(parse(text).targets) == listing(REPO), "run compat/gen_coverage.py"
    assert problems(text, REPO) == []


def test_a_missing_or_dangling_target_fails(tmp_path):
    (tmp_path / "compat").mkdir()
    (tmp_path / "compat" / "test_x.py").write_text("def test_real():\n    pass\n")
    (tmp_path / "crates" / "memriver-core" / "src").mkdir(parents=True)
    (tmp_path / "crates" / "memriver-core" / "src" / "a.rs").write_text("fn real_test() {}\n")
    ledger = """finished phases: 1
phase 1: packages/core/tests/**, packages/cli/tests/test_a.py

packages/core/tests/test_a.py::test_compat -> compat:test_x.py::test_real
packages/core/tests/test_a.py::test_rust -> rust:memriver-core::a::real_test
packages/core/tests/test_a.py::test_both -> compat:test_x.py::test_real + rust:memriver-core::a::real_test
packages/core/tests/test_a.py::test_na -> n/a: an import boundary Cargo enforces
packages/core/tests/sub/test_b.py::test_missing ->
packages/cli/tests/test_a.py::test_no_case -> compat:test_x.py::test_gone
packages/cli/tests/test_a.py::test_no_file -> compat:test_y.py::test_real
packages/cli/tests/test_a.py::test_no_rust -> rust:memriver-core::a::gone_test
packages/cli/tests/test_a.py::test_no_reason -> n/a:
packages/cli/tests/test_a.py::test_odd -> somewhere
packages/cli/tests/test_later.py::test_unfinished ->
packages/cli/tests/test_later.py::test_unfinished_dangling -> compat:test_x.py::test_gone
"""
    assert problems(ledger, tmp_path) == [
        "packages/core/tests/sub/test_b.py::test_missing: no target",
        "packages/cli/tests/test_a.py::test_no_case: compat:test_x.py::test_gone does not exist",
        "packages/cli/tests/test_a.py::test_no_file: compat:test_y.py::test_real does not exist",
        "packages/cli/tests/test_a.py::test_no_rust: rust:memriver-core::a::gone_test does not exist",
        "packages/cli/tests/test_a.py::test_no_reason: n/a without a reason",
        "packages/cli/tests/test_a.py::test_odd: not a target: somewhere",
        "packages/cli/tests/test_later.py::test_unfinished_dangling: compat:test_x.py::test_gone does not exist",
    ]
    # a finished phase must say which files it covers
    assert problems("finished phases: 2\n", tmp_path) == ["phase 2 is finished but has no 'phase 2:' line"]
