"""MaintenanceService over a real SQLite store: the maintenance run's reads and writes."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from memriver_core import ContentRejected, GroupConflict, UndoConflict, bootstrap
from memriver_core.models import (
    ChangeGroup,
    CreateOp,
    Memory,
    Review,
    SoftDeleteOp,
    UpdateOp,
    new_id,
    now,
    timestamp_shift,
)
from memriver_core.repository.sqlite.database import DATABASE_FILENAME
from memriver_core.settings import Settings

SECRET = "token ghp_" + "a" * 36          # trips the github-pat rule
SNAPSHOT = '{"type":"project","description":"cue","body":"x"}'


@pytest.fixture
def world(tmp_path):
    store, home = tmp_path / "store", tmp_path / "home"
    work, other = tmp_path / "work", tmp_path / "other"
    for directory in (home, work, other):
        directory.mkdir()
    settings = Settings(root=store)
    service = bootstrap.build_service(settings, root=store, home=home)
    global_id = service.ensure_global()
    project = service.init_project("demo", service.plan_root(str(work)))
    other_project = service.init_project("other", service.plan_root(str(other)))
    return SimpleNamespace(
        store=store, service=service, global_id=global_id, project=project,
        other=other_project, context=service.open_project_context(str(work)),
        other_context=service.open_project_context(str(other)),
        maintenance=bootstrap.build_maintenance_service(settings, root=store))


def _write(world, body="uv manages python", *, description="cue", context=None) -> Memory:
    return world.service.record(content=body, type="project", sync=True, harness="test",
                                description=description, context=context or world.context)


def _sql(world, statement: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(world.store / DATABASE_FILENAME)) as conn, conn:
        return conn.execute(statement, params).fetchall()


def _plant(world, project_id: str, body: str, *, description: str = "cue",
           created: str | None = None, last_read_at: str | None = None, trust: str = "agent",
           sync: int = 1) -> str:
    """A row written behind the service's back: global rows, old rows, secrets."""
    memory_id, stamp = new_id(), created or now()
    _sql(world, "INSERT INTO memories (id, project_id, type, source_harness, source_method, "
                "trust, sync, description, body, created, updated, version, last_read_at) "
                "VALUES (?, ?, 'project', 'test', 'agent', ?, ?, ?, ?, ?, ?, 1, ?)",
         memory_id, project_id, trust, sync, description, body, stamp, stamp, last_read_at)
    return memory_id


def _days_ago(days: float) -> str:
    return timestamp_shift(now(), days=-days)


def test_projects_list_global_last_and_memories_list_active_rows_oldest_first(world):
    first, second, gone = _write(world, "first"), _write(world, "second"), _write(world, "gone")
    world.service.delete(gone.id, world.context, expected_version=1)
    assert world.maintenance.projects()[-1].id == world.global_id
    assert world.maintenance.global_project_id() == world.global_id
    assert [m.id for m in world.maintenance.memories(world.project.id)] == [first.id, second.id]


def test_no_maintenance_read_touches_last_read_at(world):
    memory = _write(world)
    world.maintenance.memories(world.project.id)
    world.maintenance.sources_of(memory.id)
    world.maintenance.derived_from(memory.id)
    world.maintenance.ttl_candidates(now(), 1, 5, 10)
    assert world.service.show(memory.id).last_read_at is None


def _set(world, derived: str, version: int, *sources: Memory) -> None:
    """A source set recorded for `derived` at `version` (no sources: an empty set)."""
    _sql(world, "INSERT INTO memory_source_sets VALUES (?, ?)", derived, version)
    for source in sources:
        _sql(world, "INSERT INTO memory_sources VALUES (?, ?, ?, 1, ?, ?)", derived, version,
             source.id, world.project.id, SNAPSHOT)


def test_sources_of_is_the_effective_set_and_derived_from_the_active_citers(world):
    a, b = _write(world, "a"), _write(world, "b")
    derived = _plant(world, world.global_id, "a and b")
    gone = _plant(world, world.global_id, "old")
    _set(world, derived, 1, a)
    _set(world, derived, 2, a, b)
    _set(world, gone, 1, a)
    # version 3 was written without a set (a human edit): version 2's set carries forward
    _sql(world, "UPDATE memories SET version = 3 WHERE id = ?", derived)
    _sql(world, "UPDATE memories SET deleted_at = ?, version = 2 WHERE id = ?", now(), gone)
    assert sorted((s.source_id, s.source_version, s.snapshot["body"])
                  for s in world.maintenance.sources_of(derived)) == sorted(
        [(a.id, 1, "x"), (b.id, 1, "x")])
    assert world.maintenance.derived_from(a.id) == [derived]


