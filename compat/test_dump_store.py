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


def future(tmp_path, statement):
    """A seeded case in which `statement` has put one stored time ahead of the clock."""
    seed = seed_store(tmp_path)
    conn = sqlite3.connect(seed / "memriver.db")
    with conn:
        conn.execute(statement)
    conn.close()
    return Case(tmp_path, seed=seed, rs=PY)


AHEAD = "UPDATE memories SET updated = '2090-01-01T00:00:00.000000Z' WHERE description = 'deploy script location'"


def test_a_field_that_only_moves_forward_may_not_go_back_to_the_clock(tmp_path):
    case = future(tmp_path, AHEAD)

    def back_to_now(side):
        side.api(UPDATE.format(body="moved"))
        if side is case.rs:     # inside the step's span, but behind the row's previous time
            sql(side, "UPDATE memories SET updated = ? WHERE body = 'moved'", harness._stamp(harness._now()))

    with pytest.raises(AssertionError, match="memories.updated: .* is not after 2090-01-01T00:00:00.000000Z"):
        case.step(back_to_now)


def test_a_field_that_only_moves_forward_may_not_go_back_to_a_time_already_seen(tmp_path):
    case = future(tmp_path, AHEAD)

    def back_to_created(side):
        side.api(UPDATE.format(body="moved"))
        if side is case.rs:     # a time the comparison has named already
            sql(side, "UPDATE memories SET updated = created WHERE body = 'moved'")

    with pytest.raises(AssertionError, match="memories.updated: .* is not after 2090"):
        case.step(back_to_created)


def test_a_new_version_must_move_the_time_forward(tmp_path):
    case = seeded(tmp_path)
    (before,) = sql(case.rs, "SELECT updated FROM memories WHERE description = 'deploy script location'")[0]

    def unmoved(side):
        side.api(UPDATE.format(body="moved"))
        if side is case.rs:
            sql(side, "UPDATE memories SET updated = ? WHERE body = 'moved'", before)

    with pytest.raises(AssertionError, match=f"memories.updated: {before} is not after {before}"):
        case.step(unmoved)


START = '{{"session_id": "s-move", "source": "startup", "cwd": "{cwd}", "transcript_path": "{home}/t1.jsonl"}}'
MOVE = """from memriver_core.models import SessionKey, now
key = SessionKey("claude-code", "s-move")
store = services.session._session_store
applied = store.move(key, expected_transcript_path=store.get(key).transcript_path,
                     entry_cwd=str(WORK / "other"), branch=None, transcript_path=str(HOME / "t2.jsonl"),
                     project_id=None, project_root=None, at=now())
assert applied is not None and applied[1] is not None"""


def moving(tmp_path):
    """A seeded case with one session whose last activity is ahead of the clock."""
    case = seeded(tmp_path)
    payload = START.format(cwd=case.work / "proj", home=case.home)
    case.step(lambda side: side.run(["hook", "session-start", "--harness", "claude-code"],
                                    cwd=case.work / "proj", stdin=payload))
    for side in (case.py, case.rs):
        assert sql(side, "SELECT count(*) FROM sessions") == [(1,)]
        sql(side, "UPDATE sessions SET last_active_at = '2090-01-01T00:00:00.000000Z'")
    return case


def test_a_move_writes_the_advanced_time_to_the_session_and_to_its_move(tmp_path):
    case = moving(tmp_path)
    case.step(lambda side: side.api(MOVE))
    case.assert_same_store()
    for side in (case.py, case.rs):
        assert sql(side, "SELECT last_active_at FROM sessions") == [("2090-01-01T00:00:00.000001Z",)]
        assert sql(side, "SELECT moved_at FROM session_moves") == [("2090-01-01T00:00:00.000001Z",)]


def test_each_of_two_moves_in_one_step_advances_by_one(tmp_path):
    case = moving(tmp_path)

    def twice(side):
        side.api(MOVE)
        side.api(MOVE)

    case.step(twice)
    case.assert_same_store()
    for side in (case.py, case.rs):
        assert sql(side, "SELECT last_active_at FROM sessions") == [("2090-01-01T00:00:00.000002Z",)]
        assert sql(side, "SELECT moved_at FROM session_moves ORDER BY seq") == [
            ("2090-01-01T00:00:00.000001Z",), ("2090-01-01T00:00:00.000002Z",)]


