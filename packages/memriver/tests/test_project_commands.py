from __future__ import annotations

import io
from pathlib import Path

import pytest
from memriver import project_context
from memriver.project_commands import run_adopt, run_explain, run_init, run_unbind
from memriver.project_context import bind, load_registry, resolve
from memriver_core import ProjectNotFound
from memriver_core.bootstrap import build_service
from memriver_core.config import Settings
from memriver_core.models import ID_RE, AccessContext, new_id


def _tree(directory: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(directory)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(directory.rglob("*"))}


def _run(fn, *args, answer="y", tty=True, yes=False, **kw):
    out = io.StringIO()
    code = fn(*args, yes=yes, stdin_is_tty=tty, input_fn=lambda _: answer, stdout=out, **kw)
    return code, out.getvalue()


def _case_insensitive(monkeypatch):
    real = project_context.same_directory

    def fake(a: str, b: str):
        if a.lower() == b.lower() and a != b:
            return True
        return real(a, b)

    monkeypatch.setattr(project_context, "same_directory", fake)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    work = home / "99_git" / "work"
    (work / "frontend" / ".git").mkdir(parents=True)
    (work / "backend" / ".git").mkdir(parents=True)
    store = home / "agent-memory"
    return {"home": home, "work": work, "store": store}


def _service(env):
    return build_service(Settings(root=env["store"]), root=env["store"])


def _project(env, name: str = "work") -> str:
    return _service(env).create_project(name).id


def _bind(env, project_id: str, directory: Path) -> None:
    bind(env["store"], _service(env), project_id, str(directory.resolve()))


def _init(env, directory=None, name=None, **kw):
    return _run(run_init, directory, name=name, root=env["store"], cwd=env["work"],
                home=env["home"], **kw)


def _adopt(env, pid, directory=None, **kw):
    return _run(run_adopt, pid, directory, root=env["store"], cwd=env["work"], home=env["home"], **kw)


def _unbind(env, pid, directory, **kw):
    return _run(run_unbind, pid, directory, root=env["store"], cwd=env["work"], home=env["home"], **kw)


def _nothing_created(env) -> bool:
    return not (env["store"] / "projects").exists() and not (env["store"] / "registry").exists()


# --- init -----------------------------------------------------------------------

def test_init_creates_a_named_project_and_binds_the_directory(env):
    before = _tree(env["work"])
    code, out = _init(env)
    assert code == 0
    (registered,) = load_registry(env["store"]).projects
    assert ID_RE.fullmatch(registered.id)
    assert registered.roots == (str(env["work"].resolve()),)
    assert _service(env).read_project(registered.id).name == "work"
    assert "project: work  (new; id assigned when written)" in out
    assert f"created project work [{registered.id}]" in out
    assert "Direct child directories containing a .git marker: 2" in out
    assert "including repositories added later" in out
    assert "Restart the affected harness sessions" in out
    assert _tree(env["work"]) == before


def test_init_takes_a_name_option_and_refuses_a_bad_name_before_the_prompt(env):
    code, _ = _init(env, name="  My Work  ", yes=True)
    assert code == 0
    (registered,) = load_registry(env["store"]).projects
    assert _service(env).read_project(registered.id).name == "My Work"
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
    monkeypatch.setattr(project_context, "same_directory", lambda a, b: None)
    code, out = _init(env, yes=True)
    assert code == 2 and "could not verify" in out and _nothing_created(env)


def test_init_refuses_store_inside_target(env):
    code, out = _run(run_init, env["home"] / "99_git", name=None, yes=True,
                     root=env["work"] / "frontend" / "mem", cwd=env["work"], home=env["home"])
    assert code == 2 and "store" in out


def test_init_refuses_a_directory_inside_the_memory_store(env):
    inside = env["store"] / "projects" / "somewhere"
    inside.mkdir(parents=True)
    before = _tree(env["store"])
    code, out = _init(env, inside, yes=True)
    assert code == 2 and f"lies inside the memory store {env['store']}" in out
    assert _tree(env["store"]) == before


def test_init_refuses_missing_directory_and_already_bound_dir(env):
    code, _ = _init(env, env["work"] / "nope", yes=True)
    assert code == 2
    _bind(env, _project(env), env["work"])
    before = _tree(env["work"])
    code, out = _init(env, env["work"], yes=True)
    assert code == 2 and "already bound" in out
    assert len(load_registry(env["store"]).projects) == 1
    assert _tree(env["work"]) == before


