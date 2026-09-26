"""Shared fixtures: a real store with a project and global, a scripted executor and
transcripts handed over as objects -- never a real harness, file or model."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from memriver_core import bootstrap
from memriver_core.models import new_id, now
from memriver_core.settings import Settings
from memriver_dream.protocols import ExecutorResult, Run
from memriver_dream.settings import DreamSettings


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


@pytest.fixture
def world(tmp_path, executor, transcripts):
    store, home, work = tmp_path / "store", tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    settings = Settings(root=store)
    dream = DreamSettings(executor="claude", executor_path="/usr/bin/true")
    service = bootstrap.build_service(settings, root=store, home=home)
    global_id = service.ensure_global()
    project = service.init_project("demo", service.plan_root(str(work)))
    maintenance = bootstrap.build_maintenance_service(settings, root=store)
    lines: list[str] = []

    def run(**overrides) -> Run:
        values = {"maintenance": maintenance, "executor": executor,
                  "transcripts": transcripts, "dream": dream, "now": now(),
                  "run_id": new_id(), "log": lines.append}
        return Run(**(values | overrides))

    def sql(statement: str, *params) -> list[tuple]:
        with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
            return conn.execute(statement, params).fetchall()

    def plant(project_id: str, body: str, *, description: str = "cue",
              created: str | None = None, last_read_at: str | None = None,
              type: str = "project") -> str:
        memory_id, stamp = new_id(), created or now()
        sql("INSERT INTO memories (id, project_id, type, source_harness, source_method, "
            "trust, sync, description, body, created, updated, version, last_read_at) "
            "VALUES (?, ?, ?, 'test', 'agent', 'agent', 1, ?, ?, ?, ?, 1, ?)",
            memory_id, project_id, type, description, body, stamp, stamp, last_read_at)
        return memory_id

    return SimpleNamespace(store=store, settings=settings, dream=dream, service=service,
                           maintenance=maintenance, global_id=global_id, project=project,
                           context=service.open_project_context(str(work)),
                           executor=executor, transcripts=transcripts, lines=lines, run=run,
                           sql=sql, plant=plant)