def test_an_empty_set_is_no_sources_not_a_carried_one(world):
    a = _write(world, "a")
    derived = _plant(world, world.project.id, "rewritten")
    _set(world, derived, 1, a)
    _set(world, derived, 2)                        # an explicitly empty set
    _sql(world, "UPDATE memories SET version = 2 WHERE id = ?", derived)
    assert world.maintenance.sources_of(derived) == []
    assert world.maintenance.derived_from(a.id) == []
    _sql(world, "UPDATE memories SET version = 1 WHERE id = ?", derived)
    assert [s.source_id for s in world.maintenance.sources_of(derived)] == [a.id]


@pytest.mark.parametrize(("reads", "age_days", "due"), [
    (0, 91, True), (0, 89, False),          # the base TTL: 90 days
    (1, 181, True), (1, 179, False),        # one read doubles it
    (9, 451, True), (9, 449, False),        # capped at 5 x 90 days
])
def test_the_effective_ttl_grows_with_reads_up_to_the_cap(world, reads, age_days, due):
    stamp = _days_ago(age_days)
    memory_id = _plant(world, world.project.id, "fact", created=stamp, last_read_at=stamp)
    for _ in range(reads):
        _sql(world, "INSERT INTO memory_reads VALUES (?, 1, ?, 'codex', NULL)", memory_id, stamp)
    candidates = world.maintenance.ttl_candidates(now(), 90, 5, 10)
    assert (memory_id in [c.memory.id for c in candidates]) is due
    if due:
        assert candidates[0].reads == reads


def test_a_review_not_yet_due_suppresses_a_candidate_and_the_oldest_come_first(world):
    old = _plant(world, world.project.id, "old", created=_days_ago(300),
                 last_read_at=_days_ago(300))
    older = _plant(world, world.global_id, "older", created=_days_ago(400),
                   last_read_at=_days_ago(400))
    reviewed = _plant(world, world.project.id, "kept", created=_days_ago(500),
                      last_read_at=_days_ago(500))
    _sql(world, "INSERT INTO dream_reviews VALUES (?, 1, ?, 'keep', 'still true', 0, ?, 'r', "
                "'claude', 'dream-1')", reviewed, now(), timestamp_shift(now(), days=30))
    assert [c.memory.id for c in world.maintenance.ttl_candidates(now(), 90, 5, 10)] == [
        older, old]
    assert [c.memory.id for c in world.maintenance.ttl_candidates(now(), 90, 5, 1)] == [older]


def test_passes_policy_refuses_a_memory_whose_body_or_description_breaks_a_rule(world):
    clean = _plant(world, world.project.id, "fact")
    secret_body = _plant(world, world.project.id, SECRET)
    secret_cue = _plant(world, world.project.id, "fact", description=SECRET)
    by_id = {m.id: m for m in world.maintenance.memories(world.project.id)}
    assert world.maintenance.passes_policy(by_id[clean])
    assert not world.maintenance.passes_policy(by_id[secret_body])
    assert not world.maintenance.passes_policy(by_id[secret_cue])
    assert world.maintenance.text_passes_policy("plain words")
    assert not world.maintenance.text_passes_policy(SECRET)
    assert world.maintenance.text_passes_policy("")          # nothing there to leak


def test_an_untouched_store_has_no_fingerprints_and_no_changes(world):
    assert world.maintenance.fingerprint_of(f"consolidate:{world.project.id}") is None
    assert world.maintenance.changes(10) == []


def test_a_description_of_only_control_characters_is_treated_as_empty_not_rejected(world):
    control_only = chr(1) + chr(2) + chr(3)
    memory_id = _plant(world, world.project.id, "fact", description=control_only)
    by_id = {m.id: m for m in world.maintenance.memories(world.project.id)}
    assert world.maintenance.text_passes_policy(control_only)
    assert world.maintenance.passes_policy(by_id[memory_id])


def _group(world, kind: str, ops, *, project_id: str | None = None,
           reason: str = "same fact twice") -> ChangeGroup:
    return ChangeGroup(run_id="run1", kind=kind, project_id=project_id or world.project.id,
                       reason=reason, harness="codex", ops=tuple(ops))


