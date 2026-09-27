"""Global layer, extraction (spec §6.5): not implemented yet; the run reports it and moves on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import PassResult

if TYPE_CHECKING:
    from ..run import Context


def run(ctx: Context) -> PassResult:
    ctx.report.line("not implemented yet")
    return PassResult(finished=False)
