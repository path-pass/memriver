"""apply_group: every change dream makes goes through here (spec §6.1).

The group limit first, then "applying <kind> <ids>" in the report, then
MemoryService.apply as "dream" with the executor's harness, then the change id, its
undo command and one line per step. A conflict or a policy refusal is reported and
returns None: the phase counts it as a failure and its pass does not finish (§6.9).
A change that touches global, and a group the content classifier blocks, is also
listed under Needs you.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from memriver_core.models.changes import Create, Op
from memriver_core.models.errors import BatchConflict, ContentRejected

if TYPE_CHECKING:
    from .run import Context


def apply_group(ctx: Context, kind: str, items: Sequence[str], ops: Sequence[Op], *,
                touches_global: bool = False) -> str | None:
    """The change id, or None when the group was not applied (limit, conflict, policy).

    `items` are the ids of the existing memories the group changes, as the report
    names them; a created memory's id is known only afterwards and is listed then.
    `touches_global`: the caller knows the group writes global (apply_group cannot
    tell from an Update's id); such a change is also listed under Needs you with
    every memory it touched and its undo command.
    """
    report = ctx.report
    if ctx.groups_used >= ctx.settings.max_groups_per_run:
        report.line(f"not applied (group limit): {' '.join([kind, *items])}")
        return None
    report.applying(kind, items, creates=any(isinstance(op, Create) for op in ops))
    try:
        change = ctx.services.memory.apply(ops, changed_by="dream",
                                           changed_via=ctx.executor.harness)
    except BatchConflict as err:
        report.not_applied(f"conflict {err.reason}"
                           + (f" {err.memory_id}" if err.memory_id else ""))
        return None
    except ContentRejected as err:
        report.not_applied(f"policy {err.rule_id}")
        if err.rule_id.startswith("classifier-"):
            report.needs_you(f"blocked by the content classifier "
                             f"({err.rule_id.removeprefix('classifier-')}): "
                             f"{' '.join([kind, *items])}")
        return None
    ctx.groups_used += 1
    report.applied(change.change_id)
    for step in change.steps:
        before = "new" if step.before_version is None else f"v{step.before_version}"
        report.line(f"  {step.op} {step.memory_id} {before}→v{step.after_version}")
    if touches_global:
        touched = " ".join(step.memory_id for step in change.steps)
        report.needs_you(f"global changed: {kind} {touched} — undo: memriver undo "
                         f"{change.change_id}")
    return change.change_id
