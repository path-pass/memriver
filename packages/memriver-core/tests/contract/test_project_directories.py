"""ProjectStore directory rules: plan, bind, unbind, resolve (spec §5.3)."""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from memriver_core.models import Project, new_id
from memriver_core.models.errors import BindingRefused, IdCollision, ProjectNotFound
from memriver_core.repository import directories
from memriver_core.repository.sqlite import SqliteProjectStore


def _sql(store: Path, statement: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        return conn.execute(statement, params).fetchall()


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = tmp_path / "store"
    work = tmp_path / "work"
    work.mkdir()
    project_store = SqliteProjectStore(store, home=home, busy_timeout_ms=2000)
    return {"tmp": tmp_path, "home": home, "store": store, "work": work,
            "project_store": project_store}


def _init(project_store, directory, name="demo") -> Project:
    project = Project.new(name, max_chars=120)
    project_store.create(project, project_store.plan_root(str(directory), None))
    return project


def _refused(reason, call, *args):
    with pytest.raises(BindingRefused) as excinfo:
        call(*args)
    assert excinfo.value.reason == reason
    return excinfo.value


def test_init_creates_the_project_and_its_directory_together(env):
    project = _init(env["project_store"], env["work"])
    read = env["project_store"].read(project.id)
    assert (read.name, read.root) == ("demo", str(env["work"].resolve()))


def test_a_refused_plan_leaves_no_project(env):
    plan = env["project_store"].plan_root(str(env["work"]), None)
    env["work"].rmdir()
    _refused("plan-changed", env["project_store"].create, Project.new("x", max_chars=120), plan)
    assert env["project_store"].list_projects() == []
    assert not env["store"].exists()


def test_create_never_reuses_an_id(env):
    project = _init(env["project_store"], env["work"])
    other = env["tmp"] / "other"
    other.mkdir()
    with pytest.raises(IdCollision):
        env["project_store"].create(Project(id=project.id, name="again"),
                                    env["project_store"].plan_root(str(other), None))


@pytest.mark.parametrize("target", ["home", "home-parent", "root"])
def test_plan_refuses_home_its_ancestors_and_the_filesystem_root(env, target):
    directory = {"home": env["home"], "home-parent": env["home"].parent, "root": Path("/")}[target]
    _refused("covers-home", env["project_store"].plan_root, str(directory), None)


def test_plan_refuses_the_store_inside_the_directory_and_the_directory_inside_the_store(env):
    env["store"].mkdir()
    (env["store"] / "inner").mkdir()
    outer = env["tmp"] / "outer"
    (outer / "store").mkdir(parents=True)
    nested_store = SqliteProjectStore(outer / "store", home=env["home"], busy_timeout_ms=2000)
    _refused("covers-store", nested_store.plan_root, str(outer), None)
    _refused("inside-store", env["project_store"].plan_root, str(env["store"] / "inner"), None)


def test_plan_refuses_a_missing_directory_and_a_file(env):
    (env["tmp"] / "file").write_text("x")
    _refused("not-a-directory", env["project_store"].plan_root, str(env["tmp"] / "nope"), None)
    _refused("not-a-directory", env["project_store"].plan_root, str(env["tmp"] / "file"), None)


def test_a_directory_bound_elsewhere_is_refused_and_names_the_owner(env):
    owner = _init(env["project_store"], env["work"])
    refusal = _refused("bound-elsewhere", env["project_store"].plan_root, str(env["work"]), None)
    assert refusal.project_id == owner.id


def test_a_case_alias_of_a_bound_directory_is_bound_elsewhere(env, monkeypatch):
    _init(env["project_store"], env["work"])
    real = directories.same_directory
    monkeypatch.setattr(directories, "same_directory",
                        lambda a, b: True if a.lower() == b.lower() and a != b else real(a, b))
    alias = env["tmp"] / "WORK"
    alias.mkdir(exist_ok=True)          # on a case-insensitive disk it is already "work"
    _refused("bound-elsewhere", env["project_store"].plan_root, str(alias), None)


def test_an_uncheckable_comparison_is_unverifiable_never_a_non_match(env, monkeypatch):
    _init(env["project_store"], env["work"])
    other = env["tmp"] / "other"
    other.mkdir()
    bound_root = str(env["work"].resolve())
    real = directories.same_directory
    monkeypatch.setattr(directories, "same_directory",
                        lambda a, b: None if bound_root in (a, b) else real(a, b))
    _refused("unverifiable", env["project_store"].plan_root, str(other), None)


def test_plan_lists_bound_directories_under_the_target_as_nested(env):
    child = env["work"] / "child"
    child.mkdir()
    inner = _init(env["project_store"], child, "inner")
    plan = env["project_store"].plan_root(str(env["work"]), None)
    assert [p.id for p in plan.nested] == [inner.id]


def test_adopt_binds_an_unbound_project_and_is_idempotent(env):
    project = _init(env["project_store"], env["work"])
    store = env["project_store"]
    plan = store.plan_root(str(env["work"]), project.id)
    assert plan.already_bound
    store.bind(project.id, plan)                          # same directory: nothing to do
    unbind_plan, _ = store.plan_unbind(project.id, str(env["work"].resolve()), str(env["tmp"]))
    store.unbind(unbind_plan)
    assert store.read(project.id).root is None
    store.bind(project.id, store.plan_root(str(env["work"]), project.id))
    assert store.read(project.id).root == str(env["work"].resolve())


def test_adopting_an_alias_of_the_held_directory_is_a_no_op(env, monkeypatch):
    project = _init(env["project_store"], env["work"])
    store = env["project_store"]
    real = directories.same_directory
    monkeypatch.setattr(directories, "same_directory",
                        lambda a, b: True if a.lower() == b.lower() and a != b else real(a, b))
    alias = env["tmp"] / "WORK"
    alias.mkdir(exist_ok=True)          # on a case-insensitive disk it is already "work"
    plan = store.plan_root(str(alias), project.id)
    assert plan.already_bound
    before = (env["store"] / "memriver.db").read_bytes()
    store.bind(project.id, plan)
    assert store.read(project.id).root == str(env["work"].resolve())
    assert (env["store"] / "memriver.db").read_bytes() == before


def test_bind_decides_on_the_current_row_not_on_the_plan(env):
    project = _init(env["project_store"], env["work"])
    store = env["project_store"]
    stale = store.plan_root(str(env["work"]), project.id)          # already_bound=True
    unbind_plan, _ = store.plan_unbind(project.id, str(env["work"].resolve()), str(env["tmp"]))
    store.unbind(unbind_plan)                                      # a peer unbinds meanwhile
    store.bind(project.id, stale)
    assert store.read(project.id).root == str(env["work"].resolve())


def test_a_project_with_another_directory_must_unbind_first(env):
    project = _init(env["project_store"], env["work"])
    other = env["tmp"] / "other"
    other.mkdir()
    _refused("has-directory", env["project_store"].plan_root, str(other), project.id)


def test_adopt_refuses_global_and_unknown_projects(env):
    global_id = env["project_store"].ensure_global()
    _refused("is-global", env["project_store"].plan_root, str(env["work"]), global_id)
    _refused("no-such-project", env["project_store"].plan_root, str(env["work"]), new_id())
    _refused("no-such-project", env["project_store"].plan_root, str(env["work"]), "not-an-id")


def test_create_refuses_a_target_a_peer_bound_after_planning(env):
    store = env["project_store"]
    other = env["tmp"] / "other"
    other.mkdir()
    plan = store.plan_root(str(other), None)
    peer = _init(store, other, "peer")
    refusal = _refused("bound-elsewhere", store.create, Project.new("x", max_chars=120), plan)
    assert refusal.project_id == peer.id
    assert [p.id for p in store.list_projects()] == [peer.id]


def test_bind_refuses_a_project_a_peer_bound_elsewhere_after_planning(env):
    project = _init(env["project_store"], env["work"])
    store = env["project_store"]
    unbind_plan, _ = store.plan_unbind(project.id, str(env["work"].resolve()), str(env["tmp"]))
    store.unbind(unbind_plan)
    other = env["tmp"] / "other"
    other.mkdir()
    plan = store.plan_root(str(other), project.id)
    third = env["tmp"] / "third"
    third.mkdir()
    store.bind(project.id, store.plan_root(str(third), project.id))     # the peer
    _refused("has-directory", store.bind, project.id, plan)
    assert store.read(project.id).root == str(third.resolve())


def test_bind_and_unbind_against_a_deleted_store_create_nothing(env):
    project = _init(env["project_store"], env["work"])
    store = env["project_store"]
    unbind_plan, _ = store.plan_unbind(project.id, str(env["work"].resolve()), str(env["tmp"]))
    store.unbind(unbind_plan)
    bind_plan = store.plan_root(str(env["work"]), project.id)
    shutil.rmtree(env["store"])
    _refused("binding-changed", store.unbind, unbind_plan)
    assert not env["store"].exists()
    _refused("no-such-project", store.bind, project.id, bind_plan)
    assert not env["store"].exists()


def test_a_confirmed_target_re_pointed_through_a_symlink_is_refused(env):
    store = env["project_store"]
    plan = store.plan_root(str(env["work"]), None)
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()
    env["work"].rmdir()
    env["work"].symlink_to(elsewhere)
    _refused("plan-changed", store.create, Project.new("x", max_chars=120), plan)
    assert store.list_projects() == []


def test_a_store_redirected_to_an_empty_place_during_the_prompt_gets_no_database(env):
    link = env["tmp"] / "link"
    real = env["tmp"] / "real"
    real.mkdir()
    link.symlink_to(real)
    store = SqliteProjectStore(link, home=env["home"], busy_timeout_ms=2000)
    plan = store.plan_root(str(env["work"]), None)
    other = env["tmp"] / "other-store"
    other.mkdir()
    link.unlink()
    link.symlink_to(other)
    _refused("plan-changed", store.create, Project.new("x", max_chars=120), plan)
    assert not (other / "memriver.db").exists()


def test_unbind_needs_the_exact_confirmed_pair(env):
    project = _init(env["project_store"], env["work"])
    store = env["project_store"]
    plan, preview = store.plan_unbind(project.id, str(env["work"].resolve()), str(env["work"]))
    assert preview.state == "none"                   # the preview ignores this binding
    store.unbind(plan)
    _refused("binding-changed", store.unbind, plan)  # already gone
    store.bind(project.id, store.plan_root(str(env["work"]), project.id))
    store.unbind(plan)                               # unbound then rebound: same pair, allowed


def test_unbind_of_a_directory_deleted_from_disk_still_works(env):
    project = _init(env["project_store"], env["work"])
    store = env["project_store"]
    root = str(env["work"].resolve())
    env["work"].rmdir()
    plan, _ = store.plan_unbind(project.id, root, str(env["tmp"]))
    assert plan.root == root
    store.unbind(plan)
    assert store.read(project.id).root is None


def test_unbind_matches_the_stored_root_by_its_realpath_too(env):
    project = _init(env["project_store"], env["work"])
    link = env["tmp"] / "via"
    link.symlink_to(env["work"])
    plan, _ = env["project_store"].plan_unbind(project.id, str(link), str(env["tmp"]))
    assert plan.root == str(env["work"].resolve())


def test_unbind_plans_refuse_unknown_projects_and_foreign_roots(env):
    project = _init(env["project_store"], env["work"])
    _refused("no-such-project", env["project_store"].plan_unbind, new_id(), "/x", "/")
    _refused("binding-changed", env["project_store"].plan_unbind, project.id,
             str(env["tmp"] / "other"), "/")


def test_an_unbind_plan_catches_a_store_redirected_during_the_prompt(env):
    link = env["tmp"] / "link"
    real = env["tmp"] / "real"
    real.mkdir()
    link.symlink_to(real)
    store = SqliteProjectStore(link, home=env["home"], busy_timeout_ms=2000)
    project = _init(store, env["work"])
    plan, _ = store.plan_unbind(project.id, str(env["work"].resolve()), str(env["tmp"]))
    copy = env["tmp"] / "copy"
    copy.mkdir()
    (copy / "memriver.db").write_bytes((real / "memriver.db").read_bytes())
    link.unlink()
    link.symlink_to(copy)
    _refused("plan-changed", store.unbind, plan)
    assert _sql(copy, "SELECT root FROM projects WHERE id = ?", project.id) == \
        [(str(env["work"].resolve()),)]


def test_resolve_finds_the_nearest_bound_directory_and_reports_its_project(env):
    project = _init(env["project_store"], env["work"])
    (env["work"] / "src").mkdir()
    resolution = env["project_store"].resolve(str(env["work"] / "src"))
    assert resolution.state == "registered"
    assert (resolution.project.id, resolution.project.root) == \
        (project.id, str(env["work"].resolve()))
    assert env["project_store"].resolve(str(env["tmp"])).state == "none"


def test_resolve_of_a_missing_store_is_none_and_creates_nothing(env):
    assert env["project_store"].resolve(str(env["work"])).state == "none"
    assert not env["store"].exists()


def test_resolve_degrades_on_a_re_pointed_root(env):
    _init(env["project_store"], env["work"])
    moved = env["tmp"] / "moved"
    env["work"].rename(moved)
    env["work"].symlink_to(moved)
    resolution = env["project_store"].resolve(str(moved))
    assert resolution.state == "degraded"
    assert resolution.diagnostic.endswith("registered root is no longer a canonical path")


def test_list_projects_puts_global_last(env):
    global_id = env["project_store"].ensure_global()
    project = _init(env["project_store"], env["work"])
    assert [p.id for p in env["project_store"].list_projects()] == [project.id, global_id]


def test_ensure_global_racing_from_nothing_returns_one_id(env):
    import threading

    ids: list[str] = []
    barrier = threading.Barrier(2)

    def call() -> None:
        store = SqliteProjectStore(env["store"], home=env["home"], busy_timeout_ms=5000)
        barrier.wait()
        ids.append(store.ensure_global())

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ids) == 2 and ids[0] == ids[1]
    assert _sql(env["store"], "SELECT count(*) FROM projects WHERE is_global = 1") == [(1,)]


