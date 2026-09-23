from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest
from memriver.views import run_delete, run_export, run_list, run_search, run_show
from memriver_core.bootstrap import build_service
from memriver_core.settings import Settings


@pytest.fixture
def world(tmp_path):
    store, work, home = tmp_path / "mem", tmp_path / "work", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    service = build_service(Settings(root=store), root=store, home=home)
    service.ensure_global()
    project = service.init_project("demo", service.plan_root(str(work)))
    session = service.open_session(str(work))
    memory = service.record(content="line one\nline two", type="project", sync=True,
                            harness="t", description="the cue", read_write_set=session.read_write_set)
    return {"store": store, "work": work, "home": home, "service": service,
            "project": project, "memory": memory, "session": session}


def _out(fn, *args, **kwargs) -> tuple[int, str]:
    out = io.StringIO()
    code = fn(*args, stdout=out, **kwargs)
    return code, out.getvalue()


def test_list_shows_every_project_and_its_memories(world):
    code, out = _out(run_list, root=world["store"], project_id=None, home=world["home"])
    assert code == 0
    assert f"{world['project'].id}  demo  ({world['work'].resolve()})" in out
    assert f"  {world['memory'].id}  [project]  {world['memory'].updated[:10]}  the cue" in out
    assert "global" in out and "(no memories)" in out


def test_show_prints_fields_then_the_body_with_its_newlines(world):
    code, out = _out(run_show, world["memory"].id, root=world["store"], deleted=False,
                     home=world["home"])
    assert code == 0
    assert "version: 1" in out and "deleted:" not in out
    assert out.endswith("---\nline one\nline two\n")


def test_show_neutralises_terminal_escapes_in_the_body_but_keeps_newlines(world):
    service, session = world["service"], world["session"]
    memory = service.record(content="a\x1b[2Jb\nc", type="project", sync=True, harness="t",
                            description="", read_write_set=session.read_write_set)
    _, out = _out(run_show, memory.id, root=world["store"], deleted=False, home=world["home"])
    assert "\x1b" not in out and "a [2Jb\nc" in out


def test_show_of_a_soft_deleted_memory_needs_the_flag(world):
    world["service"].delete(world["memory"].id, world["session"].read_write_set,
                            expected_version=1)
    code, out = _out(run_show, world["memory"].id, root=world["store"], deleted=False,
                     home=world["home"])
    assert code == 2 and "no such memory" in out
    code, out = _out(run_show, world["memory"].id, root=world["store"], deleted=True,
                     home=world["home"])
    assert code == 0 and "deleted: " in out


def test_search_finds_across_projects_and_by_project(world, tmp_path):
    service = world["service"]
    other_work = tmp_path / "other"
    other_work.mkdir()
    other_project = service.init_project("other", service.plan_root(str(other_work)))
    other_session = service.open_session(str(other_work))
    other_memory = service.record(content="another line entirely", type="project", sync=True,
                                  harness="t", description="",
                                  read_write_set=other_session.read_write_set)

    code, out = _out(run_search, "LINE", root=world["store"], project_id=None, limit=None,
                     home=world["home"])
    assert code == 0
    assert world["memory"].id in out and other_memory.id in out

    _, out = _out(run_search, "LINE", root=world["store"], project_id=world["project"].id,
                  limit=None, home=world["home"])
    assert world["memory"].id in out and other_memory.id not in out

    _, out = _out(run_search, "LINE", root=world["store"], project_id=other_project.id,
                  limit=None, home=world["home"])
    assert other_memory.id in out and world["memory"].id not in out

    _, out = _out(run_search, "LINE", root=world["store"], project_id=None, limit=1,
                  home=world["home"])
    assert len(out.splitlines()) == 1

    _, out = _out(run_search, "nothing-like-this", root=world["store"], project_id=None,
                  limit=None, home=world["home"])
    assert out == "(no matches)\n"


def test_export_writes_a_private_snapshot_and_never_reads_it_back(world, tmp_path):
    target = tmp_path / "snap"
    code, out = _out(run_export, target, root=world["store"], home=world["home"], cwd=tmp_path)
    assert code == 0 and out == f"exported 1 memories to {target}\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700
    path = target / world["project"].id / f"{world['memory'].id}.md"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    raw = path.read_bytes()
    header, body = raw.split(b"---\n")[1], raw.split(b"---\n")[2]
    fields = dict(line.split(": ", 1) for line in header.decode("utf-8").strip().splitlines())
    assert json.loads(fields["version"]) == 1 and "deleted_at" not in fields
    assert json.loads(fields["source_harness"]) == "t"
    assert json.loads(fields["source_method"]) == "agent"
    assert "source" not in fields
    # byte-for-byte (spec section 10.4): no trailing newline appended after the body
    assert body == world["memory"].body.encode("utf-8")
    assert world["project"].id in (target / "projects.md").read_text()


