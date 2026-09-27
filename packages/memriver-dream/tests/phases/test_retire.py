"""TTL (spec §6.7) over a real store and a scripted executor: the 30/3 formula, the
review records and the uncertain streak, and the soft delete guarded by the listing
time."""

from __future__ import annotations

import pytest
from memriver_core.models.changes import Update, Usage
from memriver_dream.phases import PassResult, retire
from memriver_dream.protocols import ExecutorResult
from memriver_dream.settings import DREAM_REASON_CHARS
from memriver_dream.store import DreamStore, ReviewRow, shift_days


def _aged(world, days: float, *, project_id: str | None = None,
          body: str = "the staging host is stage-3") -> str:
    """A memory created, last updated and last read `days` before the run."""
    memory_id = world.create(project_id or world.project.id, body)
    _age(world, memory_id, days)
    return memory_id


def _age(world, memory_id: str, days: float) -> None:
    stamp = shift_days(world.now, -days)
    world.sql("UPDATE memories SET created = ?, updated = ?, last_read_at = ? WHERE id = ?",
              stamp, stamp, stamp, memory_id)


def _decision(decision: str, reason: str = "nothing contradicts it", evidence=()) -> dict:
    return {"decision": decision, "reason": reason, "evidence": list(evidence)}


def _pass(world, **overrides) -> tuple[PassResult, str]:
    ctx = world.context(**overrides)
    result = retire.run(ctx)
    text = ctx.report.path.read_text()
    ctx.report.path.unlink()
    return result, text


def _candidates(world, **overrides) -> list[str]:
    return [memory.id for memory in retire.candidates(world.context(**overrides))[1]]


def _review(world, memory_id: str) -> ReviewRow | None:
    return DreamStore(world.root / "dream" / "dream.db").review(memory_id)


def _deleted(world, memory_id: str) -> bool:
    return max(world.services.memory.versions(memory_id),
               key=lambda version: version.version).deleted


def _later(world, days: float) -> str:
    return shift_days(world.now, days)


@pytest.mark.parametrize(("reads", "days", "due"), [
    (0, 30, True), (0, 29, False), (1, 60, True), (1, 59, False),
    (2, 90, True), (2, 89, False), (9, 90, True), (9, 89, False)])
def test_the_ttl_is_30_days_times_one_plus_reads_capped_at_3(world, monkeypatch, reads, days,
                                                              due):
    # §10 item 11: 30/3
    memory_id = _aged(world, days)
    monkeypatch.setattr(world.services.memory, "usage",
                        lambda ids: {i: Usage(reads=reads, last_read_at=None) for i in ids})
    assert (_candidates(world) == [memory_id]) is due


def test_the_last_read_counts_from_usage(world, monkeypatch):
    memory_id = _aged(world, 100)
    recent = _later(world, -10)
    monkeypatch.setattr(world.services.memory, "usage",
                        lambda ids: {i: Usage(reads=0, last_read_at=recent) for i in ids})
    assert _candidates(world) == []
    monkeypatch.setattr(world.services.memory, "usage",
                        lambda ids: {i: Usage(reads=0, last_read_at=None) for i in ids})
    assert _candidates(world) == [memory_id]


def test_recorded_reads_stretch_the_ttl(world):   # §10 item 11, through usage()
    memory_id = world.create(world.project.id, "the staging host is stage-3")
    context = world.services.project.open_project_context(str(world.work))
    for _ in range(2):
        world.services.memory.read(memory_id, context, harness="codex")
    _age(world, memory_id, 89)
    assert _candidates(world) == []                # two reads: 30 x 3 days
    _age(world, memory_id, 90)
    assert _candidates(world) == [memory_id]


def test_candidates_are_oldest_first_limited_and_never_excluded(world):
    middle = _aged(world, 40)
    oldest = _aged(world, 50)
    _aged(world, 35)
    held = _aged(world, 60)
    two = world.dream.model_copy(update={"max_candidates_per_run": 2})
    assert _candidates(world, settings=two, excluded={held}) == [oldest, middle]


def test_global_memories_are_reviewed_with_the_same_ttl(world):
    memory_id = _aged(world, 30, project_id=world.global_id)
    _aged(world, 29, project_id=world.global_id)
    assert _candidates(world) == [memory_id]


def test_keep_is_recorded_and_the_memory_is_not_reviewed_again_before_its_date(world):
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("keep")]
    result, text = _pass(world)
    assert result == PassResult(finished=True)
    assert text == f"keep {memory_id}: nothing contradicts it\n"
    assert _review(world, memory_id) == ReviewRow(memory_id, 1, "keep", 0, world.now,
                                                  _later(world, 30), "nothing contradicts it")
    assert len(world.services.memory.versions(memory_id)) == 1
    # held back while next_review_at > now
    assert _candidates(world) == [] and _candidates(world, now=_later(world, 29)) == []
    assert _candidates(world, now=_later(world, 30)) == [memory_id]


