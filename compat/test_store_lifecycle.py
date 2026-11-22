"""A store from nothing, and a store too old to use: both implementations do the same."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from .differences import MASKS
from .harness import PY, RS, Case, run, snapshot

# the binary gets `project init` and the commands that read a store later; until then
# the Rust side cannot take part, and each case is checked between two Python sides
BOTH = [pytest.param(PY, id="python-twice"),
        pytest.param(RS, id="rust",
                     marks=pytest.mark.xfail(strict=True, reason="needs project init"))]


def _files(root: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in sorted(root.iterdir())}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("rs", BOTH)
def test_schema_text_is_identical(tmp_path, rs):
    case = Case(tmp_path, rs=rs)
    directory = case.work / "a"
    directory.mkdir()
    py, rs_done = case.step(lambda side: side.run(["project", "init", str(directory), "--yes"]))
    assert py.code == 0
    case.assert_same_output(py, rs_done, "project-init")
    ours, theirs = snapshot(case.py.root), snapshot(case.rs.root)
    assert len(ours.schema) == 18
    assert theirs.schema == ours.schema
    assert theirs.user_version == ours.user_version == 6
    for side in (case.py, case.rs):
        assert (_mode(side.root), _mode(side.root / "memriver.db")) == (0o700, 0o600)
        assert sorted(entry.name for entry in side.root.iterdir()) == ["memriver.db"]
        with closing(sqlite3.connect(f"{(side.root / 'memriver.db').as_uri()}?mode=ro",
                                     uri=True)) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    case.assert_same_store()


@pytest.mark.parametrize("rs", BOTH)
def test_a_store_below_5_is_refused_by_both_and_left_byte_identical(tmp_path, rs):
    # a store the Python implementation created, set back to version 3
    home, work, seed = tmp_path / "home", tmp_path / "work", tmp_path / "seed"
    (work / "a").mkdir(parents=True)
    home.mkdir()
    made = run(PY, ["project", "init", str(work / "a"), "--yes"], home=home, root=seed, cwd=work)
    assert made.code == 0
    with closing(sqlite3.connect(seed / "memriver.db")) as conn:
        conn.execute("PRAGMA user_version = 3")
    case = Case(tmp_path, seed=seed, rs=rs)
    before = [_files(side.root) for side in (case.py, case.rs)]
    assert before[0] == before[1] and list(before[0]) == ["memriver.db"]
    for command in (["list"], ["search", "x"], ["project", "explain"], ["sessions"], ["doctor"]):
        py, rs_done = case.step(lambda side, command=command: side.run(command, cwd=work / "a"))
        assert (py.stdout, py.code) == (b"", 1), command
        # one line, whatever the command puts before it (`memriver:`, `memriver doctor:`)
        assert py.stderr.count(b"\n") == 1 and py.stderr.endswith(
            b": the memory store is at schema version 3; this memriver needs schema version 4\n"
        ), command
        case.assert_same_output(py, rs_done, allow=("needs-upgrade-line",))
        assert [_files(side.root) for side in (case.py, case.rs)] == before, command


def test_the_needs_upgrade_mask_covers_only_the_tail_that_differs():
    mask = MASKS["needs-upgrade-line"]
    head = "memriver: the memory store is at schema version 3; this memriver "
    python = head + "needs schema version 4\n"
    rust = head + ("needs schema version 6. Upgrade the store to version 6 with an earlier "
                   "memriver release first\n")
    assert mask.sub("<x>", python) == mask.sub("<x>", rust) == head + "<x>"
    # the version found stays compared, and so does every other text
    other = python.replace("version 3;", "version 2;")
    assert mask.sub("<x>", other) != mask.sub("<x>", rust)
    for text in (head + "needs schema version 5\n",
                 head + "needs schema version 6\n",
                 python + "more\n",
                 "needs schema version 4\n",
                 python.replace("this memriver", "that memriver"),
                 rust.replace("an earlier", "a later")):
        assert mask.sub("<x>", text) == text, text
