"""One store with two projects and global, driven through the composed services."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from memriver_core.bootstrap import build_services
from memriver_core.models import SessionKey
from memriver_core.models.changes import Create
from memriver_core.settings import Settings


def _sql(store, statement: str, *params) -> list[tuple]:
    """One statement behind the services' backs (foreign keys off), committed."""
    with closing(sqlite3.connect(store / "memriver.db")) as conn, conn:
        return conn.execute(statement, params).fetchall()


@pytest.fixture
def world(tmp_path):
    store, home, work, other = (tmp_path / name for name in ("store", "home", "work", "other"))
    for directory in (home, work, other):
        directory.mkdir()
    services = build_services(Settings(root=store), root=store, home=home)
    global_id = services.project.ensure_global()
    mine = services.project.init_project("mine", services.project.plan_root(str(work)))
    theirs = services.project.init_project("theirs", services.project.plan_root(str(other)))

    def create(body: str = "a fact", *, project_id: str | None = None,
               description: str = "cue", **fields) -> str:
        """A memory made through `apply` by a human; its id."""
        change = services.memory.apply(
            [Create(project_id or mine.id, "project", description, body, **fields)],
            changed_by="human")
        return change.steps[0].memory_id

    return {"store": store, "work": work, "services": services, "memory": services.memory,
            "global": global_id, "mine": mine.id, "theirs": theirs.id,
            "context": services.project.open_project_context(str(work)),
            "other_context": services.project.open_project_context(str(other)),
            "sql": lambda statement, *params: _sql(store, statement, *params),
            "create": create}


@pytest.fixture
def session(world):
    """A registered session in `mine` with one prompt and no save yet."""
    services, key = world["services"], SessionKey("codex", "session-1")
    context = services.session.start_session(key, source="startup",
                                             entry_dir=str(world["work"]),
                                             transcript_path=None)
    services.session.observe_prompt(key, prompt="remember this", entry_dir=str(world["work"]),
                                    transcript_path=None)

    def watermark() -> tuple[int, int]:
        stored = next(s for s in services.session.list_sessions() if s.key == key)
        return stored.prompt_count, stored.last_write_prompt_count

    def prompt() -> None:
        services.session.observe_prompt(key, prompt="and this", entry_dir=str(world["work"]),
                                        transcript_path=None)

    return {"key": key, "context": context, "watermark": watermark, "prompt": prompt}