def test_export_into_a_missing_parent_is_a_fixed_refusal(world, tmp_path):
    code, out = _out(run_export, tmp_path / "missing" / "snap", root=world["store"],
                     home=world["home"], cwd=tmp_path)
    assert code == 2 and "could not be created" in out


def test_export_losing_the_creation_race_is_a_refusal(world, tmp_path, monkeypatch):
    target = tmp_path / "snap"
    real_mkdir = Path.mkdir

    def racing_mkdir(self, *args, **kwargs):
        if self == target:
            real_mkdir(self)                       # a peer created it first
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    code, out = _out(run_export, target, root=world["store"], home=world["home"], cwd=tmp_path)
    assert code == 2 and "already exists" in out


def test_a_failure_mid_export_says_the_snapshot_is_partial(world, tmp_path, monkeypatch):
    from memriver import views

    real_write = views._write_private

    def failing(dir_fd, name, text):
        if name != "projects.md":
            raise OSError(28, "No space left on device")
        real_write(dir_fd, name, text)

    monkeypatch.setattr(views, "_write_private", failing)
    code, out = _out(run_export, tmp_path / "snap", root=world["store"], home=world["home"],
                     cwd=tmp_path)
    assert code == 2
    assert "stopped after 0 memories (No space left on device)" in out
    assert "partial snapshot" in out and "exported" not in out


def test_export_refuses_when_the_target_becomes_a_symlink_after_its_own_mkdir(
        world, tmp_path, monkeypatch):
    """A target swapped for a symlink right after `target.mkdir()` must not be followed."""
    target = tmp_path / "snap"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_mkdir = Path.mkdir

    def swapping_mkdir(self, *args, **kwargs):
        real_mkdir(self, *args, **kwargs)
        if self == target:
            self.rmdir()
            self.symlink_to(outside)

    monkeypatch.setattr(Path, "mkdir", swapping_mkdir)
    code, out = _out(run_export, target, root=world["store"], home=world["home"], cwd=tmp_path)
    assert code == 2 and "stopped after 0 memories" in out and "partial snapshot" in out
    assert list(outside.iterdir()) == []


