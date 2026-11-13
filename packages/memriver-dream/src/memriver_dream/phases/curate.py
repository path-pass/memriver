"""Global curation: a level and tags for every current global entry.

One call: every global entry the policy scan did not exclude, with its level, tags
and the re-statements extraction recorded for it, and the tag names in use with their
descriptions. The model judges each entry's level by aversion (1: a standing rule,
2: the rest) and gives it 1..tags_per_memory_max tag names; code then holds the answer
to the rules before anything is written. A promotion to 1 needs cited evidence -- a
quote found verbatim in the entry's description or body, or a recorded re-statement
-- and a body within standing_max_chars (the model's rewrite, when longer). Which
words are the user's own is the model's call: code checks that a quote is in the
text, not who said it; a re-statement is the evidence code can trace. A demotion
from 1 needs the entry judged 2 in standing_demote_runs curate runs in a row: a curate
run that does not judge it 2 -- it was left out, its answer was invalid, the call
failed, or the change was not applied -- breaks the streak. Rules that have not met
that keep their place under standing_max_count; the room left goes to the new
candidates with the strongest evidence (re-statements, then recency), and the rest
stay 2, under Needs you. A new tag name without a description is not applied, nor
are more tag names than tags_max. Every change is one apply_group, then the tag
descriptions. Curation never merges, splits or deletes. dream.db keeps each rule's
demotion streak and, apart from it and for good, the evidence of every promotion:
written pending before the change, marked with its change id right after it, and
reconciled from the store's history at the next curate pass if the run died between.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from memriver_core.models import TAG_NAME_MAX_CHARS, TAG_NAME_RE
from memriver_core.models.changes import Update
from memriver_core.models.errors import ContentRejected, MemoryNotFound

from ..calls import storable
from ..changes import apply_group, classifier_refusal
from ..store import CurationRow, EvidenceRow
from . import (
    PassResult,
    ask,
    input_estimate,
    near_budget,
    reason_problem,
    shown,
    too_large,
    usable,
)

if TYPE_CHECKING:
    from memriver_core.models import Memory
    from memriver_core.models.changes import MetadataCaps

    from ..run import Context
    from ..store import RestatementRow

SCOPE = "curation"

SYSTEM_PROMPT = (
    "You curate the global memory a coding agent reads in every project: the user's "
    "cross-project rules and facts. For every entry, judge its level and its tags. "
    "Level, by aversion -- if an agent did not follow this entry, how much would the user "
    "mind? 1: a standing rule the user would be annoyed to see broken; it is shown in full "
    "at the start of every session. 2: everything else; it is found through its tags. "
    "Give level 1 only with evidence: in quotes, the user's own words copied exactly from "
    "the entry's description or body; in restated_by, ids from the entry's restated_by "
    "list (project memories that said it again). An entry already at level 1 stays there "
    "unless it no longer reads as a rule the user insists on; never lower one because "
    "nobody re-stated it. At most {count} entries can be level 1. "
    "Tags: give each entry 1 to {per} names -- lowercase words of a-z and 0-9 joined by "
    "single hyphens, at most 32 characters -- that group what an agent would look up "
    "together. Reuse a name already in use whenever one fits and add a name only when none "
    "does; at most {total} names in all. In tags, describe every name you add in one line "
    "of at most 60 characters holding the keywords an agent would look for, and give a new "
    "description for a name in use only when its description is missing or wrong. "
    "A level-1 entry whose body is longer than {chars} characters needs body: the same "
    "rule rewritten within {chars} characters, keeping its condition and the user's "
    "meaning; otherwise leave body empty. Each entry has a one-sentence reason that says "
    "why, without copying the entry's text. Never merge, split or delete entries: judge "
    "every entry given, once.")
PROMPT = ("Global entries:\n<entries>\n{entries}\n</entries>\n\n"
          "Tag names in use:\n<tags>\n{tags}\n</tags>")

_ENTRY = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "level", "tags", "quotes", "restated_by", "body", "reason"],
    "properties": {
        "id": {"type": "string"},
        "level": {"type": "integer", "enum": [1, 2]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "quotes": {"type": "array", "items": {"type": "string"}},
        "restated_by": {"type": "array", "items": {"type": "string"}},
        "body": {"type": "string"},                    # a level-1 rewrite, else ""
        "reason": {"type": "string"}}}
_TAG = {"type": "object", "additionalProperties": False, "required": ["name", "description"],
        "properties": {"name": {"type": "string"}, "description": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["entries", "tags"],
          "properties": {"entries": {"type": "array", "items": _ENTRY},
                         "tags": {"type": "array", "items": _TAG}}}


@dataclass
class _Plan:
    """One entry's outcome: what it becomes, and what the report says about it."""

    memory: Memory
    level: int | None
    tags: tuple[str, ...]
    body: str | None = None                    # a rewrite, when one is applied
    note: str = ""                             # promoted / demoted / kept line
    judged_two: int = 0                        # curate runs in a row judged 2 while at 1
    evidence: list[EvidenceRow] = field(default_factory=list)   # of a promotion


