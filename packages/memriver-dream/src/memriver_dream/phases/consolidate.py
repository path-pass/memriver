"""Project layer (spec §6.4): not implemented yet; the run reports it and moves on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import PassResult

if TYPE_CHECKING:
    from ..run import Context


def run(ctx: Context, project_id: str, scope: str) -> PassResult:
    ctx.report.line("not implemented yet")
    return PassResult(finished=False)
