from __future__ import annotations

import pytest

from .harness import Case, both


def test_both_print_a_version(tmp_path):
    case = Case(tmp_path)
    py, rs = both(["--version"], home=case.home, root=case.py.root, cwd=case.work)
    assert (py.stdout, py.code) == (b"0.1.0\n", 0)
    assert (rs.stdout, rs.code) == (b"0.2.0\n", 0)
    case.assert_same_output(py, rs, allow=("version-string",))
    with pytest.raises(AssertionError):
        case.assert_same_output(py, rs)


def test_an_exception_must_be_a_listed_difference(tmp_path):
    case = Case(tmp_path)
    py, rs = both(["--version"], home=case.home, root=case.py.root, cwd=case.work)
    with pytest.raises(KeyError):
        case.assert_same_output(py, rs, allow=("not-a-listed-difference",))
