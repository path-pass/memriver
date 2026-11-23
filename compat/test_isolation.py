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


def test_run_refuses_a_store_argument_or_a_working_directory_outside_tmp_path(tmp_path):
    inside = {"home": tmp_path / "home", "root": tmp_path / "store"}
    outside = tmp_path.parent
    for args in (["list", "--root", str(outside / "store")], ["list", f"--root={outside}"],
                 ["--root", "../../store", "list"], ["list", "--root"]):
        with pytest.raises(AssertionError, match="outside|without a path"):
            run(PY, args, cwd=tmp_path, **inside)
    with pytest.raises(AssertionError, match="outside"):
        run(PY, ["--version"], cwd=outside, **inside)
    # a relative store is read from the working directory, and both are inside
    (tmp_path / "work").mkdir()
    done = run(PY, ["list", "--root", "../other-store"], cwd=tmp_path / "work", **inside)
    assert (done.stdout, done.code) == (b"", 0)
    assert not (tmp_path / "other-store").exists()


def test_processes_are_started_only_by_the_harness(tmp_path):
    tokens = ["sub" + "process", "os." + "system", "os." + "popen", "sp" + "awn",
              "os." + "exec", "os." + "fork"]
    here = Path(__file__).parent

    def named(directory: Path) -> list[str]:
        return sorted(path.name for path in directory.rglob("*.py")
                      if any(token in path.read_text() for token in tokens))
    assert named(here) == ["harness.py"]
    # each way of starting a process is one the scan sees
    for at, token in enumerate(tokens):
        (tmp_path / f"case_{at}.py").write_text(f"import os\n{token}('true')\n")
    assert named(tmp_path) == [f"case_{at}.py" for at in range(len(tokens))]
