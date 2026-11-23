"""The recorded `updated` values the Rust tests assert (`crates/memriver-core/tests/
updated_matrix.rs`) are what the Python implementation answers today: the file is recorded
again and compared. Python 3.14 is the reference -- earlier versions read a time of 24:00
and a fraction after the hour or the minute differently -- so on another version this is
skipped."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from .harness import REPO, run

FIXTURE = REPO / "crates/memriver-core/tests/fixtures/updated_matrix.jsonl"
RECORDER = [sys.executable, "-I", "-B", str(REPO / "compat/record_updated_matrix.py")]


@pytest.mark.skipif(sys.version_info[:2] != (3, 14), reason="recorded on Python 3.14")
def test_the_recorded_matrix_is_what_python_answers(tmp_path: Path):
    out = tmp_path / "matrix.jsonl"
    done = run(RECORDER, [str(out)], home=tmp_path / "home", root=tmp_path / "store",
               cwd=tmp_path)
    assert done.code == 0, done.stderr.decode(errors="replace")
    again = [json.loads(line) for line in out.read_text(encoding="ascii").splitlines()]
    recorded = [json.loads(line) for line in FIXTURE.read_text(encoding="ascii").splitlines()]
    assert len(recorded) == 3200
    assert [case["text"] for case in again] == [case["text"] for case in recorded]
    for now, then in zip(again, recorded, strict=True):
        assert now == then, then["text"]
    # and the file itself, so that a recording is committed exactly as it was written
    assert out.read_bytes() == FIXTURE.read_bytes()
    assert not (tmp_path / "store").exists()


def test_the_matrix_has_values_that_are_read_and_values_that_are_refused():
    recorded = [json.loads(line) for line in FIXTURE.read_text(encoding="ascii").splitlines()]
    read = [case for case in recorded if case["utc"] is not None]
    assert len(read) > 100 and len(recorded) - len(read) > 100
    # the two places where Python 3.14 and earlier versions differ are in it
    answers = {case["text"]: case["utc"] for case in recorded}
    assert answers["2026-08-31T24:00:00Z"] == "2026-09-01T00:00:00.000000"
    assert answers["2026-08-31T00:00.5Z"] is None
    assert answers["2026-08-31T00:00:00:00Z"] is None