def test_init_refuses_an_unaddressable_path_without_a_traceback(env):
    code, out = _init(env, Path("/x\x00y"), yes=True)
    assert code == 2 and "is not an existing directory" in out
    assert _nothing_created(env)


def test_init_inside_a_registered_parent_is_allowed(env):
    parent = _project(env)
    _bind(env, parent, env["work"])
    code, _ = _init(env, env["work"] / "frontend", yes=True)
    assert code == 0 and len(load_registry(env["store"]).projects) == 2
    assert resolve(env["store"], env["work"] / "frontend").project_id != parent
    assert resolve(env["store"], env["work"] / "backend").project_id == parent


def test_parent_init_lists_existing_child_roots_as_independent(env):
    child = _project(env, "frontend")
    _bind(env, child, env["work"] / "frontend")
    code, out = _init(env, yes=True)
    assert code == 0
    assert "These registered sub-projects stay independent:" in out
    assert f"{child}: {(env['work'] / 'frontend').resolve()}" in out
    assert resolve(env["store"], env["work"] / "frontend").project_id == child


def test_parent_plan_lists_a_case_alias_child_root_as_independent(env, monkeypatch):
    _case_insensitive(monkeypatch)
    child_alias = env["work"] / "FRONTEND"
    child_alias.mkdir(exist_ok=True)
    child = _project(env, "frontend")
    _bind(env, child, child_alias)
    out = io.StringIO()
    run_init(None, name=None, root=env["store"], yes=False, stdin_is_tty=False,
             input_fn=lambda _: "n", stdout=out, cwd=env["work"], home=env["home"])
    assert "These registered sub-projects stay independent:" in out.getvalue()
    assert f"{child}: {child_alias.resolve()}" in out.getvalue()


def test_init_independent_list_never_forges_a_fake_project_line(env):
    canonical_work = env["work"].resolve()
    fake_id = new_id()
    forged = f"{canonical_work}/child\n    {fake_id}: /forged"
    (env["store"] / "registry").mkdir(parents=True)
    (env["store"] / "registry" / f"{new_id()}.toml").write_text(
        'roots = ["' + forged.replace("\n", "\\n") + '"]\n', encoding="utf-8")
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


def test_init_with_an_invalid_manifest_creates_nothing(env):
    env["store"].mkdir(parents=True)
    (env["store"] / "store.toml").write_text("global_project = 'nope'\n")
    code, out = _init(env, yes=True)
    assert code == 2 and "could not complete the registry write" in out
    assert _nothing_created(env)


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
    assert code == 2 and "out of date" in out.getvalue()
    assert _tree(env["work"]) == before


def test_a_failed_bind_after_creation_names_the_empty_project_and_the_recovery(env, monkeypatch):
    from memriver_core import StorageFailure

    def boom(*a, **kw):
        raise StorageFailure()

    monkeypatch.setattr("memriver.project_commands.bind", boom)
    code, out = _init(env, yes=True)
    assert code == 2
    created = [p.stem for p in (env["store"] / "projects").iterdir()]
    assert len(created) == 1
    assert (f"refused: project {created[0]} was created but {env['work'].resolve()} could not "
            f"be bound (the store could not be written); run memriver project adopt "
            f"{created[0]} {env['work'].resolve()}") in out


# --- adopt ------------------------------------------------------------------------

def test_adopt_binds_an_existing_project_and_is_idempotent(env):
    pid = _project(env)
    code, out = _adopt(env, pid, env["work"], yes=True)
    assert code == 0 and "project: work" in out
    assert load_registry(env["store"]).projects[0].roots == (str(env["work"].resolve()),)
    code, out = _adopt(env, pid, env["work"], yes=True)
    assert code == 0 and "already bound" in out
    code, out = _adopt(env, new_id(), env["work"], yes=True)
    assert code == 2 and "no such project" in out
    code, out = _adopt(env, "x" * 300, env["work"], yes=True)
    assert code == 2 and "no such project" in out


def test_adopt_refuses_the_global_project(env):
    global_id = _service(env).ensure_global()
    code, out = _adopt(env, global_id, env["work"], yes=True)
    assert code == 2 and "refused: the global project cannot be bound to a directory" in out
    assert not (env["store"] / "registry").exists()


