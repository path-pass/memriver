from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from memriver.project_commands import run_adopt, run_explain, run_init, run_unbind
from memriver_core import ProjectNotFound, StorageFailure
from memriver_core.bootstrap import build_service
from memriver_core.models import ID_RE, new_id
from memriver_core.settings import Settings


def _tree(directory: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(directory)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(directory.rglob("*"))}


def _run(fn, *args, answer="y", tty=True, yes=False, **kw):
    out = io.StringIO()
    code = fn(*args, yes=yes, stdin_is_tty=tty, input_fn=lambda _: answer, stdout=out, **kw)
    return code, out.getvalue()


def _case_insensitive(monkeypatch):
    from memriver_core.repository import directories

    real = directories.same_directory

    def fake(a: str, b: str):
        if a.lower() == b.lower() and a != b:
            return True
        return real(a, b)

    monkeypatch.setattr(directories, "same_directory", fake)


def _sql(store: Path, statement: str, *params) -> list[tuple]:
    """Run one statement behind the stores' backs (foreign keys off) and commit."""
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        return conn.execute(statement, params).fetchall()


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    work = home / "99_git" / "work"
    (work / "frontend" / ".git").mkdir(parents=True)
    (work / "backend" / ".git").mkdir(parents=True)
    store = home / "agent-memory"
    return {"home": home, "work": work, "store": store}


def _service(env):
    return build_service(Settings(root=env["store"]), root=env["store"], home=env["home"])


def _init_bound(env, directory: Path, name: str = "work") -> str:
    service = _service(env)
    return service.init_project(name, service.plan_root(str(directory))).id


def _project(env, name: str = "work") -> str:
    """A project with no directory: bound to a scratch directory, then unbound."""
    scratch = env["home"] / "scratch" / new_id()
    scratch.mkdir(parents=True)
    pid = _init_bound(env, scratch, name)
    service = _service(env)
    plan, _ = service.plan_unbind(pid, str(scratch.resolve()), str(env["work"]))
    service.unbind(plan)
    return pid


def _init(env, directory=None, name=None, **kw):
    return _run(run_init, directory, name=name, root=env["store"], cwd=env["work"],
                home=env["home"], **kw)


def _adopt(env, pid, directory=None, **kw):
    return _run(run_adopt, pid, directory, root=env["store"], cwd=env["work"], home=env["home"], **kw)


def _unbind(env, pid, directory, **kw):
    return _run(run_unbind, pid, directory, root=env["store"], cwd=env["work"], home=env["home"], **kw)


def _nothing_created(env) -> bool:
    return not (env["store"] / "memriver.db").exists() or _service(env).list_projects() == []


def _resolved_id(env, directory: Path) -> str | None:
    return _service(env).open_project_context(str(directory)).read_write_set.project_id


# --- init -----------------------------------------------------------------------

def test_init_creates_a_named_project_and_binds_the_directory(env):
    before = _tree(env["work"])
    code, out = _init(env)
    assert code == 0
    (registered,) = _service(env).list_projects()
    assert ID_RE.fullmatch(registered.id)
    assert registered.root == str(env["work"].resolve())
    assert registered.name == "work"
    assert "project: work  (new; id assigned when written)" in out
    assert f"created project work [{registered.id}]" in out
    assert "Direct child directories containing a .git marker: 2" in out
    assert "including repositories added later" in out
    assert "Restart the affected harness sessions" in out
    assert _tree(env["work"]) == before


def test_init_takes_a_name_option_and_refuses_a_bad_name_before_the_prompt(env):
    code, _ = _init(env, name="  My Work  ", yes=True)
    assert code == 0
    (registered,) = _service(env).list_projects()
    assert registered.name == "My Work"
    prompts: list[str] = []
    out = io.StringIO()
    code = run_init(env["work"] / "frontend", name="x" * 121, root=env["store"], yes=False,
                    stdin_is_tty=True, input_fn=lambda p: prompts.append(p) or "y",
                    stdout=out, cwd=env["work"], home=env["home"])
    assert code == 2 and "refused: project name is longer than 120" in out.getvalue()
    assert prompts == []


