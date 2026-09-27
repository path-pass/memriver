"""Global layer, source re-check (spec §6.6, U10): a global entry whose cited sources
changed since it cited them is judged again against the change.

Eligible: every current global entry the policy scan did not exclude with a cited
source whose current state differs from the version cited (updated or soft-deleted).
One call per entry sends it at its current version and, for every changed source,
the version cited and the source's current state; a soft-deleted source comes with
its successors -- memories that are current, not deleted, not excluded, not the
entry itself, and whose current source set cites it. A changed source whose needed
version the policy scan holds back skips the entry this run. The re-check digest is
over exactly the (id, version) pairs sent; `source_checks` holding it skips the call.
This runs every run, gated by `source_checks` alone, never by the extraction skip.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from memriver_core.models.changes import SourceRef, Update
from memriver_core.models.errors import MemoryNotFound

from ..changes import apply_group
from ..store import input_digest
from . import PassResult
from .consolidate import (
    INPUT_CHANGED,
    INVALID,
    REFUSED,
    Problem,
    ask,
    current_sources,
    details,
    reason_problem,
    shown,
    text_problem,
)

if TYPE_CHECKING:
    from memriver_core.models import Memory

    from ..run import Context

SYSTEM_PROMPT = (
    "You re-check one entry of the global memory a coding agent shares across projects. "
    "Some memories it cites as sources changed since it cited them: for each you get the "
    "version it cited and the source's current state, and for a retired source the "
    "memories that now carry it forward (its successors). Decide whether the entry still "
    "holds. "
    "keep: it still holds as written. "
    "refresh: it still holds as written and its sources should point to the new state -- "
    "list replacements, each a changed source with by = the same id (its current version) "
    "or, for a retired source, by = one of its successors. "
    "revise: the new versions or successors show it needs new wording -- give the new "
    "description and body and the replacements, at least one. "
    "overturned: the changes show it no longer holds; the user decides. "
    "A revision must be supported by the new versions or successors given, never by "
    "anything else; write principles, not concrete commands, and keep the condition under "
    "which it holds. When in doubt, answer keep or overturned. Give id = the entry's id "
    "and a one-sentence reason that says why, without copying memory text; leave the "
    "fields a decision does not use empty (\"\" or []).")
PROMPT = ("The global entry:\n<global-entry>\n{entry}\n</global-entry>\n\n"
          "Its changed sources:\n<changed-sources>\n{changes}\n</changed-sources>")

_REPLACEMENT = {"type": "object", "additionalProperties": False, "required": ["source", "by"],
                "properties": {"source": {"type": "string"}, "by": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["decision", "id", "description", "body", "replacements", "reason"],
          "properties": {
              "decision": {"type": "string",
                           "enum": ["keep", "refresh", "revise", "overturned"]},
              "id": {"type": "string"},
              "description": {"type": "string"},          # revise
              "body": {"type": "string"},                 # revise
              "replacements": {"type": "array", "items": _REPLACEMENT},   # refresh, revise
              "reason": {"type": "string"}}}


def _moved(source: Memory, ref: SourceRef) -> bool:
    """Whether a cited source's current state is not the version cited."""
    return source.deleted_at is not None or source.version != ref.version


def _held_back(ctx: Context, source: Memory, ref: SourceRef) -> bool:
    """§6.2: a version this re-check must send -- the one cited, and an updated
    source's current one -- is held back by the policy scan."""
    if ref.version in ctx.history_hits.get(source.id, set()):
        return True
    return source.deleted_at is None and source.id in ctx.excluded


def _successors(ctx: Context, source_id: str, memory: Memory,
                everything: dict[str, Memory]) -> list[Memory]:
    """Current, non-deleted, not excluded memories other than `memory` whose current
    source set cites `source_id`."""
    found: dict[str, Memory] = {}
    for citation in ctx.services.memory.citing(source_id):
        citing = everything.get(citation.memory_id)
        if citing is not None and citing.version == citation.version \
                and citing.deleted_at is None and citing.id not in ctx.excluded \
                and citing.id != memory.id:
            found[citing.id] = citing
    return [found[memory_id] for memory_id in sorted(found)]


def _state(memory: Memory) -> dict:
    return {"id": memory.id, "version": memory.version, "type": memory.type,
            "description": memory.description, "body": memory.body}


