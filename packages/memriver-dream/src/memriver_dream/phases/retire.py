"""Phase 3: retire memories unused past their TTL, after a model review (spec §8).

The TTL only nominates: the model is asked whether there is reason enough to
retire each candidate -- not whether it was used -- and core re-checks, when
it soft-deletes, that nothing read or changed it meanwhile.
"""

from __future__ import annotations

import json

from memriver_core import ContentRejected
from memriver_core.models import Candidate, Memory, Review, single_line, timestamp_shift

from ..calls import (
    DATA_RULE,
    PROMPT_VERSION,
    call,
    effective_sources,
    estimate_tokens,
    sendable_time,
    storable,
)
from ..protocols import Run
from ..report import PhaseReport
from ..settings import DREAM_REASON_CHARS

SYSTEM_PROMPT = (
    "You review one memory from a coding agent's long-term memory that has not been used for "
    "a long time. Decide whether there is enough reason to retire it -- not whether it was "
    "used. Answer delete only when the memory itself or the other memories show it is wrong, "
    "obsolete (it or another memory says what it describes is gone, replaced or "
    "decommissioned), duplicated or no longer relevant; keep when it may still hold; "
    "uncertain when you cannot tell. A rule whose description already carries it, and that nothing "
    "contradicts, is kept: it does its work without being read. List in evidence the ids of "
    "the memories your decision rests on.")
_PROMPT = ("The memory under review:\n<memory>\n{memory}\n</memory>\n\n"
           "The other memories of its project:\n<other-memories>\n{others}\n</other-memories>")
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["decision", "reason", "evidence"],
          "properties": {"decision": {"type": "string", "enum": ["keep", "delete", "uncertain"]},
                         "reason": {"type": "string"},
                         "evidence": {"type": "array", "items": {"type": "string"}}}}


def _candidate_entry(run: Run, memory: Memory) -> dict:
    return {"id": memory.id, "project": memory.project_id, "type": memory.type,
            "description": memory.description, "body": memory.body,
            "created": sendable_time(memory.created), "updated": sendable_time(memory.updated),
            "last_read_at": memory.last_read_at, "sources": effective_sources(run, memory.id),
            "derived": run.maintenance.derived_from(memory.id)}


def _other_text(memory: Memory) -> str:
    return json.dumps({"id": memory.id, "type": memory.type, "description": memory.description,
                       "body": memory.body, "updated": sendable_time(memory.updated)},
                      ensure_ascii=False)


def _review(run: Run, memory: Memory, decision: str, reason: str, streak: int) -> Review:
    return Review(memory_id=memory.id, memory_version=memory.version, decided_at=run.now,
                  decision=decision, reason=reason, uncertain_streak=streak,
                  next_review_at=timestamp_shift(run.now, days=run.dream.ttl_days),
                  run_id=run.run_id, executor=run.executor.name, prompt_version=PROMPT_VERSION)


def _reason(raw: str) -> str | None:
    """The model's reason as stored, or None when it may not be: the policy sees the
    whole reason, so the length limit cannot cut a secret into a form it misses."""
    reason = single_line(raw)
    if not storable(reason):
        return None
    return reason[:DREAM_REASON_CHARS] or "no reason given"


def review(run: Run, candidate: Candidate) -> tuple[str, dict | None]:
    memory = candidate.memory
    if not run.maintenance.passes_policy(memory):
        return "policy", None               # the safety re-scan normally caught it already
    others = [m for m in run.maintenance.memories(memory.project_id)
              if m.id != memory.id and run.maintenance.passes_policy(m)]
    candidate_text = json.dumps(_candidate_entry(run, memory), ensure_ascii=False)
    for _ in range(2):                      # a too-large answer: once more, half the comparison
        prompt = _PROMPT.format(memory=candidate_text,
                                others="\n".join(_other_text(m) for m in others))
        result = ("too-large"
                  if estimate_tokens(SYSTEM_PROMPT + DATA_RULE + prompt) > run.budget_tokens
                  else call(run.executor, system_prompt=SYSTEM_PROMPT, prompt=prompt,
                            schema=SCHEMA))
        if result != "too-large" or not others:
            break
        others = others[:len(others) // 2]
    if isinstance(result, str):
        return result, None                 # nothing recorded; the next run asks again
    # evidence is neither stored nor acted on: only the candidate, at the version sent, is
    reason = _reason(result["reason"])
    if reason is None:
        return "invalid", None
    if not run.maintenance.text_passes_policy(result["reason"]):
        return "rejected", None
    decision = result["decision"]
    streak = 0
    if decision == "uncertain":
        # "in a row" means judgments of the same content (D24): any edit starts over
        previous = candidate.review
        same = previous is not None and previous.decision == "uncertain" \
            and previous.memory_version == memory.version
        streak = 1 + (previous.uncertain_streak if same else 0)
    if decision == "keep" or (decision == "uncertain" and streak < run.dream.uncertain_limit):
        try:
            recorded = run.maintenance.record_review(
                _review(run, memory, decision, reason, streak))
        except ContentRejected:
            return "rejected", None
        if not recorded:
            return "moved", None            # edited, deleted or purged while it was judged
        return decision, {"memory_id": memory.id, "decision": decision}
    try:
        change_id = run.maintenance.retire(
            memory.id, judged_version=memory.version, ttl_days=run.dream.ttl_days,
            multiplier_max=run.dream.ttl_read_multiplier_max, now=run.now,
            review=_review(run, memory, "delete", reason, streak))
    except ContentRejected:
        return "rejected", None
    if change_id is None:
        return "moved", None                # read or changed while it was judged
    return "retired", None                  # the report reads the change from the change log


def run(run: Run, phase: PhaseReport) -> None:
    for candidate in run.maintenance.ttl_candidates(run.now, run.dream.ttl_days,
                                                    run.dream.ttl_read_multiplier_max,
                                                    run.dream.max_candidates_per_run):
        outcome, item = review(run, candidate)
        phase.record(outcome, item)
        run.log(f"retire {candidate.memory.id}: {outcome}")