def _name(name: object) -> bool:
    return isinstance(name, str) and len(name) <= TAG_NAME_MAX_CHARS \
        and bool(TAG_NAME_RE.fullmatch(name))


def _tags(raw: list, caps: MetadataCaps) -> tuple[str, ...] | None:
    """The answer's tag names for one entry, sorted; None when they break a rule."""
    if not 1 <= len(raw) <= caps.tags_per_memory_max or len(set(raw)) != len(raw) \
            or not all(_name(name) for name in raw):
        return None
    return tuple(sorted(raw))


def _evidence(ctx: Context, memory: Memory, raw: dict,
              restated: list[RestatementRow]) -> list[EvidenceRow]:
    """What the answer cites that holds, as traceable records: each quote by where it
    first stands in the description or the body of the version judged, each
    re-statement dream.db recorded for this entry by its project memory and version."""
    found: dict[tuple, EvidenceRow] = {}
    for quote in raw["quotes"]:
        quote = quote.strip()
        for name in ("description", "body"):
            start = getattr(memory, name).find(quote) if quote else -1
            if start >= 0:
                key = ("quote", name, start, start + len(quote))
                found.setdefault(key, EvidenceRow(
                    memory.id, memory.version, ctx.run_id, "quote", name, start,
                    start + len(quote), None, None, ctx.now))
                break
    cited = set(raw["restated_by"])
    for row in restated:
        if row.memory_id in cited:
            found.setdefault(("restatement", row.memory_id, row.memory_version), EvidenceRow(
                memory.id, memory.version, ctx.run_id, "restatement", None, None, None,
                row.memory_id, row.memory_version, ctx.now))
    return list(found.values())


def _judge(ctx: Context, memory: Memory, raw: dict, restated: list[RestatementRow],
           record: CurationRow | None, caps: MetadataCaps) -> _Plan:
    """One entry's answer held to the rules; never raises for the model's output."""
    report, settings = ctx.report, ctx.settings
    reason = shown(raw["reason"])
    tags = _tags(raw["tags"], caps)
    if tags is None:
        report.line(f"refused tags {memory.id}: tags")
        tags = memory.tags
    plan = _Plan(memory, memory.level, tags)
    if raw["level"] == 2:
        if memory.level == 1:
            plan.judged_two = 1 + (0 if record is None else record.judged_two)
            if plan.judged_two < settings.standing_demote_runs:
                plan.note = (f"kept {memory.id} at level 1 (judged 2, {plan.judged_two} of "
                             f"{settings.standing_demote_runs}): {reason}")
                return plan
            plan.note = (f"demoted {memory.id} (judged 2 in {plan.judged_two} curate runs "
                         f"in a row): {reason}")
        plan.level, plan.judged_two = 2, 0
        return plan
    promoted = memory.level != 1
    evidence = _evidence(ctx, memory, raw, restated) if promoted else []
    if promoted and not evidence:
        report.line(f"refused level 1 {memory.id}: evidence")
        plan.level = 2
        return plan
    if len(memory.body) > caps.standing_max_chars:
        rewrite = raw["body"].strip()
        if storable(rewrite) and rewrite and len(rewrite) <= caps.standing_max_chars:
            plan.body = rewrite
        elif promoted:
            report.line(f"refused level 1 {memory.id}: body")
            plan.level = 2
            return plan
    plan.level, plan.evidence = 1, evidence
    if promoted:
        plan.note = (f"promoted {memory.id} "
                     f"({'; '.join(row.reference() for row in evidence)}): {reason}")
    return plan


def _strength(plan: _Plan, restated: dict[str, list[RestatementRow]]) -> tuple:
    rows = restated.get(plan.memory.id, [])
    return (len(rows), max((row.seen_at for row in rows), default=""), plan.memory.updated,
            plan.memory.id)


def _standing(memories: list[Memory], levels: dict[str, int | None],
              bodies: dict[str, str]) -> tuple[int, int]:
    """(level-1 entries, their characters) for `levels` and `bodies` by id."""
    standing = [m for m in memories if levels[m.id] == 1]
    return len(standing), sum(len(bodies[m.id]) for m in standing)


