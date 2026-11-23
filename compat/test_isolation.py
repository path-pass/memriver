from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from . import conftest, harness
from .harness import PY, REPO, run

ENV = ["/usr/bin/env"]
# What this module was imported with, which is what the top of any case's module sees.
AT_IMPORT = {name: value for name, value in os.environ.items()
             if name == "HOME" or name.upper().startswith("MEMRIVER_")}


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
                 ["list", "--roo"], ["list", "--root", "--root", str(outside)]):
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


def test_run_refuses_a_path_the_child_would_find_outside_tmp_path(tmp_path, monkeypatch):
    # the test's directory is `case`; `sibling` beside it stands for another test's
    case, sibling = tmp_path / "case", tmp_path / "sibling"
    (case / "deep" / "er").mkdir(parents=True)
    (case / "--samples").mkdir()
    sibling.mkdir()
    monkeypatch.setattr(harness, "TMP_PATH", case)
    inside = {"home": case / "home", "root": case / "store"}
    # a link and `..`: the system goes up from where the link leads and stays inside, a
    # path tidied by its text goes up from where the link lies
    (case / "link").symlink_to(case / "deep" / "er")
    (case / "out").symlink_to(sibling)
    for store in ("link/../../sibling/store", "out/store", str(case / "link" / ".." / ".." / "sibling")):
        for args in (["project", "init", "--root", store, "--yes"], ["list", f"--ro={store}"]):
            with pytest.raises(AssertionError, match="--root .* is outside"):
                run(PY, args, cwd=case, **inside)
    # `--` hides no directory, and after it one may begin with hyphens
    for args in (["export", "--", "--samples/../../sibling/snapshot"],
                 ["export", "--root", "store", "--", "../sibling/snapshot"],
                 ["export", "link/../../sibling/snapshot"], ["export", "out/snapshot"],
                 ["--", "export", "../sibling/snapshot"]):
        with pytest.raises(AssertionError, match="export .* is outside"):
            run(PY, args, cwd=case, **inside)
    # a relative home or store is the child's, read from the child's working directory
    monkeypatch.chdir(case / "deep" / "er")
    for where in ({"home": Path("../.."), "root": case / "store"},
                  {"home": case / "home", "root": Path("../../store")},
                  {**inside, "extra_env": {"HOME": "../.."}},
                  {**inside, "extra_env": {"MEMRIVER_ROOT": "link/../../sibling"}}):
        with pytest.raises(AssertionError, match="(HOME|MEMRIVER_ROOT)=.* is outside"):
            run(ENV, [], cwd=case, **where)
    assert list(sibling.iterdir()) == [] and not (tmp_path / "store").exists()
    # the same spellings that stay inside are let through
    done = run(PY, ["list", "--root", "link/../store"], cwd=case,
               home=Path("home"), root=Path("deep/../store"))
    assert (done.stdout, done.stderr, done.code) == (b"", b"", 0)


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


def test_a_module_is_imported_with_the_sessions_home_and_store():
    # set before anything was collected: the top of a module and a fixture of a wider
    # scope run before the fixture that gives each test its own
    session = conftest.SESSION
    assert AT_IMPORT == {"HOME": str(session / "home"), "MEMRIVER_ROOT": str(session / "store")}
    started = conftest.STARTED_WITH
    assert not session.is_relative_to(Path(started["HOME"]).resolve())
    assert not set(started.values()) & set(AT_IMPORT.values())


def test_what_pytest_is_started_with_reaches_no_module(tmp_path):
    # the suite started as from a developer's shell, up to the collection of one module
    shell = {"home": tmp_path / "shell-home", "root": tmp_path / "shell-store",
             "extra_env": {"MEMRIVER_SEARCH_LIMIT_MAX": "7", "memriver_tags_max": "3",
                           "TMPDIR": str(tmp_path)}}
    done = run([sys.executable, "-B", "-m", "pytest"],
               [str(REPO / "compat" / "test_isolation.py"), "--rootdir", str(REPO),
                "-c", str(REPO / "pyproject.toml"), "-p", "no:cacheprovider", "-q",
                "--basetemp", str(tmp_path / "cases"),
                "-k", "test_a_module_is_imported_with_the_sessions_home_and_store"],
               cwd=tmp_path, **shell)
    assert done.code == 0 and b"1 passed" in done.stdout, done.stdout.decode()
    assert not shell["home"].exists() and not shell["root"].exists()


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
