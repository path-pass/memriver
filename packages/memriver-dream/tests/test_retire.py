"""Phase 3 over a real store and a scripted executor."""

from __future__ import annotations

import pytest
from memriver_core import StorageFailure
from memriver_core.models import now, timestamp_shift
from memriver_dream.protocols import ExecutorResult
from memriver_dream.report import PhaseReport
from memriver_dream.retire import SYSTEM_PROMPT, run
from memriver_dream.settings import DREAM_REASON_CHARS

SECRET = "token ghp_" + "a" * 36


def _stale(world, project_id=None, body: str = "the staging host is stage-3") -> str:
    stamp = timestamp_shift(now(), days=-200)
    return world.plant(project_id or world.project.id, body, created=stamp, last_read_at=stamp)


def _decision(decision: str, reason: str = "nothing contradicts it") -> dict:
    return {"decision": decision, "reason": reason, "evidence": []}


def _phase(world, *, days_later: int = 0, **overrides) -> PhaseReport:
    phase = PhaseReport()
    run(world.run(now=timestamp_shift(now(), days=days_later), **overrides), phase)
    return phase


def _review_row(world, memory_id: str) -> tuple | None:
    rows = world.sql("SELECT decision, uncertain_streak FROM dream_reviews WHERE memory_id = ?",
                     memory_id)
    return rows[0] if rows else None


def test_a_delete_decision_retires_the_memory_as_an_undoable_change(world):
    memory_id = _stale(world)
    world.executor.replies = [_decision("delete", "stage-3 was decommissioned")]
    phase = _phase(world)
    (change,) = world.maintenance.changes(10)
    assert (phase.outcomes, change.kind, [r.id for r in change.rows]) == (
        {"retired": 1}, "retire", [memory_id])
    assert world.service.show(memory_id, include_deleted=True).deleted_at is not None
    assert _review_row(world, memory_id) == ("delete", 0)


def test_keep_touches_nothing_and_the_memory_is_not_reviewed_again_before_its_date(world):
    memory_id = _stale(world)
    before = world.service.show(memory_id)
    world.executor.replies = [_decision("keep")]
    assert _phase(world).items == [{"memory_id": memory_id, "decision": "keep"}]
    after = world.service.show(memory_id)
    assert (after.version, after.updated, after.last_read_at) == (
        before.version, before.updated, before.last_read_at)
    _phase(world)
    assert len(world.executor.calls) == 1              # suppressed until next_review_at


def test_uncertain_twice_in_a_row_retires(world):
    memory_id = _stale(world)
    world.executor.replies = [_decision("uncertain"), _decision("uncertain")]
    assert _phase(world).outcomes == {"uncertain": 1}
    assert _review_row(world, memory_id) == ("uncertain", 1)
    assert _phase(world, days_later=91).outcomes == {"retired": 1}
    assert _review_row(world, memory_id) == ("delete", 2)


def test_a_keep_in_between_resets_the_uncertain_streak(world):
    memory_id = _stale(world)
    world.executor.replies = [_decision("uncertain"), _decision("keep"), _decision("uncertain")]
    _phase(world)
    _phase(world, days_later=91)
    assert _phase(world, days_later=182).outcomes == {"uncertain": 1}
    assert _review_row(world, memory_id) == ("uncertain", 1)


def test_a_content_change_between_two_uncertain_judgments_resets_the_streak(world):
    memory_id = _stale(world)
    world.executor.replies = [_decision("uncertain"), _decision("uncertain")]
    _phase(world)
    # the content changes but stays old enough to be a candidate again
    world.sql("UPDATE memories SET version = 2, body = 'stage-4 now' WHERE id = ?", memory_id)
    assert _phase(world, days_later=91).outcomes == {"uncertain": 1}
    assert _review_row(world, memory_id) == ("uncertain", 1)


@pytest.mark.parametrize("change", [
    "UPDATE memories SET version = 2, body = 'edited' WHERE id = ?",
    "UPDATE memories SET version = 2, deleted_at = '2026-09-25T00:00:00.000000Z' WHERE id = ?",
])
def test_a_judgment_of_content_that_moved_meanwhile_records_nothing(world, change):
    memory_id = _stale(world)

    def change_then_keep(prompt, schema):
        world.sql(change, memory_id)
        return _decision("keep")

    world.executor.replies = [change_then_keep]
    assert _phase(world).outcomes == {"moved": 1}
    assert _review_row(world, memory_id) is None


def test_a_too_large_answer_retries_once_with_half_the_comparison(world):
    memory_id = _stale(world)
    for index in range(4):
        world.plant(world.project.id, f"recent fact {index}")
    world.executor.replies = [ExecutorResult(error="too-large"), _decision("keep")]
    assert _phase(world).outcomes == {"keep": 1}
    first, second = (call["prompt"] for call in world.executor.calls)
    assert len(second) < len(first)
    assert (first.count("recent fact"), second.count("recent fact")) == (4, 2)
    assert _review_row(world, memory_id) == ("keep", 0)
    assert world.maintenance.changes(10) == []      # the candidate is kept, never retired


def test_a_failed_call_records_nothing(world):
    memory_id = _stale(world)
    world.executor.replies = [ExecutorResult(error="login")]
    assert _phase(world).outcomes == {"login": 1}
    assert _review_row(world, memory_id) is None


