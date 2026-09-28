"""apply_group (spec §6.1): the group limit, the two-part report line (R9), apply as
"dream" with the executor's harness, and conflicts/policy refusals reported, not
raised."""

from __future__ import annotations

import memriver_dream.report as report_module
import pytest
from memriver_core import Verdict
from memriver_core.bootstrap import build_services
from memriver_core.models.changes import Create, SoftDelete, SourceRef, Update
from memriver_core.settings import Settings
from memriver_dream.changes import apply_group


def test_a_failed_completion_append_after_a_real_apply_still_marks_it_unknown(
        world, monkeypatch):
    # core already committed the change by the time the completion append runs;
    # a transient failure there must not lose the pending line -- the next write
    # still closes it as "outcome unknown", and the memory stays changed either way
    memory_id = world.create(world.project.id, "old body")
    ctx = world.context()
    real_append = report_module._append

    def flaky(path, text):
        if text.startswith(" -> change"):
            raise OSError("injected")
        real_append(path, text)

    monkeypatch.setattr(report_module, "_append", flaky)
    with pytest.raises(OSError, match="^injected$"):
        apply_group(ctx, "rewrite", [memory_id],
                    [Update(memory_id=memory_id, expected_version=1, body="new body")])
    assert [version.version for version in world.services.memory.versions(memory_id)] == [1, 2]
    monkeypatch.setattr(report_module, "_append", real_append)
    ctx.report.footer(status="failed", finished_at=world.now)
    assert (f"applying rewrite {memory_id} -> outcome unknown — see memriver history "
            f"{memory_id}\n") in ctx.report.path.read_text()


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


def _needs(ctx, world) -> list[str]:
    ctx.report.footer(status="completed", finished_at=world.now)
    text = ctx.report.path.read_text()
    if "== Needs you ==\n" not in text:
        return []
    return text.split("== Needs you ==\n", 1)[1].split("\n\nstatus:")[0].splitlines()


class Blocking:
    def __init__(self, verdict) -> None:
        self.verdict = verdict

    def classify(self, text, *, changed_by):
        return self.verdict


def test_a_change_that_touches_global_is_listed_with_its_undo(world):
    memory_id = world.create(world.global_id, "old principle")
    ctx = world.context()
    change_id = apply_group(ctx, "rewrite", [memory_id],
                            [Update(memory_id=memory_id, expected_version=1, body="new")],
                            touches_global=True)
    assert _needs(ctx, world) == [
        f"global changed: rewrite {memory_id} — undo: memriver undo {change_id}"]


def test_a_created_global_entry_is_named_by_its_new_id(world):
    ctx = world.context()
    change_id = apply_group(ctx, "new", [], [Create(project_id=world.global_id,
                                                    type="feedback", description="p",
                                                    body="a principle")],
                            touches_global=True)
    (step,) = world.services.memory.change(change_id).steps
    assert _needs(ctx, world) == [
        f"global changed: new {step.memory_id} — undo: memriver undo {change_id}"]


def test_a_project_change_is_not_listed(world):
    memory_id = world.create(world.project.id, "old body")
    ctx = world.context()
    apply_group(ctx, "rewrite", [memory_id],
                [Update(memory_id=memory_id, expected_version=1, body="new body")])
    assert _needs(ctx, world) == []


@pytest.mark.parametrize(("verdict", "category"), [
    (Verdict("instruction"), "instruction"),
    (Verdict("unavailable", detail="timeout"), "unavailable")])
def test_a_classifier_block_is_listed_and_the_group_is_not_applied(world, verdict, category):
    memory_id = world.create(world.project.id, "old body")
    services = build_services(Settings(root=world.root), root=world.root, home=world.home,
                              classifier=Blocking(verdict))
    ctx = world.context(services=services)
    assert apply_group(ctx, "rewrite", [memory_id],
                       [Update(memory_id=memory_id, expected_version=1, body="new body")]) \
        is None
    assert ctx.groups_used == 0
    assert (f"applying rewrite {memory_id} -> not applied: policy classifier-{category}"
            in ctx.report.path.read_text())
    assert _needs(ctx, world) == [
        f"blocked by the content classifier ({category}): rewrite {memory_id}"]
    assert [v.version for v in world.services.memory.versions(memory_id)] == [1]


def test_a_content_policy_refusal_is_not_a_classifier_block(world):
    memory_id = world.create(world.project.id, "old body")
    ctx = world.context()
    assert apply_group(ctx, "rewrite", [memory_id],
                       [Update(memory_id=memory_id, expected_version=1, body=world.secret)]) \
        is None
    assert _needs(ctx, world) == []
