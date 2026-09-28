"""Shared fixtures: a scripted executor and transcripts handed over as objects, and a
real core store with a project and global -- never a real harness, file or model."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from memriver_core.bootstrap import build_services
from memriver_core.models.changes import Create
from memriver_core.settings import Settings
from memriver_dream.protocols import ExecutorResult
from memriver_dream.settings import DreamSettings

NOW = "2026-09-27T04:00:00.000000Z"
SECRET = "token ghp_" + "a" * 36            # what the content policy refuses (github-pat)


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


@pytest.fixture
def world(tmp_path, executor, transcripts):
    root, home, work = tmp_path / "store", tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    services = build_services(Settings(root=root), root=root, home=home)
    global_id = services.project.ensure_global()
    project = services.project.init_project("demo", services.project.plan_root(str(work)))
    dream = DreamSettings(executor="claude", executor_path="/usr/bin/true")
    reports = root / "dream" / "reports"

    def create(project_id: str, body: str, *, description: str = "cue",
               type: str = "project") -> str:
        change = services.memory.apply(
            [Create(project_id=project_id, type=type, description=description, body=body)],
            changed_by="test")
        return change.steps[0].memory_id

    def sql(statement: str, *params) -> list[tuple]:
        with closing(sqlite3.connect(root / "memriver.db")) as conn, conn:
            return conn.execute(statement, params).fetchall()

    def plant(memory_id: str, version: int, *, body: str) -> None:
        """Put `body` into a stored version past the content policy, as the migration's
        import can (spec R8); the current row follows when `version` is current."""
        sql("UPDATE memory_versions SET body = ? WHERE memory_id = ? AND version = ?",
            body, memory_id, version)
        sql("UPDATE memories SET body = ? WHERE id = ? AND version = ?", body, memory_id, version)

    def context(**overrides):
        # imported here, not at module level: the store and report tests run before
        # run.py exists
        from memriver_dream.report import Report
        from memriver_dream.run import Context
        from memriver_dream.store import DreamStore

        reports.mkdir(parents=True, exist_ok=True)
        values = {"services": services, "executor": executor, "transcripts": transcripts,
                  "settings": dream, "now": NOW,
                  "store": DreamStore(root / "dream" / "dream.db"),
                  "report": Report(reports / "test.txt", services.maintenance.check_text),
                  "excluded": set(), "history_hits": {}}
        return Context(**(values | overrides))

    def run(**overrides):
        from memriver_dream.run import run_dream

        values = {"services": services, "executor": executor, "transcripts": transcripts,
                  "settings": dream, "root": root, "now": NOW, "trigger": "manual"}
        return run_dream(**(values | overrides))

    def report_text(row) -> str:
        return (reports / row.report_file).read_text(encoding="utf-8")

    return SimpleNamespace(root=root, home=home, work=work, services=services,
                           global_id=global_id, project=project, dream=dream, reports=reports,
                           now=NOW, secret=SECRET, executor=executor, transcripts=transcripts,
                           create=create, sql=sql, plant=plant, context=context, run=run,
                           report_text=report_text)
