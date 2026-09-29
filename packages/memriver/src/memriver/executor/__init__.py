"""memriver's executor layer: everything that talks to a model.

An Executor answers one request -- a system prompt, a prompt and the JSON schema the
answer must fit -- with the answer object or the kind of a failure (FAILURE_KINDS),
never any of the output: it may repeat the text that was sent. harness.py runs the
user's own claude -p / codex exec; each backend module is imported only when chosen,
so a caller that builds no executor loads none. Nothing here knows memory writes,
source switches, thresholds or dream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings import ExecutorSettings

__all__ = ["FAILURE_KINDS", "Executor", "Result", "make_executor"]

# every failure an executor reports, never its output: "start" (no process, or no
# scratch directory), "timeout", "login" and "quota" (the backend's own wording or
# status, or a key or provider variable missing), "too-large" (input too long for the
# model), "exit" (any other failure) and "unparsable" (no answer that fits the schema)
FAILURE_KINDS = ("start", "timeout", "login", "quota", "too-large", "exit", "unparsable")


@dataclass(frozen=True)
class Result:
    """The answer object, per the request's schema, or one of FAILURE_KINDS."""

    value: dict | None = None
    error: str | None = None


class Executor(ABC):
    name: str                   # "claude" | "codex" | "jev"

    @abstractmethod
    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> Result:
        """One request. A failure of the backend is a Result, never an exception, and
        never a fallback to another model."""


def make_executor(settings: ExecutorSettings, *, env: Mapping[str, str],
                  scratch_prefix: str) -> Executor:
    """The executor `settings.executor` names, built from the table's executor keys.
    `env` is what a harness run gets; `scratch_prefix` names its temporary directories
    (each caller its own)."""
    from .harness import ClaudeExecutor, CodexExecutor

    if settings.executor == "claude":
        return ClaudeExecutor(settings.executor_path, env=env, scratch_prefix=scratch_prefix,
                              model=settings.model, settings_path=settings.claude_settings)
    if settings.executor == "codex":
        return CodexExecutor(settings.executor_path, env=env, scratch_prefix=scratch_prefix,
                             model=settings.model, overrides=settings.codex_overrides)
    raise ValueError(f"no executor named {settings.executor}")