def _merge(world, a: Memory, b: Memory, body: str = "uv manages python and its versions"):
    return _group(world, "merge", [CreateOp(world.project.id, "project", "merged cue", body,
                                            ((a.id, a.version), (b.id, b.version)))])


def _rewrite(world, target: Memory | str, version: int, body: str, *evidence: Memory):
    target_id = target if isinstance(target, str) else target.id
    return _group(world, "rewrite", [UpdateOp(target_id, version, "cue", body,
                                              tuple((e.id, e.version) for e in evidence))],
                  reason="the evidence says otherwise")


def _extract(world, *sources: Memory, update: tuple[str, int] | None = None):
    pairs = tuple((s.id, s.version) for s in sources)
    op = (CreateOp(world.global_id, "project", "python tooling", "Use uv for python.", pairs)
          if update is None else
          UpdateOp(update[0], update[1], "python tooling", "Use uv (two projects).", pairs))
    return _group(world, "extract", [op], project_id=world.global_id,
                  reason="holds beyond one project")


def _count(world, table: str) -> int:
    return _sql(world, f"SELECT count(*) FROM {table}")[0][0]


def _counts(world) -> list[int]:
    return [_count(world, t) for t in ("memories", "dream_changes", "memory_source_sets",
                                        "memory_sources")]


def _sources(world, memory_id: str) -> set[tuple[str, int]]:
    return {(s.source_id, s.source_version) for s in world.maintenance.sources_of(memory_id)}


def test_a_merge_creates_a_derived_memory_keeps_its_sources_and_logs_the_change(world):
    a, b = _write(world, "uv manages python"), _write(world, "python is managed by uv")
    change_id = world.maintenance.apply_group(_merge(world, a, b))
    change = world.maintenance.change(change_id)
    (row,) = change.rows
    merged = world.service.show(row.id)
    assert (change.kind, change.project_id, change.run_id) == ("merge", world.project.id, "run1")
    assert (row.before, row.before_sources, row.after_version) == (None, None, 1)
    assert (merged.source, merged.trust, merged.sync) == (
        {"harness": "codex", "method": "dream"}, "agent", True)
    assert _sources(world, merged.id) == {(a.id, 1), (b.id, 1)}
    # the sources stay; whether they go is the TTL's decision later
    assert [m.id for m in world.maintenance.memories(world.project.id)] == [a.id, b.id,
                                                                            merged.id]
    assert [c.change_id for c in world.maintenance.changes(10)] == [change_id]


def test_derived_trust_is_the_least_trusted_source_and_sync_needs_every_source(world):
    a = _plant(world, world.project.id, "a", trust="user")
    b = _plant(world, world.project.id, "b", trust="untrusted-derived", sync=0)
    change_id = world.maintenance.apply_group(_group(world, "merge", [
        CreateOp(world.project.id, "project", "cue", "a and b", ((a, 1), (b, 1)))]))
    merged = world.service.show(world.maintenance.change(change_id).rows[0].id)
    assert (merged.trust, merged.sync) == ("untrusted-derived", False)


def test_an_evidence_rewrite_takes_its_trust_and_sync_from_the_evidence(world):
    target = _plant(world, world.project.id, "the API runs on port 8000", trust="user")
    evidence = _plant(world, world.project.id, "the API moved to 9000",
                      trust="untrusted-derived", sync=0)
    world.maintenance.apply_group(_group(world, "rewrite", [
        UpdateOp(target, 1, "api port", "The API runs on port 9000.", ((evidence, 1),))]))
    rewritten = world.service.show(target)
    assert (rewritten.trust, rewritten.sync, rewritten.source) == (
        "untrusted-derived", False, {"harness": "codex", "method": "dream"})
    assert _sources(world, target) == {(evidence, 1)}


@pytest.mark.parametrize("ops", [
    "merge-with-update", "unsafe-with-create", "two-ops", "rewrite-without-evidence",
    "merge-with-one-source"])
