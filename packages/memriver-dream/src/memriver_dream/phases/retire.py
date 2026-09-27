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
    reason_problem,
    shown,
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
    first, at most max_candidates_per_run."""
    listed_at = clock()
    settings = ctx.settings
    memories = usable(ctx, None)
    usage = ctx.services.memory.usage([memory.id for memory in memories])
    due = []
    for memory in memories:
        use = usage.get(memory.id)
        reads = 0 if use is None else use.reads
        last_read = None if use is None else use.last_read_at
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


def _candidate_entry(ctx: Context, memory: Memory) -> dict:
    return {"id": memory.id, "project": memory.project_id, "type": memory.type,
            "description": memory.description, "body": memory.body,
            "created": sendable_time(memory.created), "updated": sendable_time(memory.updated),
            "last_read_at": _time(memory.last_read_at),
            "sources": sorted({source.memory_id for source in current_sources(ctx, memory)}),
            "derived": sorted({citation.memory_id
                               for citation in ctx.services.memory.citing(memory.id)
                               if citation.current})}


def _other_text(memory: Memory) -> str:
    return json.dumps({"id": memory.id, "type": memory.type, "description": memory.description,
                       "body": memory.body, "updated": sendable_time(memory.updated)},
                      ensure_ascii=False)


def review(ctx: Context, memory: Memory, listed_at: str) -> bool:
    """One candidate judged and its judgment recorded or carried out; False when
    nothing could be recorded (the next run asks again)."""
    report, settings = ctx.report, ctx.settings
    others = [other for other in usable(ctx, memory.project_id) if other.id != memory.id]
    try:
        candidate = json.dumps(_candidate_entry(ctx, memory), ensure_ascii=False)
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
        report.line(f"{memory.id}: not processed: {result}")
        return False
    # evidence is neither stored nor acted on: only the candidate, at the version sent, is
    problem = reason_problem(ctx, result["reason"])
    if problem is not None:
        report.line(f"{memory.id}: {problem} reason")
        return False
    reason, decision, streak = shown(result["reason"]), result["decision"], 0
    if decision == "uncertain":
        # "in a row" means judgments of the same content (D24): any new version starts over
        previous = ctx.store.review(memory.id)
        same = previous is not None and previous.decision == "uncertain" \
            and previous.memory_version == memory.version
        streak = 1 + (previous.uncertain_streak if same else 0)
        if streak >= settings.uncertain_limit:
            decision = "delete"
    if decision == "delete":
        ops = [SoftDelete(memory_id=memory.id, expected_version=memory.version,
                          unread_since=listed_at)]
        if apply_group(ctx, "retire", [memory.id], ops) is None:
            return False                # read or changed since the listing: nothing recorded
        details(ctx, memory.description, reason)
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
    finished = True
    for memory in due:
        finished = review(ctx, memory, listed_at) and finished
    return PassResult(finished=finished)