def test_adopt_with_an_invalid_manifest_writes_nothing(env):
    pid = _project(env)
    (env["store"] / "store.toml").write_text("global_project = 'nope'\n")
    code, out = _adopt(env, pid, env["work"], yes=True)
    assert code == 2 and "could not complete the registry write" in out
    assert not (env["store"] / "registry").exists()


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
        (env["store"] / "projects" / f"{pid}.toml").unlink()
        return "y"

    out = io.StringIO()
    code = run_adopt(pid, env["work"], root=env["store"], yes=False, stdin_is_tty=True,
                     input_fn=answer, stdout=out, cwd=env["work"], home=env["home"])
    assert code == 2 and "no such project" in out.getvalue()
    assert not (env["store"] / "registry").exists()


# --- unbind -----------------------------------------------------------------------

def test_unbind_literal_first_and_shows_next_identity(env):
    import shutil

    pid = _project(env)
    old_dir = env["home"] / "99_git" / "old-work"
    old_dir.mkdir(parents=True)
    old = str(old_dir.resolve())
    _bind(env, pid, old_dir)
    _bind(env, pid, env["work"])
    shutil.rmtree(old_dir)
    code, out = _unbind(env, pid, Path(old), yes=True)
    assert code == 0 and load_registry(env["store"]).projects[0].roots == (str(env["work"].resolve()),)
    code, out = _unbind(env, pid, env["work"], yes=True)
    assert code == 0 and "afterwards: none" in out
    code, out = _unbind(env, pid, env["work"], yes=True)
    assert code == 2 and "not bound" in out


def test_unbind_of_a_root_that_became_a_symlink_removes_the_stored_spelling(env):
    pid = _project(env)
    old = env["home"] / "99_git" / "old"
    new = env["home"] / "99_git" / "new"
    old.mkdir(parents=True)
    _bind(env, pid, old)
    old.rmdir()
    new.mkdir()
    old.symlink_to(new)
    code, _ = _unbind(env, pid, old, yes=True)
    assert code == 0 and load_registry(env["store"]).projects[0].roots == ()


def test_unbind_cannot_repair_a_cross_id_conflict_and_says_so(env):
    a_dir, b_dir = env["home"] / "99_git" / "a", env["home"] / "99_git" / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir()
    a, b = _project(env, "a"), _project(env, "b")
    _bind(env, a, a_dir)
    _bind(env, b, b_dir)
    a_dir.rmdir()
    a_dir.symlink_to(b_dir)
    code, out = _unbind(env, a, a_dir, yes=True)
    assert code == 2 and "root is already bound to another project" in out
    assert f"edit {env['store'].resolve() / 'registry' / f'{a}.toml'}" in out
    assert f"remove the root {a_dir}" in out


def test_unbind_shows_parent_as_next_identity(env):
    parent, child = _project(env, "work"), _project(env, "frontend")
    _bind(env, parent, env["work"])
    _bind(env, child, env["work"] / "frontend")
    out = io.StringIO()
    code = run_unbind(child, env["work"] / "frontend", root=env["store"], yes=True,
                      stdin_is_tty=True, input_fn=lambda _: "y", stdout=out,
                      cwd=env["work"] / "frontend", home=env["home"])
    assert code == 0 and f"afterwards: registered {parent}" in out.getvalue()


def test_unbind_cleans_up_a_registry_file_whose_project_is_missing(env):
    orphan = new_id()
    (env["store"] / "registry").mkdir(parents=True)
    (env["store"] / "registry" / f"{orphan}.toml").write_text(
        f'roots = ["{env["work"].resolve()}"]\n')
    code, out = _unbind(env, orphan, env["work"], yes=True)
    assert code == 0, out
    assert load_registry(env["store"]).projects[0].roots == ()


def test_unbind_resolves_a_relative_directory_against_the_injected_cwd(env):
    link = env["work"] / "link"
    link.symlink_to(env["work"] / "frontend", target_is_directory=True)
    pid = _project(env)
    _bind(env, pid, env["work"] / "frontend")
    code, out = _unbind(env, pid, Path("link"), yes=True)
    assert code == 0, out
    assert load_registry(env["store"]).projects[0].roots == ()


# --- explain ----------------------------------------------------------------------

def _explain(env, project_dir=None, cwd=None):
    out = io.StringIO()
    code = run_explain(root=env["store"], project_dir=project_dir, stdout=out,
                       cwd=cwd or env["work"], home=env["home"])
    return code, out.getvalue()


