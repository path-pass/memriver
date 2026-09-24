from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from .helpers import is_timestamp

# only the harnesses that hand memriver a session id of their own (spec §3.1)
Harness = Literal["claude-code", "codex"]
SessionStatus = Literal["registered", "pending"]
SessionOrigin = Literal["start", "first-seen"]
PromptOmission = Literal["secret", "too-large", "invalid", "scan-error"]

_SESSION_ID_MAX_CHARS = 128


@dataclass(frozen=True)
class SessionKey:
    """One harness session. The id is the harness's own string, never checked against ID_RE."""

    harness: Harness
    session_id: str

    def __post_init__(self) -> None:
        if self.harness not in get_args(Harness):
            raise ValueError("unknown harness")
        # printable and not whitespace: a format character (a bidi override, a
        # zero-width space) or a lone surrogate is invisible where the id is shown
        if not (isinstance(self.session_id, str)
                and 1 <= len(self.session_id) <= _SESSION_ID_MAX_CHARS
                and all(ch.isprintable() and not ch.isspace() for ch in self.session_id)):
            raise ValueError("invalid session id")


@dataclass(frozen=True)
class PromptEntry:
    """One prompt as recorded: its text, or why the text was left out. Never both."""

    at: str
    text: str | None = None
    omitted: PromptOmission | None = None

    def __post_init__(self) -> None:
        # messages name the rule only: a prompt's text never reaches an error
        if not is_timestamp(self.at):
            raise ValueError("prompt entry time is not a timestamp")
        if (self.text is None) == (self.omitted is None):
            raise ValueError("a prompt entry holds exactly one of text and omitted")
        if self.text is not None and not isinstance(self.text, str):
            raise ValueError("prompt entry text is not text")
        if self.omitted is not None and self.omitted not in get_args(PromptOmission):
            raise ValueError("unknown prompt omission")


@dataclass(frozen=True)
class Session:
    """One stored `sessions` row (spec §3.1); the stored row is the answer, never a caller's guess."""

    key: SessionKey
    status: SessionStatus
    origin: SessionOrigin
    project_id: str | None          # registered: the project, or None (entry unbound)
    candidate_id: str | None        # pending: the project to confirm, or None
    candidate_root: str | None      # pending: the candidate's root when it was computed
    entry_cwd: str
    branch: str | None
    transcript_path: str | None
    started_at: str                 # first recorded by memriver, not the harness's start
    last_active_at: str
    ended_at: str | None
    prompt_count: int
    last_write_prompt_count: int
    last_nudge_prompt_count: int
    first_prompt: PromptEntry | None
    recent_prompts: tuple[PromptEntry, ...]   # newest last