def test_export_refuses_when_a_project_folder_becomes_a_symlink_after_its_mkdir(
        world, tmp_path, monkeypatch):
    """A project folder swapped for a symlink right after its own mkdir must not be followed."""
    target = tmp_path / "snap"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_mkdir = os.mkdir

    def swapping_mkdir(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is not None and path == world["project"].id:
            planted = target / path
            planted.rmdir()
            planted.symlink_to(outside)

    monkeypatch.setattr(os, "mkdir", swapping_mkdir)
    code, out = _out(run_export, target, root=world["store"], home=world["home"], cwd=tmp_path)
    assert code == 2 and "stopped after 0 memories" in out
    assert list(outside.iterdir()) == []


def test_write_private_refuses_a_name_that_would_leave_the_directory(tmp_path):
    from memriver import views

    inner = tmp_path / "inner"
    inner.mkdir()
    fd = os.open(inner, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            views._write_private(fd, "../x", "t")
    finally:
        os.close(fd)
    assert not (tmp_path / "x").exists()
    assert list(inner.iterdir()) == []


def test_list_and_search_neutralise_control_characters_in_stored_fields(world):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("UPDATE memories SET updated = ?, description = ? WHERE id = ?",
                     ("\x1b[2J2026-09-2", "cue\x1b[31m", world["memory"].id))
    for fn, args in ((run_list, ()), (run_search, ("line",))):
        kwargs = {"root": world["store"], "project_id": None, "home": world["home"]}
        if fn is run_search:
            kwargs["limit"] = None
        _, out = _out(fn, *args, **kwargs)
        assert "\x1b" not in out


def test_export_refuses_an_existing_directory(world, tmp_path):
    (tmp_path / "snap").mkdir()
    code, out = _out(run_export, tmp_path / "snap", root=world["store"], home=world["home"],
                     cwd=tmp_path)
    assert code == 2 and "already exists" in out


def _delete(world, *, version, hard=False, answer="y", cwd=None):
    out = io.StringIO()
    code = run_delete(world["memory"].id, version=version, hard=hard, yes=False,
                      root=world["store"], stdin_is_tty=True, input_fn=lambda _: answer,
                      stdout=out, cwd=cwd or world["work"], home=world["home"])
    return code, out.getvalue()


def test_delete_is_soft_by_default_and_hard_purges_a_soft_deleted_memory(world):
    code, out = _delete(world, version=1)
    assert code == 0 and out.endswith(f"deleted {world['memory'].id}\n")
    code, out = _delete(world, version=2, hard=True)
    assert code == 0 and out.endswith(f"purged {world['memory'].id}\n")
    code, out = _out(run_show, world["memory"].id, root=world["store"], deleted=True,
                     home=world["home"])
    assert code == 2


def test_delete_needs_the_current_version(world):
    code, out = _delete(world, version=5)
    assert code == 2 and "changed since version 5" in out


def test_delete_from_outside_the_project_finds_nothing(world, tmp_path):
    code, out = _delete(world, version=1, cwd=tmp_path)
    assert code == 2 and "no such memory" in out


def test_delete_declined_changes_nothing(world):
    code, _ = _delete(world, version=1, answer="n")
    assert code == 1
    assert world["service"].show(world["memory"].id).version == 1


def _never_called(_):
    raise AssertionError("input_fn must not be called")


def _plant_global_memory(world) -> str:
    """A memory row inserted straight into the global project, behind the service's back."""
    import sqlite3
    from contextlib import closing

    from memriver_core.models import Memory

    global_id = world["service"].global_project_id()
    memory = Memory.new(body="a global note", type="project", project_id=global_id,
                        source={"harness": "t", "method": "agent"})
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute(
            "INSERT INTO memories (id, project_id, type, source_harness, source_method, "
            "trust, sync, description, body, created, updated, version, deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory.id, memory.project_id, memory.type, memory.source["harness"],
             memory.source["method"], memory.trust, int(memory.sync), memory.description,
             memory.body, memory.created, memory.updated, memory.version, memory.deleted_at))
    return memory.id


def test_delete_refuses_a_global_memory_without_a_plan_line_or_prompt(world):
    memory_id = _plant_global_memory(world)
    out = io.StringIO()
    code = run_delete(memory_id, version=1, hard=False, yes=False, root=world["store"],
                      stdin_is_tty=True, input_fn=_never_called, stdout=out,
                      cwd=world["work"], home=world["home"])
    assert code == 2
    assert out.getvalue() == "refused: global memories cannot be deleted here\n"


def test_delete_without_yes_over_a_non_tty_is_refused(world):
    out = io.StringIO()
    code = run_delete(world["memory"].id, version=1, hard=False, yes=False, root=world["store"],
                      stdin_is_tty=False, input_fn=_never_called, stdout=out,
                      cwd=world["work"], home=world["home"])
    assert code == 2
    assert "stdin is not a terminal" in out.getvalue()


def test_delete_with_yes_skips_the_prompt(world):
    out = io.StringIO()
    code = run_delete(world["memory"].id, version=1, hard=False, yes=True, root=world["store"],
                      stdin_is_tty=False, input_fn=_never_called, stdout=out,
                      cwd=world["work"], home=world["home"])
    assert code == 0
    assert out.getvalue().endswith(f"deleted {world['memory'].id}\n")


def test_delete_prompt_eof_is_treated_as_declined(world):
    def _eof(_):
        raise EOFError

    out = io.StringIO()
    code = run_delete(world["memory"].id, version=1, hard=False, yes=False, root=world["store"],
                      stdin_is_tty=True, input_fn=_eof, stdout=out, cwd=world["work"],
                      home=world["home"])
    assert code == 1
    assert out.getvalue().endswith("aborted; nothing was changed\n")


def test_list_reports_a_fixed_sentence_when_the_service_cannot_be_built(world, monkeypatch):
    import memriver_core.bootstrap as bootstrap_module
    from memriver import views

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(bootstrap_module, "build_service", _boom)
    code, out = _out(run_list, root=world["store"], project_id=None, home=world["home"])
    assert code == 2
    assert out == views.STORE_UNREADABLE + "\n"


def test_show_reports_a_fixed_sentence_when_the_service_cannot_be_built(world, monkeypatch):
    import memriver_core.bootstrap as bootstrap_module
    from memriver import views

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(bootstrap_module, "build_service", _boom)
    code, out = _out(run_show, world["memory"].id, root=world["store"], deleted=False,
                     home=world["home"])
    assert code == 2
    assert out == views.STORE_UNREADABLE + "\n"


def test_export_skips_a_soft_deleted_memory(world, tmp_path):
    world["service"].delete(world["memory"].id, world["session"].read_write_set,
                            expected_version=1)
    target = tmp_path / "snap"
    code, out = _out(run_export, target, root=world["store"], home=world["home"], cwd=tmp_path)
    assert code == 0 and out == f"exported 0 memories to {target}\n"
    assert not (target / world["project"].id).exists()


def test_show_neutralises_an_escape_planted_in_the_description(world):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("UPDATE memories SET description = ? WHERE id = ?",
                     ("cue\x1b[31m", world["memory"].id))
    code, out = _out(run_show, world["memory"].id, root=world["store"], deleted=False,
                     home=world["home"])
    assert code == 0
    assert "\x1b" not in out
    assert "description: cue [31m" in out


def test_list_with_an_unknown_project_id_is_refused(world):
    code, out = _out(run_list, root=world["store"], project_id="zzzzzzzzzz", home=world["home"])
    assert code == 2 and "no such project" in out
