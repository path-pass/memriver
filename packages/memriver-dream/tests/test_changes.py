"""apply_group (spec §6.1): the group limit, the two-part report line (R9), apply as
"dream" with the executor's harness, and conflicts/policy refusals reported, not
raised."""

from __future__ import annotations

from memriver_core.models.changes import Create, SoftDelete, SourceRef, Update
from memriver_dream.changes import apply_group


def test_a_group_is_applied_by_dream_and_reported_with_its_undo(world):
    memory_id = world.create(world.project.id, "old body")
    ctx = world.context()
    change_id = apply_group(ctx, "rewrite", [memory_id],
                            [Update(memory_id=memory_id, expected_version=1, body="new body")])
    change = world.services.memory.change(change_id)
    assert (change.changed_by, change.changed_via) == ("dream", "fake-harness")
    assert ctx.groups_used == 1
    assert ctx.report.path.read_text().splitlines() == [
        f"applying rewrite {memory_id} -> change {change_id}; undo: memriver undo {change_id}",
        f"  update {memory_id} v1→v2"]


def test_a_group_that_creates_says_so_and_lists_the_created_memory_after(world):
    first = world.create(world.project.id, "alpha")
    second = world.create(world.project.id, "beta")
    ctx = world.context()
    ops = [Create(project_id=world.project.id, type="project", description="merged",
                  body="alpha and beta", sources=(SourceRef(first, 1), SourceRef(second, 1))),
           SoftDelete(memory_id=first, expected_version=1),
           SoftDelete(memory_id=second, expected_version=1)]
    change_id = apply_group(ctx, "merge", [first, second], ops)
    change = world.services.memory.change(change_id)
    created = next(step.memory_id for step in change.steps if step.op == "create")
    lines = ctx.report.path.read_text().splitlines()
    assert lines[0] == (f"applying merge {first} {second} (creates a memory) -> change "
                        f"{change_id}; undo: memriver undo {change_id}")
    assert sorted(lines[1:]) == sorted([f"  create {created} new→v1",
                                        f"  soft_delete {first} v1→v2",
                                        f"  soft_delete {second} v1→v2"])


def test_a_conflict_is_reported_writes_nothing_and_does_not_count(world):   # §10 item 9
    memory_id = world.create(world.project.id, "body")
    ctx = world.context()
    ops = [SoftDelete(memory_id=memory_id, expected_version=2)]
    assert apply_group(ctx, "supersede", [memory_id], ops) is None
    assert ctx.groups_used == 0
    assert ctx.report.path.read_text() == (
        f"applying supersede {memory_id} -> not applied: conflict version {memory_id}\n")
    assert [version.version for version in world.services.memory.versions(memory_id)] == [1]


def test_a_policy_refusal_is_reported_by_rule_id_only(world):   # §10 item 12
    ctx = world.context()
    ops = [Create(project_id=world.project.id, type="project", description="cue",
                  body=world.secret)]
    assert apply_group(ctx, "new", [], ops) is None
    rule = world.services.maintenance.check_text(world.secret)
    assert ctx.report.path.read_text() == (
        f"applying new (creates a memory) -> not applied: policy {rule}\n")
    assert ctx.groups_used == 0


def test_the_group_limit_stops_before_apply(world):   # §10 item 9: a cut group
    first = world.create(world.project.id, "first")
    second = world.create(world.project.id, "second")
    ctx = world.context(settings=world.dream.model_copy(update={"max_groups_per_run": 1}))
    assert apply_group(ctx, "supersede", [first],
                       [SoftDelete(memory_id=first, expected_version=1)]) is not None
    assert apply_group(ctx, "supersede", [second],
                       [SoftDelete(memory_id=second, expected_version=1)]) is None
    assert [version.version for version in world.services.memory.versions(second)] == [1]
    assert ctx.report.path.read_text().splitlines()[-1] == (
        f"not applied (group limit): supersede {second}")
    assert ctx.groups_used == 1