def test_a_review_of_another_version_does_not_hold_the_memory_back(world):
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("keep")]
    _pass(world)
    world.services.memory.apply([Update(memory_id=memory_id, expected_version=1,
                                        body="the staging host is stage-4")], changed_by="test")
    _age(world, memory_id, 200)
    assert _candidates(world) == [memory_id]


def test_uncertain_twice_on_the_same_version_retires(world):   # §10 item 11
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("uncertain", "unclear"), _decision("uncertain")]
    _, text = _pass(world)
    assert text == f"uncertain {memory_id} (1 of 2): unclear\n"
    assert _review(world, memory_id) == ReviewRow(memory_id, 1, "uncertain", 1, world.now,
                                                  _later(world, 30), "unclear")
    later = _later(world, 31)
    result, text = _pass(world, now=later)
    assert result.finished and _deleted(world, memory_id)
    assert text.startswith(f"applying retire {memory_id} -> change ")
    assert f"  soft_delete {memory_id} v1→v2\n" in text
    assert '  description: "cue"\n  reason: nothing contradicts it\n' in text


def test_the_uncertain_streak_counts_the_same_version_only(world):   # §10 item 11
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("uncertain"), _decision("uncertain")]
    _pass(world)
    # the content changes but stays old enough to be a candidate again
    world.services.memory.apply([Update(memory_id=memory_id, expected_version=1,
                                        body="the staging host is stage-4")], changed_by="test")
    _age(world, memory_id, 200)
    _, text = _pass(world, now=_later(world, 31))
    assert text == f"uncertain {memory_id} (1 of 2): nothing contradicts it\n"
    assert _review(world, memory_id).memory_version == 2
    assert not _deleted(world, memory_id)


def test_a_keep_in_between_resets_the_streak(world):
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("uncertain"), _decision("keep"), _decision("uncertain")]
    _pass(world)
    _pass(world, now=_later(world, 31))
    _pass(world, now=_later(world, 62))
    assert (_review(world, memory_id).decision, _review(world, memory_id).uncertain_streak) == (
        "uncertain", 1)
    assert not _deleted(world, memory_id)


def test_a_delete_is_a_soft_delete_by_dream_and_records_no_review(world):
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("delete", "stage-3 was decommissioned")]
    result, text = _pass(world)
    assert result.finished and _deleted(world, memory_id)
    change = max(world.services.memory.versions(memory_id), key=lambda v: v.version).change
    assert (change.changed_by, change.changed_via) == ("dream", "fake-harness")
    assert f"undo: memriver undo {change.change_id}\n" in text
    assert "  reason: stage-3 was decommissioned\n" in text
    assert _review(world, memory_id) is None


def test_a_read_after_the_listing_makes_the_delete_a_conflict_that_records_nothing(world):
    # §10 item 11
    memory_id = _aged(world, 200)
    context = world.services.project.open_project_context(str(world.work))

    def read_then_delete(prompt, schema):
        world.services.memory.read(memory_id, context, harness="codex")
        return _decision("delete")

    world.executor.replies = [read_then_delete]
    result, text = _pass(world)
    assert not result.finished
    assert f"applying retire {memory_id} -> not applied: conflict read-since" in text
    assert not _deleted(world, memory_id) and _review(world, memory_id) is None


def test_a_too_large_answer_retries_once_with_half_the_comparison(world):
    memory_id = _aged(world, 200)
    for index in range(4):
        world.create(world.project.id, f"recent fact {index}")
    world.executor.replies = [ExecutorResult(error="too-large"), _decision("keep")]
    result, _ = _pass(world)
    assert result.finished
    first, second = (call["prompt"] for call in world.executor.calls)
    assert (first.count("recent fact"), second.count("recent fact")) == (4, 2)
    assert _review(world, memory_id).decision == "keep"


@pytest.mark.parametrize("reply", [ExecutorResult(error="login"),
                                   {"decision": "maybe", "reason": "", "evidence": []}])
def test_a_failed_call_or_an_answer_off_the_schema_records_nothing(world, reply):
    memory_id = _aged(world, 200)
    world.executor.replies = [reply]
    result, text = _pass(world)
    kind = "login" if isinstance(reply, ExecutorResult) else "schema"
    assert not result.finished
    assert text == f"{memory_id}: not processed: {kind}\n"
    assert _review(world, memory_id) is None


