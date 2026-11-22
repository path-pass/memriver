"""The store comparison itself: what it must see and what it must leave alone.

Both sides are the Python implementation here; the comparison is what is tested.
"""

from __future__ import annotations

import itertools
import json
import shutil
import sqlite3
from datetime import UTC, datetime

import pytest

from . import harness
from .harness import (
    PY,
    SEEDS,
    Case,
    Completed,
    NotComparable,
    load_seed,
    run,
    seed_store,
    snapshot,
)

CREATE = """change = services.memory.apply(
    [Create(project_id({project!r}), "project", {description!r}, {body!r})], changed_by="compat")
print(json.dumps({{"change_id": change.change_id, "id": change.steps[0].memory_id,
                  "project_id": project_id({project!r})}}))"""


def create(side, project, description, body):
    return side.api(CREATE.format(project=project, description=description, body=body))


def seeded(tmp_path):
    return Case(tmp_path, seed=seed_store(tmp_path), rs=PY)


def sql(side, statement, *parameters):
    conn = sqlite3.connect(side.root / "memriver.db")
    try:
        with conn:
            return conn.execute(statement, parameters).fetchall()
    finally:
        conn.close()


def column(case, side, table, name):
    position = [c for c in snapshot(side.root).tables[table][0]].index(name)
    return sorted(str(row[position]) for row in case.renamed(side)["tables"][table])


def test_seed_ids_are_compared_verbatim(tmp_path):
    case = seeded(tmp_path)
    case.assert_same_store()
    assert case.py.names == {} == case.rs.names
    (old,) = sql(case.rs, "SELECT id FROM memories ORDER BY id LIMIT 1")[0]
    for table, name in [("memories", "id"), ("memory_versions", "memory_id"),
                        ("change_steps", "memory_id"), ("memory_sources", "memory_id"),
                        ("memory_sources", "source_id"), ("memory_reads", "memory_id")]:
        sql(case.rs, f"UPDATE {table} SET {name} = 'zzzzzzzzzz' WHERE {name} = ?", old)
    with pytest.raises(AssertionError, match="memories"):
        case.assert_same_store()


def test_swapped_foreign_keys_are_a_difference(tmp_path):
    case = seeded(tmp_path)

    def two(side):
        create(side, "demo proj", "first cue", "first body")
        create(side, "repo with history", "second cue", "second body")

    case.step(two)
    case.assert_same_store()
    first, second = case.rs.raw("<memory:1>"), case.rs.raw("<memory:2>")
    a, b = (sql(case.rs, "SELECT project_id FROM memories WHERE id = ?", i)[0][0]
            for i in (first, second))
    assert a != b
    sql(case.rs, "UPDATE memories SET project_id = ? WHERE id = ?", b, first)
    sql(case.rs, "UPDATE memories SET project_id = ? WHERE id = ?", a, second)
    with pytest.raises(AssertionError, match="memories"):
        case.assert_same_store()


def test_global_and_a_project_created_by_project_init_are_paired_without_a_changes_row(tmp_path):
    case = Case(tmp_path, rs=PY)
    directory = case.work / "a"
    directory.mkdir()
    py, rs = case.step(lambda side: side.run(["project", "init", str(directory), "--yes"]))
    assert py.code == 0, py.stderr
    assert py.stdout != rs.stdout
    case.assert_same_output(py, rs, "project-init")
    assert "created project a [<project:1>] and bound" in case.rendered(case.py, py, "project-init")[0]
    case.step(lambda side: side.api("services.project.ensure_global()"))
    payload = json.dumps({"session_id": "s-1", "source": "startup", "cwd": str(directory)})
    case.step(lambda side: side.run(["hook", "session-start", "--harness", "claude-code"],
                                    cwd=directory, stdin=payload))
    py, rs = case.step(lambda side: side.run(["project", "explain"], cwd=directory))
    case.assert_same_output(py, rs, "project-explain")
    assert "project: <project:1>\n" in case.rendered(case.rs, rs, "project-explain")[0]
    case.assert_same_store()
    assert sorted(case.py.names.values()) == ["<project:1>", "<project:2>"]
    assert column(case, case.py, "projects", "id") == ["<project:1>", "<project:2>"]
    assert column(case, case.rs, "sessions", "project_id") == ["<project:1>"]
    assert snapshot(case.py.root).tables["changes"] == []


