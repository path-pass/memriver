from __future__ import annotations

import io
from pathlib import Path

import pytest
from memriver import project_context
from memriver.project_commands import run_adopt, run_explain, run_init, run_unbind
from memriver.project_context import bind, load_registry, resolve
from memriver_core.models import ProjectId

PID = ProjectId("work-0123456789abcdef")


def _tree(directory: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(directory)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(directory.rglob("*"))}


def _run(fn, *args, answer="y", tty=True, yes=False, **kw):
    out = io.StringIO()
    code = fn(*args, yes=yes, stdin_is_tty=tty, input_fn=lambda _: answer, stdout=out, **kw)
    return code, out.getvalue()


def _case_insensitive(monkeypatch):
    # patched on project_context, where covers/same_directory live: the CLI reaches
    # them through that module, so the stub is what the CLI actually calls
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


def _init(env, directory=None, **kw):
    return _run(run_init, directory, root=env["store"], cwd=env["work"], home=env["home"], **kw)


def _adopt(env, pid, directory=None, **kw):
    return _run(run_adopt, pid, directory, root=env["store"], cwd=env["work"], home=env["home"], **kw)


def _unbind(env, pid, directory, **kw):
    return _run(run_unbind, pid, directory, root=env["store"], cwd=env["work"], home=env["home"], **kw)


def test_init_registers_the_directory_after_confirmation(env):
    before = _tree(env["work"])
    code, out = _init(env)
    assert code == 0
    (project,) = load_registry(env["store"]).projects
    assert project.roots == (str(env["work"].resolve()),)
    assert project.id.startswith("work-") and len(project.id) == len("work-") + 16
    assert "Direct child directories containing a .git marker: 2" in out
    assert "including repositories added later" in out
    assert "Restart the affected harness sessions" in out
    assert _tree(env["work"]) == before


def test_init_aborts_on_no_and_refuses_non_tty_without_yes(env):
    before = _tree(env["work"])
    code, _ = _init(env, answer="n")
    assert code == 1 and not (env["store"] / "projects").exists()
    code, out = _init(env, tty=False)
    assert code == 2 and "memriver project init" in out and not (env["store"] / "projects").exists()
    assert _tree(env["work"]) == before
    code, _ = _init(env, tty=False, yes=True)
    assert code == 0


@pytest.mark.parametrize("target", ["/", "{home}", "{home_parent}"])
def test_init_refuses_root_home_and_home_ancestors(env, target):
    env["home"].mkdir(parents=True, exist_ok=True)
    path = Path(target.format(home=env["home"], home_parent=env["home"].parent))
    code, out = _init(env, path, yes=True)
    assert code == 2 and "refused" in out and not (env["store"] / "projects").exists()


def test_init_refuses_case_alias_of_home_and_of_the_store_parent(env, monkeypatch):
    _case_insensitive(monkeypatch)
    # on a case-sensitive filesystem the alias spelling is a distinct real directory;
    # the stub declares it the same directory, which is exactly what APFS does
    alias_home = env["home"].parent / env["home"].name.upper()
    alias_home.mkdir(parents=True, exist_ok=True)
    code, out = _init(env, alias_home, yes=True)
    assert code == 2 and "home directory" in out
    store_parent_alias = env["store"].parent.parent / env["store"].parent.name.upper()   # HOME/, again
    store_parent_alias.mkdir(parents=True, exist_ok=True)
    code, out = _run(run_init, store_parent_alias, yes=True, root=env["store"], cwd=env["work"],
                     home=env["home"].parent / "unrelated-home")
    assert code == 2 and "store" in out


def test_init_refuses_when_containment_cannot_be_verified(env, monkeypatch):
    monkeypatch.setattr(project_context, "same_directory", lambda a, b: None)
    code, out = _init(env, yes=True)
    assert code == 2 and "could not verify" in out and not (env["store"] / "projects").exists()


def test_init_refuses_store_inside_target(env):
    code, out = _run(run_init, env["home"] / "99_git", yes=True,
                     root=env["work"] / "frontend" / "mem", cwd=env["work"], home=env["home"])
    assert code == 2 and "store" in out


def test_init_refuses_missing_directory_and_already_bound_dir(env):
    code, _ = _init(env, env["work"] / "nope", yes=True)
    assert code == 2
    bind(env["store"], PID, str(env["work"].resolve()), create=True)
    before = _tree(env["work"])
    code, out = _init(env, env["work"], yes=True)
    assert code == 2 and "already bound" in out
    assert len(load_registry(env["store"]).projects) == 1
    assert _tree(env["work"]) == before