@pytest.mark.parametrize(("statement", "problem"), [
    # the second move repeats the first one's time
    ("UPDATE session_moves SET moved_at = '2090-01-01T00:00:00.000001Z' WHERE seq = 2",
     "session_moves.moved_at: 2090-01-01T00:00:00.000001Z is not after .* by its 2 advance"),
    # the session stops one advance short
    ("UPDATE sessions SET last_active_at = '2090-01-01T00:00:00.000001Z'",
     "sessions.last_active_at: 2090-01-01T00:00:00.000001Z is not after .* by its 2 advance"),
])
def test_two_moves_that_do_not_advance_twice_are_a_difference(tmp_path, statement, problem):
    case = moving(tmp_path)

    def twice(side):
        side.api(MOVE)
        side.api(MOVE)
        if side is case.rs:
            sql(side, statement)

    with pytest.raises(AssertionError, match=problem):
        case.step(twice)


def test_two_versions_in_one_step_advance_by_two(tmp_path):
    case = future(tmp_path, AHEAD)

    def twice(side, short=False):
        side.api(UPDATE.format(body="moved once"))
        side.api(UPDATE.format(body="moved twice"))
        if short:
            sql(side, "UPDATE memories SET updated = '2090-01-01T00:00:00.000001Z' WHERE body = 'moved twice'")

    case.step(twice)
    case.assert_same_store()
    assert sql(case.rs, "SELECT updated FROM memories WHERE body = 'moved twice'") == [
        ("2090-01-01T00:00:00.000002Z",)]
    with pytest.raises(AssertionError, match="memories.updated: 2090-01-01T00:00:00.000001Z is not after"):
        case.step(lambda side: twice(side, short=side is case.rs))


@pytest.mark.parametrize(("moved_at", "problem"), [
    (None, "is not after 2090-01-01T00:00:00.000000Z"),          # the clock, behind the session
    ("2090-01-01T00:00:00.000002Z", "is outside the step"),      # further than one advance
])
def test_a_move_recorded_at_another_time_is_a_difference(tmp_path, moved_at, problem):
    case = moving(tmp_path)

    def act(side):
        side.api(MOVE)
        if side is case.rs:
            sql(side, "UPDATE session_moves SET moved_at = ?", moved_at or harness._stamp(harness._now()))

    with pytest.raises(AssertionError, match=f"session_moves.moved_at: .* {problem}"):
        case.step(act)


def test_a_forward_only_time_is_never_before_the_step(tmp_path):
    """A real move on both sides, the stored time long past: the reference side writes the
    clock, the other side only the stored time plus one."""
    case = seeded(tmp_path)
    payload = START.format(cwd=case.work / "proj", home=case.home)
    case.step(lambda side: side.run(["hook", "session-start", "--harness", "claude-code"],
                                    cwd=case.work / "proj", stdin=payload))
    for side in (case.py, case.rs):
        sql(side, "UPDATE sessions SET last_active_at = '2026-01-01T00:00:00.000000Z'")

    def act(side):
        side.api(MOVE)
        if side is case.rs:
            sql(side, "UPDATE sessions SET last_active_at = '2026-01-01T00:00:00.000001Z'")
            sql(side, "UPDATE session_moves SET moved_at = '2026-01-01T00:00:00.000001Z'")

    with pytest.raises(AssertionError, match="2026-01-01T00:00:00.000001Z is outside the step"):
        case.step(act)


LONG_AGO = "UPDATE memories SET updated = '2026-01-01T00:00:00.000000Z' WHERE description = 'deploy script location'"


@pytest.mark.parametrize("updated", ["2026-01-01T00:00:00.000001Z", "2026-06-01T00:00:00.000000Z"])
def test_a_new_version_is_never_before_the_step(tmp_path, updated):
    case = future(tmp_path, LONG_AGO)

    def early(side):
        side.api(UPDATE.format(body="moved"))
        if side is case.rs:     # after the stored time, and long before the step
            sql(side, "UPDATE memories SET updated = ? WHERE body = 'moved'", updated)

    with pytest.raises(AssertionError, match=f"memories.updated: {updated} is outside the step"):
        case.step(early)


