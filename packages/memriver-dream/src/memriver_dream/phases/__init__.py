"""The run's phases (spec §6.2–§6.7), called by run_dream in spec order, and the input
and output helpers they share.

Each phase module has `run(ctx) -> PassResult`, except consolidate, which works on
one scope per call: `run(ctx, project_id, scope)`. A phase imports Context only for
type checking: run.py imports the phases. This module imports no phase module.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memriver_core.models import single_line
from memriver_core.models.changes import SourceRef

from ..calls import DATA_RULE, call, estimate_tokens, sendable_time, storable
from ..settings import DREAM_REASON_CHARS

if TYPE_CHECKING:
    from memriver_core.models import Memory

    from ..run import Context

EXTRACTION_SCOPE = "extraction"     # scope_passes key of the global layer's extraction
GLOBAL_SCOPE = "global"             # scope_passes key of the project layer run on global
# an ordinary project's key is f"project:{project_id}", built by run_dream

TYPES = ("user", "feedback", "project", "reference")
INVALID, REFUSED = "invalid", "refused"
# a memory listed for this input was hard-deleted before the rest of the input was read
# (a human may, during a run): the item is left unfinished, with no digest, for the next run
INPUT_CHANGED = "input changed; not processed"


@dataclass(frozen=True)
class PassResult:
    """What a phase hands back to run_dream.

    `finished` (§6.9): every judgment was valid and either applied or legitimately
    no-op/report-only, no group was cut by max_groups_per_run, no executor failure,
    no BatchConflict, no policy refusal. `digest`: the scope's input digest
    (store.input_digest over the (memory_id, version) pairs sent), captured when the
    input was built; None when the phase sent nothing for its scope (digest unchanged,
    empty input) or keeps no scope digest. run_dream stores `digest` for the scope
    only when `finished` is true.
    """

    finished: bool
    digest: str | None = None


# --- helpers shared with the global layer and TTL --------------------------------

def usable(ctx: Context, project_id: str | None) -> list[Memory]:
    """The current, non-deleted memories of `project_id` (None: every project and global)
    that the policy scan did not exclude (§6.2)."""
    return [memory for memory in ctx.services.memory.memories(project_id)
            if memory.id not in ctx.excluded]


def current_sources(ctx: Context, memory: Memory) -> tuple[SourceRef, ...]:
    """The source set of `memory` at the version it was read at. Raises MemoryNotFound
    when it was hard-deleted since it was listed; callers treat that as INPUT_CHANGED,
    never as an empty source set."""
    by_version = {version.version: version.sources
                  for version in ctx.services.memory.versions(memory.id)}
    return by_version.get(memory.version, ())


def entry(memory: Memory, sources: Sequence[SourceRef], **extra: object) -> str:
    """One memory as it is sent: one JSON line. Times the policy does not check go
    through sendable_time."""
    return json.dumps({"id": memory.id, "version": memory.version, "type": memory.type,
                       "description": memory.description, "body": memory.body,
                       "created": sendable_time(memory.created),
                       "updated": sendable_time(memory.updated), **extra,
                       "sources": sorted({source.memory_id for source in sources})},
                      ensure_ascii=False)


def input_estimate(system_prompt: str, prompt: str) -> int:
    """The estimated tokens one call's input takes, as ask() measures it."""
    return estimate_tokens(system_prompt + DATA_RULE + prompt)


def ask(ctx: Context, system_prompt: str, prompt: str, schema: dict) -> dict | str:
    """The parsed answer, or the kind of failure; input over the room is "too-large"
    without a call (spec §5.4: the input is never cut to fit)."""
    if input_estimate(system_prompt, prompt) > ctx.budget_tokens:
        return "too-large"
    return call(ctx.executor, system_prompt=system_prompt, prompt=prompt, schema=schema)