def test_two_memories_in_one_apply_batch_pair_by_step_position(tmp_path):
    case = seeded(tmp_path)
    case.step(lambda side: side.api(
        'twin = Create(project_id("demo proj"), "project", "same cue", "same body")\n'
        'services.memory.apply([twin, twin], changed_by="compat")'))
    case.assert_same_store()
    for side in (case.py, case.rs):
        steps = dict(sql(side, "SELECT memory_id, step FROM change_steps WHERE change_id = ?",
                         side.raw("<change:1>")))
        assert steps == {side.raw("<memory:1>"): 1, side.raw("<memory:2>"): 2}


def test_a_body_equal_to_a_new_id_is_not_renamed(tmp_path):
    case = Case(tmp_path / "per-side", seed=seed_store(tmp_path / "per-side"), rs=PY)
    case.step(lambda side: create(side, "demo proj", "cue", "body"))
    # each side's own new id as the body: two different strings
    case.step(lambda side: create(side, "demo proj", "quotes an id", side.raw("<memory:1>")))
    with pytest.raises(AssertionError, match="memories"):
        case.assert_same_store()

    case = Case(tmp_path / "literal", seed=seed_store(tmp_path / "literal"), rs=PY)
    case.step(lambda side: create(side, "demo proj", "cue", "body"))
    literal = case.py.raw("<memory:1>")
    case.step(lambda side: create(side, "demo proj", "quotes an id", literal))
    case.assert_same_store()
    assert literal in column(case, case.py, "memories", "body")
    assert "<memory:1>" not in column(case, case.py, "memories", "body")


def test_a_project_name_equal_to_an_id_is_left_alone(tmp_path):
    case = Case(tmp_path, rs=PY)
    for name in ("a", "b"):
        (case.work / name).mkdir()
    case.step(lambda side: side.run(["project", "init", str(case.work / "a"), "--yes"]))
    literal = case.py.raw("<project:1>")
    py, rs = case.step(lambda side: side.run(
        ["project", "init", str(case.work / "b"), "--name", literal, "--yes"]))
    assert py.code == 0, py.stderr
    case.assert_same_store()
    assert column(case, case.py, "projects", "name") == sorted(["a", literal])
    case.assert_same_output(py, rs, "project-init")
    assert f"created project {literal} [<project:2>]" in case.rendered(case.py, py, "project-init")[0]


def test_ambiguous_step_is_not_comparable(tmp_path):
    case = seeded(tmp_path)

    def twice(side):
        create(side, "demo proj", "same cue", "same body")
        create(side, "demo proj", "same cue", "same body")

    with pytest.raises(NotComparable, match="same logical key"):
        case.step(twice)

    def uneven(side):
        for _ in range(1 if side is case.py else 2):
            create(side, "demo proj", "one or two", str(_))

    with pytest.raises(NotComparable, match="1 new .* 2"):
        case.step(uneven)


def test_new_ids_are_renamed_consistently_across_tables_and_declared_output_fields(tmp_path):
    case = seeded(tmp_path)
    py, rs = case.step(lambda side: create(side, "demo proj", "cue", "body"))
    assert py.stdout != rs.stdout
    case.assert_same_output(py, rs, "change-json")
    assert json.loads(case.rendered(case.py, py, "change-json")[0]) == {
        "change_id": "<change:1>", "id": "<memory:1>", "project_id": json.loads(py.stdout)["project_id"]}
    with pytest.raises(AssertionError):     # nothing is renamed unless the kind says where
        case.assert_same_output(py, rs)
    case.assert_same_store()
    for side in (case.py, case.rs):
        for table, name in [("memories", "id"), ("memory_versions", "memory_id"),
                            ("change_steps", "memory_id")]:
            assert "<memory:1>" in column(case, side, table, name), (table, name)
        for table in ("changes", "change_steps", "memory_versions"):
            assert "<change:1>" in column(case, side, table, "change_id"), table
    py, rs = case.step(lambda side: side.run(["list"]))
    assert "\n  <memory:1>  [project]" in case.rendered(case.py, py, "list")[0]
    case.assert_same_output(py, rs, "list")
    # the same id in a field that is not declared stays as it is, and differs
    py, rs = case.step(lambda side: side.api(
        f'print(json.dumps({{"note": {side.raw("<memory:1>")!r}}}))'))
    with pytest.raises(AssertionError):
        case.assert_same_output(py, rs, "change-json")