def test_init_aborts_on_no_and_refuses_non_tty_without_yes(env):
    before = _tree(env["work"])
    code, _ = _init(env, answer="n")
    assert code == 1 and _nothing_created(env)
    code, out = _init(env, tty=False)
    assert code == 2 and "memriver project init" in out and _nothing_created(env)
    assert _tree(env["work"]) == before
    code, _ = _init(env, tty=False, yes=True)
    assert code == 0


def test_init_prompt_eof_aborts_without_a_traceback(env):
    def _eof(_):
        raise EOFError

    out = io.StringIO()
    code = run_init(None, name=None, root=env["store"], yes=False, stdin_is_tty=True,
                    input_fn=_eof, stdout=out, cwd=env["work"], home=env["home"])
    assert code == 1
    assert out.getvalue().endswith("aborted; nothing was written\n")
    assert _nothing_created(env)


@pytest.mark.parametrize("target", ["/", "{home}", "{home_parent}"])
def test_init_refuses_root_home_and_home_ancestors(env, target):
    env["home"].mkdir(parents=True, exist_ok=True)
    path = Path(target.format(home=env["home"], home_parent=env["home"].parent))
    code, out = _init(env, path, yes=True)
    assert code == 2 and "refused" in out and _nothing_created(env)


def test_init_refuses_case_alias_of_home_and_of_the_store_parent(env, monkeypatch):
    _case_insensitive(monkeypatch)
    alias_home = env["home"].parent / env["home"].name.upper()
    alias_home.mkdir(parents=True, exist_ok=True)
    code, out = _init(env, alias_home, yes=True)
    assert code == 2 and "home directory" in out
    store_parent_alias = env["store"].parent.parent / env["store"].parent.name.upper()
    store_parent_alias.mkdir(parents=True, exist_ok=True)
    code, out = _run(run_init, store_parent_alias, name=None, yes=True, root=env["store"],
                     cwd=env["work"], home=env["home"].parent / "unrelated-home")
    assert code == 2 and "store" in out


def test_init_refuses_when_containment_cannot_be_verified(env, monkeypatch):
    from memriver_core.repository import directories

    monkeypatch.setattr(directories, "same_directory", lambda a, b: None)
    code, out = _init(env, yes=True)
    assert code == 2 and "could not verify" in out and _nothing_created(env)


def test_init_refuses_store_inside_target(env):
    code, out = _run(run_init, env["home"] / "99_git", name=None, yes=True,
                     root=env["work"] / "frontend" / "mem", cwd=env["work"], home=env["home"])
    assert code == 2 and "store" in out


def test_init_refuses_a_directory_inside_the_memory_store(env):
    inside = env["store"] / "somewhere"
    inside.mkdir(parents=True)
    before = _tree(env["store"])
    code, out = _init(env, inside, yes=True)
    assert code == 2 and f"lies inside the memory store {env['store']}" in out
    assert _tree(env["store"]) == before


def test_init_refuses_missing_directory_and_already_bound_dir(env):
    code, _ = _init(env, env["work"] / "nope", yes=True)
    assert code == 2
    pid = _init_bound(env, env["work"])
    before = _tree(env["work"])
    code, out = _init(env, env["work"], yes=True)
    assert code == 2 and f"already bound to project {pid}" in out
    assert len(_service(env).list_projects()) == 1
    assert _tree(env["work"]) == before


def test_init_refuses_an_unaddressable_path_without_a_traceback(env):
    code, out = _init(env, Path("/x\x00y"), yes=True)
    assert code == 2 and "is not an existing directory" in out
    assert _nothing_created(env)


def test_init_inside_a_registered_parent_is_allowed(env):
    parent = _init_bound(env, env["work"])
    code, _ = _init(env, env["work"] / "frontend", yes=True)
    assert code == 0 and len(_service(env).list_projects()) == 2
    assert _resolved_id(env, env["work"] / "frontend") != parent
    assert _resolved_id(env, env["work"] / "backend") == parent


def test_parent_init_lists_existing_child_roots_as_independent(env):
    child = _init_bound(env, env["work"] / "frontend", "frontend")
    code, out = _init(env, yes=True)
    assert code == 0
    assert "These registered sub-projects stay independent:" in out
    assert f"{child}: {(env['work'] / 'frontend').resolve()}" in out
    assert _resolved_id(env, env["work"] / "frontend") == child