def test_explain_states_and_exit_codes(env):
    code, text = _explain(env)
    assert code == 0 and "state: none" in text
    # no manifest yet: global does not exist, so it is not claimed readable
    assert "reads: none\n" in text and "writes: none" in text
    assert not env["store"].exists()
    pid = _project(env)
    _bind(env, pid, env["work"])
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
    (env["store"] / "registry" / f"{pid}.toml").write_text("roots = [\n")
    code, text = _explain(env)
    assert code == 1 and "state: degraded" in text and "registry file is not valid TOML" in text


@pytest.mark.parametrize("bound", [True, False])
def test_explain_with_an_invalid_manifest_reports_no_rights_and_exits_1(env, bound):
    pid = _project(env)
    if bound:
        _bind(env, pid, env["work"])
    (env["store"] / "store.toml").write_text("global_project = 'nope'\n")
    code, text = _explain(env)
    assert code == 1
    assert "diagnostic: the memory store could not be read" in text
    assert "reads: none\n" in text and "writes: none" in text


def test_explain_of_a_registered_project_missing_from_the_store(env):
    orphan = new_id()
    (env["store"] / "registry").mkdir(parents=True)
    (env["store"] / "registry" / f"{orphan}.toml").write_text(
        f'roots = ["{env["work"].resolve()}"]\n')
    code, text = _explain(env)
    assert code == 1
    assert f"project: {orphan}" in text
    assert "diagnostic: this registered project does not exist in the store" in text
    assert "writes: none" in text


def test_explain_keeps_consecutive_spaces_in_a_registered_root(env):
    target = env["work"] / "two  spaces"
    target.mkdir()
    pid = _project(env)
    _bind(env, pid, target)
    code, text = _explain(env, project_dir=target)
    assert code == 0 and f"root: {target.resolve()}\n" in text


def test_explain_diagnostic_never_forges_a_fake_root_line(env, monkeypatch):
    forged = "/nonexistent-xyz\n  root: /forged"
    (env["store"] / "registry").mkdir(parents=True)
    (env["store"] / "registry" / f"{new_id()}.toml").write_text(
        'roots = ["' + forged.replace("\n", "\\n") + '"]\n', encoding="utf-8")
    true_lstat = project_context.os.lstat

    def fake_lstat(path, *a, **kw):
        if str(path) == forged:
            raise PermissionError(13, "denied")
        return true_lstat(path, *a, **kw)

    monkeypatch.setattr(project_context.os, "lstat", fake_lstat)
    code, text = _explain(env, cwd=env["home"])
    assert code == 1
    assert not any(line.startswith("  root: /forged") for line in text.splitlines())
    assert "/forged" in text


def test_explain_never_takes_the_lock_and_never_creates_the_store(env, monkeypatch):
    from memriver_core import bootstrap

    def forbidden(root):
        raise AssertionError("explain must not take the store lock")

    monkeypatch.setattr(bootstrap, "store_lock", forbidden)
    code, _ = _explain(env)
    assert code == 0 and not env["store"].exists()


# --- end to end through a server ------------------------------------------------------

def test_adopting_a_second_directory_makes_the_project_memories_readable_there(env):
    import asyncio

    from fastmcp import Client
    from memriver.server import build_server

    service = _service(env)
    global_id = service.ensure_global()
    pid = _project(env)
    memory = service.record(content="kept fact", type="project", sync=True, harness="t",
                            description="", ctx=AccessContext(project_id=pid,
                                                              global_project_id=global_id))
    code, _ = _adopt(env, pid, env["work"] / "frontend", yes=True)
    assert code == 0
    server = build_server(root=env["store"], project_dir=env["work"] / "frontend")

    async def probe():
        async with Client(server) as c:
            return (await c.call_tool("memory_read", {"memory_id": memory.id})).data

    assert asyncio.run(probe())["body"] == "kept fact"


def test_a_server_built_before_bind_keeps_its_identity(env):
    import asyncio

    from fastmcp import Client
    from memriver.server import build_server

    stale = build_server(root=env["store"], project_dir=env["work"])
    pid = _project(env)
    _bind(env, pid, env["work"])
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
    if scenario.startswith(("adopt", "unbind", "init-already")):
        pid = _project(env)
    if scenario.startswith(("unbind", "init-already")):
        _bind(env, pid, work)
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