def test_init_inside_a_registered_parent_is_allowed(env):
    bind(env["store"], PID, str(env["work"].resolve()), create=True)
    code, _ = _init(env, env["work"] / "frontend", yes=True)
    assert code == 0 and len(load_registry(env["store"]).projects) == 2
    assert resolve(env["store"], env["work"] / "frontend").project_id != PID
    assert resolve(env["store"], env["work"] / "backend").project_id == PID


def test_parent_init_lists_existing_child_roots_as_independent(env):
    bind(env["store"], ProjectId("frontend-0123456789abcdef"), str((env["work"] / "frontend").resolve()), create=True)
    code, out = _init(env, yes=True)
    assert code == 0
    assert "These registered sub-projects stay independent:" in out
    assert f"frontend-0123456789abcdef: {(env['work'] / 'frontend').resolve()}" in out
    assert resolve(env["store"], env["work"] / "frontend").project_id == "frontend-0123456789abcdef"


def test_parent_plan_lists_a_case_alias_child_root_as_independent(env, monkeypatch):
    _case_insensitive(monkeypatch)
    child_alias = env["work"] / "FRONTEND"                # alias spelling of work/frontend
    child_alias.mkdir(exist_ok=True)
    bind(env["store"], ProjectId("frontend-0123456789abcdef"), str(child_alias.resolve()), create=True)
    out = io.StringIO()
    run_init(None, root=env["store"], yes=False, stdin_is_tty=False, input_fn=lambda _: "n",
             stdout=out, cwd=env["work"], home=env["home"])          # non-tty: plan printed, then refused
    assert "These registered sub-projects stay independent:" in out.getvalue()
    assert f"frontend-0123456789abcdef: {child_alias.resolve()}" in out.getvalue()


def test_adopt_existing_unbound_project_and_idempotence(env):
    (env["store"] / "projects" / "old-abc123" / "entries").mkdir(parents=True)
    code, out = _adopt(env, "old-abc123", env["work"], yes=True)
    assert code == 0
    assert load_registry(env["store"]).projects[0].roots == (str(env["work"].resolve()),)
    code, out = _adopt(env, "old-abc123", env["work"], yes=True)
    assert code == 0 and "already bound" in out
    code, out = _adopt(env, "missing-0123456789abcdef", env["work"], yes=True)
    assert code == 2 and "no such project" in out
    code, out = _adopt(env, "x" * 300, env["work"], yes=True)
    assert code == 2 and "no such project" in out


def test_adopt_applies_the_same_target_checks(env):
    (env["store"] / "projects" / "old-abc123").mkdir(parents=True)
    code, out = _adopt(env, "old-abc123", env["home"], yes=True)
    assert code == 2 and "refused" in out


def test_adopt_of_a_project_deleted_during_confirmation_exits_2(env):
    (env["store"] / "projects" / "old-abc123").mkdir(parents=True)

    def answer(_prompt):
        import shutil
        shutil.rmtree(env["store"] / "projects" / "old-abc123")
        return "y"

    out = io.StringIO()
    code = run_adopt("old-abc123", env["work"], root=env["store"], yes=False, stdin_is_tty=True,
                     input_fn=answer, stdout=out, cwd=env["work"], home=env["home"])
    assert code == 2 and "no such project" in out.getvalue()
    assert not (env["store"] / "projects" / "old-abc123").exists()


def test_unbind_literal_first_and_shows_next_identity(env):
    import shutil

    old_dir = env["home"] / "99_git" / "old-work"
    old_dir.mkdir(parents=True)
    old = str(old_dir.resolve())
    bind(env["store"], PID, old, create=True)
    bind(env["store"], PID, str(env["work"].resolve()), create=False)
    shutil.rmtree(old_dir)                                 # moved away: the stored path no longer exists
    code, out = _unbind(env, PID, Path(old), yes=True)
    assert code == 0 and load_registry(env["store"]).projects[0].roots == (str(env["work"].resolve()),)
    code, out = _unbind(env, PID, env["work"], yes=True)
    assert code == 0 and "afterwards: none" in out
    code, out = _unbind(env, PID, env["work"], yes=True)
    assert code == 2 and "not bound" in out


def test_unbind_of_a_root_that_became_a_symlink_removes_the_stored_spelling(env):
    old = env["home"] / "99_git" / "old"
    new = env["home"] / "99_git" / "new"
    old.mkdir(parents=True)
    bind(env["store"], PID, str(old.resolve()), create=True)
    old.rmdir()
    new.mkdir()
    old.symlink_to(new)
    code, _ = _unbind(env, PID, old, yes=True)
    assert code == 0 and load_registry(env["store"]).projects[0].roots == ()


