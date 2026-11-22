from __future__ import annotations

from pathlib import Path

import pytest

from .harness import PY, run

ENV = ["/usr/bin/env"]


def test_run_refuses_a_home_outside_tmp_path(tmp_path):
    outside = tmp_path.parent
    with pytest.raises(AssertionError, match="outside"):
        run(PY, ["--version"], home=outside, root=tmp_path / "store", cwd=tmp_path)
    with pytest.raises(AssertionError, match="outside"):
        run(PY, ["--version"], home=tmp_path / "home", root=outside / "store", cwd=tmp_path)
    with pytest.raises(AssertionError, match="outside"):
        run(PY, ["--version"], home=tmp_path / "home" / ".." / "..", root=tmp_path, cwd=tmp_path)


def test_a_child_sees_only_the_constructed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMRIVER_SEARCH_LIMIT_MAX", "7")
    home, root = tmp_path / "home", tmp_path / "store"
    done = run(ENV, [], home=home, root=root, cwd=tmp_path, extra_env={"EXTRA": "1"})
    assert sorted(done.stdout.decode().splitlines()) == [
        "EXTRA=1", f"HOME={home}", f"MEMRIVER_ROOT={root}", "PATH=/usr/bin:/bin"]
    assert (done.stderr, done.code) == (b"", 0)


def test_extra_env_cannot_move_the_home_or_the_store(tmp_path):
    for name in ("HOME", "MEMRIVER_ROOT"):
        with pytest.raises(AssertionError, match="outside"):
            run(ENV, [], home=tmp_path / "home", root=tmp_path / "store", cwd=tmp_path,
                extra_env={name: str(tmp_path.parent)})


def test_processes_are_started_only_by_the_harness():
    token = "sub" + "process"
    here = Path(__file__).parent
    named = sorted(path.name for path in here.rglob("*.py") if token in path.read_text())
    assert named == ["harness.py"]