def test_time_order_survives_renaming(tmp_path):
    case = seeded(tmp_path)
    case.step(lambda side: create(side, "demo proj", "earlier", "body one"))
    case.step(lambda side: create(side, "demo proj", "later", "body two"))
    case.assert_same_store()
    stamps = {}
    for side in (case.py, case.rs):
        earlier, later = (sql(side, "SELECT updated FROM memories WHERE id = ?",
                              side.raw(name))[0][0] for name in ("<memory:1>", "<memory:2>"))
        assert earlier < later
        assert int(side.times[earlier][3:-1]) < int(side.times[later][3:-1])
        stamps[side] = (earlier, later)
    assert stamps[case.py] != stamps[case.rs]
    earlier, later = stamps[case.rs]
    sql(case.rs, "UPDATE memories SET updated = ? WHERE id = ?", later, case.rs.raw("<memory:1>"))
    sql(case.rs, "UPDATE memories SET updated = ? WHERE id = ?", earlier, case.rs.raw("<memory:2>"))
    with pytest.raises(AssertionError, match="memories"):
        case.assert_same_store()


def tampered(tmp_path, tamper):
    """One step that creates a memory on both sides; `tamper` then rewrites what the
    second side stored, as an implementation writing other values would have."""
    case = seeded(tmp_path)

    def act(side):
        create(side, "demo proj", "cue", "body")
        if side is case.rs:
            tamper(side)

    return case, act


def test_a_new_timestamp_in_another_format_is_a_difference(tmp_path):
    def offset_form(side):
        sql(side, "UPDATE memories SET created = replace(created, 'Z', '+00:00'), "
                  "updated = replace(updated, 'Z', '+00:00') WHERE description = 'cue'")
        sql(side, "UPDATE changes SET at = replace(at, 'Z', '+00:00') WHERE changed_by = 'compat'")

    case, act = tampered(tmp_path, offset_form)
    with pytest.raises(AssertionError, match=r"not a timestamp: '.*\+00:00'"):
        case.step(act)
    assert not any(stamp.endswith("+00:00") for stamp in case.rs.times)


def test_a_new_timestamp_that_is_not_the_time_of_the_step_is_a_difference(tmp_path):
    def long_ago(side):
        sql(side, "UPDATE memories SET created = '1999-01-01T00:00:00.000002Z', "
                  "updated = '1999-01-01T00:00:00.000002Z' WHERE description = 'cue'")
        sql(side, "UPDATE changes SET at = '1999-01-01T00:00:00.000001Z' WHERE changed_by = 'compat'")

    case, act = tampered(tmp_path, long_ago)
    with pytest.raises(AssertionError, match="1999-01-01T00:00:00.00000[12]Z is outside the step"):
        case.step(act)


def test_a_new_timestamp_that_is_any_other_text_is_a_difference(tmp_path):
    def junk(side):
        sql(side, "UPDATE memories SET created = 'a', updated = 'a' WHERE description = 'cue'")
        sql(side, "UPDATE changes SET at = 'a' WHERE changed_by = 'compat'")

    case, act = tampered(tmp_path, junk)
    with pytest.raises(AssertionError, match="not a timestamp: 'a'"):
        case.step(act)


