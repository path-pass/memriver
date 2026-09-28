"""Global layer, extraction (spec §6.5): shared principles from the ordinary projects
into global.

Input: every project's and global's current memories the policy scan did not
exclude; the pass is skipped when their digest equals the extraction scope's. Code
admits a new or grown global entry only when its sources trace to at least two
distinct ordinary projects (C4): a project memory counts its own project; a global
memory counts, through its cited versions and recursively, the projects they trace
to, and never a project of its own. Fewer than two is refused and goes to
"Needs you".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from memriver_core.models.changes import Create, SourceRef, Update
from memriver_core.models.errors import MemoryNotFound

from ..changes import apply_group
from ..store import input_digest
from . import EXTRACTION_SCOPE, PassResult
from .consolidate import (
    INPUT_CHANGED,
    INVALID,
    REFUSED,
    TYPES,
    Problem,
    ask,
    current_sources,
    details,
    entry,
    ids_problem,
    reason_problem,
    shown,
    split_no_change,
    text_problem,
    usable,
)

if TYPE_CHECKING:
    from memriver_core.models import Memory
    from memriver_core.models.changes import Op

    from ..run import Context

SYSTEM_PROMPT = (
    "You extract shared principles from the long-term memories a coding agent keeps for "
    "several projects into the global memory every project reads. The memories given are "
    "the only evidence. Each entry names its project, or \"global\" for an entry already "
    "in the global memory, and the ids of the memories it cites as sources. Answer with "
    "judgments of these kinds. "
    "new: a principle that the memories of at least two projects show -- give a global "
    "memory that states it and name those memories in source_ids; a global entry among "
    "them counts the projects its own sources come from, never a project of its own. "
    "supplement: an existing global entry (id) is right but incomplete, and memories not "
    "yet among its sources add to it -- give its new description and body and name the new "
    "memories in source_ids; its current sources are kept for you. "
    "add_sources: memories not yet among an existing global entry's sources (id) show the "
    "same principle -- name them in source_ids; its text stays. "
    "no_change: nothing to extract. "
    "Write principles, not concrete commands: \"Python projects prefer pytest for tests\", "
    "never \"pytest -q\". Keep the condition under which a principle holds, and word it so "
    "that it reads right in any project; never make a project-local requirement global "
    "without its condition, and never extract what is about one project itself -- its "
    "code, hosts, ports or names. Prefer supplementing or adding sources to an existing "
    "entry over a new one, and never reword an entry without new evidence. When in doubt, "
    "answer no_change. Each judgment has a one-sentence reason that names ids and says "
    "why, without copying memory text. Fill only the fields its kind uses; leave the "
    "others empty (\"\" or []).")
PROMPT = "Memories of every project and of global:\n<memories>\n{entries}\n</memories>"

_JUDGMENT = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "id", "type", "description", "body", "source_ids", "reason"],
    "properties": {
        "kind": {"type": "string", "enum": ["new", "supplement", "add_sources", "no_change"]},
        "id": {"type": "string"},                     # supplement, add_sources
        "type": {"type": "string", "enum": [*TYPES, ""]},       # new
        "description": {"type": "string"},            # new, supplement
        "body": {"type": "string"},                   # new, supplement
        "source_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["judgments"],
          "properties": {"judgments": {"type": "array", "items": _JUDGMENT}}}


def traced_projects(ctx: Context, global_id: str, refs: Iterable[SourceRef],
                    owner: dict[str, str]) -> set[str] | None:
    """The distinct ordinary projects `refs` trace to (C4), through every cited
    version -- a global memory's sources as recorded on the version cited, deleted
    memories and old versions included. `owner` maps every memory id to its project
    id, built once per run. None when a memory on the way was hard-deleted during the
    run: the input changed, and a vanished memory is never counted as one without
    sources."""
    found: set[str] = set()
    seen: set[SourceRef] = set()
    # a global memory's whole version→sources history, read at most once per id for
    # this trace: the same memory is often reached through more than one path
    history: dict[str, dict[int, tuple[SourceRef, ...]]] = {}
    stack = list(refs)
    while stack:
        ref = stack.pop()
        if ref in seen:
            continue
        if ref.memory_id not in owner:
            return None
        seen.add(ref)
        if owner[ref.memory_id] != global_id:
            found.add(owner[ref.memory_id])
            continue
        cited = history.get(ref.memory_id)
        if cited is None:
            try:
                cited = {version.version: version.sources
                         for version in ctx.services.memory.versions(ref.memory_id)}
            except MemoryNotFound:
                return None
            history[ref.memory_id] = cited
        stack.extend(cited.get(ref.version, ()))
    return found


def _plan(raw: dict, global_id: str, sent: dict[str, Memory],
          sources: dict[str, tuple[SourceRef, ...]]
          ) -> tuple[list[str], tuple[SourceRef, ...], list[Op], str] | Problem:
    """(items for the report, the resulting source set, ops, description), or why it
    fails: INVALID for malformed output (an id not sent, unstorable text), REFUSED for
    a counted rule. Every id a judgment uses is checked against `sent` before any
    REFUSED outcome, so a rule refusal never carries an id the model invented -- the
    Needs-you line it feeds only ever names ids that were sent."""
    kind, new = raw["kind"], raw["source_ids"]
    if kind == "new":
        if problem := ids_problem(new, sent, 1, "source_ids"):
            return problem
        if problem := text_problem(raw):
            return problem
        if raw["type"] not in TYPES:
            return Problem(REFUSED, "type")
        refs = tuple(SourceRef(memory_id, sent[memory_id].version) for memory_id in new)
        create = Create(project_id=global_id, type=raw["type"],
                        description=raw["description"].strip(), body=raw["body"].strip(),
                        sources=refs)
        return [], refs, [create], create.description
    target = sent.get(raw["id"])
    if target is None:
        return Problem(INVALID, "id")
    if problem := ids_problem(new, sent, 1, "source_ids"):
        return problem
    # add_sources never uses text at all; checking it here (before it is otherwise
    # ready to send) would refuse it on fields it never fills
    if kind == "supplement" and (problem := text_problem(raw)):
        return problem
    if target.project_id != global_id:
        return Problem(REFUSED, "id")           # only a global entry grows
    cited = {source.memory_id: source.version for source in sources[target.id]}
    grown = cited | {memory_id: sent[memory_id].version for memory_id in new}
    if target.id in new or grown == cited:
        return Problem(REFUSED, "source_ids")   # no new evidence: nothing to cite, no rewording
    refs = tuple(SourceRef(memory_id, version) for memory_id, version in sorted(grown.items()))
    if kind == "add_sources":
        update = Update(memory_id=target.id, expected_version=target.version, sources=refs)
        return [target.id], refs, [update], target.description
    update = Update(memory_id=target.id, expected_version=target.version,
                    description=raw["description"].strip(), body=raw["body"].strip(),
                    sources=refs)
    return [target.id], refs, [update], update.description


def _refuse(ctx: Context, raw: dict, why: str, reason: str) -> bool:
    """A well-formed judgment a rule refuses: reported, and the pass may still finish
    -- the same input would be refused again. `_plan` never returns REFUSED before
    `source_ids` passed `ids_problem`, so every id joined below was sent.

    The full entry -- kind, target id (if any), the validated source ids and the
    refusal reason -- is written in the section line at judgment time, not only
    collected for the footer: a run killed before the footer is ever written must
    not lose it. The footer's own Needs-you entry, below, is the summary."""
    kind = raw["kind"]
    target = "" if kind == "new" else f" {raw['id']}"
    entry = (f"extraction refused: {kind}{target} from "
            f"{' '.join(raw['source_ids'])} ({why}): {reason}")
    ctx.report.line(entry)
    ctx.report.needs_you(entry)
    return True