def test_unbind_cannot_repair_a_cross_id_conflict_and_says_so(env):
    a_dir, b_dir = env["home"] / "99_git" / "a", env["home"] / "99_git" / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir()
    bind(env["store"], ProjectId("a-0123456789abcdef"), str(a_dir.resolve()), create=True)
    bind(env["store"], ProjectId("b-0123456789abcdef"), str(b_dir.resolve()), create=True)
    a_dir.rmdir()
    a_dir.symlink_to(b_dir)                                  # a now aliases b: two ids, one directory
    code, out = _unbind(env, "a-0123456789abcdef", a_dir, yes=True)
    assert code == 2 and "root is already bound to another project" in out
    # the recovery names the file of the project the user asked to unbind and the
    # literal root to remove -- never the file where load_registry noticed the clash
    assert f"edit {env['store'].resolve() / 'projects' / 'a-0123456789abcdef' / 'project.toml'}" in out
    assert f"remove the root {a_dir}" in out


def test_unbind_shows_parent_as_next_identity(env):
    bind(env["store"], PID, str(env["work"].resolve()), create=True)
    child = ProjectId("frontend-0123456789abcdef")
    bind(env["store"], child, str((env["work"] / "frontend").resolve()), create=True)
    out = io.StringIO()
    code = run_unbind(child, env["work"] / "frontend", root=env["store"], yes=True, stdin_is_tty=True,
                      input_fn=lambda _: "y", stdout=out, cwd=env["work"] / "frontend", home=env["home"])
    assert code == 0 and f"afterwards: registered {PID}" in out.getvalue()


def test_explain_states_and_exit_codes(env):
    out = io.StringIO()
    assert run_explain(root=env["store"], project_dir=None, stdout=out, cwd=env["work"], home=env["home"]) == 0
    assert "state: none" in out.getvalue() and "scopes: global\n" in out.getvalue() and "writes: none" in out.getvalue()
    assert not env["store"].exists()
    bind(env["store"], PID, str(env["work"].resolve()), create=True)
    out = io.StringIO()
    assert run_explain(root=env["store"], project_dir=env["work"] / "frontend", stdout=out, cwd=env["work"], home=env["home"]) == 0
    text = out.getvalue()
    assert f"project: {PID}" in text and f"root: {env['work'].resolve()}" in text
    assert f"scopes: global, project:{PID}" in text and f"writes: project:{PID}" in text
    (env["store"] / "projects" / PID / "project.toml").write_text("roots = [\n")
    out = io.StringIO()
    assert run_explain(root=env["store"], project_dir=None, stdout=out, cwd=env["work"], home=env["home"]) == 1
    assert "state: degraded" in out.getvalue() and "project file is not valid TOML" in out.getvalue()


def test_explain_never_takes_the_lock_and_never_creates_the_store(env, monkeypatch):
    from memriver_core import bootstrap

    def forbidden(root):
        raise AssertionError("explain must not take the store lock")

    monkeypatch.setattr(bootstrap, "store_lock", forbidden)
    out = io.StringIO()
    assert run_explain(root=env["store"], project_dir=None, stdout=out, cwd=env["work"], home=env["home"]) == 0
    assert not env["store"].exists()


def test_target_deleted_while_the_prompt_is_open_is_refused(env):
    target = env["work"] / "ephemeral"
    target.mkdir()

    def answer(_prompt):
        target.rmdir()
        return "y"

    out = io.StringIO()
    code = run_init(target, root=env["store"], yes=False, stdin_is_tty=True, input_fn=answer,
                    stdout=out, cwd=env["work"], home=env["home"])
    assert code == 2 and "out of date" in out.getvalue()
    assert not (env["store"] / "projects").exists()


def test_store_repointed_into_target_while_the_prompt_is_open_is_refused(env):
    before = _tree(env["work"])

    def answer(_prompt):
        env["store"].parent.mkdir(parents=True, exist_ok=True)
        env["store"].symlink_to(env["work"])          # the store path now points into the target
        return "y"

    out = io.StringIO()
    code = run_init(None, root=env["store"], yes=False, stdin_is_tty=True, input_fn=answer,
                    stdout=out, cwd=env["work"], home=env["home"])
    assert code == 2 and "out of date" in out.getvalue()
    assert _tree(env["work"]) == before                  # no .lock, no projects/ inside the target


def test_store_failure_is_a_fixed_line_and_exit_2(env, monkeypatch):
    from memriver_core import StorageFailure

    def boom(*a, **kw):
        raise StorageFailure()

    monkeypatch.setattr("memriver.project_commands.bind", boom)
    code, out = _init(env, yes=True)
    assert code == 2 and "could not complete the registry write" in out


