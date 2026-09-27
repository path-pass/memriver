"""The policy scan (spec §6.2): every stored version against today's content policy.

Every hit goes to "Needs you"; nothing is deleted. A memory whose current version
hits is left out of every model step of the run (`ctx.excluded`); a hit on an older
version keeps only that version away from the model (`ctx.history_hits`, read by
the source re-check).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import PassResult

if TYPE_CHECKING:
    from ..run import Context


def run(ctx: Context) -> PassResult:
    hits = ctx.services.maintenance.scan_policy()
    for hit in hits:
        if hit.current:
            ctx.excluded.add(hit.memory_id)
        else:
            ctx.history_hits.setdefault(hit.memory_id, set()).add(hit.version)
        where = "current" if hit.current else "history"
        ctx.report.needs_you(f"policy hit: {hit.memory_id} v{hit.version} ({where}) rule "
                             f"{hit.rule_id} — memriver delete {hit.memory_id} --hard")
    ctx.report.line(f"policy hits: {len(hits)}; left out of model steps: {len(ctx.excluded)}")
    return PassResult(finished=True)