def test_core_refuses_a_group_that_breaks_the_kind_rules(world, ops):
    a, b = _write(world, "a"), _write(world, "b")
    create = CreateOp(world.project.id, "project", "c", "x", ((a.id, 1), (b.id, 1)))
    group = {
        "merge-with-update": _group(world, "merge", [UpdateOp(a.id, 1, "c", "x",
                                                              ((b.id, 1),))]),
        "unsafe-with-create": _group(world, "unsafe", [create]),
        "two-ops": _group(world, "merge", [create, create]),
        "rewrite-without-evidence": _group(world, "rewrite", [UpdateOp(a.id, 1, "c", "x", ())]),
        "merge-with-one-source": _group(world, "merge", [
            CreateOp(world.project.id, "project", "c", "x", ((a.id, 1),))]),
    }[ops]
    counts = _counts(world)
    with pytest.raises(ValueError):
        world.maintenance.apply_group(group)
    assert _counts(world) == counts


def test_a_stale_precondition_writes_nothing(world):
    a, b = _write(world, "a"), _write(world, "b")
    stale = _merge(world, a, b)
    world.service.update(b.id, "b2", world.context, expected_version=1)
    counts = _counts(world)
    with pytest.raises(GroupConflict) as caught:
        world.maintenance.apply_group(stale)
    assert (caught.value.change_id, caught.value.ids) == (None, (b.id,))
    assert _counts(world) == counts


def test_a_source_from_another_project_is_refused_outside_extract(world):
    a, foreign = _write(world, "a"), _write(world, "b", context=world.other_context)
    with pytest.raises(GroupConflict) as caught:
        world.maintenance.apply_group(_merge(world, a, foreign))
    assert caught.value.ids == (foreign.id,)


def test_extract_writes_global_and_nothing_else_creates_there(world):
    a = _write(world, "use uv for python")
    change_id = world.maintenance.apply_group(_extract(world, a))
    created = world.service.show(world.maintenance.change(change_id).rows[0].id)
    assert created.project_id == world.global_id
    with pytest.raises(GroupConflict):      # a merge planned for the project cannot land in global
        world.maintenance.apply_group(_group(world, "merge", [
            CreateOp(world.global_id, "project", "cue", "x", ((a.id, 1), (a.id, 1)))]))
    with pytest.raises(GroupConflict):      # an extract is planned for global only
        world.maintenance.apply_group(_group(world, "extract", [
            CreateOp(world.project.id, "project", "cue", "x", ((a.id, 1),))]))


def test_a_source_cited_at_its_current_version_is_not_extracted_twice(world):
    a = _write(world, "use uv for python")
    world.maintenance.apply_group(_extract(world, a))
    with pytest.raises(GroupConflict) as caught:
        world.maintenance.apply_group(_extract(world, a))   # a re-run planning it again
    assert caught.value.ids == (a.id,)


def test_an_extract_update_cannot_consume_a_source_another_global_entry_holds(world):
    a = _write(world, "use uv for python")
    b = _write(world, "this repo pins ruff", context=world.other_context)
    world.maintenance.apply_group(_extract(world, a))                       # G1 <- a v1
    second = world.maintenance.change(world.maintenance.apply_group(
        _extract(world, b))).rows[0].id                                     # G2 <- b v1
    counts = _counts(world)
    with pytest.raises(GroupConflict) as caught:
        world.maintenance.apply_group(_extract(world, a, update=(second, 1)))
    assert caught.value.ids == (a.id,)
    assert _counts(world) == counts                  # no version, set or change row
    assert (world.service.show(second).version, world.service.show(second).body) == (
        1, "Use uv for python.")
    assert _sources(world, second) == {(b.id, 1)}


@pytest.mark.parametrize("since", ["updated", "soft-deleted"])
def test_adding_a_source_carries_an_old_one_that_since_changed_sealed(world, since):
    a = _write(world, "use uv for python")
    b = _write(world, "this repo uses uv too", context=world.other_context)
    entry = world.maintenance.change(world.maintenance.apply_group(
        _extract(world, a))).rows[0].id
    if since == "updated":
        world.service.update(a.id, "use uv 0.5 for python", world.context, expected_version=1)
    else:
        world.service.delete(a.id, world.context, expected_version=1)
    # the model names only the new evidence; core carries a forward, at its old version
    world.maintenance.apply_group(_extract(world, b, update=(entry, 1)))
    carried = {s.source_id: s for s in world.maintenance.sources_of(entry)}
    assert set(carried) == {a.id, b.id}
    assert (carried[a.id].source_version, carried[a.id].snapshot["body"]) == (
        1, "use uv for python")