def _entries(memories: list[Memory], restated: dict[str, list[RestatementRow]]) -> str:
    return "\n".join(json.dumps(
        {"id": m.id, "type": m.type, "description": m.description, "body": m.body,
         "level": m.level, "tags": list(m.tags),
         "restated_by": sorted({row.memory_id for row in restated.get(m.id, [])})},
        ensure_ascii=False) for m in memories)


def _describe(ctx: Context, global_id: str, name: str, description: str) -> bool:
    """One tag description through core (policy and classifier); False when refused."""
    try:
        ctx.services.memory.describe_tag(global_id, name, description, changed_by="dream")
    except ContentRejected as err:
        ctx.report.line(f"tag {name}: description not set: policy {err.rule_id}")
        classifier_refusal(ctx, err, f"description of tag {name}")
        return False
    ctx.report.line(f"tag {name}: description set")
    return True


def _streaks(ctx: Context, current: list[Memory], plans: dict[str, _Plan]) -> None:
    """Every current entry's demotion streak after this curate run: kept for a rule the
    run judged 2 and kept at 1; ended for every other entry -- judged 1, demoted, or not
    judged at all, which breaks "in a row"."""
    for memory in current:
        plan = plans.get(memory.id)
        if plan is not None and plan.level == 1 and plan.judged_two:
            ctx.store.put_curation(CurationRow(memory.id, plan.judged_two, ctx.now))
        else:
            ctx.store.delete_curation(memory.id)


def _reconcile(ctx: Context) -> None:
    """Pending evidence left by an earlier run, settled from the store's history: the
    promotion was applied if the next version of the entry is a level-1 version made by
    dream, whose change then marks the rows; otherwise (or if the entry is gone) the
    rows are dropped."""
    applied: dict[str, list[tuple[str, int, str]]] = {}
    dropped: list[tuple[str, int, str]] = []
    for row in ctx.store.pending_evidence():
        key = (row.memory_id, row.memory_version, row.run_id)
        try:
            versions = ctx.services.memory.versions(row.memory_id)
        except MemoryNotFound:
            versions = []
        after = next((v for v in versions if v.version == row.memory_version + 1), None)
        if after is not None and after.level == 1 and after.change is not None \
                and after.change.changed_by == "dream":
            applied.setdefault(after.change.change_id, []).append(key)
        else:
            dropped.append(key)
    for change_id, keys in applied.items():
        ctx.store.settle_evidence(keys, change_id)
    if dropped:
        ctx.store.settle_evidence(dropped, None)


