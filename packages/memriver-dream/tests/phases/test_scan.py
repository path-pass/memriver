"""The policy scan (spec §6.2): every hit to "Needs you", current hits excluded from
model steps, history hits kept apart, nothing deleted, no text in the report."""

from __future__ import annotations

from memriver_core.models.changes import Update
from memriver_dream.phases import PassResult, scan


def test_hits_go_to_needs_you_and_split_current_from_history(world):   # §10 item 12
    history = world.create(world.project.id, "first body")
    world.services.memory.apply(
        [Update(memory_id=history, expected_version=1, body="second body")], changed_by="test")
    world.plant(history, 1, body=world.secret)
    current = world.create(world.global_id, "plain body")
    world.plant(current, 1, body=world.secret)
    clean = world.create(world.project.id, "clean body")
    ctx = world.context()
    assert scan.run(ctx) == PassResult(finished=True)
    assert ctx.excluded == {current}
    assert ctx.history_hits == {history: {1}}
    ctx.report.footer(status="completed", finished_at=world.now)
    text = ctx.report.path.read_text()
    rule = world.services.maintenance.check_text(world.secret)
    assert rule is not None
    needs = text.split("== Needs you ==\n")[1]
    assert (f"policy hit: {history} v1 (history) rule {rule} — memriver delete {history} "
            "--hard\n") in needs
    assert (f"policy hit: {current} v1 (current) rule {rule} — memriver delete {current} "
            "--hard\n") in needs
    assert "policy hits: 2; left out of model steps: 1\n" in text
    assert clean not in text and world.secret not in text and "ghp_" not in text
    # nothing is deleted
    assert {history, current, clean} <= {memory.id for memory in world.services.memory.memories()}


def test_a_clean_store_reports_no_hit_and_excludes_nothing(world):
    world.create(world.project.id, "clean body")
    ctx = world.context()
    assert scan.run(ctx) == PassResult(finished=True)
    assert (ctx.excluded, ctx.history_hits) == (set(), {})
    assert ctx.report.path.read_text() == "policy hits: 0; left out of model steps: 0\n"