def test_adopt_makes_old_entries_readable_through_a_server(env):
    import asyncio

    from fastmcp import Client
    from memriver.server import build_server
    from memriver_core.models import Memory, Scope
    from memriver_core.repository.filesystem.markdown_codec import encode

    # encode is test-only: it seeds a raw entry file the way an older memriver would

    old = ProjectId("old-abc123")
    entries = env["store"] / "projects" / old / "entries"
    entries.mkdir(parents=True)
    m = Memory.new(body="old body", type="project", scope=Scope.project(old), source={"harness": "t", "method": "agent"}, id="old")
    (entries / "old.md").write_text(encode(m), encoding="utf-8")
    before = (entries / "old.md").read_bytes()
    code, _ = _adopt(env, old, env["work"], yes=True)
    assert code == 0
    server = build_server(root=env["store"], project_dir=env["work"] / "frontend")

    async def probe():
        async with Client(server) as c:
            return (await c.call_tool("memory_read", {"entry_id": "old"})).data

    assert asyncio.run(probe())["body"] == "old body"
    assert (entries / "old.md").read_bytes() == before


def test_a_server_built_before_bind_keeps_its_identity(env):
    import asyncio

    from fastmcp import Client
    from memriver.server import build_server

    stale = build_server(root=env["store"], project_dir=env["work"])
    bind(env["store"], PID, str(env["work"].resolve()), create=True)
    fresh = build_server(root=env["store"], project_dir=env["work"])

    async def header(server):
        async with Client(server) as c:
            return (await c.call_tool("memory_index", {})).data.splitlines()[0]

    assert asyncio.run(header(stale)).startswith("project: none")
    assert asyncio.run(header(fresh)).startswith(f"project: {PID}")


@pytest.mark.parametrize("scenario", [
    "init-ok", "init-no", "init-non-tty", "init-refused-home", "init-already-bound",
    "adopt-ok", "adopt-missing-id", "adopt-refused-home",
    "unbind-ok", "unbind-not-bound", "explain",
])
def test_no_command_or_refusal_touches_the_target(env, scenario):
    work = env["work"]
    if scenario.startswith(("adopt", "unbind")):
        bind(env["store"], PID, str(work.resolve()), create=True)
    before = _tree(work)
    out = io.StringIO()
    common = {"root": env["store"], "stdin_is_tty": True, "input_fn": lambda _: "y",
              "stdout": out, "cwd": work, "home": env["home"]}
    match scenario:
        case "init-ok": run_init(None, yes=True, **common)
        case "init-no": run_init(None, yes=False, **{**common, "input_fn": lambda _: "n"})
        case "init-non-tty": run_init(None, yes=False, **{**common, "stdin_is_tty": False})
        case "init-refused-home": run_init(env["home"], yes=True, **common)
        case "init-already-bound":
            bind(env["store"], PID, str(work.resolve()), create=True)
            run_init(work, yes=True, **common)
        case "adopt-ok": run_adopt(PID, work / "frontend", yes=True, **common)
        case "adopt-missing-id": run_adopt("nope-0123456789abcdef", work, yes=True, **common)
        case "adopt-refused-home": run_adopt(PID, env["home"], yes=True, **common)
        case "unbind-ok": run_unbind(PID, work, yes=True, **common)
        case "unbind-not-bound": run_unbind(PID, work / "backend", yes=True, **common)
        case "explain": run_explain(root=env["store"], project_dir=None, stdout=out, cwd=work, home=env["home"])
    assert _tree(work) == before


def test_init_refuses_a_directory_inside_the_memory_store(env):
    # the store holds the registry: a directory inside it is memriver's own
    # bookkeeping, never a project the user works in
    inside = env["store"] / "projects" / "somewhere"
    inside.mkdir(parents=True)
    before = _tree(env["store"])
    code, out = _init(env, inside, yes=True)
    assert code == 2 and f"lies inside the memory store {env['store']}" in out
    assert _tree(env["store"]) == before


def test_init_refuses_an_unaddressable_path_without_a_traceback(env):
    # a NUL in the path makes resolve() raise ValueError, not OSError
    code, out = _init(env, Path("/x\x00y"), yes=True)
    assert code == 2 and "is not an existing directory" in out
    assert not (env["store"] / "projects").exists()


def test_unbind_resolves_a_relative_directory_against_the_injected_cwd(env):
    # the fallback lookup used the process cwd, so a relative path that only
    # exists under the injected cwd never found the root it names
    link = env["work"] / "link"
    link.symlink_to(env["work"] / "frontend", target_is_directory=True)
    bind(env["store"], PID, str((env["work"] / "frontend").resolve()), create=True)
    code, out = _unbind(env, PID, Path("link"), yes=True)
    assert code == 0, out
    assert load_registry(env["store"]).projects[0].roots == ()