@pytest.mark.parametrize(("updated", "problem"), [
    ("2090-01-01T00:00:00.000001Z", "is not after"),            # short of the two advances
    ("2090-01-01T00:00:00.000003Z", "is outside the step"),     # past them
])
def test_a_time_past_the_clock_is_only_the_stored_time_plus_its_advances(tmp_path, updated, problem):
    case = future(tmp_path, AHEAD)

    def twice(side):
        side.api(UPDATE.format(body="moved once"))
        side.api(UPDATE.format(body="moved twice"))
        if side is case.rs:
            sql(side, "UPDATE memories SET updated = ? WHERE body = 'moved twice'", updated)

    with pytest.raises(AssertionError, match=f"memories.updated: {updated} {problem}"):
        case.step(twice)


@pytest.mark.parametrize(("name", "directory"), [
    ("ordinary", "repo [abcde12345] and bound docs"),
    ("n [abcde12345] and bound m", "plain"),
    ("n [abcde12345] and bound m", "repo [zzzzz99999] and bound docs"),
    ("a [b] c", "created project x [abcde12345] and bound y"),
])
def test_project_init_finds_the_id_whatever_the_name_and_the_directory_say(tmp_path, name, directory):
    case = Case(tmp_path, rs=PY)
    (case.work / directory).mkdir()
    py, rs = case.step(lambda side: side.run(
        ["project", "init", str(case.work / directory), "--name", name, "--yes"]))
    assert py.code == 0, py
    case.assert_same_store()
    case.assert_same_output(py, rs, "project-init")
    line = f"created project {name} [<project:1>] and bound {case.work / directory}\n"
    assert line in case.rendered(case.py, py, "project-init")[0]
    assert case.rendered(case.py, py, "project-init")[0].count("<project:1>") == 1
    # the id at that place is compared: another one there is a difference
    other = Completed(rs.stdout.replace(case.rs.raw("<project:1>").encode(), b"abcde12345"), b"", 0)
    with pytest.raises(AssertionError, match="the outputs differ"):
        case.assert_same_output(py, other, "project-init")


def test_a_line_break_in_a_directory_name_does_not_make_a_field(tmp_path):
    case = Case(tmp_path, rs=PY)
    plain, odd = case.work / "a", case.work / "b\nproject: abcde12345\nreads: abcde12345"
    plain.mkdir()
    odd.mkdir()
    case.step(lambda side: side.run(["project", "init", str(plain), "--yes"]))
    case.step(lambda side: side.run(["project", "init", str(odd), "--name", "odd", "--yes"]))
    py, rs = case.step(lambda side: side.run(["project", "explain"], cwd=odd))
    case.assert_same_output(py, rs, "project-explain")
    shown = case.rendered(case.py, py, "project-explain")[0]
    assert "state: registered\nproject: <project:2>\n" in shown and "abcde12345" in shown
    assert shown.count("<project:2>") == 3      # project, reads, writes


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


def seeds_with(tmp_path, change):
    """A copy of the committed seeds whose layout `change` has edited."""
    seeds = tmp_path / "seeds"
    shutil.copytree(SEEDS / "small", seeds / "small")
    layout = json.loads((seeds / "small" / "layout.json").read_text())
    change(layout)
    (seeds / "small" / "layout.json").write_text(json.dumps(layout))
    return seeds