def test_parent_plan_lists_a_case_alias_child_root_as_independent(env, monkeypatch):
    _case_insensitive(monkeypatch)
    child_alias = env["work"] / "FRONTEND"
    child_alias.mkdir(exist_ok=True)
    child = _init_bound(env, child_alias, "frontend")
    out = io.StringIO()
    run_init(None, name=None, root=env["store"], yes=False, stdin_is_tty=False,
             input_fn=lambda _: "n", stdout=out, cwd=env["work"], home=env["home"])
    assert "These registered sub-projects stay independent:" in out.getvalue()
    assert f"{child}: {child_alias.resolve()}" in out.getvalue()


def test_init_independent_list_never_forges_a_fake_project_line(env):
    canonical_work = env["work"].resolve()
    fake_id = new_id()
    forged = f"{canonical_work}/child\n    {fake_id}: /forged"
    _service(env).ensure_global()
    _sql(env["store"], "INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, ?, 0)",
         new_id(), "forger", forged)
    code, out = _init(env, yes=True)
    assert code == 0
    assert not any(line.startswith(f"    {fake_id}: /forged") for line in out.splitlines())
    assert "/forged" in out


def test_init_of_a_directory_named_with_a_newline_never_forges_a_line(env):
    target = env["work"] / "bad\nFORGED-LINE"
    target.mkdir()
    code, out = _init(env, target, yes=True)
    assert code == 0
    assert not any(line.startswith("FORGED-LINE") for line in out.splitlines())
    assert "bad FORGED-LINE" in out


def test_target_deleted_while_the_prompt_is_open_is_refused(env):
    target = env["work"] / "ephemeral"
    target.mkdir()

    def answer(_prompt):
        target.rmdir()
        return "y"

    out = io.StringIO()
    code = run_init(target, name=None, root=env["store"], yes=False, stdin_is_tty=True,
                    input_fn=answer, stdout=out, cwd=env["work"], home=env["home"])
    assert code == 2 and "out of date" in out.getvalue()
    assert _nothing_created(env)


def test_store_repointed_into_target_while_the_prompt_is_open_is_refused(env):
    before = _tree(env["work"])

    def answer(_prompt):
        env["store"].parent.mkdir(parents=True, exist_ok=True)
        env["store"].symlink_to(env["work"])
        return "y"

    out = io.StringIO()
    code = run_init(None, name=None, root=env["store"], yes=False, stdin_is_tty=True,
                    input_fn=answer, stdout=out, cwd=env["work"], home=env["home"])
    # the confirmed plan's store now lies inside the target: refused before
    # anything is opened, so nothing lands in the target
    assert code == 2 and "lies inside this directory" in out.getvalue()
    assert _tree(env["work"]) == before


def test_init_is_one_transaction_and_a_changed_target_creates_nothing(env):
    answers = iter(["y"])

    def answer_after_breaking_the_target(_prompt):
        env["work"].rename(env["work"].with_name("moved"))
        return next(answers)

    out = io.StringIO()
    code = run_init(env["work"], name=None, root=env["store"], yes=False, stdin_is_tty=True,
                    input_fn=answer_after_breaking_the_target, stdout=out, cwd=env["work"].parent,
                    home=env["home"])
    assert code == 2 and "plan is out of date" in out.getvalue()
    assert _service(env).list_projects() == []


@pytest.mark.parametrize("command", ["init", "adopt", "unbind"])
def test_a_store_link_redirected_during_the_prompt_is_refused(env, command):
    real, other = env["home"] / "store-a", env["home"] / "store-b"
    real.mkdir()
    other.mkdir()
    link = env["home"] / "store-link"
    link.symlink_to(real)
    service = build_service(Settings(root=link), root=link, home=env["home"])
    service.ensure_global()
    pid = None
    if command != "init":
        pid = service.init_project("work", service.plan_root(str(env["work"]))).id
        if command == "adopt":
            plan, _ = service.plan_unbind(pid, str(env["work"].resolve()), str(env["work"]))
            service.unbind(plan)

    def redirect(_prompt):
        link.unlink()
        link.symlink_to(other)
        return "y"

    out = io.StringIO()
    common = {"root": link, "yes": False, "stdin_is_tty": True, "input_fn": redirect,
              "stdout": out, "cwd": env["work"], "home": env["home"]}
    if command == "init":
        code = run_init(env["work"], name=None, **common)
    elif command == "adopt":
        code = run_adopt(pid, env["work"], **common)
    else:
        code = run_unbind(pid, env["work"], **common)
    assert code == 2 and "plan is out of date" in out.getvalue()
    assert not (other / "memriver.db").exists()