def test_an_invalid_project_row_is_storage_failure_not_absence(env):
    from memriver_core.models.errors import StorageFailure

    env["project_store"].ensure_global()
    bad = new_id()
    _sql(env["store"], "INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, NULL, 0)",
         bad, "two\nlines")
    with pytest.raises(StorageFailure):
        env["project_store"].read(bad)
    with pytest.raises(ProjectNotFound):
        env["project_store"].read(new_id())


def test_an_invalid_bound_row_fails_resolution_and_planning_whole(env):
    # a partly read set of bindings could hide the nearer project: no skipping
    from memriver_core.models.errors import StorageFailure

    env["project_store"].ensure_global()
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()
    _sql(env["store"], "INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, ?, 0)",
         new_id(), "two\nlines", str(elsewhere.resolve()))
    with pytest.raises(StorageFailure):
        env["project_store"].resolve(str(env["work"]))
    with pytest.raises(StorageFailure):
        env["project_store"].plan_root(str(env["work"]), None)


def test_list_projects_skips_an_invalid_row_that_read_still_fails_on(env):
    from memriver_core.models.errors import StorageFailure

    global_id = env["project_store"].ensure_global()
    bad = new_id()
    _sql(env["store"], "INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, NULL, 0)",
         bad, "two\nlines")
    assert [p.id for p in env["project_store"].list_projects()] == [global_id]
    with pytest.raises(StorageFailure):
        env["project_store"].read(bad)


def test_the_database_file_is_private(env):
    env["project_store"].ensure_global()
    assert oct(os.stat(env["store"] / "memriver.db").st_mode & 0o777) == "0o600"