def test_a_new_id_outside_the_id_alphabet_is_a_difference(tmp_path):
    def odd_id(side):
        (old,) = sql(side, "SELECT id FROM memories WHERE description = 'cue'")[0]
        for table, name in [("memories", "id"), ("memory_versions", "memory_id"),
                            ("change_steps", "memory_id")]:
            sql(side, f"UPDATE {table} SET {name} = 'UUUU-IL!O_' WHERE {name} = ?", old)

    case, act = tampered(tmp_path, odd_id)
    with pytest.raises(AssertionError, match="memories: not an id: 'UUUU-IL!O_'"):
        case.step(act)
    assert ("memory", "UUUU-IL!O_") not in case.rs.names


def test_a_timestamp_that_is_no_calendar_time_is_a_difference(tmp_path, monkeypatch):
    case = seeded(tmp_path)
    # a step that crosses a minute: as text, second 99 of 19:59 sorts inside it
    ticks = itertools.cycle([datetime(2026, 11, 22, 19, 59, 59, tzinfo=UTC),
                             datetime(2026, 11, 22, 20, 0, 0, tzinfo=UTC)])
    monkeypatch.setattr(harness, "_now", lambda: next(ticks))

    def act(side):
        create(side, "demo proj", "cue", "body")
        stamp = "2026-11-22T19:59:59.900000Z" if side is case.py else "2026-11-22T19:59:99.900000Z"
        sql(side, "UPDATE memories SET created = ?, updated = ? WHERE description = 'cue'", stamp, stamp)
        sql(side, "UPDATE changes SET at = ? WHERE changed_by = 'compat'", stamp)

    with pytest.raises(AssertionError, match="not a timestamp: '2026-11-22T19:59:99.900000Z'"):
        case.step(act)
    assert "2026-11-22T19:59:59.900000Z" in case.py.times


UPDATE = """(found,) = [m for m in services.memory.memories(project_id("demo proj"))
            if m.description == "deploy script location"]
services.memory.apply([Update(found.id, found.version, body={body!r})], changed_by="compat")"""


def test_a_field_that_only_moves_forward_may_pass_the_clock(tmp_path):
    seed = seed_store(tmp_path)
    conn = sqlite3.connect(seed / "memriver.db")
    with conn:     # the clock went back: the stored time is ahead of it
        conn.execute("UPDATE memories SET updated = '2090-01-01T00:00:00.000000Z' "
                     "WHERE description = 'deploy script location'")
    conn.close()
    case = Case(tmp_path, seed=seed, rs=PY)
    case.step(lambda side: side.api(UPDATE.format(body="moved once")))
    case.assert_same_store()
    for side in (case.py, case.rs):
        assert sql(side, "SELECT updated, version FROM memories WHERE body = 'moved once'") == [
            ("2090-01-01T00:00:00.000001Z", 2)]

    # one step past the stored time is all the rule allows
    def too_far(side):
        side.api(UPDATE.format(body="moved twice"))
        if side is case.rs:
            sql(side, "UPDATE memories SET updated = '2090-01-01T00:00:00.000007Z' "
                      "WHERE body = 'moved twice'")

    with pytest.raises(AssertionError, match="2090-01-01T00:00:00.000007Z is outside the step"):
        case.step(too_far)


def test_the_same_text_as_an_id_of_another_type_is_not_renamed(tmp_path):
    case = seeded(tmp_path)
    (case.work / "c").mkdir()
    (seed_memory,) = sql(case.rs, "SELECT id FROM memories ORDER BY id LIMIT 1")[0]

    def act(side):
        side.run(["project", "init", str(case.work / "c"), "--yes"])
        if side is case.rs:     # this side's new project happens to get a seed memory's id
            sql(side, "UPDATE projects SET id = ? WHERE name = 'c'", seed_memory)

    case.step(act)
    assert case.rs.raw("<project:1>") == seed_memory
    case.assert_same_store()
    assert seed_memory in column(case, case.rs, "memories", "id")
    assert seed_memory not in column(case, case.rs, "projects", "id")