def _judge(ctx: Context, raw: dict, global_id: str, sent: dict[str, Memory],
           sources: dict[str, tuple[SourceRef, ...]], owner: dict[str, str]) -> bool:
    """One judgment validated and carried out; False when it keeps the pass from
    finishing (§6.9): malformed output, a reason the policy hits, or an apply that did
    not happen. A rule refusal, C4 included, is reported and does not."""
    report, kind = ctx.report, raw["kind"]
    problem = reason_problem(ctx, raw["reason"])
    if problem is not None:
        report.line(f"{problem} {kind}: reason")
        return False
    reason = shown(raw["reason"])
    plan = _plan(raw, global_id, sent, sources)
    if isinstance(plan, Problem):
        if plan.outcome == INVALID:
            return plan.report(ctx, kind)
        return _refuse(ctx, raw, plan.field, reason)
    items, refs, ops, description = plan
    projects = traced_projects(ctx, global_id, refs, owner)
    if projects is None:
        report.line(f"{kind}: {INPUT_CHANGED}")
        return False
    count = len(projects)
    if count < 2:
        return _refuse(ctx, raw, f"traces to {count} project(s)", reason)
    if apply_group(ctx, kind, items, ops) is None:
        return False
    details(ctx, description, reason)
    return True


def run(ctx: Context) -> PassResult:
    global_id = ctx.services.project.global_project_id()
    if global_id is None:
        ctx.report.line("no global project")
        return PassResult(finished=True)
    memories = usable(ctx, None)
    if not memories:
        ctx.report.line("no memories")
        return PassResult(finished=True)
    digest = input_digest((memory.id, memory.version) for memory in memories)
    if digest == ctx.store.scope_digest(EXTRACTION_SCOPE):
        ctx.report.line("unchanged input; skipped")
        return PassResult(finished=True)
    sent = {memory.id: memory for memory in memories}
    try:
        sources = {memory.id: current_sources(ctx, memory) for memory in memories}
    except MemoryNotFound:
        ctx.report.line(INPUT_CHANGED)
        return PassResult(finished=False)
    prompt = PROMPT.format(entries="\n".join(
        entry(memory, sources[memory.id],
              project="global" if memory.project_id == global_id else memory.project_id)
        for memory in memories))
    result = ask(ctx, SYSTEM_PROMPT, prompt, SCHEMA)
    if isinstance(result, str):
        ctx.report.line(f"not processed: {result}")
        return PassResult(finished=False, digest=digest)
    # one read of every memory's project, shared by every judgment's C4 trace this run
    owner = {memory.id: memory.project_id
             for memory in ctx.services.memory.memories(include_deleted=True)}
    judgments, finished = split_no_change(ctx, result["judgments"])
    if not judgments and finished:
        ctx.report.line("no change")
    for raw in judgments:
        finished = _judge(ctx, raw, global_id, sent, sources, owner) and finished
    return PassResult(finished=finished, digest=digest)
