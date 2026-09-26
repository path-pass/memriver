"""What the maintenance run consumes: an executor, a transcript source, and each run's context.

The umbrella implements Executor and TranscriptSource for real harnesses;
nothing here knows one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from memriver_core.models import Session

from .settings import (
    DREAM_CONTEXT_BUDGET_TOKENS,
    DREAM_INPUT_MARGIN_TOKENS,
    DREAM_OUTPUT_RESERVE_TOKENS,
    DreamSettings,
)

if TYPE_CHECKING:
    from memriver_core.bootstrap import MaintenanceService

RecordKind = Literal["user", "assistant", "tool", "compact", "title"]
# "start": the process could not be started (argument list too long, executable
# missing or not executable); "too-large": the harness said the input exceeds the
# model's context
FailureKind = Literal["timeout", "exit", "start", "unparsable", "schema", "login", "quota",
                      "too-large"]


@dataclass(frozen=True)
class Record:
    """One normalized transcript entry: a prompt, an answer, a shortened tool output,
    a compaction summary or a title."""

    kind: RecordKind
    at: str | None
    text: str


@dataclass(frozen=True)
class Transcript:
    records: tuple[Record, ...]
    fingerprint: str        # over the bytes read, so any change to them changes it
    complete: bool          # False when the file ended inside a record


@dataclass(frozen=True)
class ExecutorResult:
    """The parsed JSON object, or the kind of failure -- never any of the output."""

    value: dict | None = None
    error: FailureKind | None = None


class Executor(Protocol):
    name: str               # recorded on reviews and runs
    harness: str             # recorded as source_harness of the memories it writes

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> ExecutorResult: ...


class TranscriptSource(Protocol):
    def read(self, session: Session) -> Transcript | None: ...
    # None: no readable transcript for this session (missing, unreadable, not a file)


@dataclass(frozen=True)
class Run:
    """One run's context, handed to every model phase."""

    maintenance: MaintenanceService
    executor: Executor
    transcripts: TranscriptSource
    dream: DreamSettings
    now: str
    run_id: str
    log: Callable[[str], None]
    budget_tokens: int = (DREAM_CONTEXT_BUDGET_TOKENS - DREAM_OUTPUT_RESERVE_TOKENS
                          - DREAM_INPUT_MARGIN_TOKENS)