def test_a_read_landing_while_the_model_judges_keeps_the_memory(world):
    memory_id = _stale(world)

    def read_then_delete(prompt, schema):
        world.service.read(memory_id, world.context, harness="codex")
        return _decision("delete")

    world.executor.replies = [read_then_delete]
    assert _phase(world).outcomes == {"moved": 1}
    assert world.service.show(memory_id).deleted_at is None


def test_a_candidate_failing_the_policy_is_skipped_and_secrets_never_reach_a_prompt(world):
    _stale(world, body="old token " + SECRET)
    clean = _stale(world, body="an old but clean fact")
    world.executor.replies = [_decision("keep")]
    phase = _phase(world)
    assert (phase.outcomes["policy"], phase.outcomes["keep"]) == (1, 1)
    assert len(world.executor.calls) == 1
    assert "ghp_" not in world.executor.calls[0]["prompt"]
    assert clean in world.executor.calls[0]["prompt"]


def test_global_memories_are_reviewed_with_the_same_ttl(world):
    memory_id = _stale(world, project_id=world.global_id)
    world.executor.replies = [_decision("keep")]
    assert _phase(world).items == [{"memory_id": memory_id, "decision": "keep"}]


def test_the_prompt_asks_for_a_reason_to_retire_and_keeps_description_carried_rules(world):
    _stale(world)
    world.executor.replies = [_decision("keep")]
    _phase(world)
    system = world.executor.calls[0]["system_prompt"]
    assert "not whether it was used" in system
    assert "A rule whose description already carries it" in SYSTEM_PROMPT


def test_the_review_is_registered_as_the_retire_phase():
    from memriver_dream.run import _PHASES
    assert _PHASES["retire"] is run


def test_one_unstorable_reason_fails_only_its_own_candidate(world):
    first = _stale(world, body="the first old fact")
    second = _stale(world, body="the second old fact")
    world.executor.replies = [_decision("delete", "gone " + chr(0xD800)), _decision("keep")]
    phase = _phase(world)
    assert (phase.outcomes, phase.items) == (
        {"invalid": 1, "keep": 1}, [{"memory_id": second, "decision": "keep"}])
    assert _review_row(world, first) is None
    assert world.service.show(first).deleted_at is None


def test_a_delete_citing_an_unknown_id_still_retires_evidence_is_never_acted_on(world):
    memory_id = _stale(world)
    world.executor.replies = [{"decision": "delete", "reason": "superseded",
                               "evidence": ["0" * 26]}]
    assert _phase(world).outcomes == {"retired": 1}
    assert world.service.show(memory_id, include_deleted=True).deleted_at is not None


def test_a_keep_citing_an_unknown_id_is_still_recorded(world):
    memory_id = _stale(world)
    run_at = now()
    world.executor.replies = [{"decision": "keep", "reason": "still holds",
                               "evidence": ["0" * 26, "not an id"]}]
    phase = PhaseReport()
    run(world.run(now=run_at), phase)
    assert phase.outcomes == {"keep": 1}
    assert world.sql("SELECT decision, next_review_at FROM dream_reviews WHERE memory_id = ?",
                     memory_id) == [("keep", timestamp_shift(run_at, days=world.dream.ttl_days))]


def test_an_answer_of_the_wrong_shape_records_nothing(world):
    memory_id = _stale(world)
    world.executor.replies = [{"decision": "maybe", "reason": "", "evidence": []}]
    assert _phase(world).outcomes == {"schema": 1}
    assert _review_row(world, memory_id) is None


def test_a_reason_holding_a_secret_is_rejected_even_where_the_limit_would_cut_it(world):
    memory_id = _stale(world)
    straddling = "x" * (DREAM_REASON_CHARS - 20) + " " + SECRET
    world.executor.replies = [_decision("delete", straddling)]
    assert _phase(world).outcomes == {"rejected": 1}
    assert _review_row(world, memory_id) is None
    assert world.service.show(memory_id).deleted_at is None


def test_a_long_reason_is_stored_on_one_line_within_the_limit(world):
    memory_id = _stale(world)
    world.executor.replies = [_decision("keep", "line one\nline two " + "y" * 400)]
    _phase(world)
    (reason,) = world.sql("SELECT reason FROM dream_reviews WHERE memory_id = ?", memory_id)[0]
    assert reason.startswith("line one line two") and len(reason) == DREAM_REASON_CHARS


def test_a_malformed_stored_time_is_sent_as_unknown(world):
    memory_id = _stale(world)
    world.sql("UPDATE memories SET updated = '0000-hand-edited' WHERE id = ?", memory_id)
    world.executor.replies = [_decision("keep")]
    _phase(world)
    prompt = world.executor.calls[0]["prompt"]
    assert "hand-edited" not in prompt and '"updated": ""' in prompt


def test_the_log_holds_ids_and_outcomes_never_text(world):
    memory_id = _stale(world, body="the staging host is stage-3")
    world.executor.replies = [_decision("keep", "a private reason")]
    _phase(world)
    assert world.lines == [f"retire {memory_id}: keep"]


def test_a_storage_failure_is_not_swallowed(world, monkeypatch):
    _stale(world)

    def broken(review):
        raise StorageFailure

    monkeypatch.setattr(world.maintenance, "record_review", broken)
    world.executor.replies = [_decision("keep")]
    with pytest.raises(StorageFailure):
        _phase(world)