def test_relative_directories_resolve_against_the_injected_cwd(env):
    code, out = _run(run_init, Path("frontend"), name=None, root=env["store"], cwd=env["work"],
                     home=env["home"], yes=True)
    assert code == 0, out
    assert f"bound {(env['work'] / 'frontend').resolve()}" in out


# --- adopt ------------------------------------------------------------------------

def test_adopt_binds_an_existing_project_and_is_idempotent(env):
    pid = _init_bound(env, env["work"])
    code, out = _adopt(env, pid, env["work"], yes=True)
    assert code == 0 and "nothing to do" in out
    code, _ = _unbind(env, pid, env["work"], yes=True)
    assert code == 0 and _service(env).read_project(pid).root is None
    code, out = _adopt(env, pid, env["work"], yes=True)
    assert code == 0 and "project: work" in out
    assert _service(env).read_project(pid).root == str(env["work"].resolve())
    other = env["home"] / "99_git" / "other"
    other.mkdir()
    code, out = _adopt(env, pid, other, yes=True)
    assert code == 2 and "already has a directory" in out and "unbind it first" in out
    code, out = _adopt(env, new_id(), env["work"], yes=True)
    assert code == 2 and "no such project" in out
    code, out = _adopt(env, "x" * 300, env["work"], yes=True)
    assert code == 2 and "no such project" in out


@pytest.mark.parametrize("error, sentence", [
    (StorageFailure(), "could not complete the store write"),
    (ProjectNotFound("x"), "no such project"),
])
def test_adopt_survives_the_project_read_failing_after_has_directory(env, monkeypatch,
                                                                     error, sentence):
    from memriver_core.application.service import MemoryService

    pid = _init_bound(env, env["work"])
    other = env["home"] / "99_git" / "other"
    other.mkdir()

    def failing_read(self, project_id):
        raise error

    monkeypatch.setattr(MemoryService, "read_project", failing_read)
    code, out = _adopt(env, pid, other, yes=True)
    assert code == 2 and sentence in out


def test_adopt_refuses_the_global_project(env):
    global_id = _service(env).ensure_global()
    code, out = _adopt(env, global_id, env["work"], yes=True)
    assert code == 2 and "refused: the global project cannot be bound to a directory" in out
    assert _service(env).read_project(global_id).root is None


def test_an_invalid_project_id_argument_cannot_forge_an_output_line(env):
    code, out = _adopt(env, "bad\nFORGED-LINE", env["work"], yes=True)
    assert code == 2
    assert not any(line.startswith("FORGED-LINE") for line in out.splitlines())
    assert "no such project: bad FORGED-LINE" in out
    code, out = _unbind(env, "bad\nFORGED-LINE", env["work"], yes=True)
    assert code == 2
    assert not any(line.startswith("FORGED-LINE") for line in out.splitlines())


def test_adopt_applies_the_same_target_checks(env):
    pid = _project(env)
    code, out = _adopt(env, pid, env["home"], yes=True)
    assert code == 2 and "refused" in out


def test_adopt_of_a_project_removed_during_confirmation_exits_2(env):
    pid = _project(env)

    def answer(_prompt):
        _sql(env["store"], "DELETE FROM projects WHERE id = ?", pid)
        return "y"

    out = io.StringIO()
    code = run_adopt(pid, env["work"], root=env["store"], yes=False, stdin_is_tty=True,
                     input_fn=answer, stdout=out, cwd=env["work"], home=env["home"])
    assert code == 2 and "no such project" in out.getvalue()
    assert _resolved_id(env, env["work"]) is None


# --- unbind -----------------------------------------------------------------------

