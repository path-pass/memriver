"""TTL (spec §6.7): memories unused past their TTL, reviewed by the model, one judgment
each per run.

The TTL only nominates: a current memory is a candidate once max(created, updated,
last read) is ttl_days x min(1 + reads, ttl_read_multiplier_max) days old, unless a
review of its current version is not due yet. The model is asked whether there is
reason enough to retire it -- not whether it was used. keep and uncertain are
recorded in dream.db, an uncertain streak counting judgments of the same version
only; delete, or a streak reaching uncertain_limit, soft-deletes it through apply
with unread_since = the listing time, so a read after the listing makes the delete
a conflict, which records nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from memriver_core.models import is_timestamp
from memriver_core.models import now as clock
from memriver_core.models.changes import SoftDelete
from memriver_core.models.errors import MemoryNotFound

from ..calls import sendable_time
from ..changes import apply_group
from ..store import ReviewRow, shift_days
from . import PassResult
from .consolidate import (
    INPUT_CHANGED,
    ask,
    current_sources,
    details,
    input_estimate,
    reason_problem,
    shown,
    too_large,
    usable,
)

if TYPE_CHECKING:
    from memriver_core.models import Memory

    from ..run import Context

SYSTEM_PROMPT = (
    "You review one memory from a coding agent's long-term memory that has not been used for "
    "a long time. Decide whether there is enough reason to retire it -- not whether it was "
    "used. Answer delete only when the memory itself or the other memories show it is wrong, "
    "obsolete (it or another memory says what it describes is gone, replaced or "
    "decommissioned), duplicated or no longer relevant; keep when it may still hold; "
    "uncertain when you cannot tell. A rule whose description already carries it, and that nothing "
    "contradicts, is kept: it does its work without being read. List in evidence the ids of "
    "the memories your decision rests on.")
PROMPT = ("The memory under review:\n<memory>\n{memory}\n</memory>\n\n"
          "The other memories of its project:\n<other-memories>\n{others}\n</other-memories>")
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["decision", "reason", "evidence"],
          "properties": {"decision": {"type": "string", "enum": ["keep", "delete", "uncertain"]},
                         "reason": {"type": "string"},
                         "evidence": {"type": "array", "items": {"type": "string"}}}}


def candidates(ctx: Context) -> tuple[str, list[Memory]]:
    """The listing time (the soft delete's unread_since) and the candidates, oldest
    first, at most max_candidates_per_run.

    created and updated must be valid timestamps to prove an age at all (old or
    hand-edited data can hold anything): a memory with either one malformed has no
    provable age and is skipped, never retired on an unknown age. A malformed or
    absent last_read_at simply does not count towards the age.
    """
    listed_at = clock()
    settings = ctx.settings
    memories = usable(ctx, None)
    usage = ctx.services.memory.usage([memory.id for memory in memories])
    due = []
    for memory in memories:
        if not (is_timestamp(memory.created) and is_timestamp(memory.updated)):
            ctx.report.line(f"skipped {memory.id}: unknown time")
            continue
        use = usage.get(memory.id)
        reads = 0 if use is None else use.reads
        last_read = use.last_read_at if use is not None and is_timestamp(use.last_read_at) \
            else None
        last = max(memory.created, memory.updated, last_read or "")
        days = settings.ttl_days * min(1 + reads, settings.ttl_read_multiplier_max)
        if last > shift_days(ctx.now, -days):
            continue
        review = ctx.store.review(memory.id)
        if review is not None and review.memory_version == memory.version \
                and review.next_review_at > ctx.now:
            continue                    # judged at this version, not due again yet
        due.append((last, memory.id, memory))
    due.sort(key=lambda item: item[:2])
    return listed_at, [memory for _, _, memory in due[:settings.max_candidates_per_run]]


def _time(value: str | None) -> str | None:
    return None if value is None else sendable_time(value)


def _grouped(ctx: Context) -> tuple[set[str], dict[str, list[Memory]]]:
    """Every usable memory's id, and the usable memories of every project grouped by
    project id -- computed once per run, not rescanned for every candidate."""
    memories = usable(ctx, None)
    by_project: dict[str, list[Memory]] = {}
    for memory in memories:
        by_project.setdefault(memory.project_id, []).append(memory)
    return {memory.id for memory in memories}, by_project


def _candidate_entry(ctx: Context, memory: Memory, live: set[str]) -> dict:
    # a citation's `current` flag only says it was recorded at the citing memory's
    # latest version, not that the citing memory is still live: a soft-deleted
    # memory's last version is still "current" by that flag, so `live` (every usable
    # memory's id) filters it out the same way usable() would leave it out
    return {"id": memory.id, "project": memory.project_id, "type": memory.type,
            "description": memory.description, "body": memory.body,
            "created": sendable_time(memory.created), "updated": sendable_time(memory.updated),
            "last_read_at": _time(memory.last_read_at),
            "sources": sorted({source.memory_id for source in current_sources(ctx, memory)}),
            "derived": sorted({citation.memory_id
                               for citation in ctx.services.memory.citing(memory.id)
                               if citation.current and citation.memory_id in live})}


def _other_text(memory: Memory) -> str:
    return json.dumps({"id": memory.id, "type": memory.type, "description": memory.description,
                       "body": memory.body, "updated": sendable_time(memory.updated)},
                      ensure_ascii=False)


def review(ctx: Context, memory: Memory, listed_at: str, live: set[str],
          by_project: dict[str, list[Memory]]) -> bool:
    """One candidate judged and its judgment recorded or carried out; False when
    nothing could be recorded (the next run asks again). `live` and `by_project` come
    from `_grouped`, computed once per run."""
    report, settings = ctx.report, ctx.settings
    others = [other for other in by_project.get(memory.project_id, ()) if other.id != memory.id]
    try:
        candidate = json.dumps(_candidate_entry(ctx, memory, live), ensure_ascii=False)
    except MemoryNotFound:
        # hard-deleted since the listing (a human may, during a run): nothing to judge
        report.line(f"{memory.id}: {INPUT_CHANGED}")
        return False
    for _ in range(2):                  # a too-large answer: once more, half the comparison
        prompt = PROMPT.format(memory=candidate,
                               others="\n".join(_other_text(other) for other in others))
        result = ask(ctx, SYSTEM_PROMPT, prompt, SCHEMA)
        if result != "too-large" or not others:
            break
        others = others[:len(others) // 2]
    if isinstance(result, str):
        if result == "too-large":           # final: after the halved retry
            too_large(ctx, memory.id, input_estimate(SYSTEM_PROMPT, prompt),
                      ctx.budget_tokens)
        report.line(f"{memory.id}: not processed: {result}")
        return False
    # evidence is neither stored nor acted on: only the candidate, at the version sent, is
    problem = reason_problem(ctx, result["reason"])
    if problem is not None:
        report.line(f"{memory.id}: {problem} reason")
        return False
    reason, decision, streak, by_streak = shown(result["reason"]), result["decision"], 0, False
    if decision == "uncertain":
        # "in a row" means judgments of the same content (D24): any new version starts over
        previous = ctx.store.review(memory.id)
        same = previous is not None and previous.decision == "uncertain" \
            and previous.memory_version == memory.version
        streak = 1 + (previous.uncertain_streak if same else 0)
        if streak >= settings.uncertain_limit:
            decision, by_streak = "delete", True
    if decision == "delete":
        ops = [SoftDelete(memory_id=memory.id, expected_version=memory.version,
                          unread_since=listed_at)]
        # retire reviews every project and global: only a global memory's retirement
        # is listed under Needs you
        touches_global = memory.project_id == ctx.services.project.global_project_id()
        if apply_group(ctx, "retire", [memory.id], ops, touches_global=touches_global) is None:
            return False                # read or changed since the listing: nothing recorded
        # a later candidate of this run must not see it as a live comparison or dependent
        live.discard(memory.id)
        by_project[memory.project_id] = [other for other in by_project.get(memory.project_id, [])
                                         if other.id != memory.id]
        details(ctx, memory.description,
               f"{streak} uncertain reviews in a row" if by_streak else reason)
        return True
    ctx.store.put_review(ReviewRow(memory.id, memory.version, decision, streak, ctx.now,
                                   shift_days(ctx.now, settings.ttl_days), reason))
    label = (f"uncertain {memory.id} ({streak} of {settings.uncertain_limit})"
             if decision == "uncertain" else f"keep {memory.id}")
    report.line(f"{label}: {reason}")
    return True


def run(ctx: Context) -> PassResult:
    listed_at, due = candidates(ctx)
    if not due:
        ctx.report.line("no candidate")
    live, by_project = _grouped(ctx)
    finished = True
    for memory in due:
        finished = review(ctx, memory, listed_at, live, by_project) and finished
    return PassResult(finished=finished)