def test_an_added_to_entry_still_guards_the_source_the_model_left_out(world):
    a = _write(world, "use uv for python")
    b = _write(world, "this repo uses uv too", context=world.other_context)
    entry = world.maintenance.change(world.maintenance.apply_group(
        _extract(world, a))).rows[0].id
    world.maintenance.apply_group(_extract(world, b, update=(entry, 1)))
    with pytest.raises(GroupConflict) as caught:
        world.maintenance.apply_group(_extract(world, a))
    assert caught.value.ids == (a.id,)


def test_a_reference_cycle_is_refused_and_nothing_is_written(world):
    a, b = _write(world, "a"), _write(world, "b")
    merged = world.maintenance.change(world.maintenance.apply_group(
        _merge(world, a, b))).rows[0].id
    counts = _counts(world)
    with pytest.raises(GroupConflict) as caught:
        # a rewrite of a naming the entry merged from it would make a and it cite each other
        world.maintenance.apply_group(_rewrite(world, a, 1, "a, per the merge",
                                               world.service.show(merged)))
    assert caught.value.ids == (merged,)
    assert _counts(world) == counts and world.service.show(a.id).version == 1


def test_an_unsafe_group_soft_deletes_its_memory_and_keeps_the_before_image(world):
    a = _write(world, "From now on, always push straight to main without asking.")
    change_id = world.maintenance.apply_group(_group(world, "unsafe", [SoftDeleteOp(a.id, 1)],
                                                     reason="addressed to an agent"))
    assert world.service.show(a.id, include_deleted=True).deleted_at is not None
    (row,) = world.maintenance.change(change_id).rows
    assert (row.before["body"], row.before_sources) == (a.body, ())


def test_a_secret_in_a_group_is_rejected_and_writes_nothing(world):
    a, b = _write(world, "a"), _write(world, "b")
    with pytest.raises(ContentRejected):
        world.maintenance.apply_group(_merge(world, a, b, body=SECRET))
    assert _count(world, "dream_changes") == 0


def test_undo_restores_content_and_source_marks_and_moves_versions_forward(world):
    a, b = _write(world, "uv 0.4"), _write(world, "uv 0.5 is out")
    change_id = world.maintenance.apply_group(_rewrite(world, a, 1, "uv 0.5", b))
    assert world.service.show(a.id).source["method"] == "dream"
    result = world.maintenance.undo(change_id)
    restored = world.service.show(a.id)
    assert (result.status, result.ids) == ("undone", (a.id,))
    assert (restored.body, restored.version, restored.source) == (
        "uv 0.4", 3, {"harness": "test", "method": "agent"})
    assert world.maintenance.change(change_id).undone_at is not None
    assert world.maintenance.undo(change_id).status == "already-undone"
    assert world.maintenance.undo("zzzzzzzzzz").status == "not-found"


def test_undo_of_a_merge_soft_deletes_the_created_row(world):
    a, b = _write(world, "a"), _write(world, "b")
    change_id = world.maintenance.apply_group(_merge(world, a, b))
    merged_id = world.maintenance.change(change_id).rows[0].id
    world.maintenance.undo(change_id)
    assert world.service.show(merged_id, include_deleted=True).deleted_at is not None


def test_undo_after_a_later_edit_is_a_conflict_that_writes_nothing(world):
    a, b = _write(world, "uv 0.4"), _write(world, "uv 0.5 is out")
    change_id = world.maintenance.apply_group(_rewrite(world, a, 1, "uv 0.5", b))
    world.service.update(a.id, "uv 0.6", world.context, expected_version=2)
    with pytest.raises(UndoConflict) as caught:
        world.maintenance.undo(change_id)
    assert (caught.value.change_id, caught.value.ids) == (change_id, (a.id,))
    assert world.service.show(a.id).body == "uv 0.6"
    assert world.maintenance.change(change_id).undone_at is None


def test_undo_of_a_first_rewrite_restores_no_sources(world):
    a, b = _write(world, "uv 0.4"), _write(world, "uv 0.5 is out")
    change_id = world.maintenance.apply_group(_rewrite(world, a, 1, "uv 0.5", b))
    assert _sources(world, a.id) == {(b.id, 1)}
    world.maintenance.undo(change_id)
    assert world.maintenance.sources_of(a.id) == []