def test_unbind_literal_first_and_shows_next_identity(env):
    import shutil

    old_dir = env["home"] / "99_git" / "old-work"
    old_dir.mkdir(parents=True)
    old = str(old_dir.resolve())
    pid = _init_bound(env, old_dir)
    shutil.rmtree(old_dir)
    code, out = _unbind(env, pid, Path(old), yes=True)
    assert code == 0 and "afterwards: none" in out
    assert _service(env).read_project(pid).root is None
    code, out = _unbind(env, pid, Path(old), yes=True)
    assert code == 2 and "not bound" in out


def test_unbind_of_a_root_that_became_a_symlink_removes_the_stored_spelling(env):
    old = env["home"] / "99_git" / "old"
    new = env["home"] / "99_git" / "new"
    old.mkdir(parents=True)
    pid = _init_bound(env, old)
    old.rmdir()
    new.mkdir()
    old.symlink_to(new)
    code, _ = _unbind(env, pid, old, yes=True)
    assert code == 0 and _service(env).read_project(pid).root is None


def test_unbind_shows_parent_as_next_identity(env):
    parent = _init_bound(env, env["work"])
    child = _init_bound(env, env["work"] / "frontend", "frontend")
    out = io.StringIO()
    code = run_unbind(child, env["work"] / "frontend", root=env["store"], yes=True,
                      stdin_is_tty=True, input_fn=lambda _: "y", stdout=out,
                      cwd=env["work"] / "frontend", home=env["home"])
    assert code == 0 and f"afterwards: registered {parent}" in out.getvalue()


def test_unbind_resolves_a_relative_directory_against_the_injected_cwd(env):
    link = env["work"] / "link"
    link.symlink_to(env["work"] / "frontend", target_is_directory=True)
    pid = _init_bound(env, env["work"] / "frontend")
    code, out = _unbind(env, pid, Path("link"), yes=True)
    assert code == 0, out
    assert _service(env).read_project(pid).root is None


def test_unbind_executes_exactly_the_pair_it_showed(env):
    pid = _init_bound(env, env["work"])

    def rebind_elsewhere_while_prompting(_prompt):
        service = _service(env)
        plan, _ = service.plan_unbind(pid, str(env["work"].resolve()), str(env["work"]))
        service.unbind(plan)
        other = env["home"] / "99_git" / "other"
        other.mkdir()
        service.adopt(pid, service.plan_root(str(other), pid))
        return "y"

    out = io.StringIO()
    code = run_unbind(pid, env["work"], root=env["store"], yes=False, stdin_is_tty=True,
                      input_fn=rebind_elsewhere_while_prompting, stdout=out, cwd=env["work"],
                      home=env["home"])
    assert code == 2 and "binding changed while waiting" in out.getvalue()
    assert _service(env).read_project(pid).root == str((env["home"] / "99_git" / "other").resolve())


# --- explain ----------------------------------------------------------------------

def _explain(env, project_dir=None, cwd=None):
    out = io.StringIO()
    code = run_explain(root=env["store"], project_dir=project_dir, stdout=out,
                       cwd=cwd or env["work"], home=env["home"])
    return code, out.getvalue()


def test_explain_states_and_exit_codes(env):
    code, text = _explain(env)
    assert code == 0 and "state: none" in text
    # no store yet: global does not exist, so it is not claimed readable
    assert "reads: none\n" in text and "writes: none" in text
    assert not env["store"].exists()
    pid = _init_bound(env, env["work"])
    code, text = _explain(env, project_dir=env["work"] / "frontend")
    assert code == 0 and f"reads: {pid}\n" in text
    _service(env).ensure_global()
    code, text = _explain(env, project_dir=env["work"] / "frontend")
    assert code == 0
    assert f"project: {pid}" in text and "name: work\n" in text
    assert f"root: {env['work'].resolve()}" in text
    assert f"reads: {pid}, global" in text and f"writes: {pid}" in text
    code, text = _explain(env, cwd=env["home"])
    assert code == 0 and "state: none" in text and "reads: global\n" in text
    # one re-pointed root degrades every directory, never a silent fall-through
    elsewhere, moved = env["home"] / "elsewhere", env["home"] / "moved"
    elsewhere.mkdir()
    _init_bound(env, elsewhere, "elsewhere")
    elsewhere.rename(moved)
    elsewhere.symlink_to(moved)
    code, text = _explain(env)
    assert code == 1 and "state: degraded" in text
    assert "registered root is no longer a canonical path" in text
    assert "writes: none" in text
    (env["store"] / "memriver.db").write_bytes(b"not a database")
    code, text = _explain(env)
    assert code == 1 and "state: unavailable" in text
    assert "diagnostic: the memory store could not be read" in text
    assert "reads: none\n" in text and "writes: none" in text


