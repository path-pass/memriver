"""memriver dream: the harness-neutral maintenance run (policy scan, summaries,
consolidation, TTL).

This module is the package's surface for the umbrella: the run, the report records,
the executor and transcript protocols, the [dream] table and the constants the
umbrella's commands use. Every other module is internal.
"""

from .calls import storable
from .protocols import (
    Executor,
    ExecutorResult,
    FailureKind,
    Record,
    Transcript,
    TranscriptSource,
)
from .run import RunRecord, find_run, recent_runs, run_dream
from .settings import (
    DEFAULT_DREAM_REPORT_RETENTION_DAYS,
    DEFAULT_DREAM_SCHEDULE_AT,
    DEFAULT_DREAM_TTL_DAYS,
    DREAM_DIRECTORY,
    DREAM_KILL_GRACE_S,
    DREAM_LAUNCH_AGENT_LABEL,
    DREAM_LOG_FILENAME,
    DREAM_REPORTS_DIRECTORY,
    DREAM_TOOL_OUTPUT_CHARS,
    DreamSettings,
    check_dream_table,
    load_dream_settings,
)

__version__ = "0.1.0"
__all__ = [
    "DEFAULT_DREAM_REPORT_RETENTION_DAYS",
    "DEFAULT_DREAM_SCHEDULE_AT",
    "DEFAULT_DREAM_TTL_DAYS",
    "DREAM_DIRECTORY",
    "DREAM_KILL_GRACE_S",
    "DREAM_LAUNCH_AGENT_LABEL",
    "DREAM_LOG_FILENAME",
    "DREAM_REPORTS_DIRECTORY",
    "DREAM_TOOL_OUTPUT_CHARS",
    "DreamSettings",
    "Executor",
    "ExecutorResult",
    "FailureKind",
    "Record",
    "RunRecord",
    "Transcript",
    "TranscriptSource",
    "__version__",
    "check_dream_table",
    "find_run",
    "load_dream_settings",
    "recent_runs",
    "run_dream",
    "storable",
]
