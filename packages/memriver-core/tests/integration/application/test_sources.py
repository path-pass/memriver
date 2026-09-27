"""Sources, derived trust/sync and Restore over a real store (spec §3.3, §4.1).

Covers acceptance §10 item 5, item 14 (without undo), item 1's sources-only update and
item 3's policy refusal through restore.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from memriver_core.models.changes import Create, SoftDelete, SourceRef, Update
from memriver_core.models.errors import BatchConflict, ContentRejected

SECRET = "aws key AKIAIOSFODNN7EXAMPLE ok"          # from the secret-scanner tests


def _sources(world, memory_id: str, version: int) -> list[tuple]:
    return world["sql"]("SELECT source_id, source_version FROM memory_sources "
                        "WHERE memory_id = ? AND version = ? ORDER BY source_id",
                        memory_id, version)


def _trust_sync(world, memory_id: str) -> tuple:
    return world["sql"]("SELECT trust, sync FROM memories WHERE id = ?", memory_id)[0]


def _merge(world, *originals: str, originals_first: bool = False) -> str:
    create = Create(world["mine"], "project", "merged", "the merged fact",
                    sources=tuple(SourceRef(original, 1) for original in originals))
    deletes = [SoftDelete(original, 1) for original in originals]
    ops = [*deletes, create] if originals_first else [create, *deletes]
    change = world["memory"].apply(ops, changed_by="human")
    return next(step.memory_id for step in change.steps if step.op == "create")


def _counts(world) -> list[int]:
    return [world["sql"](f"SELECT count(*) FROM {table}")[0][0]
            for table in ("memory_versions", "memory_sources", "changes", "change_steps")]


# --- item 14 ---------------------------------------------------------------------

@pytest.mark.parametrize("originals_first", [False, True])
def test_a_merge_may_soft_delete_its_originals_in_the_same_batch(world, originals_first):
    alpha, beta = world["create"]("alpha"), world["create"]("beta")
    merged = _merge(world, alpha, beta, originals_first=originals_first)
    assert _sources(world, merged, 1) == sorted([(alpha, 1), (beta, 1)])
    assert world["sql"]("SELECT version, deleted_at IS NOT NULL FROM memories "
                        "WHERE id IN (?, ?)", alpha, beta) == [(2, 1), (2, 1)]


def test_after_the_originals_changed_carrying_and_restoring_succeed_newly_citing_fails(world):
    memory = world["memory"]
    alpha, beta = world["create"]("alpha"), world["create"]("beta")
    merged = _merge(world, alpha, beta)
    memory.restore(alpha, 1, expected_version=2, changed_by="human")         # alpha v3
    memory.apply([Update(alpha, 3, body="alpha, later")], changed_by="human")  # alpha v4

    # carried: an edit of the merged entry keeps citing alpha@1
    memory.apply([Update(merged, 1, body="the merged fact, edited")], changed_by="human")
    assert _sources(world, merged, 2) == _sources(world, merged, 1)
    # carried while the set changes: beta@1 stays, alpha@1 is dropped
    memory.apply([Update(merged, 2, sources=(SourceRef(beta, 1),))], changed_by="human")
    assert _sources(world, merged, 3) == [(beta, 1)]
    # restored: version 1's set comes back although alpha@1 is not current
    memory.restore(merged, 1, expected_version=3, changed_by="human")
    assert _sources(world, merged, 4) == _sources(world, merged, 1)

    # newly citing the same old version is refused, by a create and by an update
    with pytest.raises(BatchConflict) as by_create:
        memory.apply([Create(world["mine"], "project", "", "again",
                             sources=(SourceRef(alpha, 1),))], changed_by="human")
    assert (by_create.value.index, by_create.value.memory_id, by_create.value.reason) == \
        (0, None, "source")
    other = world["create"]("other")
    with pytest.raises(BatchConflict) as by_update:
        memory.apply([Update(other, 1, sources=(SourceRef(alpha, 1),))], changed_by="human")
    assert (by_update.value.memory_id, by_update.value.reason) == (other, "source")
    # moving a citation to the source's current version is newly citing it, and allowed
    memory.apply([Update(merged, 4, sources=(SourceRef(alpha, 4), SourceRef(beta, 1)))],
                 changed_by="human")
    assert _sources(world, merged, 5) == sorted([(alpha, 4), (beta, 1)])


def test_a_version_made_earlier_in_the_same_batch_cannot_be_newly_cited(world):
    before = _counts(world)
    first = world["create"]("first")
    with pytest.raises(BatchConflict) as excinfo:
        # a source must be current and not deleted *before* the batch: version 2
        # exists only after the batch's own first op
        world["memory"].apply([SoftDelete(first, 1),
                               Create(world["mine"], "project", "", "x",
                                      sources=(SourceRef(first, 2),))], changed_by="human")
    assert excinfo.value.reason == "source"
    assert _counts(world)[0] == before[0] + 1          # only `first`'s own version


def test_a_source_set_citing_one_memory_twice_is_a_value_error(world):
    source = world["create"]("source")
    with pytest.raises(ValueError):
        world["memory"].apply([Create(world["mine"], "project", "", "x",
                                      sources=(SourceRef(source, 1), SourceRef(source, 1)))],
                              changed_by="human")


# --- item 1: a sources-only update ----------------------------------------------

def test_a_sources_only_update_creates_a_version(world):
    source, target = world["create"]("source"), world["create"]("target")
    change = world["memory"].apply([Update(target, 1, sources=(SourceRef(source, 1),))],
                                   changed_by="human")
    assert change.steps[0].after_version == 2
    assert world["sql"]("SELECT body FROM memory_versions WHERE memory_id = ? ORDER BY version",
                        target) == [("target",), ("target",)]
    assert _sources(world, target, 2) == [(source, 1)]
    with pytest.raises(BatchConflict) as excinfo:                  # the same set again
        world["memory"].apply([Update(target, 2, sources=(SourceRef(source, 1),))],
                              changed_by="human")
    assert excinfo.value.reason == "same-state"


# --- item 5 --------------------------------------------------------------------------

def test_a_cited_version_is_deletable_only_together_with_every_version_citing_it(world):
    source = world["create"]("source")
    derived = world["create"]("derived", sources=(SourceRef(source, 1),))
    with closing(sqlite3.connect(world["store"] / "memriver.db", isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        conn.execute("DELETE FROM memories WHERE id = ?", (source,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("COMMIT")                    # the deferred key holds at commit
        conn.execute("ROLLBACK")
        conn.execute("BEGIN")
        conn.execute("DELETE FROM memories WHERE id IN (?, ?)", (source, derived))
        conn.execute("COMMIT")                        # the cascade takes versions and sources
    assert world["sql"]("SELECT count(*) FROM memory_sources")[0][0] == 0


def test_cycles_through_any_version_are_refused(world):
    memory = world["memory"]
    alpha = world["create"]("alpha")
    beta = world["create"]("beta", sources=(SourceRef(alpha, 1),))
    memory.apply([Update(beta, 1, sources=())], changed_by="human")    # beta v2 cites nothing
    with pytest.raises(BatchConflict) as through_history:              # beta v1 still does
        memory.apply([Update(alpha, 1, sources=(SourceRef(beta, 2),))], changed_by="human")
    assert (through_history.value.memory_id, through_history.value.reason) == (alpha, "cycle")
    with pytest.raises(BatchConflict) as itself:
        memory.apply([Update(alpha, 1, sources=(SourceRef(alpha, 1),))], changed_by="human")
    assert itself.value.reason == "cycle"
    gamma, delta = world["create"]("gamma"), world["create"]("delta")
    before = _counts(world)
    with pytest.raises(BatchConflict) as within_batch:
        memory.apply([Update(gamma, 1, sources=(SourceRef(delta, 1),)),
                      Update(delta, 1, sources=(SourceRef(gamma, 1),))], changed_by="human")
    assert within_batch.value.reason == "cycle"
    assert _counts(world) == before


def test_trust_and_sync_are_derived_on_create_and_update_and_recorded_on_restore(world):
    memory = world["memory"]
    stated = world["create"]("stated by the user", trust="user")
    fetched = world["create"]("read on a web page", trust="untrusted-derived", sync=False)
    both = world["create"]("both", sources=(SourceRef(stated, 1), SourceRef(fetched, 1)),
                           trust="user", sync=True)        # the arguments are ignored
    assert _trust_sync(world, both) == ("untrusted-derived", 0)

    plain = world["create"]("plain")                        # agent, sync
    memory.apply([Update(plain, 1, sources=(SourceRef(stated, 1),))], changed_by="human")
    assert _trust_sync(world, plain) == ("agent", 1)        # never above the previous state
    memory.apply([Update(plain, 2, sources=(SourceRef(stated, 1), SourceRef(fetched, 1)))],
                 changed_by="human")
    assert _trust_sync(world, plain) == ("untrusted-derived", 0)
    memory.restore(plain, 1, expected_version=3, changed_by="human")
    assert _trust_sync(world, plain) == ("agent", 1)        # version 1's recorded values
    assert world["sql"]("SELECT trust, sync FROM memory_versions WHERE memory_id = ? "
                        "AND version = 4", plain) == [("agent", 1)]


# --- Restore -----------------------------------------------------------------------------

def test_restore_undeletes_and_restoring_a_deleted_version_deletes(world):
    memory, memory_id = world["memory"], world["create"]("kept")
    memory.apply([SoftDelete(memory_id, 1)], changed_by="human")
    change = memory.restore(memory_id, 1, expected_version=2, changed_by="human",
                            changed_via="cli")
    assert (change.steps[0].op, change.steps[0].before_version,
            change.steps[0].after_version) == ("restore", 2, 3)
    assert world["sql"]("SELECT body, deleted_at FROM memories WHERE id = ?",
                        memory_id) == [("kept", None)]
    memory.restore(memory_id, 2, expected_version=3, changed_by="human")
    assert world["sql"]("SELECT version, deleted_at IS NOT NULL FROM memories WHERE id = ?",
                        memory_id) == [(4, 1)]


def test_restore_of_a_missing_or_the_current_state_is_refused(world):
    memory, memory_id = world["memory"], world["create"]()
    memory.apply([Update(memory_id, 1, body="second")], changed_by="human")
    with pytest.raises(BatchConflict) as missing:
        memory.restore(memory_id, 9, expected_version=2, changed_by="human")
    assert missing.value.reason == "missing"
    with pytest.raises(BatchConflict) as same:
        memory.restore(memory_id, 2, expected_version=2, changed_by="human")
    assert same.value.reason == "same-state"
    with pytest.raises(BatchConflict) as stale:
        memory.restore(memory_id, 1, expected_version=1, changed_by="human")
    assert stale.value.reason == "version"


def test_restoring_a_deleted_version_whose_text_hits_the_policy_is_refused(world):
    memory, memory_id = world["memory"], world["create"]("clean")
    memory.apply([SoftDelete(memory_id, 1)], changed_by="human")              # v2, deleted
    world["sql"]("UPDATE memory_versions SET body = ? WHERE memory_id = ? AND version = 2",
                 SECRET, memory_id)
    memory.restore(memory_id, 1, expected_version=2, changed_by="human")      # v3, live
    before = _counts(world)
    with pytest.raises(ContentRejected) as excinfo:
        memory.restore(memory_id, 2, expected_version=3, changed_by="human")
    assert excinfo.value.memory_id == memory_id and excinfo.value.rule_id
    assert _counts(world) == before
    assert world["sql"]("SELECT version, deleted_at FROM memories WHERE id = ?",
                        memory_id) == [(3, None)]


def test_a_policy_violation_is_refused_identically_through_restore_and_apply(world):
    memory, memory_id = world["memory"], world["create"]("clean")
    world["sql"]("UPDATE memory_versions SET body = ? WHERE memory_id = ? AND version = 1",
                 SECRET, memory_id)                         # a history version that hits
    memory.apply([Update(memory_id, 1, body="edited")], changed_by="human")
    with pytest.raises(ContentRejected) as by_restore:
        memory.restore(memory_id, 1, expected_version=2, changed_by="human")
    with pytest.raises(ContentRejected) as by_apply:
        memory.apply([Update(memory_id, 2, body=SECRET)], changed_by="human")
    assert (by_restore.value.rule_id, by_restore.value.memory_id) == \
        (by_apply.value.rule_id, memory_id)
    assert world["sql"]("SELECT version FROM memories WHERE id = ?", memory_id) == [(2,)]