def run(ctx: Context) -> PassResult:
    report, memory_service = ctx.report, ctx.services.memory
    global_id = ctx.services.project.global_project_id()
    if global_id is None:
        report.line("no global project")
        return PassResult(finished=True)
    _reconcile(ctx)
    memories = usable(ctx, global_id)
    if not memories:
        report.line("no global memories")
        return PassResult(finished=True)
    # every current global entry, those the scan left out too: the limits count them
    current = memory_service.memories(global_id)
    caps = memory_service.metadata_caps
    restated = ctx.store.restatements()
    described = memory_service.tag_descriptions(global_id)
    in_use = sorted({name for m in current for name in m.tags})
    system = SYSTEM_PROMPT.format(count=caps.standing_max_count,
                                  per=caps.tags_per_memory_max, total=caps.tags_max,
                                  chars=caps.standing_max_chars)
    prompt = PROMPT.format(entries=_entries(memories, restated), tags="\n".join(
        json.dumps({"name": name, "description": described.get(name, "")}, ensure_ascii=False)
        for name in in_use))
    estimate = input_estimate(system, prompt)
    result = ask(ctx, system, prompt, SCHEMA)
    if result != "too-large":
        near_budget(ctx, SCOPE, estimate)
    if isinstance(result, str):
        if result == "too-large":
            too_large(ctx, SCOPE, estimate, ctx.budget_tokens)
        report.line(f"not processed: {result}")
        _streaks(ctx, current, {})                 # a curate run that judged nothing
        return PassResult(finished=False)
    for memory in memories:
        if memory.level is None and memory.type == "project":
            report.needs_you(f"global entry {memory.id} is of type project: it probably "
                             "belongs to a project")
    sent = {memory.id: memory for memory in memories}
    finished, plans = True, {}
    for raw in result["entries"]:
        memory = sent.get(raw["id"])
        if memory is None or memory.id in plans:
            report.line("invalid entry: id")
            finished = False
            continue
        if (problem := reason_problem(ctx, raw["reason"])) is not None:
            report.line(f"{problem} {memory.id}: reason")
            finished = False
            continue
        plans[memory.id] = _judge(ctx, memory, raw, restated.get(memory.id, []),
                                  ctx.store.curation(memory.id), caps)
    for memory in memories:
        if memory.id not in plans:
            report.line(f"not judged: {memory.id}")
            finished = False
    # rules that have not met the demotion rule keep their place, judged this run or not;
    # the room left goes to the new candidates with the strongest evidence
    room = caps.standing_max_count - sum(
        m.level == 1 for m in current if m.id not in plans or plans[m.id].level == 1)
    candidates = sorted((plan for plan in plans.values()
                         if plan.level == 1 and plan.memory.level != 1),
                        key=lambda plan: _strength(plan, restated), reverse=True)
    for plan in candidates[max(room, 0):]:
        report.needs_you(f"global curation: {plan.memory.id} judged a standing rule but held "
                         f"at level 2 by standing_max_count ({caps.standing_max_count})")
        plan.level, plan.body, plan.evidence = 2, None, []
        plan.note = f"held {plan.memory.id} at level 2: standing_max_count"
    offered = {raw["name"]: raw["description"].strip() for raw in result["tags"]
               if storable(raw["description"]) and raw["description"].strip()}
    for plan in plans.values():
        # a name new to global needs a description, given now or described before
        missing = sorted(name for name in set(plan.tags) - set(in_use)
                         if name not in described and name not in offered)
        if missing:
            report.line(f"refused tags {plan.memory.id}: no description for "
                        f"{' '.join(missing)}")
            plan.tags = plan.memory.tags
            finished = False
    tags_after = {m.id: plans[m.id].tags if m.id in plans else m.tags for m in current}
    names_after = {name for tags in tags_after.values() for name in tags}
    if len(names_after) > caps.tags_max:
        report.needs_you(f"global curation proposed {len(names_after)} tag names, more than "
                         f"tags_max ({caps.tags_max}); no tags were changed")
        for plan in plans.values():
            plan.tags = plan.memory.tags
        names_after = set(in_use)
    ops = [Update(plan.memory.id, plan.memory.version,
                  level=plan.level if plan.level != plan.memory.level else None,
                  tags=plan.tags if plan.tags != plan.memory.tags else None, body=plan.body)
           for plan in plans.values()
           if (plan.level, plan.tags, plan.body) != (plan.memory.level, plan.memory.tags, None)]
    before = _standing(current, {m.id: m.level for m in current},
                       {m.id: m.body for m in current})
    # the evidence is written pending before the change and marked with its id right
    # after it, ahead of any other dream.db write: a failure in between leaves a pending
    # row the next pass reconciles from the store's history
    pending = [row for plan in plans.values() for row in plan.evidence] if ops else []
    keys = sorted({(row.memory_id, row.memory_version, row.run_id) for row in pending})
    if pending:
        ctx.store.add_evidence(pending)
    change_id = apply_group(ctx, "curate", [op.memory_id for op in ops], ops,
                            touches_global=True) if ops else None
    if ops and change_id is None:
        ctx.store.settle_evidence(keys, None)
        _streaks(ctx, current, {})                 # nothing this run judged took effect
        return PassResult(finished=False)
    if pending:
        ctx.store.settle_evidence(keys, change_id)
    _streaks(ctx, current, plans)
    for plan in plans.values():
        if plan.note:
            report.line(plan.note)
    after = _standing(current, {m.id: plans[m.id].level if m.id in plans else m.level
                                for m in current},
                      {m.id: (plans[m.id].body if m.id in plans and plans[m.id].body
                              else m.body) for m in current})
    report.line(f"level 1 after this curation: {after[0]} entries, {after[1]} characters "
                f"(before it: {before[0]}, {before[1]})")
    added, dropped = sorted(names_after - set(in_use)), sorted(set(in_use) - names_after)
    if added or dropped:
        report.line(f"tags added: {' '.join(added) or '-'}; dropped: {' '.join(dropped) or '-'}")
    for name, text in offered.items():
        if name not in names_after or text == described.get(name):
            continue
        if _describe(ctx, global_id, name, text):
            described[name] = text
        else:
            finished = False
    seen = sum(row.run_id == ctx.run_id for rows in restated.values() for row in rows)
    report.line(f"re-statements seen this run: {seen}")
    return PassResult(finished=finished)