@pytest.mark.parametrize("where", ["directory", "repository", "worktree"])
def test_a_relative_layout_path_is_read_from_the_case_not_from_where_the_caller_stands(
        tmp_path, monkeypatch, where):
    def change(layout):
        if where == "directory":
            layout["directories"].append("../escaped")
        elif where == "repository":
            layout["git"][0]["path"] = "../../escaped"
        else:
            layout["git"][0]["worktrees"][0]["path"] = "../escaped"

    seeds = seeds_with(tmp_path, change)
    case = tmp_path / "outer" / "case"
    (case / "deep" / "deeper").mkdir(parents=True)
    monkeypatch.chdir(case / "deep" / "deeper")     # from here, ../../escaped is inside the case
    with pytest.raises(AssertionError, match="outside"):
        load_seed("small", case, seeds=seeds)
    assert sorted(p.name for p in case.iterdir()) == ["deep"]
    assert sorted(p.name for p in (tmp_path / "outer").iterdir()) == ["case"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["outer", "seeds"]


def test_a_relative_layout_path_inside_the_case_is_created_there(tmp_path, monkeypatch):
    seeds = seeds_with(tmp_path, lambda layout: layout["directories"].append("work/relative/d"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    load_seed("small", tmp_path / "case", seeds=seeds)
    assert (tmp_path / "case" / "work" / "relative" / "d").is_dir()
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("target", ["there", "not-there"])
def test_a_layout_path_through_a_link_out_of_the_case_is_refused(tmp_path, target):
    seeds = seeds_with(tmp_path, lambda layout: layout["directories"].append("link/sub/d"))
    outside, case = tmp_path / "outside", tmp_path / "case"
    (outside / "there").mkdir(parents=True)
    case.mkdir()
    (case / "link").symlink_to(outside / target)     # dangling when the target is not there
    with pytest.raises(AssertionError, match="outside"):
        load_seed("small", case, seeds=seeds)
    assert sorted(p.name for p in outside.iterdir()) == ["there"]
    assert list((outside / "there").iterdir()) == []
    assert sorted(p.name for p in case.iterdir()) == ["link"]


def outside_repository(tmp_path):
    """A repository of its own beside the case, with no commit yet."""
    outside = tmp_path / "outside"
    outside.mkdir()
    done = run(harness.GIT, ["init", "-q", "-b", "elsewhere", str(outside)],
               home=tmp_path, root=tmp_path, cwd=tmp_path)
    assert done.code == 0, done
    return outside


def untouched(outside):
    """Whether nothing was committed to the repository at `outside`."""
    return (not list((outside / ".git" / "refs" / "heads").iterdir())
            and not (outside / ".git" / "worktrees").exists()
            and sorted(p.name for p in outside.iterdir()) == [".git"])


def test_a_seed_repository_is_not_built_over_a_binding_to_another_repository(tmp_path):
    seeds = seeds_with(tmp_path, lambda layout: layout["git"][0].update(path="repo"))
    outside, case = outside_repository(tmp_path), tmp_path / "case"
    (case / "repo").mkdir(parents=True)
    (case / "repo" / ".git").write_text(f"gitdir: {outside / '.git'}\n")
    with pytest.raises(AssertionError, match="is not empty"):
        load_seed("small", case, seeds=seeds)
    assert untouched(outside)
    assert sorted(p.name for p in case.iterdir()) == ["repo"]


@pytest.mark.parametrize("where", ["a link to a bound directory", "a bound worktree", "a home with git settings",
                                   "an empty directory"])
def test_a_seed_is_built_only_in_an_empty_place(tmp_path, where):
    seeds = seeds_with(tmp_path, lambda layout: None)
    outside, case = outside_repository(tmp_path), tmp_path / "case"
    case.mkdir()
    if where == "a link to a bound directory":      # work/repo -> bound, inside the case
        (case / "bound").mkdir()
        (case / "bound" / ".git").write_text(f"gitdir: {outside / '.git'}\n")
        (case / "work").mkdir()
        (case / "work" / "repo").symlink_to(case / "bound")
    elif where == "a bound worktree":
        (case / "work" / "repo-linked").mkdir(parents=True)
        (case / "work" / "repo-linked" / ".git").write_text(f"gitdir: {outside / '.git'}\n")
    elif where == "a home with git settings":
        (case / "home").mkdir()
        (case / "home" / ".gitconfig").write_text(f"[core]\n\tworktree = {outside}\n")
    else:
        (case / "unrelated").mkdir()
    before = sorted(str(p) for p in case.rglob("*"))
    with pytest.raises(AssertionError, match="is not empty"):
        load_seed("small", case, seeds=seeds)
    assert untouched(outside)
    assert sorted(str(p) for p in case.rglob("*")) == before


def test_a_seed_is_built_in_an_empty_directory_that_exists(tmp_path):
    (tmp_path / "case").mkdir()
    root = load_seed("small", tmp_path / "case")
    assert root == (tmp_path / "case").resolve() / "store" and (tmp_path / "case" / "work" / "repo" / ".git").is_dir()


def test_a_seed_is_not_loaded_outside_the_tests_temporary_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "TMP_PATH", tmp_path / "mine")
    with pytest.raises(AssertionError, match="outside"):
        load_seed("small", tmp_path / "not-mine")
    assert not (tmp_path / "not-mine").exists()


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
