from __future__ import annotations

import os
import re
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
    # a shortened option names a store too, and the last of two is the one that is used
    for args in (["--version", "--roo", str(outside / "store")], ["list", f"--ro={outside}"],
                 ["list", "--r", "../../store"], ["list", "--root", "ok", "--root", str(outside)],
                 ["list", "--roo"]):
        with pytest.raises(AssertionError, match="outside|without a path"):
            run(PY, args, cwd=tmp_path, **inside)
    # the one directory a command writes into
    for args in (["export", str(outside / "snapshot")], ["export", "../../snapshot"],
                 ["export", "--root", "store", str(outside / "snapshot")]):
        with pytest.raises(AssertionError, match="export .* is outside"):
            run(PY, args, cwd=tmp_path, **inside)
    assert not (outside / "snapshot").exists() and not (outside / "store").exists()
    # a directory that is only looked at may lie anywhere: the command refuses it itself
    done = run(PY, ["project", "init", "/", "--yes"], cwd=tmp_path, **inside)
    assert done.code == 2 and done.stdout.startswith(b"refused: ")
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


def test_the_tests_own_process_finds_its_home_and_store_under_tmp_path(tmp_path, monkeypatch):
    from memriver_core.settings import load_settings, storage_root

    inside = tmp_path.resolve()
    assert Path.home().resolve().is_relative_to(inside)
    # with the variable, without it, and for a call that names nothing at all
    assert storage_root() == tmp_path / "store"                        # default-store: asserted
    assert load_settings().root == tmp_path / "store"                  # default-store: asserted
    monkeypatch.delenv("MEMRIVER_ROOT")
    assert storage_root() == tmp_path / "home" / "agent-memory"        # default-store: asserted
    assert not [name for name in os.environ if name.upper().startswith("MEMRIVER_")]
    assert not (tmp_path / "store").exists() and not (tmp_path / "home" / "agent-memory").exists()


def test_a_variable_of_the_developers_shell_does_not_reach_a_test(tmp_path):
    # set by the fixture below before this module's autouse fixture ran
    assert os.environ["MEMRIVER_ROOT"] == str(tmp_path / "store")
    assert "MEMRIVER_SEARCH_LIMIT_MAX" not in os.environ and "memriver_tags_max" not in os.environ


@pytest.fixture(autouse=True, scope="module")
def _a_developers_shell():
    """What a developer's shell might hold, put there before any test of this module."""
    held = {"MEMRIVER_ROOT": "/nonexistent/developer/store", "MEMRIVER_SEARCH_LIMIT_MAX": "7",
            "memriver_tags_max": "3"}
    before = {name: os.environ.get(name) for name in held}
    os.environ.update(held)
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# A call of the Python implementation inside the test's own process that could look for a
# store by itself. The fixture keeps such a call inside tmp_path; this keeps it from being
# written in the first place. Allowed: the template the harness runs in a child with a
# constructed environment, the recorder that names each case's own root, and the test above.
DEFAULT_STORE = re.compile("|".join(
    [r"\b" + name + r"\(" for name in ("load_" + "settings", "build_" + "services", "storage_" + "root")]
    + [r"\broot=" + "None"]))
ALLOWED = {
    ("harness.py",
     "services = build_services(load_settings(root_override=ROOT), root=ROOT, home=HOME)"),  # default-store: the list
    ("record_settings_matrix.py", "settings = load_settings(root_override=root)"),  # default-store: the list
}


def _default_store_calls(directory: Path) -> list[tuple[str, str]]:
    return sorted((path.name, line.strip()) for path in directory.glob("*.py")
                  for line in path.read_text().splitlines()
                  if DEFAULT_STORE.search(line) and (path.name, line.strip()) not in ALLOWED
                  # this file alone may mark a line of its own: the list above, the test above
                  and not (path == Path(__file__) and "# default-store: " in line))


def test_no_case_asks_the_python_implementation_for_a_store_it_does_not_name(tmp_path):
    assert _default_store_calls(Path(__file__).parent) == []
    # every way of asking is one the scan sees
    asking = ["settings = load_" + "settings()", "services = build_" + "services(settings)",
              "root = storage_" + "root()", "run_delete(memory_id, root=" + "None, home=home)"]
    (tmp_path / "case.py").write_text("\n".join(asking) + "\nproject_root=None\nroot = None\n")
    assert _default_store_calls(tmp_path) == sorted(("case.py", line) for line in asking)