def test_explain_keeps_consecutive_spaces_in_a_registered_root(env):
    target = env["work"] / "two  spaces"
    target.mkdir()
    _init_bound(env, target)
    code, text = _explain(env, project_dir=target)
    assert code == 0 and f"root: {target.resolve()}\n" in text


def test_explain_diagnostic_never_forges_a_fake_root_line(env):
    # a root whose nearest existing ancestor is a symlink is "re-pointed", and
    # its stored spelling lands in the diagnostic
    real = env["home"] / "real"
    real.mkdir(parents=True)
    link = env["home"] / "link"
    link.symlink_to(real)
    forged = f"{link}/x\n  root: /forged"
    _service(env).ensure_global()
    _sql(env["store"], "INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, ?, 0)",
         new_id(), "forger", forged)
    code, text = _explain(env, cwd=env["home"])
    assert code == 1
    assert not any(line.startswith("  root: /forged") for line in text.splitlines())
    assert "/forged" in text


def test_explain_never_creates_or_writes_the_store(env):
    code, _ = _explain(env)
    assert code == 0 and not env["store"].exists()
    _init_bound(env, env["work"])
    _service(env).ensure_global()
    before = _tree(env["store"])
    code, _ = _explain(env)
    assert code == 0 and _tree(env["store"]) == before


# --- end to end through a server ------------------------------------------------------

def test_a_server_built_before_bind_keeps_its_identity(env):
    import asyncio

    from fastmcp import Client
    from memriver.server import build_server

    stale = build_server(root=env["store"], project_dir=env["work"])
    pid = _init_bound(env, env["work"])
    fresh = build_server(root=env["store"], project_dir=env["work"])

    async def header(server):
        async with Client(server) as c:
            return (await c.call_tool("memory_index", {})).data.splitlines()[0]

    assert asyncio.run(header(stale)).startswith("project: none")
    assert asyncio.run(header(fresh)).startswith(f"project: work [{pid}]")


@pytest.mark.parametrize("scenario", [
    "init-ok", "init-no", "init-non-tty", "init-refused-home", "init-already-bound",
    "adopt-ok", "adopt-missing-id", "adopt-refused-home",
    "unbind-ok", "unbind-not-bound", "explain",
])
def test_no_command_or_refusal_touches_the_target(env, scenario):
    work = env["work"]
    pid = None
    if scenario.startswith("adopt"):
        pid = _project(env)
    if scenario.startswith(("unbind", "init-already")):
        pid = _init_bound(env, work)
    before = _tree(work)
    out = io.StringIO()
    common = {"root": env["store"], "stdin_is_tty": True, "input_fn": lambda _: "y",
              "stdout": out, "cwd": work, "home": env["home"]}
    match scenario:
        case "init-ok": run_init(None, name=None, yes=True, **common)
        case "init-no": run_init(None, name=None, yes=False, **{**common, "input_fn": lambda _: "n"})
        case "init-non-tty": run_init(None, name=None, yes=False, **{**common, "stdin_is_tty": False})
        case "init-refused-home": run_init(env["home"], name=None, yes=True, **common)
        case "init-already-bound": run_init(work, name=None, yes=True, **common)
        case "adopt-ok": run_adopt(pid, work / "frontend", yes=True, **common)
        case "adopt-missing-id": run_adopt(new_id(), work, yes=True, **common)
        case "adopt-refused-home": run_adopt(pid, env["home"], yes=True, **common)
        case "unbind-ok": run_unbind(pid, work, yes=True, **common)
        case "unbind-not-bound": run_unbind(pid, work / "backend", yes=True, **common)
        case "explain": run_explain(root=env["store"], project_dir=None, stdout=out, cwd=work,
                                    home=env["home"])
    assert _tree(work) == before


def test_read_project_is_how_existence_is_decided(env):
    with pytest.raises(ProjectNotFound):
        _service(env).read_project(new_id())
