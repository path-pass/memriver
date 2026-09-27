"""apply_group: every change dream makes goes through here (spec §6.1).

The group limit first, then "applying <kind> <ids>" in the report, then
MemoryService.apply as "dream" with the executor's harness, then the change id, its
undo command and one line per step. A conflict or a policy refusal is reported and
returns None: the phase counts it as a failure and its pass does not finish (§6.9).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from memriver_core.models.changes import Create, Op
from memriver_core.models.errors import BatchConflict, ContentRejected

if TYPE_CHECKING:
    from .run import Context


def apply_group(ctx: Context, kind: str, items: Sequence[str], ops: Sequence[Op]) -> str | None:
    """The change id, or None when the group was not applied (limit, conflict, policy).

    `items` are the ids of the existing memories the group changes, as the report
    names them; a created memory's id is known only afterwards and is listed then.
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
        return None
    ctx.groups_used += 1
    report.applied(change.change_id)
    for step in change.steps:
        before = "new" if step.before_version is None else f"v{step.before_version}"
        report.line(f"  {step.op} {step.memory_id} {before}→v{step.after_version}")
    return change.change_id