def _judge(ctx: Context, raw: dict, memory: Memory, sources: tuple[SourceRef, ...],
           allowed: dict[str, set[str]], everything: dict[str, Memory], digest: str) -> bool:
    """The decision validated and carried out; False when it did not finish (a refused
    decision is reported and counts as finished). `allowed` maps each changed source to
    the ids it may be replaced by."""
    report, decision, subject = ctx.report, raw["decision"], f" {memory.id}"
    if raw["id"] != memory.id:
        return Problem(INVALID, "id").report(ctx, decision, subject)
    problem = reason_problem(ctx, raw["reason"])
    if problem is not None:
        report.line(f"{problem} {decision} {memory.id}: reason")
        return False
    reason = shown(raw["reason"])
    if decision == "keep":
        ctx.store.put_source_check(memory.id, digest, ctx.now)
        report.line(f"keep {memory.id}: {reason}")
        return True
    if decision == "overturned":
        report.line(f"overturned {memory.id}: reported under Needs you")
        report.needs_you(f"overturned global entry {memory.id}: {reason}")
        return True
    replacements = raw["replacements"]
    replaced = [item["source"] for item in replacements]
    # naming a source or a successor that was not sent is malformed output; none, or
    # one source twice, is refused
    if not all(item["by"] in allowed.get(item["source"], ()) for item in replacements):
        return Problem(INVALID, "replacements").report(ctx, decision, subject)
    # refresh never uses text at all; checking it here (before it is otherwise ready
    # to send) would refuse it on fields it never fills
    if decision != "refresh" and (problem := text_problem(raw)):
        return problem.report(ctx, decision, subject)
    if not replacements or len(set(replaced)) != len(replaced):
        return Problem(REFUSED, "replacements").report(ctx, decision, subject)
    cited = {source.memory_id: source.version for source in sources}
    for source_id in replaced:
        del cited[source_id]
    for item in replacements:
        cited[item["by"]] = everything[item["by"]].version      # the version sent
    refs = tuple(SourceRef(memory_id, version) for memory_id, version in sorted(cited.items()))
    if decision == "refresh":
        update = Update(memory_id=memory.id, expected_version=memory.version, sources=refs)
        description = memory.description
    else:
        update = Update(memory_id=memory.id, expected_version=memory.version,
                        description=raw["description"].strip(), body=raw["body"].strip(),
                        sources=refs)
        description = update.description
    if apply_group(ctx, decision, [memory.id], [update]) is None:
        return False
    details(ctx, description, reason)
    return True


def _material(ctx: Context, memory: Memory, changed: list[SourceRef],
              everything: dict[str, Memory]
              ) -> tuple[list[tuple[str, int]], dict[str, set[str]], list[str]]:
    """What one re-check sends: the (id, version) pairs, the ids each changed source
    may be replaced by, and one JSON line per changed source. Raises MemoryNotFound
    when a memory it reads was hard-deleted during the run."""
    pairs = [(memory.id, memory.version)]
    allowed: dict[str, set[str]] = {}
    lines = []
    for ref in changed:
        source = everything[ref.memory_id]
        cited = {version.version: version
                 for version in ctx.services.memory.versions(source.id)}[ref.version]
        pairs += [(source.id, ref.version), (source.id, source.version)]
        item = {"source": source.id,
                "cited": {"version": ref.version, "description": cited.description,
                          "body": cited.body}}
        if source.deleted_at is None:
            allowed[source.id] = {source.id}
            item["current"] = {"version": source.version, "deleted": False,
                               "description": source.description, "body": source.body}
            item["successors"] = []
        else:
            successors = _successors(ctx, source.id, memory, everything)
            allowed[source.id] = {successor.id for successor in successors}
            pairs += [(successor.id, successor.version) for successor in successors]
            item["current"] = {"version": source.version, "deleted": True,
                               "description": "", "body": ""}
            item["successors"] = [_state(successor) for successor in successors]
        lines.append(json.dumps(item, ensure_ascii=False))
    return pairs, allowed, lines


def _recheck(ctx: Context, memory: Memory, everything: dict[str, Memory]) -> bool | None:
    """None when no cited source changed; else whether the re-check finished."""
    report = ctx.report
    try:
        sources = current_sources(ctx, memory)
        missing = [ref.memory_id for ref in sources if ref.memory_id not in everything]
        if missing:                         # never read as unchanged
            raise MemoryNotFound(missing[0])
        changed = [ref for ref in sources if _moved(everything[ref.memory_id], ref)]
        if not changed:
            return None
        held = [ref.memory_id for ref in changed
                if _held_back(ctx, everything[ref.memory_id], ref)]
        if held:
            report.line(f"{memory.id}: skipped (policy: {' '.join(held)})")
            return False
        pairs, allowed, lines = _material(ctx, memory, changed, everything)
    except MemoryNotFound:
        # hard-deleted since the listing (a human may, during a run): nothing is
        # stored, and the entry, if it is still there, is re-checked next run
        report.line(f"{memory.id}: {INPUT_CHANGED}")
        return False
    digest = input_digest(pairs)
    if ctx.store.source_check(memory.id) == digest:
        report.line(f"{memory.id}: unchanged since its last keep")
        return True
    prompt = PROMPT.format(entry=json.dumps(_state(memory), ensure_ascii=False),
                           changes="\n".join(lines))
    result = ask(ctx, SYSTEM_PROMPT, prompt, SCHEMA)
    if isinstance(result, str):
        report.line(f"{memory.id}: not processed: {result}")
        return False
    return _judge(ctx, result, memory, sources, allowed, everything, digest)


def run(ctx: Context) -> PassResult:
    global_id = ctx.services.project.global_project_id()
    if global_id is None:
        ctx.report.line("no global project")
        return PassResult(finished=True)
    everything = {memory.id: memory
                  for memory in ctx.services.memory.memories(include_deleted=True)}
    finished, checked = True, 0
    for memory in sorted(everything.values(), key=lambda memory: memory.id):
        if memory.project_id != global_id or memory.deleted_at is not None \
                or memory.id in ctx.excluded:
            continue
        outcome = _recheck(ctx, memory, everything)
        if outcome is None:
            continue
        checked += 1
        finished = outcome and finished
    if not checked:
        ctx.report.line("no global entry with a changed source")
    return PassResult(finished=finished)
