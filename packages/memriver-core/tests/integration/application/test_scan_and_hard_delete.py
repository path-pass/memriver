"""The policy scan, check_text, the cascade hard delete and the history diagnosis (spec §4.4).

Covers acceptance §10 items 6 and 7.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from memriver_core.models import PlanCitation
from memriver_core.models.changes import SoftDelete, SourceRef, Update
from memriver_core.models.errors import MemoryNotFound, PlanChanged, UndoRefused

SECRET = "aws key AKIAIOSFODNN7EXAMPLE ok"          # from the secret-scanner tests
TABLES = ("memories", "memory_versions", "memory_sources", "memory_reads", "changes",
          "change_steps")


def _counts(world) -> dict[str, int]:
    return {table: world["sql"](f"SELECT count(*) FROM {table}")[0][0] for table in TABLES}


def _in(world, table: str, column: str, ids) -> int:
    ids = list(ids)
    return world["sql"](f"SELECT count(*) FROM {table} WHERE {column} IN "
                        f"({', '.join('?' for _ in ids)})", *ids)[0][0]


def _graph(world):
    """target cites source; history_only cited target in its first version only; current
    cites history_only's current version; gone (soft-deleted) cites current."""
    memory = world["memory"]
    source = world["create"]("the source")
    target = world["create"]("the target", sources=(SourceRef(source, 1),))
    history_only = world["create"]("once cited it", sources=(SourceRef(target, 1),))
    memory.apply([Update(history_only, 1, sources=())], changed_by="human")
    current = world["create"]("cites it now", sources=(SourceRef(history_only, 2),))
    gone = world["create"]("deleted citer", sources=(SourceRef(current, 1),))
    memory.apply([SoftDelete(gone, 1)], changed_by="human")
    unrelated = world["create"]("nothing to do with it")
    return source, target, history_only, current, gone, unrelated


# --- item 6: the scan ------------------------------------------------------------

def test_the_scan_finds_secrets_in_history_deleted_memories_and_global_without_text(world):
    memory, maintenance = world["memory"], world["services"].maintenance
    edited = world["create"]("clean")
    world["sql"]("UPDATE memory_versions SET body = ? WHERE memory_id = ? AND version = 1",
                 SECRET, edited)
    memory.apply([Update(edited, 1, body="clean, edited")], changed_by="human")
    gone = world["create"]("to go")
    memory.apply([SoftDelete(gone, 1)], changed_by="human")
    world["sql"]("UPDATE memory_versions SET description = ? WHERE memory_id = ? "
                 "AND version = 2", SECRET, gone)
    world["sql"]("UPDATE memories SET description = ? WHERE id = ?", SECRET, gone)
    principle = world["create"]("a principle", project_id=world["global"])
    world["sql"]("UPDATE memory_versions SET body = ? WHERE memory_id = ?", SECRET, principle)
    world["sql"]("UPDATE memories SET body = ? WHERE id = ?", SECRET, principle)
    world["create"]("nothing here")
    before = _counts(world)

    hits = maintenance.scan_policy()
    assert {(h.memory_id, h.version, h.current) for h in hits} == \
        {(edited, 1, False), (gone, 2, True), (principle, 1, True)}
    assert {h.rule_id for h in hits} == {maintenance.check_text(SECRET)}
    assert SECRET not in repr(hits) and "AKIA" not in repr(hits)
    assert _counts(world) == before                     # no deletion, no read fact


def test_check_text_names_the_rule_of_any_text_and_has_no_side_effect(world):
    maintenance = world["services"].maintenance
    world["create"]()
    before = _counts(world)
    rule = maintenance.check_text(SECRET)
    assert rule and rule not in ("empty", "too-large")
    assert maintenance.check_text("a plain sentence. " * 1000) is None   # no length rule
    assert maintenance.check_text("") is None and maintenance.check_text("\x01") is None
    assert _counts(world) == before


# --- item 7: the cascade hard delete -------------------------------------------------

def test_the_plan_follows_every_version_of_every_member_and_never_a_members_sources(world):
    _source, target, history_only, current, gone, _unrelated = _graph(world)
    plan = world["services"].maintenance.plan_hard_delete(target)
    assert plan.target == target
    assert [item.memory_id for item in plan.items] == \
        [target, *sorted([history_only, current, gone])]
    by_id = {item.memory_id: item for item in plan.items}
    assert (by_id[target].version, by_id[target].deleted, by_id[target].citations) == \
        (1, False, ())
    assert by_id[history_only].citations == (PlanCitation(history_only, 1, target, 1, False),)
    assert by_id[current].citations == (PlanCitation(current, 1, history_only, 2, True),)
    assert by_id[gone].citations == (PlanCitation(gone, 1, current, 1, False),
                                     PlanCitation(gone, 2, current, 1, True))
    assert (by_id[gone].version, by_id[gone].deleted, by_id[gone].project_id) == \
        (2, True, world["mine"])
    assert plan.expected == {(target, 1), (history_only, 2), (current, 1), (gone, 2)}
    assert re.fullmatch(r"[0-9a-f]{16}", plan.code)
    pairs = "\n".join(sorted(f"{memory_id}:{version}" for memory_id, version in plan.expected))
    assert plan.code == hashlib.sha256(pairs.encode("utf-8")).hexdigest()[:16]