def test_an_excluded_memory_is_never_a_candidate_nor_a_comparison(world):
    held = _aged(world, 200, body="held back by the policy scan")
    clean = _aged(world, 200, body="an old but clean fact")
    world.executor.replies = [_decision("keep")]
    _pass(world, excluded={held})
    assert len(world.executor.calls) == 1
    prompt = world.executor.calls[0]["prompt"]
    assert clean in prompt and held not in prompt and "held back" not in prompt


def test_one_unstorable_reason_fails_only_its_own_candidate(world):
    first = _aged(world, 201, body="the first old fact")
    second = _aged(world, 200, body="the second old fact")
    world.executor.replies = [_decision("delete", "gone " + chr(0xD800)), _decision("keep")]
    result, text = _pass(world)
    assert not result.finished
    assert f"{first}: invalid reason\n" in text and f"keep {second}:" in text
    assert _review(world, first) is None and not _deleted(world, first)


def test_a_reason_holding_a_secret_is_rejected_even_where_the_cut_would_drop_it(world):
    memory_id = _aged(world, 200)
    straddling = "x" * (DREAM_REASON_CHARS - 20) + " " + world.secret
    world.executor.replies = [_decision("delete", straddling)]
    result, text = _pass(world)
    assert not result.finished
    assert text == f"{memory_id}: rejected reason\n"
    assert _review(world, memory_id) is None and not _deleted(world, memory_id)


def test_a_long_reason_is_stored_on_one_line_within_the_limit(world):
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("keep", "line one\nline two " + "y" * 400)]
    _pass(world)
    reason = _review(world, memory_id).reason
    assert reason.startswith("line one line two") and len(reason) == DREAM_REASON_CHARS


def test_evidence_is_never_acted_on(world):
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("delete", "superseded", evidence=["0" * 26])]
    result, _ = _pass(world)
    assert result.finished and _deleted(world, memory_id)


def test_the_candidate_is_sent_with_its_sources_derived_entries_and_safe_times(world):
    memory_id = _aged(world, 200)
    world.sql("UPDATE memories SET updated = '0000-hand-edited' WHERE id = ?", memory_id)
    world.executor.replies = [_decision("keep")]
    _pass(world)
    prompt = world.executor.calls[0]["prompt"]
    assert "hand-edited" not in prompt and '"updated": ""' in prompt
    assert '"sources": []' in prompt and '"derived": []' in prompt


def test_the_prompt_asks_for_a_reason_to_retire_and_keeps_description_carried_rules(world):
    _aged(world, 200)
    world.executor.replies = [_decision("keep")]
    _pass(world)
    assert "not whether it was used" in world.executor.calls[0]["system_prompt"]
    assert "A rule whose description already carries it" in retire.SYSTEM_PROMPT


def test_a_candidate_hard_deleted_before_its_review_is_skipped_and_the_rest_reviewed(
        world, monkeypatch):
    # the run goes on with the other candidates
    victim = _aged(world, 201)
    kept = _aged(world, 200)
    usage = world.services.memory.usage
    pending = [victim]

    def usage_then_hard_delete(memory_ids):
        found = usage(memory_ids)
        if pending:
            plan = world.services.maintenance.plan_hard_delete(pending.pop())
            world.services.maintenance.hard_delete(plan.target, code=plan.code)
        return found

    monkeypatch.setattr(world.services.memory, "usage", usage_then_hard_delete)
    world.executor.replies = [_decision("keep")]
    result, text = _pass(world)
    assert not result.finished
    assert text == (f"{victim}: input changed; not processed\n"
                    f"keep {kept}: nothing contradicts it\n")
    assert len(world.executor.calls) == 1 and _review(world, kept) is not None


def test_a_huge_ttl_is_clamped_never_an_overflow(world):
    # every date shift goes through store.shift_days, which clamps
    memory_id = _aged(world, 200)
    huge = world.dream.model_copy(update={"ttl_days": 1_000_000_000})
    assert _candidates(world, settings=huge) == []
    (memory,) = world.services.memory.memories(world.project.id)
    world.executor.replies = [_decision("keep")]
    assert retire.review(world.context(settings=huge), memory, world.now)
    row = _review(world, memory_id)
    assert row.next_review_at == shift_days(world.now, 1_000_000_000)
    assert len(row.next_review_at) == len(world.now)          # still the fixed-width form


def test_no_candidate_is_said_so(world):
    world.create(world.project.id, "fresh")
    result, text = _pass(world)
    assert (result, text) == (PassResult(finished=True), "no candidate\n")
    assert world.executor.calls == []


def test_run_dream_runs_the_review_under_its_section(world):
    memory_id = _aged(world, 200)
    world.executor.replies = [_decision("keep")]
    row = world.run(phases={"retire"})
    assert f"== TTL ==\nkeep {memory_id}: nothing contradicts it\n" in world.report_text(row)
