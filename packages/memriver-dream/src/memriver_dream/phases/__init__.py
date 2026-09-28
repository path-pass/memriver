"""The run's phases (spec §6.2–§6.7), called by run_dream in spec order.

Each phase module has `run(ctx) -> PassResult`, except consolidate, which works on
one scope per call: `run(ctx, project_id, scope)`. A phase imports Context only for
type checking: run.py imports the phases.
"""

from __future__ import annotations

from dataclasses import dataclass

EXTRACTION_SCOPE = "extraction"     # scope_passes key of the global layer's extraction
GLOBAL_SCOPE = "global"             # scope_passes key of the project layer run on global
# an ordinary project's key is f"project:{project_id}", built by run_dream


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