def too_large(ctx: Context, subject: str, estimate: int, room: int) -> None:
    """The Needs-you line for an input still too large once its phase gave up (after
    any halving). `estimate > room`: our own estimate rejected it before any call was
    made, and the user's lever is to raise the budget. `estimate <= room`: the input
    fit our estimate but the executor itself refused it -- raising the budget would
    not help, so the advice is to lower it instead, closer to what the executor
    actually accepts."""
    if estimate > room:
        ctx.report.needs_you(f"{subject}: input too large ({estimate}/{room} tokens); not "
                             "processed — raise [dream] context_budget_tokens")
    else:
        ctx.report.needs_you(f"{subject}: the executor refused the input as too large "
                             f"({estimate}/{room} tokens); not processed — lower [dream] "
                             "context_budget_tokens")


def near_budget(ctx: Context, scope: str, estimate: int) -> None:
    """The Needs-you line for a sent input above 70% of the room: the next growth of
    this scope may not fit."""
    room = ctx.budget_tokens
    if room * 7 < estimate * 10 <= room * 10:
        ctx.report.needs_you(f"{scope}: input at {estimate * 100 // room}% of the budget "
                             f"({estimate}/{room} tokens)")


@dataclass(frozen=True)
class Problem:
    """Why a judgment is not carried out. INVALID: malformed output -- an id that was not
    sent, text no UTF-8 column takes -- so the pass does not finish (§6.9). REFUSED:
    well-formed, but a counted rule refuses it; the same input would be refused again,
    so it is reported and the pass may still finish."""

    outcome: str
    field: str

    def report(self, ctx: Context, kind: str, subject: str = "") -> bool:
        """Reported in the scope's section; True when the pass may still finish."""
        ctx.report.line(f"{self.outcome} {kind}{subject}: {self.field}")
        return self.outcome == REFUSED


def ids_problem(ids: Sequence[str], sent: dict[str, Memory], minimum: int,
                field: str) -> Problem | None:
    """An id that was not sent is invalid output; fewer than `minimum` ids, or one
    named twice, is refused."""
    if not all(memory_id in sent for memory_id in ids):
        return Problem(INVALID, field)
    if len(ids) < minimum or len(set(ids)) != len(ids):
        return Problem(REFUSED, field)
    return None


def reason_problem(ctx: Context, raw: str) -> str | None:
    """Why a model reason may not be used: "invalid" when no UTF-8 file takes it,
    "rejected" when the content policy hits it -- checked whole, before any cut, so a
    cut can never hide a secret from the check."""
    if not storable(raw):
        return "invalid"
    if ctx.services.maintenance.check_text(raw) is not None:
        return "rejected"
    return None


def shown(raw: str) -> str:
    """A reason reason_problem let through, as reported and stored: one line, cut."""
    return single_line(raw)[:DREAM_REASON_CHARS] or "no reason given"


def text_problem(raw: dict) -> Problem | None:
    """A description or body no UTF-8 column takes is invalid; a blank one is refused."""
    texts = (raw["description"], raw["body"])
    if not all(storable(text) for text in texts):
        return Problem(INVALID, "text")
    if not all(text.strip() for text in texts):
        return Problem(REFUSED, "text")
    return None


def details(ctx: Context, description: str, reason: str) -> None:
    """The lines under an applied change: the entry's description and the reason."""
    ctx.report.line(f'  description: "{ctx.report.safe(description)}"')
    ctx.report.line(f"  reason: {reason}")


def split_no_change(ctx: Context, judgments: list[dict]) -> tuple[list[dict], bool]:
    """`judgments` without its no_change entries, and whether the pass may still
    finish. no_change is dropped, not judged, but its reason is checked exactly like
    every other kind's (§6.9): a malformed or policy-hit reason must not let the pass
    finish and the scope's digest get stored unnoticed. Shared by consolidate and
    extract."""
    finished = True
    kept = []
    for raw in judgments:
        if raw["kind"] != "no_change":
            kept.append(raw)
            continue
        problem = reason_problem(ctx, raw["reason"])
        if problem is not None:
            ctx.report.line(f"{problem} no_change: reason")
            finished = False
    return kept, finished
