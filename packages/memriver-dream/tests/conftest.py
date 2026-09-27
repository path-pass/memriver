"""Shared test doubles: a scripted executor and transcripts handed over as objects --
never a real harness, file or model."""

from __future__ import annotations

import pytest
from memriver_dream.protocols import ExecutorResult


class FakeExecutor:
    """Replays `replies` in order, then `default`; records every call.

    A reply is a dict (the parsed object), an ExecutorResult, or a callable
    taking (prompt, schema) and returning either.
    """

    name = "fake"
    harness = "fake-harness"

    def __init__(self) -> None:
        self.replies: list = []
        self.default = None
        self.calls: list[dict] = []

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> ExecutorResult:
        self.calls.append({"system_prompt": system_prompt, "prompt": prompt, "schema": schema,
                           "timeout_s": timeout_s})
        reply = self.replies.pop(0) if self.replies else self.default
        if reply is None:
            return ExecutorResult(error="exit")
        if callable(reply):
            reply = reply(prompt, schema)
        return reply if isinstance(reply, ExecutorResult) else ExecutorResult(value=reply)


class FakeTranscripts:
    """Transcripts by session id; a session without one reads as None (unreadable)."""

    def __init__(self) -> None:
        self.by_session: dict = {}

    def read(self, session):
        return self.by_session.get(session.key.session_id)


@pytest.fixture
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture
def transcripts() -> FakeTranscripts:
    return FakeTranscripts()