def test_undo_restores_the_set_in_force_even_when_the_before_version_wrote_none(world):
    a, b, c = _write(world, "a"), _write(world, "b"), _write(world, "c")
    merged = world.maintenance.change(world.maintenance.apply_group(
        _merge(world, a, b))).rows[0].id
    world.service.update(merged, "edited by hand", world.context, expected_version=1)  # v2
    rewrite = world.maintenance.apply_group(_rewrite(world, merged, 2, "c says otherwise", c))
    assert _sources(world, merged) == {(a.id, 1), (b.id, 1), (c.id, 1)}
    world.maintenance.undo(rewrite)
    assert _sources(world, merged) == {(a.id, 1), (b.id, 1)}


def test_set_fingerprint_stores_and_replaces_a_scope_fingerprint(world):
    world.maintenance.set_fingerprint("consolidate:x", "one", now())
    world.maintenance.set_fingerprint("consolidate:x", "two", now())
    assert world.maintenance.fingerprint_of("consolidate:x") == "two"


def test_an_empty_description_is_no_violation_but_a_secret_one_is(world):
    a, b = _write(world, "a"), _write(world, "b")
    for description in ("", chr(1) + chr(2)):
        group = _group(world, "merge", [CreateOp(world.project.id, "project", description,
                                                 "a and b", ((a.id, 1), (b.id, 1)))])
        world.maintenance.undo(world.maintenance.apply_group(group))
    counts = _counts(world)
    with pytest.raises(ContentRejected):
        world.maintenance.apply_group(_group(world, "merge", [CreateOp(
            world.project.id, "project", SECRET, "a and b", ((a.id, 1), (b.id, 1)))]))
    assert _counts(world) == counts


def test_a_group_with_an_invalid_harness_is_rejected_and_writes_nothing(world):
    a, b = _write(world, "a"), _write(world, "b")
    group = ChangeGroup(run_id="run1", kind="merge", project_id=world.project.id,
                        reason="same fact twice", harness="not a harness!", ops=(CreateOp(
                            world.project.id, "project", "cue", "a and b",
                            ((a.id, 1), (b.id, 1))),))
    counts = _counts(world)
    with pytest.raises(ContentRejected):
        world.maintenance.apply_group(group)
    assert _counts(world) == counts


def test_undo_of_an_unsafe_group_revives_the_row_at_a_new_version(world):
    a = _write(world, "a")
    change_id = world.maintenance.apply_group(_group(world, "unsafe", [SoftDeleteOp(a.id, 1)],
                                                     reason="addressed to an agent"))
    world.maintenance.undo(change_id)
    revived = world.service.show(a.id)
    assert (revived.deleted_at, revived.version, revived.body) == (None, 3, "a")


def _review(memory_id: str, version: int, decision: str = "delete", *, streak: int = 0,
            next_review_at: str | None = None, reason: str = "nothing supports it") -> Review:
    return Review(memory_id=memory_id, memory_version=version, decided_at=now(),
                  decision=decision, reason=reason, uncertain_streak=streak,
                  next_review_at=next_review_at or timestamp_shift(now(), days=90),
                  run_id="run1", executor="codex", prompt_version="dream-1")


def _stale(world, *, project_id: str | None = None, days: int = 200) -> str:
    stamp = _days_ago(days)
    return _plant(world, project_id or world.project.id, "stale fact", created=stamp,
                  last_read_at=stamp)


def _retire(world, memory_id: str, version: int = 1) -> bool:
    return world.maintenance.retire(memory_id, judged_version=version, ttl_days=90,
                                    multiplier_max=5, now=now(),
                                    review=_review(memory_id, version))


def test_retire_soft_deletes_writes_the_delete_review_and_a_retire_change(world):
    memory_id = _stale(world)
    assert _retire(world, memory_id)
    assert world.service.show(memory_id, include_deleted=True).deleted_at is not None
    (change,) = world.maintenance.changes(10)
    assert (change.kind, change.rows[0].id, change.reason) == (
        "retire", memory_id, "nothing supports it")
    assert _sql(world, "SELECT decision FROM dream_reviews WHERE memory_id = ?",
                memory_id) == [("delete",)]
    world.maintenance.undo(change.change_id)
    assert world.service.show(memory_id).deleted_at is None