@pytest.mark.parametrize("confirm", ["expected", "code"])
def test_a_confirmed_hard_delete_removes_every_member_row_and_keeps_the_change_rows(world,
                                                                                    confirm):
    memory, maintenance = world["memory"], world["services"].maintenance
    source, target, _history_only, current, _gone, unrelated = _graph(world)
    memory.read(current, world["context"], harness="codex")          # a read fact that goes
    mixed = memory.apply([Update(current, 1, body="cites it now, edited"),
                          Update(unrelated, 1, body="still nothing to do with it")],
                         changed_by="human")
    changes_before = _counts(world)["changes"]
    plan = maintenance.plan_hard_delete(target)
    deleted = maintenance.hard_delete(target, **{confirm: getattr(plan, confirm)})
    assert deleted == [item.memory_id for item in plan.items]
    for table, column in (("memories", "id"), ("memory_versions", "memory_id"),
                          ("memory_sources", "memory_id"), ("memory_sources", "source_id"),
                          ("memory_reads", "memory_id"), ("change_steps", "memory_id")):
        assert _in(world, table, column, deleted) == 0, (table, column)
    assert _counts(world)["changes"] == changes_before
    assert _in(world, "memories", "id", [source, unrelated]) == 2
    assert _in(world, "memory_versions", "memory_id", [source]) == 1

    # the change that touched a member and a survivor is now incomplete: not undoable,
    # listed by the diagnosis without turning it degraded
    with pytest.raises(UndoRefused) as excinfo:
        memory.undo(mixed.change_id, changed_by="human")
    assert excinfo.value.reason == "hard-deleted"
    report = maintenance.diagnose()
    assert mixed.change_id in report.incomplete_changes
    assert report.findings == () and report.state == "healthy"


def test_a_stale_set_or_code_deletes_nothing_and_carries_the_new_plan(world):
    memory, maintenance = world["memory"], world["services"].maintenance
    _source, target, _history_only, current, _gone, _unrelated = _graph(world)
    shown = maintenance.plan_hard_delete(target)
    memory.apply([Update(current, 1, body="cites it now, edited")], changed_by="human")
    before = _counts(world)
    with pytest.raises(PlanChanged) as by_set:
        maintenance.hard_delete(target, expected=shown.expected)
    with pytest.raises(PlanChanged) as by_code:
        maintenance.hard_delete(target, code=shown.code)
    assert by_set.value.plan == by_code.value.plan == maintenance.plan_hard_delete(target)
    assert by_set.value.plan.code != shown.code
    assert _counts(world) == before
    # a new referrer changes the plan just the same
    newer = maintenance.plan_hard_delete(target)
    world["create"]("a new referrer", sources=(SourceRef(target, 1),))
    with pytest.raises(PlanChanged):
        maintenance.hard_delete(target, code=newer.code)


def test_exactly_one_confirmation_is_required(world):
    maintenance = world["services"].maintenance
    target = world["create"]()
    plan = maintenance.plan_hard_delete(target)
    with pytest.raises(ValueError):
        maintenance.hard_delete(target)
    with pytest.raises(ValueError):
        maintenance.hard_delete(target, expected=plan.expected, code=plan.code)
    assert _in(world, "memories", "id", [target]) == 1


def test_an_unknown_id_is_not_found(world):
    maintenance = world["services"].maintenance
    with pytest.raises(MemoryNotFound):
        maintenance.plan_hard_delete("zzzzzzzzzz")
    with pytest.raises(MemoryNotFound):
        maintenance.hard_delete("zzzzzzzzzz", code="0123456789abcdef")


def test_a_global_or_deleted_target_is_deleted_like_any_other(world):
    maintenance = world["services"].maintenance
    principle = world["create"]("a principle", project_id=world["global"])
    gone = world["create"]("gone")
    world["memory"].apply([SoftDelete(gone, 1)], changed_by="human")
    for target in (principle, gone):
        assert maintenance.hard_delete(
            target, expected=maintenance.plan_hard_delete(target).expected) == [target]
    assert _in(world, "memories", "id", [principle, gone]) == 0