def test_free_text_after_the_separator_is_compared_as_it_is(tmp_path):
    case = seeded(tmp_path)
    (case.work / "c").mkdir()
    case.step(lambda side: side.run(["project", "init", str(case.work / "c"), "--yes"]))
    case.step(lambda side: create(side, "demo proj", "cue", "first line"))
    py, rs = case.step(lambda side: side.run(["show", side.raw("<memory:1>")]))
    assert py.code == 0 and b"\n---\nfirst line\n" in py.stdout, py
    assert py.stdout != rs.stdout
    case.assert_same_output(py, rs, "show")
    shown = case.rendered(case.py, py, "show")[0]
    assert "id: <memory:1>\nproject: " in shown and "\ncreated: <t:" in shown

    # only the body differs: a line there that looks like a field of the header
    def with_body_line(side, done):
        return Completed(done.stdout + f"project: {side.raw('<project:1>')}\n".encode(), b"", 0)

    py, rs = with_body_line(case.py, py), with_body_line(case.rs, rs)
    case.assert_same_store()
    with pytest.raises(AssertionError, match="the outputs differ"):
        case.assert_same_output(py, rs, "show")
    assert case.rendered(case.py, py, "show")[0].endswith(f"first line\nproject: {case.py.raw('<project:1>')}\n")


@pytest.mark.parametrize("where", ["directory", "repository", "worktree"])
def test_a_seed_layout_that_leaves_the_temporary_directory_is_refused(tmp_path, where):
    seeds, outside = tmp_path / "seeds", tmp_path / "outside"
    shutil.copytree(SEEDS / "small", seeds / "small")
    layout = json.loads((seeds / "small" / "layout.json").read_text())
    if where == "directory":
        layout["directories"].append(str(outside / "d"))
    elif where == "repository":
        layout["git"][0]["path"] = "{{WORK}}/../../outside/repo"
    else:
        layout["git"][0]["worktrees"][0]["path"] = str(outside / "linked")
    (seeds / "small" / "layout.json").write_text(json.dumps(layout))
    with pytest.raises(AssertionError, match="outside"):
        load_seed("small", tmp_path / "loaded", seeds=seeds)
    assert not outside.exists()
    assert not (tmp_path / "loaded").exists()


def test_load_seed_sets_user_version_and_binds_placeholders(tmp_path):
    root = load_seed("small", tmp_path)
    assert snapshot(root).user_version == 6
    assert (root / "memriver.db").stat().st_mode & 0o777 == 0o600
    home, work = tmp_path / "home", tmp_path / "work"
    listing = run(PY, ["list"], home=home, root=root, cwd=work)
    assert listing.code == 0 and b"deploy script location" in listing.stdout, listing
    roots = dict(sql_rows(root, "SELECT root, id FROM projects WHERE root IS NOT NULL"))
    assert sorted(roots) == [str(work / "project-a"), str(work / "repo")]
    for directory, project in [(work / "project-a" / "sub", roots[str(work / "project-a")]),
                               (work / "repo", roots[str(work / "repo")])]:
        explained = run(PY, ["project", "explain"], home=home, root=root, cwd=directory)
        assert f"state: registered\nproject: {project}\n" in explained.stdout.decode(), explained
    # the layout's linked worktree is one: its .git is a file pointing into the repository
    assert (work / "repo-linked" / ".git").is_file() and (work / "repo" / ".git").is_dir()


def sql_rows(root, statement):
    conn = sqlite3.connect(root / "memriver.db")
    try:
        return conn.execute(statement).fetchall()
    finally:
        conn.close()


def test_a_bare_dump_is_refused_by_the_loader(tmp_path):
    seeds = tmp_path / "seeds"
    shutil.copytree(SEEDS / "small", seeds / "small")
    data = seeds / "small" / "data.sql"
    lines = data.read_text().splitlines(keepends=True)
    assert lines[-1] == "PRAGMA user_version = 6;\n"
    data.write_text("".join(lines[:-1]))
    with pytest.raises(AssertionError, match="user_version"):
        load_seed("small", tmp_path / "loaded", seeds=seeds)
