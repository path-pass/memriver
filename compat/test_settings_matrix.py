"""The settings matrix the Rust tests assert (`crates/memriver-core/tests/settings.rs`) is what
the Python implementation answers today: it is recorded again and compared, case by case."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .harness import REPO, run

FIXTURE = REPO / "crates/memriver-core/tests/fixtures/settings_matrix.json"
RECORDER = [sys.executable, "-I", "-B", str(REPO / "compat/record_settings_matrix.py")]


def test_the_recorded_matrix_is_what_python_answers(tmp_path: Path):
    # the matrix is what Python 3.14 answers: from 3.15 tomllib reads TOML 1.1, and four cases
    # recorded as refused are read
    assert sys.version_info[:2] == (3, 14), (
        "the settings matrix is recorded with Python 3.14: run this with 3.14, not "
        + sys.version.split()[0])
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "matrix.json"
    done = run(RECORDER, [str(out), str(work)], home=tmp_path / "home", root=tmp_path / "store",
               cwd=tmp_path)
    assert done.code == 0, done.stderr.decode(errors="replace")
    again = json.loads(out.read_text(encoding="ascii"))
    recorded = json.loads(FIXTURE.read_text(encoding="ascii"))
    assert [case["name"] for case in again] == [case["name"] for case in recorded]
    for now, then in zip(again, recorded, strict=True):
        assert now == then, then["name"]
    # and the file itself, so that a recording is committed exactly as it was written
    assert out.read_bytes() == FIXTURE.read_bytes()
    # nothing was left outside the test's directory, and no store was made
    assert not (tmp_path / "store").exists()