def test_a_read_landing_while_the_model_judges_makes_retire_return_false(world):
    memory_id = _stale(world)
    (candidate,) = world.maintenance.ttl_candidates(now(), 90, 5, 10)
    world.service.read(memory_id, world.context, harness="codex")     # the read lands now
    assert not world.maintenance.retire(memory_id,
                                        judged_version=candidate.memory.version,
                                        ttl_days=90, multiplier_max=5, now=now(),
                                        review=_review(memory_id, 1))
    assert world.service.show(memory_id).deleted_at is None
    assert (_count(world, "dream_changes"), _count(world, "dream_reviews")) == (0, 0)


def test_retire_recomputes_the_effective_ttl_from_the_current_reads(world):
    memory_id = _stale(world, days=200)         # past 90 days, not past 3 x 90
    for _ in range(2):   # reads recorded long ago: last_read_at itself does not move
        _sql(world, "INSERT INTO memory_reads VALUES (?, 1, ?, 'codex', NULL)", memory_id,
             _days_ago(200))
    assert not _retire(world, memory_id)


def test_retire_refuses_a_moved_version_and_a_row_covered_by_a_newer_keep(world):
    moved = _stale(world)
    _sql(world, "UPDATE memories SET version = 2 WHERE id = ?", moved)
    assert not _retire(world, moved)            # judged at version 1
    kept = _stale(world)
    world.maintenance.record_review(_review(kept, 1, "keep"))
    assert not _retire(world, kept)


def test_keep_touches_nothing_and_suppresses_re_review_until_next_review_at(world):
    memory_id = _stale(world)
    before = world.service.show(memory_id)
    world.maintenance.record_review(_review(memory_id, 1, "keep"))
    after = world.service.show(memory_id)
    assert (after.body, after.updated, after.last_read_at, after.version) == (
        before.body, before.updated, before.last_read_at, before.version)
    assert world.maintenance.ttl_candidates(now(), 90, 5, 10) == []
    later = timestamp_shift(now(), days=91)
    (candidate,) = world.maintenance.ttl_candidates(later, 90, 5, 10)
    assert (candidate.memory.id, candidate.review.decision) == (memory_id, "keep")


def test_a_review_of_a_vanished_memory_is_refused(world):
    assert world.maintenance.record_review(_review("zzzzzzzzzz", 1, "keep")) is False
    assert _count(world, "dream_reviews") == 0


@pytest.mark.parametrize("since", ["edited", "soft-deleted"])
def test_a_judgment_of_a_version_that_moved_records_nothing(world, since):
    memory_id = _stale(world)
    if since == "edited":
        _sql(world, "UPDATE memories SET version = 2, body = 'edited' WHERE id = ?", memory_id)
    else:
        _sql(world, "UPDATE memories SET version = 2, deleted_at = ? WHERE id = ?", now(),
             memory_id)
    assert world.maintenance.record_review(_review(memory_id, 1, "keep")) is False
    assert _count(world, "dream_reviews") == 0
    if since == "edited":
        assert world.maintenance.record_review(_review(memory_id, 2, "keep")) is True


def test_a_review_reason_that_breaks_the_policy_is_refused(world):
    memory_id = _stale(world)
    with pytest.raises(ContentRejected):
        world.maintenance.record_review(_review(memory_id, 1, "keep", reason=SECRET))
    with pytest.raises(ValueError):
        world.maintenance.record_review(_review(memory_id, 1, "delete"))


def test_retire_refuses_a_review_naming_a_different_memory(world):
    stale = _stale(world)
    kept = _write(world, "kept fact").id
    world.maintenance.record_review(_review(kept, 1, "keep"))
    with pytest.raises(ValueError):
        world.maintenance.retire(stale, judged_version=1, ttl_days=90, multiplier_max=5,
                                 now=now(), review=_review(kept, 1))
    assert world.service.show(stale).deleted_at is None
    assert world.service.show(kept).deleted_at is None
    assert _sql(world, "SELECT decision FROM dream_reviews WHERE memory_id = ?",
                kept) == [("keep",)]
    assert (_count(world, "dream_reviews"), _count(world, "dream_changes")) == (1, 0)


def test_retire_refuses_a_review_whose_version_does_not_match_judged_version(world):
    memory_id = _stale(world)
    with pytest.raises(ValueError):
        world.maintenance.retire(memory_id, judged_version=1, ttl_days=90, multiplier_max=5,
                                 now=now(), review=_review(memory_id, 999))
    assert world.service.show(memory_id).deleted_at is None
    assert (_count(world, "dream_reviews"), _count(world, "dream_changes")) == (0, 0)
