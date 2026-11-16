"""SessionService operations (spec §5, §5.1, §5.2) over a real SQLite store, with the
memory writes that ask it whether a session is pending and mark it saved.

The worktree callables and the root-integrity check are fakes the test steers;
everything else -- the three stores and the secret scanner -- is the real thing.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from memriver_core import bootstrap
from memriver_core.application import contexts
from memriver_core.application.contexts import NONE_HEADER
from memriver_core.application.memory import MemoryService
from memriver_core.application.projects import ProjectService
from memriver_core.application.sessions import SessionService
from memriver_core.content_policy.secret_scanner import SecretScanner
from memriver_core.models import (
    MetadataCaps,
    MoveOutcome,
    ProjectContext,
    PromptEntry,
    ReadWriteSet,
    SessionKey,
    single_line,
)
from memriver_core.models.errors import (
    ContentRejected,
    ProjectUnavailable,
    StorageFailure,
    VersionConflict,
)
from memriver_core.repository.directories import (
    canonical_directory,
    transcript_moved,
    usable_directory,
)
from memriver_core.repository.sqlite import (
    SqliteMemoryStore,
    SqliteProjectStore,
    SqliteSessionStore,
)
from memriver_core.repository.sqlite.database import DATABASE_FILENAME
from memriver_core.settings import BUSY_TIMEOUT_MS, Settings

KEY = SessionKey("claude-code", "session-1")
OTHER_KEY = SessionKey("codex", "session-2")
OTHER_KEY_CLAUDE = SessionKey("claude-code", "session-3")
PENDING_HEADER = ("project: awaiting confirmation — this session is not registered; "
                  "ask the user, then call session_confirm")
UNIDENTIFIED_HEADER = ("project: none — this session is not registered with memriver: its "
                       "hooks may not be installed (memriver install) or trusted (Codex asks "
                       "on start), or the harness sent no session id; ask the user to fix "
                       "that, then start a new session")
PENDING_NO_CANDIDATE_HEADER = (
    "project: none — this session is not registered, and its recorded directory is not in any "
    "registered project, so global is read-only; to save, ask the user to run memriver "
    "project init in the directory this session is working in, then call session_register")
SESSION_NONE_HEADER = ("project: none — this session has no project, so global is read-only; "
                       "to save, ask the user to run memriver project init in the directory "
                       "this session is working in, then call session_register")
SESSION_PROJECT_GONE_HEADER = ("project: unavailable — this session's project no longer exists "
                               "or became global, so global is read-only; to save, ask the "
                               "user to start a new session")
SECRET_PROMPT = "token ghp_" + "a" * 36          # from the secret-scanner tests


class Broken:
    """A store whose every named method raises StorageFailure; the rest pass through."""

    def __init__(self, inner, *names: str) -> None:
        self._inner, self._names = inner, names

    def __getattr__(self, name):
        if name in self._names:
            def fail(*args, **kwargs):
                raise StorageFailure
            return fail
        return getattr(self._inner, name)


class FailingPolicy:
    def check(self, text, max_chars):
        raise RuntimeError("scanner exploded")


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.base = Path(os.path.realpath(tmp_path))
        self.store = self.base / "store"
        self.work = self.base / "work"
        self.work.mkdir()
        self.main_tree = lambda path: path          # not inside a worktree
        self.branch: str | None = "main"
        self.intact = True
        self.intact_calls: list[str] = []
        self.policy = SecretScanner()
        self.session_store = SqliteSessionStore(self.store, busy_timeout_ms=BUSY_TIMEOUT_MS)
        self.memory_store = SqliteMemoryStore(self.store, busy_timeout_ms=BUSY_TIMEOUT_MS)
        self.services = self.build()

    def build(self) -> bootstrap.Services:
        def intact(root: str) -> bool:
            self.intact_calls.append(root)
            return self.intact

        project_store = SqliteProjectStore(self.store, home=self.base / "home",
                                           busy_timeout_ms=BUSY_TIMEOUT_MS)
        session = SessionService(
            self.session_store, project_store, lambda: self.policy,
            canonical_directory=canonical_directory,
            usable_directory=usable_directory, transcript_moved=transcript_moved,
            main_tree_path=lambda path: self.main_tree(path),
            current_branch=lambda path: self.branch,
            root_is_intact=intact, header_field_chars=120,
            session_prompt_chars=512, session_recent_prompts=5,
            session_prompt_scan_max_bytes=65536, stop_nudge_min_prompts=5,
            stop_nudge_interval_prompts=5, session_search_limit_default=10,
            session_search_limit_max=50, tool_call_retention_s=3600, summary_max_chars=1_200)
        memory = MemoryService(
            self.memory_store, project_store, lambda: self.policy,
            refuse_pending=session.refuse_pending, mark_saved=session.mark_saved,
            max_body_chars=8000, metadata_max_chars=8000, search_limit_default=5,
            search_limit_max=50, index_budget_lines=100, index_cue_chars=60,
            metadata_caps=MetadataCaps(10, 200, 12, 3))
        project = ProjectService(project_store, header_field_chars=120,
                                 project_name_max_chars=120)
        # no maintenance service: these tests never diagnose
        return bootstrap.Services(memory=memory, project=project, session=session,
                                  maintenance=None)

    def initialize(self) -> None:
        self.global_id = self.services.project.ensure_global()
        self.project = self.services.project.init_project("demo",
                                                 self.services.project.plan_root(str(self.work)))

    def row(self, key: SessionKey = KEY):
        return self.session_store.get(key)

    def sql(self, statement: str, *params) -> None:
        with closing(sqlite3.connect(self.store / DATABASE_FILENAME)) as conn, conn:
            conn.execute(statement, params)

    def start(self, source: str = "startup", entry: Path | None = None, key=KEY,
              transcript: str | None = "/t/1.jsonl", cwd: object = None) -> ProjectContext:
        """The context; the move outcome is kept in `self.outcome`."""
        context, self.outcome = self.services.session.start_session(
            key, source=source, entry_dir=str(entry or self.work), transcript_path=transcript,
            current_dir=str(cwd) if isinstance(cwd, Path) else cwd)
        return context

    def prompt(self, text: object = "hello", entry: Path | None = None, key=KEY,
               transcript: str | None = None, cwd: object = None):
        """(context, created); the move outcome is kept in `self.outcome`."""
        context, created, self.outcome = self.services.session.observe_prompt(
            key, prompt=text, entry_dir=str(entry or self.work), transcript_path=transcript,
            current_dir=str(cwd) if isinstance(cwd, Path) else cwd)
        return context, created

    def write(self, context: ProjectContext, content: str = "uv manages python"):
        return self.services.memory.record(content=content, type="project", sync=False,
                                   harness="claude-code", description="cue", context=context)


@pytest.fixture
def world(tmp_path) -> World:
    world = World(tmp_path)
    world.initialize()
    return world


@pytest.fixture
def storeless(tmp_path) -> World:
    return World(tmp_path)


def _registered(world: World) -> ProjectContext:
    project = world.project
    return ProjectContext(
        "registered", f"project: demo [{project.id}] (root {project.root[:120]})",
        ReadWriteSet(project_id=project.id, global_project_id=world.global_id), project,
        session_key=KEY)


def _no_project(world: World, state: str, header: str, key=KEY) -> ProjectContext:
    return ProjectContext(state, header, ReadWriteSet(project_id=None,
                                                      global_project_id=world.global_id),
                          session_key=key)


# --- start_session ------------------------------------------------------------

def test_start_without_a_store_answers_none_and_creates_nothing(storeless):
    context = storeless.start()
    assert (context.state, context.header) == ("none", NONE_HEADER)
    assert context.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)
    assert not storeless.store.exists()


@pytest.mark.parametrize("source", ["startup", "clear", "fork"])
def test_a_fresh_start_registers_the_entry_project_and_its_branch(world, source):
    assert world.start(source) == _registered(world)
    row = world.row()
    assert (row.status, row.origin, row.project_id) == ("registered", "start", world.project.id)
    assert (row.candidate_id, row.candidate_root) == (None, None)
    assert (row.entry_cwd, row.branch, row.transcript_path) == (str(world.work), "main",
                                                                "/t/1.jsonl")
    assert row.started_at == row.last_active_at and row.prompt_count == 0


@pytest.mark.parametrize("source", ["resume", "compact"])
def test_a_resumed_unknown_session_is_pending_with_the_entry_project_as_candidate(world, source):
    assert world.start(source) == _no_project(world, "pending", PENDING_HEADER)
    row = world.row()
    assert (row.status, row.origin, row.project_id) == ("pending", "first-seen", None)
    assert (row.candidate_id, row.candidate_root) == (world.project.id, world.project.root)
    assert (row.entry_cwd, row.branch) == (str(world.work), "main")


def test_a_start_in_an_unbound_directory_registers_no_project(world):
    elsewhere = world.base / "elsewhere"
    elsewhere.mkdir()
    assert world.start(entry=elsewhere) == _no_project(world, "none", SESSION_NONE_HEADER)
    assert (world.row().status, world.row().project_id) == ("registered", None)
    world.start("resume", entry=elsewhere, key=OTHER_KEY)
    row = world.row(OTHER_KEY)
    assert (row.status, row.candidate_id, row.candidate_root) == ("pending", None, None)


def test_an_existing_row_is_touched_and_keeps_its_own_answer(world):
    world.start()
    before = world.row()
    elsewhere = world.base / "elsewhere"
    elsewhere.mkdir()
    assert world.start("resume", entry=elsewhere, transcript="/t/2.jsonl") == _registered(world)
    after = world.row()
    assert (after.status, after.project_id, after.entry_cwd) == (
        "registered", world.project.id, str(world.work))
    assert after.transcript_path == "/t/2.jsonl"
    assert after.last_active_at > before.last_active_at
    world.start(transcript=None)
    assert world.row().transcript_path == "/t/2.jsonl"


def test_a_failed_worktree_mapping_registers_nothing(world):
    world.main_tree = lambda path: None
    context = world.start()
    assert context.state == "degraded" and context.read_write_set.project_id is None
    assert world.row() is None


def test_a_degraded_resolution_registers_nothing(world):
    context = world.start(entry=world.base / "does-not-exist")
    assert context.state == "degraded"
    assert world.row() is None


def test_a_worktree_entry_registers_the_main_trees_project(world):
    worktree_sub = world.base / "wt" / "sub"
    worktree_sub.mkdir(parents=True)
    world.main_tree = lambda path: str(world.work / "sub") if path == str(worktree_sub) else path
    world.branch = "feature"
    assert world.start(entry=worktree_sub) == _registered(world)
    row = world.row()
    assert (row.project_id, row.entry_cwd, row.branch) == (world.project.id, str(worktree_sub),
                                                           "feature")


# --- observe_prompt -------------------------------------------------------------

def test_a_first_prompt_seeds_a_pending_row_and_only_that_call_created_it(world):
    assert world.prompt("first") == (_no_project(world, "pending", PENDING_HEADER), True)
    assert world.prompt("second") == (_no_project(world, "pending", PENDING_HEADER), False)
    row = world.row()
    assert (row.status, row.candidate_id, row.prompt_count) == ("pending", world.project.id, 2)
    assert row.first_prompt.text == "first"
    assert [entry.text for entry in row.recent_prompts] == ["first", "second"]


def test_a_prompt_on_a_registered_row_is_counted_and_keeps_its_registration(world):
    world.start()
    assert world.prompt() == (_registered(world), False)
    assert (world.row().status, world.row().prompt_count) == ("registered", 1)


class BothReadFirst:
    """Holds the first two `get`s until both have read -- each caller has seen no
    row, so each builds its own seed -- then lets `first_writer` write first."""

    def __init__(self, inner, first_writer: str) -> None:
        self._inner, self._first_writer = inner, first_writer
        self._barrier = threading.Barrier(2, timeout=10)
        self._gated, self._lock = 2, threading.Lock()
        self._written = threading.Event()

    def get(self, key):
        stored = self._inner.get(key)
        with self._lock:
            gated, self._gated = self._gated > 0, self._gated - 1
        if gated:
            self._barrier.wait()
        return stored

    def register(self, *args, **kwargs):
        return self._write("register", *args, **kwargs)

    def add_prompt(self, *args, **kwargs):
        return self._write("add_prompt", *args, **kwargs)

    def _write(self, name, *args, **kwargs):
        if name != self._first_writer:
            assert self._written.wait(timeout=10)
        try:
            return getattr(self._inner, name)(*args, **kwargs)
        finally:
            if name == self._first_writer:
                self._written.set()

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize(("first_writer", "status"), [("register", "registered"),
                                                      ("add_prompt", "pending")])
def test_a_first_start_and_a_first_prompt_racing_share_one_row(world, first_writer, status):
    """SessionStart's register and UserPromptSubmit's seeded add_prompt, both
    for a key with no row: one INSERT wins, both callers answer from it, and
    the prompt is counted once."""
    world.session_store = BothReadFirst(world.session_store, first_writer)
    services = world.build()
    with ThreadPoolExecutor(max_workers=2) as pool:
        started = pool.submit(services.session.start_session, KEY, source="startup",
                              entry_dir=str(world.work), transcript_path=None)
        prompted = pool.submit(services.session.observe_prompt, KEY, prompt="first",
                               entry_dir=str(world.work), transcript_path=None)
        (start_context, _), (prompt_context, created, _) = started.result(), prompted.result()
    [row] = services.session.list_sessions()
    assert (row.key, row.status, row.prompt_count, row.first_prompt.text) == (
        KEY, status, 1, "first")
    assert created is (first_writer == "add_prompt")
    assert start_context == prompt_context == services.session.session_context(KEY)


def test_prompt_text_is_one_line_capped_and_only_the_recent_ones_are_kept(world):
    long = "a\nb\t" + "x" * 600
    world.prompt(long)
    for number in range(6):
        world.prompt(f"p{number}")
    row = world.row()
    assert row.first_prompt.text == single_line(long)[:512] and len(row.first_prompt.text) == 512
    assert [entry.text for entry in row.recent_prompts] == [f"p{n}" for n in range(1, 6)]
    assert row.prompt_count == 7


@pytest.mark.parametrize(("prompt", "omitted"), [
    (SECRET_PROMPT, "secret"),
    ("x" * 65537, "too-large"),
    ("é" * 32769, "too-large"),                 # 65538 bytes in 32769 characters
    (42, "invalid"),
    (None, "invalid"),
    ("x" + chr(0xD800), "invalid"),             # a lone surrogate cannot be encoded
    ("", "invalid"),
    (" \n\t" + chr(0x2028), "invalid"),         # nothing left on one line
])
def test_a_prompt_that_cannot_be_kept_is_recorded_as_omitted(world, prompt, omitted):
    world.prompt(prompt)
    row = world.row()
    assert row.prompt_count == 1
    assert row.first_prompt == PromptEntry(at=row.first_prompt.at, omitted=omitted)
    assert row.recent_prompts == (row.first_prompt,)


def test_a_prompt_at_the_byte_limit_is_kept(world):
    world.prompt("x" * 65536)
    assert world.row().first_prompt.text == "x" * 512


def test_a_scanner_failure_is_recorded_as_a_scan_error(world):
    world.policy = FailingPolicy()
    world.prompt("hello")
    assert world.row().first_prompt.omitted == "scan-error"


def test_a_prompt_without_a_store_creates_nothing(storeless):
    context, created = storeless.prompt()
    assert (context.state, created) == ("none", False)
    assert not storeless.store.exists()


class Recording:
    """A collaborator that must not be reached: it records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return args[0] if args else None

    def check(self, *args, **kwargs):
        self.calls.append(args)


def test_without_a_store_no_directory_question_and_no_scan_is_asked(storeless):
    """No store, nothing to route: an unresolvable entry is not a `degraded`
    answer, and no git subprocess or secret scan runs for a store that is not there."""
    main_tree, policy = Recording(), Recording()
    storeless.main_tree, storeless.policy = main_tree, policy
    missing = storeless.base / "never-created-directory"
    assert storeless.services.session.store_exists() is False
    assert storeless.start(entry=missing).state == "none"
    assert storeless.prompt(entry=missing)[0].state == "none"
    assert storeless.start().state == "none"
    assert storeless.prompt()[0].state == "none"
    assert (main_tree.calls, policy.calls) == ([], [])
    assert not storeless.store.exists()


def test_an_uncheckable_store_counts_as_present(world, monkeypatch):
    """Only a store known to be absent is absent: one that cannot be checked
    goes on to its operation, which reports it as unavailable."""
    monkeypatch.setattr(world.session_store, "store_exists",
                        Broken(world.session_store, "store_exists").store_exists)
    assert world.services.session.store_exists() is True


def test_a_prompt_whose_directory_cannot_be_mapped_records_nothing(world):
    world.main_tree = lambda path: None
    context, created = world.prompt()
    assert (context.state, created) == ("degraded", False)
    assert world.row() is None


# --- end_session / stop_decision --------------------------------------------------

def test_end_marks_the_row_and_an_unknown_session_is_left_alone(world):
    world.start()
    before = world.row()
    world.services.session.end_session(KEY)
    after = world.row()
    assert after.ended_at is not None and after.last_active_at >= before.last_active_at
    world.services.session.end_session(OTHER_KEY)
    assert world.row(OTHER_KEY) is None


def test_stop_nudges_after_n_unsaved_prompts_then_every_m(world):
    world.start()
    decisions = []
    for _ in range(10):
        world.prompt()
        decisions.append(world.services.session.stop_decision(KEY))
    assert decisions == [False] * 4 + [True] + [False] * 4 + [True]
    world.write(_registered(world))
    for _ in range(4):
        world.prompt()
        assert world.services.session.stop_decision(KEY) is False


def test_stop_is_silent_for_pending_projectless_unknown_and_broken_sessions(world):
    for _ in range(6):
        world.prompt()                                  # pending
    assert world.services.session.stop_decision(KEY) is False
    world.start(entry=world.base, key=OTHER_KEY)        # registered, no project
    for _ in range(6):
        world.prompt(key=OTHER_KEY)
    assert world.services.session.stop_decision(OTHER_KEY) is False
    assert world.services.session.stop_decision(SessionKey("codex", "unknown")) is False
    world.session_store = Broken(world.session_store, "nudge_if_due")
    assert world.build().session.stop_decision(KEY) is False


# --- session_context -----------------------------------------------------------------

def test_session_context_follows_the_stored_row(world):
    assert world.services.session.session_context(None) == _no_project(
        world, "unidentified", UNIDENTIFIED_HEADER, key=None)
    assert world.services.session.session_context(KEY) == _no_project(
        world, "unidentified", UNIDENTIFIED_HEADER)
    world.prompt()
    assert world.services.session.session_context(KEY) == _no_project(world, "pending", PENDING_HEADER)
    world.start(entry=world.base, key=OTHER_KEY)
    assert world.services.session.session_context(OTHER_KEY) == _no_project(
        world, "none", SESSION_NONE_HEADER, key=OTHER_KEY)
    third = SessionKey("claude-code", "session-3")
    world.start(key=third)
    assert world.services.session.session_context(third) == ProjectContext(
        **{**_registered(world).__dict__, "session_key": third})


@pytest.mark.parametrize("project_id", ["zzzzzzzzzz", "global"])
def test_a_session_whose_project_is_gone_or_global_is_degraded(world, project_id):
    world.start()
    world.sql("UPDATE sessions SET project_id = ?",
              world.global_id if project_id == "global" else project_id)
    assert world.services.session.session_context(KEY) == ProjectContext(
        "degraded", SESSION_PROJECT_GONE_HEADER,
        ReadWriteSet(project_id=None, global_project_id=world.global_id),
        diagnostic="session project missing", session_key=KEY)


def test_a_store_failure_is_an_unavailable_session_context(world):
    world.session_store = Broken(world.session_store, "get")
    context = world.build().session.session_context(KEY)
    assert context.state == "unavailable" and context.session_key == KEY
    assert context.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)


# --- confirm_session ------------------------------------------------------------------

def test_confirming_a_candidate_registers_its_project(world):
    world.prompt()
    assert world.services.session.confirm_session(KEY) == _registered(world)
    assert world.intact_calls == [world.project.root]
    assert (world.row().status, world.row().project_id) == ("registered", world.project.id)
    # idempotent: a registered row answers unchanged
    assert world.services.session.confirm_session(KEY) == _registered(world)


def test_a_pending_session_without_a_candidate_is_not_offered_for_confirmation(world):
    context, created = world.prompt(entry=world.base)
    assert created and context == _no_project(world, "pending", PENDING_NO_CANDIDATE_HEADER)
    assert world.services.session.session_context(KEY) == context


def test_confirming_a_null_candidate_registers_no_project(world):
    world.prompt(entry=world.base)
    assert world.services.session.confirm_session(KEY) == _no_project(world, "none", SESSION_NONE_HEADER)
    assert world.intact_calls == []
    assert (world.row().status, world.row().project_id) == ("registered", None)


def test_a_candidate_whose_root_is_not_intact_is_refused_and_stays_pending(world):
    world.prompt()
    world.intact = False
    with pytest.raises(ProjectUnavailable) as caught:
        world.services.session.confirm_session(KEY)
    assert caught.value.reason == "candidate-changed"
    assert world.row().status == "pending"


def test_a_candidate_without_its_root_never_confirms_to_a_rootless_project(world):
    world.prompt()
    world.sql("UPDATE projects SET root = NULL WHERE id = ?", world.project.id)
    world.sql("UPDATE sessions SET candidate_root = NULL")
    with pytest.raises(ProjectUnavailable) as caught:
        world.services.session.confirm_session(KEY)
    assert caught.value.reason == "candidate-changed"
    assert world.row().status == "pending"


def test_confirming_an_unknown_session_is_unidentified(world):
    with pytest.raises(ProjectUnavailable) as caught:
        world.services.session.confirm_session(KEY)
    assert caught.value.reason == "unidentified"


# --- register_session ----------------------------------------------------------------

def _projectless_start(world, *parts: str, key=KEY) -> Path:
    """A session started in a directory no project covers yet."""
    entry = world.base.joinpath("later", *parts)
    entry.mkdir(parents=True)
    assert world.start(entry=entry, key=key).state == "none"
    return entry


def _init(world, directory: Path, name: str = "later"):
    directory.mkdir(parents=True, exist_ok=True)
    return world.services.project.init_project(name, world.services.project.plan_root(str(directory)))


def _registered_to(world, project, key=KEY) -> ProjectContext:
    return ProjectContext(
        "registered", f"project: {project.name} [{project.id}] (root {project.root[:120]})",
        ReadWriteSet(project_id=project.id, global_project_id=world.global_id), project,
        session_key=key)


def test_register_binds_a_projectless_session_to_a_project_inited_at_its_entry(world):
    entry = _projectless_start(world)
    before = world.row()
    project = _init(world, entry)
    assert world.services.session.register_session(KEY) == _registered_to(world, project)
    assert world.services.session.session_context(KEY) == _registered_to(world, project)
    after = world.row()
    assert (after.status, after.project_id) == ("registered", project.id)
    # only the project changes: where and how the session started stays
    assert (after.origin, after.entry_cwd, after.branch) == (
        before.origin, before.entry_cwd, before.branch)


def test_register_binds_the_project_inited_at_an_ancestor_of_the_entry(world):
    _projectless_start(world, "deep", "sub")
    project = _init(world, world.base / "later")
    assert world.services.session.register_session(KEY) == _registered_to(world, project)


def test_register_with_no_project_covering_the_entry_changes_nothing(world):
    _projectless_start(world)
    _init(world, world.base / "unrelated")
    before = world.row()
    assert world.services.session.register_session(KEY) == _no_project(world, "none",
                                                              SESSION_NONE_HEADER)
    assert world.row() == before


def test_register_makes_a_pending_session_without_a_candidate_registered(world):
    entry = world.base / "later"
    entry.mkdir()
    world.prompt(entry=entry)
    assert world.services.session.register_session(KEY) == _no_project(
        world, "pending", PENDING_NO_CANDIDATE_HEADER)
    assert world.row().status == "pending"
    project = _init(world, entry)
    assert world.services.session.register_session(KEY) == _registered_to(world, project)
    row = world.row()
    assert (row.status, row.origin, row.project_id, row.candidate_id) == (
        "registered", "first-seen", project.id, None)


def test_register_pending_with_candidate_refused(world):
    world.prompt()
    before = world.row()
    other = _init(world, world.base / "other", "other")
    for current_dir in (None, other.root):
        with pytest.raises(ProjectUnavailable) as caught:
            world.services.session.register_session(KEY, current_dir=current_dir)
        assert caught.value.reason == "pending"
    assert world.row() == before


def test_register_never_changes_a_session_that_has_a_project(world):
    (world.work / "sub").mkdir()
    assert world.start(entry=world.work / "sub") == _registered(world)
    before = world.row()
    _init(world, world.work / "sub", "sub")     # the entry now has a nearer project
    assert world.services.session.register_session(KEY) == _registered(world)
    assert world.row() == before


def test_register_keeps_a_project_the_row_got_in_the_meantime(world):
    entry = _projectless_start(world)
    project = _init(world, entry)
    other = _init(world, world.base / "other", "other")

    class GotOneMeanwhile:
        def __init__(self, inner) -> None:
            self._inner = inner

        def assign_project(self, key, project_id, **expected):
            world.sql("UPDATE sessions SET project_id = ?", other.id)
            return self._inner.assign_project(key, project_id, **expected)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    world.session_store = GotOneMeanwhile(world.session_store)
    assert world.build().session.register_session(KEY) == _registered_to(world, other)
    assert world.row().project_id == other.id != project.id


def test_register_maps_a_linked_worktree_entry_to_the_main_trees_project(world):
    worktree_sub = world.base / "wt" / "sub"
    worktree_sub.mkdir(parents=True)
    world.main_tree = lambda path: (str(world.base / "main" / "sub")
                                    if path == str(worktree_sub) else path)
    assert world.start(entry=worktree_sub).state == "none"
    project = _init(world, world.base / "main", "main")
    assert world.services.session.register_session(KEY) == _registered_to(world, project)
    assert world.row().entry_cwd == str(worktree_sub)


@pytest.mark.parametrize("break_resolution", ["unmappable", "entry-gone"])
def test_a_degraded_registration_writes_nothing(world, break_resolution):
    entry = _projectless_start(world)
    _init(world, entry)
    if break_resolution == "unmappable":
        world.main_tree = lambda path: None
    else:
        world.sql("UPDATE sessions SET entry_cwd = ?", str(world.base / "gone"))
    before = world.row()
    context = world.services.session.register_session(KEY)
    assert context.state == "degraded" and context.read_write_set.project_id is None
    assert context.session_key == KEY
    assert world.row() == before


def test_register_without_a_store_answers_none_and_creates_nothing(storeless):
    context = storeless.services.session.register_session(KEY)
    assert (context.state, context.header) == ("none", NONE_HEADER)
    assert not storeless.store.exists()


def test_registering_an_unknown_session_is_unidentified(world):
    with pytest.raises(ProjectUnavailable) as caught:
        world.services.session.register_session(KEY)
    assert caught.value.reason == "unidentified"


def test_a_store_failure_while_registering_propagates(world):
    _projectless_start(world)
    world.session_store = Broken(world.session_store, "assign_project")
    _init(world, world.base / "later")
    with pytest.raises(StorageFailure):
        world.build().session.register_session(KEY)


def _git(*args: str, cwd: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, env=env, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_candidate_whose_root_went_missing_in_the_main_tree_still_confirms(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    main = base / "main"
    main.mkdir()
    _git("init", "-b", "main", str(main), cwd=base)
    _git("commit", "--allow-empty", "-m", "init", cwd=main)
    _git("worktree", "add", "-b", "feature", str(base / "wt"), cwd=main)
    (main / "sub").mkdir()
    (base / "wt" / "sub").mkdir()
    services = bootstrap.build_services(Settings(root=base / "store"), root=base / "store",
                                      home=base / "home")
    services.project.ensure_global()
    project = services.project.init_project("demo", services.project.plan_root(str(main / "sub")))
    services.session.observe_prompt(KEY, prompt="hi", entry_dir=str(base / "wt" / "sub"),
                                    transcript_path=None)
    assert services.session.pending_candidate(services.session.session_context(KEY)) == project
    (main / "sub").rmdir()          # the main tree's checkout no longer has it
    context = services.session.confirm_session(KEY)
    assert (context.state, context.read_write_set.project_id) == ("registered", project.id)



def _real_services(base: Path) -> bootstrap.Services:
    services = bootstrap.build_services(Settings(root=base / "store"), root=base / "store",
                                      home=base / "home")
    services.project.ensure_global()
    return services


def test_a_symlinked_alias_registers_and_proposes_its_targets_project(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    (base / "a" / "sub").mkdir(parents=True)
    (base / "b").mkdir()
    alias = base / "b" / "alias"
    alias.symlink_to(base / "a" / "sub")
    services = _real_services(base)
    target = services.project.init_project("a", services.project.plan_root(str(base / "a")))
    services.project.init_project("b", services.project.plan_root(str(base / "b")))
    assert services.project.open_project_context(str(alias)).project == target
    context, _ = services.session.start_session(KEY, source="startup", entry_dir=str(alias),
                                                transcript_path=None)
    assert context.project == target
    assert services.session.list_sessions()[0].entry_cwd == str(base / "a" / "sub")
    pending, _, _ = services.session.observe_prompt(OTHER_KEY, prompt="hi",
                                                    entry_dir=str(alias), transcript_path=None)
    assert pending.state == "pending" and services.session.pending_candidate(pending) == target


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_an_alias_into_a_linked_worktree_registers_the_main_trees_project_and_branch(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    main = base / "main"
    main.mkdir()
    _git("init", "-b", "main", str(main), cwd=base)
    _git("commit", "--allow-empty", "-m", "init", cwd=main)
    _git("worktree", "add", "-b", "feature", str(base / "wt"), cwd=main)
    (base / "wt" / "sub").mkdir()
    (base / "link").symlink_to(base / "wt" / "sub")
    services = _real_services(base)
    project = services.project.init_project("demo", services.project.plan_root(str(main)))
    context, _ = services.session.start_session(KEY, source="startup",
                                                entry_dir=str(base / "link"), transcript_path=None)
    assert context.project == project
    row = services.session.list_sessions()[0]
    assert (row.entry_cwd, row.branch) == (str(base / "wt" / "sub"), "feature")


def test_an_entry_with_a_nul_is_degraded_and_writes_nothing(tmp_path):
    services = _real_services(Path(os.path.realpath(tmp_path)))
    entry = "/tmp/x" + chr(0) + "y"
    assert services.session.start_session(KEY, source="startup", entry_dir=entry,
                                          transcript_path=None)[0].state == "degraded"
    context, created, _ = services.session.observe_prompt(KEY, prompt="hi", entry_dir=entry,
                                                          transcript_path=None)
    assert (context.state, created) == ("degraded", False)
    assert services.session.list_sessions() == []


# --- search_sessions / list_sessions / pending_candidate / entry_of -----------------------

def test_session_search_sees_only_the_writable_projects_registered_rows(world):
    for number in range(12):
        world.start(key=SessionKey("codex", f"s{number}"))
    world.prompt(key=SessionKey("codex", "pending"))
    world.start(entry=world.base, key=SessionKey("codex", "unbound"))
    registered = _registered(world)
    assert len(world.services.session.search_sessions("", registered)) == 10
    assert len(world.services.session.search_sessions("", registered, 100)) == 12
    assert len(world.services.session.search_sessions("", registered, 0)) == 1
    assert {s.key.session_id for s in world.services.session.search_sessions("", registered, 50)} == {
        f"s{n}" for n in range(12)}
    for context in (world.services.session.session_context(SessionKey("codex", "pending")),
                    world.services.session.session_context(SessionKey("codex", "unbound")),
                    world.services.session.session_context(None)):
        assert world.services.session.search_sessions("", context) == []


def test_list_sessions_reads_every_row_with_optional_filters(world):
    world.start(key=SessionKey("codex", "a"))
    world.prompt("needle here", key=SessionKey("codex", "b"))
    assert {s.key.session_id for s in world.services.session.list_sessions()} == {"a", "b"}
    assert [s.key.session_id for s in world.services.session.list_sessions(query="NEEDLE")] == ["b"]
    assert [s.key.session_id for s in world.services.session.list_sessions(
        project_id=world.project.id)] == ["a"]
    assert len(world.services.session.list_sessions(limit=1)) == 1


def test_pending_candidate_and_entry_of_answer_from_the_stored_row(world):
    world.prompt()
    pending = world.services.session.session_context(KEY)
    assert world.services.session.pending_candidate(pending) == world.project
    assert world.services.session.entry_of(pending) == str(world.work)
    world.prompt(entry=world.base, key=OTHER_KEY)
    assert world.services.session.pending_candidate(world.services.session.session_context(OTHER_KEY)) is None
    directory = world.services.project.open_project_context(str(world.work))
    assert world.services.session.pending_candidate(directory) is None
    assert world.services.session.entry_of(directory) is None


# --- tools through a session context ------------------------------------------------------

def test_record_and_update_move_the_save_watermark(world):
    context = world.start()
    for _ in range(3):
        world.prompt()
    memory = world.write(context)
    assert world.row().last_write_prompt_count == 3
    world.prompt()
    world.services.memory.update(memory.id, "uv manages all python", context,
                         expected_version=memory.version)
    assert world.row().last_write_prompt_count == 4


def test_a_failed_save_leaves_the_watermark_alone(world):
    context = world.start()
    world.prompt()
    with pytest.raises(ContentRejected):
        world.write(context, SECRET_PROMPT)
    assert world.row().last_write_prompt_count == 0
    memory = world.write(context)
    world.prompt()
    with pytest.raises(VersionConflict):
        world.services.memory.update(memory.id, "changed", context,
                             expected_version=memory.version + 1)
    assert world.row().last_write_prompt_count == 1


def test_a_failed_watermark_never_fails_the_save(world):
    context = world.start()
    world.session_store = Broken(world.session_store, "mark_saved")
    services = world.build()
    memory = services.memory.record(content="kept", type="user", sync=False, harness="codex",
                            description="", context=context)
    assert services.memory.update(memory.id, "kept too", context,
                          expected_version=memory.version).version == memory.version + 1


def test_delete_never_moves_the_watermark(world):
    context = world.start()
    memory = world.write(context)
    world.prompt()
    world.services.memory.delete(memory.id, context, expected_version=memory.version)
    assert world.row().last_write_prompt_count == 0


def test_a_recorded_tool_call_names_its_session(world):
    world.start()
    world.services.session.record_tool_call(OTHER_KEY_CLAUDE, "call-1")
    assert world.services.session.call_record("claude-code", "call-1").key == OTHER_KEY_CLAUDE
    assert world.services.session.call_record("claude-code", "call-2") is None


def test_the_tool_call_mapping_never_fails_its_caller(world):
    world.start()
    world.session_store = Broken(world.session_store, "record_call", "call_record")
    services = world.build()
    assert services.session.record_tool_call(KEY, "call-1") is None
    assert services.session.call_record("claude-code", "call-1") is None


def test_only_read_records_last_read_at(world):
    context = world.start()
    memory = world.write(context)
    world.services.memory.index(context)
    world.services.memory.search("uv", context)
    assert world.services.memory.show(memory.id).last_read_at is None
    world.services.memory.read(memory.id, context, harness="claude-code")
    assert world.services.memory.show(memory.id).last_read_at is not None


def test_a_failed_last_read_at_never_fails_the_read(world):
    context = world.start()
    memory = world.write(context)
    world.memory_store = Broken(world.memory_store, "touch_read")
    assert world.build().memory.read(memory.id, context, harness="claude-code") == memory


def test_a_pending_session_can_neither_write_update_nor_delete(world):
    registered = world.start(key=OTHER_KEY)
    memory = world.services.memory.record(content="fact", type="user", sync=False, harness="codex",
                                  description="", context=registered)
    world.prompt()
    pending = world.services.session.session_context(KEY)
    for attempt in (
            lambda: world.write(pending),
            lambda: world.services.memory.update(memory.id, "x", pending,
                                         expected_version=memory.version),
            lambda: world.services.memory.delete(memory.id, pending, expected_version=memory.version)):
        with pytest.raises(ProjectUnavailable) as caught:
            attempt()
        assert caught.value.reason == "pending"
    assert world.services.memory.read(memory.id, registered,
                                      harness="claude-code").version == memory.version


def test_a_pending_session_without_a_candidate_is_refused_with_its_own_reason(world):
    registered = world.start(key=OTHER_KEY)
    memory = world.services.memory.record(content="fact", type="user", sync=False, harness="codex",
                                  description="", context=registered)
    world.prompt(entry=world.base)
    pending = world.services.session.session_context(KEY)
    for attempt in (
            lambda: world.write(pending),
            lambda: world.services.memory.update(memory.id, "x", pending,
                                         expected_version=memory.version),
            lambda: world.services.memory.delete(memory.id, pending, expected_version=memory.version)):
        with pytest.raises(ProjectUnavailable) as caught:
            attempt()
        assert caught.value.reason == "pending-no-candidate"


# --- a /cd moves the session (Claude Code) ------------------------------------------------

T_A = "/t/a/s.jsonl"                    # the transcript under the start directory's projects dir
T_B = "/t/b/s.jsonl"                    # the same file after a /cd


def _moved_to(world, directory: Path, name: str):
    return _init(world, directory, name)


def _where(world, key=KEY) -> tuple:
    row = world.row(key)
    return (row.status, row.project_id, row.candidate_id, row.entry_cwd, row.branch,
            row.transcript_path)


def test_cd_moves_registered_session_to_other_project(world):
    context = world.start(transcript=T_A)
    memory = world.write(context)
    world.prompt("before the move", transcript=T_A, cwd=world.work)
    assert world.outcome is None
    b = _moved_to(world, world.base / "b", "b")
    before = world.row()
    world.branch = "feature"
    assert world.prompt("after the move", transcript=T_B, cwd=world.base / "b") == (
        _registered_to(world, b), False)
    assert world.outcome == MoveOutcome(world.project.id, b.id, None, None,
                                        str(world.base / "b"), pending=False, degraded=False)
    assert world.outcome.changed
    after = world.row()
    assert _where(world) == ("registered", b.id, None, str(world.base / "b"), "feature", T_B)
    assert after.last_active_at > before.last_active_at
    assert after.prompt_count == before.prompt_count + 1
    [move] = world.services.session.moves(KEY)
    assert (move.seq, move.from_entry_cwd, move.from_project_id, move.to_entry_cwd,
            move.to_project_id) == (1, str(world.work), world.project.id,
                                    str(world.base / "b"), b.id)
    # what was written before the move stays in A; the row itself now belongs to B
    assert world.services.memory.show(memory.id).project_id == world.project.id
    b_context = world.services.session.session_context(KEY)
    assert [s.key for s in world.services.session.search_sessions("before", b_context)] == [KEY]
    assert world.services.session.search_sessions("before", _registered(world)) == []


def test_cd_into_unregistered_dir_unbinds(world):
    world.start(transcript=T_A)
    unbound = world.base / "c"
    unbound.mkdir()
    assert world.prompt(transcript=T_B, cwd=unbound) == (
        _no_project(world, "none", SESSION_NONE_HEADER), False)
    assert world.outcome == MoveOutcome(world.project.id, None, None, None, str(unbound),
                                        pending=False, degraded=False)
    assert world.outcome.changed
    assert _where(world) == ("registered", None, None, str(unbound), "main", T_B)


def test_cd_within_same_project_updates_entry_without_change(world):
    world.start(transcript=T_A)
    (world.work / "sub").mkdir()
    assert world.prompt(transcript=T_B, cwd=world.work / "sub") == (_registered(world), False)
    assert world.outcome is not None and not world.outcome.changed
    assert _where(world) == ("registered", world.project.id, None, str(world.work / "sub"),
                             "main", T_B)
    assert len(world.services.session.moves(KEY)) == 1


def test_cd_degraded_unbinds_at_target(world):
    world.start(transcript=T_A)
    b = _moved_to(world, world.base / "b", "b")
    world.main_tree = lambda path: None
    spelled = str(world.base / "b") + "/."
    context, _ = world.prompt(transcript=T_B, cwd=spelled)
    assert context == _no_project(world, "none", SESSION_NONE_HEADER)
    assert world.outcome == MoveOutcome(world.project.id, None, None, None, spelled,
                                        pending=False, degraded=True)
    assert world.outcome.changed
    assert _where(world) == ("registered", None, None, spelled, None, T_B)
    [move] = world.services.session.moves(KEY)
    assert (move.to_entry_cwd, move.to_project_id) == (spelled, None) and b.id != world.project.id


def test_degraded_then_shell_cd_does_not_retarget(world):
    world.start(transcript=T_A)
    unbound = world.base / "c"
    unbound.mkdir()
    b = _moved_to(world, world.base / "b", "b")
    world.main_tree = lambda path: None
    world.prompt(transcript=T_B, cwd=unbound)
    world.main_tree = lambda path: path
    before = _where(world)
    # a later shell cd into B: the transcript stays where /cd put it
    assert world.prompt(transcript=T_B, cwd=world.base / "b")[0].read_write_set.project_id \
        is None
    assert world.outcome is None
    assert _where(world) == before and before[3] == str(unbound) and b.root != before[3]
    assert len(world.services.session.moves(KEY)) == 1


def test_shell_cd_is_not_a_move(world):
    world.start(transcript=T_A)
    b = _moved_to(world, world.base / "b", "b")
    before = _where(world)
    assert world.prompt(transcript=T_A, cwd=b.root) == (_registered(world), False)
    assert world.outcome is None
    assert _where(world) == before
    assert world.services.session.moves(KEY) == []


def test_transcript_same_dir_other_spelling_is_not_a_move(world):
    real = world.base / "projects" / "x"
    real.mkdir(parents=True)
    (world.base / "alias").symlink_to(world.base / "projects")
    stored = str(real / "s.jsonl")
    world.start(transcript=stored)
    b = _moved_to(world, world.base / "b", "b")
    for spelling in (str(world.base / "alias" / "x" / "s.jsonl"),
                     f"{world.base}/projects/./x/s.jsonl",
                     f"{world.base}/projects/x/../x/s.jsonl"):
        assert not transcript_moved(stored, spelling)
        assert world.prompt(transcript=spelling, cwd=b.root) == (_registered(world), False)
        assert world.outcome is None
    assert _where(world) == ("registered", world.project.id, None, str(world.work), "main",
                             stored)
    assert world.services.session.moves(KEY) == []


@pytest.mark.parametrize("via", ["prompt", "compact"])
@pytest.mark.parametrize("cwd", [None, "rel", "/a\x00"])
def test_move_needs_absolute_cwd(world, via, cwd):
    world.start(transcript=T_A)
    b = _moved_to(world, world.base / "b", "b")
    before = _where(world)
    if via == "prompt":
        context, _ = world.prompt(transcript=T_B, cwd=cwd)
    else:
        context = world.start("compact", transcript=T_B, cwd=cwd)
    assert context == _registered(world) and world.outcome is None
    assert _where(world) == before                      # the stored path stays the old one
    assert world.services.session.moves(KEY) == []
    assert world.prompt(transcript=T_B, cwd=b.root) == (_registered_to(world, b), False)
    assert world.outcome.changed and _where(world)[1] == b.id


def test_transcript_backfilled_then_cd_moves(world):
    world.start(transcript=None)
    assert world.row().transcript_path is None
    b = _moved_to(world, world.base / "b", "b")
    world.prompt(transcript=T_A, cwd=world.work)
    assert world.outcome is None
    assert _where(world) == ("registered", world.project.id, None, str(world.work), "main", T_A)
    assert world.services.session.moves(KEY) == []
    assert world.prompt(transcript=T_B, cwd=b.root) == (_registered_to(world, b), False)
    assert _where(world)[1] == b.id and _where(world)[5] == T_B


def test_compact_backfills_null_transcript_only_when_null(world):
    world.start(transcript=None)
    assert world.start("compact", transcript="rel.jsonl", cwd=world.work) == _registered(world)
    assert world.row().transcript_path is None          # only an absolute path is adopted
    world.start("compact", transcript=T_A, cwd=world.work)
    assert world.row().transcript_path == T_A and world.outcome is None
    # a path that is already stored is never overwritten by a compact
    world.start("compact", transcript="/t/a/other.jsonl", cwd=world.work)
    assert world.row().transcript_path == T_A

    other = SessionKey("claude-code", "session-4")
    world.start(key=other, transcript=None)

    class PathArrivesMeanwhile:
        def __init__(self, inner) -> None:
            self._inner = inner

        def adopt_transcript(self, key, path):
            world.sql("UPDATE sessions SET transcript_path = ? WHERE session_id = ?",
                      "/t/c/s.jsonl", key.session_id)
            return self._inner.adopt_transcript(key, path)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    world.session_store = PathArrivesMeanwhile(world.session_store)
    world.services = world.build()
    world.start("compact", key=other, transcript=T_A, cwd=world.work)
    assert world.row(other).transcript_path == "/t/c/s.jsonl"


def test_codex_rows_never_move(world):
    world.start(key=OTHER_KEY, transcript=T_A)
    b = _moved_to(world, world.base / "b", "b")
    before = _where(world, OTHER_KEY)
    world.prompt(key=OTHER_KEY, transcript=T_B, cwd=b.root)
    assert world.outcome is None
    world.start("compact", key=OTHER_KEY, transcript=T_B, cwd=b.root)
    assert world.outcome is None
    assert _where(world, OTHER_KEY) == before
    assert world.services.session.moves(OTHER_KEY) == []


def test_resume_absorbs_new_transcript(world):
    world.start(transcript=T_A)
    b = _moved_to(world, world.base / "b", "b")
    assert world.start("resume", transcript=T_B, cwd=b.root) == _registered(world)
    assert world.outcome is None
    assert _where(world) == ("registered", world.project.id, None, str(world.work), "main", T_B)
    world.prompt(transcript=T_B, cwd=b.root)
    assert world.outcome is None and world.services.session.moves(KEY) == []


def test_compact_detects_move_before_touch(world):
    world.start(transcript=T_A)
    b = _moved_to(world, world.base / "b", "b")
    assert world.start("compact", transcript=T_B, cwd=b.root) == _registered_to(world, b)
    assert world.outcome.changed and world.outcome.to_project_id == b.id
    assert _where(world) == ("registered", b.id, None, b.root, "main", T_B)
    assert len(world.services.session.moves(KEY)) == 1


def test_compact_move_loser_keeps_stored_path(world):
    world.start(transcript=T_A)
    b = _moved_to(world, world.base / "b", "b")

    class MovedMeanwhile:
        def __init__(self, inner) -> None:
            self._inner = inner

        def move(self, key, **fields):
            world.sql("UPDATE sessions SET transcript_path = ?", "/t/c/s.jsonl")
            return self._inner.move(key, **fields)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    world.session_store = MovedMeanwhile(world.session_store)
    world.services = world.build()
    assert world.start("compact", transcript=T_B, cwd=b.root) == _registered(world)
    assert world.outcome is None
    assert _where(world) == ("registered", world.project.id, None, str(world.work), "main",
                             "/t/c/s.jsonl")
    assert world.services.session.moves(KEY) == []


def test_pending_session_moves_candidate(world):
    world.prompt(transcript=T_A, cwd=world.work)
    b = _moved_to(world, world.base / "b", "b")
    assert world.prompt(transcript=T_B, cwd=b.root) == (
        _no_project(world, "pending", PENDING_HEADER), False)
    assert world.outcome == MoveOutcome(None, None, world.project.id, b.id, b.root,
                                        pending=True, degraded=False)
    assert world.outcome.changed
    row = world.row()
    assert (row.status, row.candidate_id, row.candidate_root, row.entry_cwd) == (
        "pending", b.id, b.root, b.root)
    [move] = world.services.session.moves(KEY)
    assert (move.from_project_id, move.to_project_id) == (None, None)
    assert world.services.session.confirm_session(KEY) == _registered_to(world, b)


def test_confirmed_row_moved_unbound_then_registered(world):
    world.prompt(transcript=T_A, cwd=world.work)
    world.services.session.confirm_session(KEY)
    assert (world.row().project_id, world.row().candidate_id) == (world.project.id,
                                                                 world.project.id)
    unbound = world.base / "c"
    unbound.mkdir()
    assert world.prompt(transcript=T_B, cwd=unbound)[0] == _no_project(world, "none",
                                                                      SESSION_NONE_HEADER)
    assert _where(world) == ("registered", None, None, str(unbound), "main", T_B)
    project = _init(world, unbound, "c")
    assert world.services.session.register_session(KEY) == _registered_to(world, project)


def test_move_between_resolve_and_assign_wins(world):
    entry = _projectless_start(world)
    _init(world, entry)
    unbound = world.base / "c"
    unbound.mkdir()
    stored = world.row()

    class MovedMeanwhile:
        def __init__(self, inner) -> None:
            self._inner = inner

        def assign_project(self, key, project_id, **expected):
            self._inner.move(key, expected_transcript_path=stored.transcript_path,
                             entry_cwd=str(unbound), branch=None, transcript_path=T_B,
                             project_id=None, project_root=None, at=stored.last_active_at)
            return self._inner.assign_project(key, project_id, **expected)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    world.session_store = MovedMeanwhile(world.session_store)
    context = world.build().session.register_session(KEY)
    assert context == _no_project(world, "none", SESSION_NONE_HEADER)
    assert (world.row().project_id, world.row().entry_cwd) == (None, str(unbound))


def test_confirm_rejects_candidate_moved_by_move(world):
    world.prompt(transcript=T_A, cwd=world.work)
    b = _moved_to(world, world.base / "b", "b")
    stored = world.row()

    class MovedMeanwhile:
        def __init__(self, inner) -> None:
            self._inner = inner

        def confirm(self, key, **expected):
            self._inner.move(key, expected_transcript_path=stored.transcript_path,
                             entry_cwd=b.root, branch="main", transcript_path=T_B,
                             project_id=b.id, project_root=b.root, at=stored.last_active_at)
            return self._inner.confirm(key, **expected)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    world.session_store = MovedMeanwhile(world.session_store)
    with pytest.raises(ProjectUnavailable) as caught:
        world.build().session.confirm_session(KEY)
    assert caught.value.reason == "candidate-moved"
    assert (world.row().status, world.row().candidate_id) == ("pending", b.id)


def test_early_returns_carry_no_outcome(world, tmp_path):
    (tmp_path / "bare").mkdir()
    storeless = World(tmp_path / "bare")
    session = storeless.services.session
    assert session.start_session(KEY, source="compact", entry_dir=str(storeless.work),
                                 transcript_path=T_B, current_dir=str(storeless.work))[1] is None
    assert session.observe_prompt(KEY, prompt="hi", entry_dir=str(storeless.work),
                                  transcript_path=T_B, current_dir=str(storeless.work))[1:] == (
        False, None)
    # a new row
    assert world.prompt(transcript=T_A, cwd=world.work)[1] is True and world.outcome is None
    world.start("compact", key=OTHER_KEY_CLAUDE, transcript=T_A, cwd=world.work)
    assert world.outcome is None
    # a store failure
    world.start(key=SessionKey("claude-code", "session-5"), transcript=T_A)
    world.session_store = Broken(world.session_store, "get")
    broken = world.build().session
    started = broken.start_session(SessionKey("claude-code", "session-5"), source="compact",
                                   entry_dir=str(world.work), transcript_path=T_B,
                                   current_dir=str(world.work))
    assert (started[0].state, started[1]) == ("unavailable", None)
    prompted = broken.observe_prompt(SessionKey("claude-code", "session-5"), prompt="hi",
                                     entry_dir=str(world.work), transcript_path=T_B,
                                     current_dir=str(world.work))
    assert (prompted[0].state, prompted[1:]) == ("unavailable", (False, None))


def test_headers_name_working_directory():
    assert contexts.SESSION_NONE_HEADER == SESSION_NONE_HEADER
    assert contexts.PENDING_NO_CANDIDATE_HEADER == PENDING_NO_CANDIDATE_HEADER
    for header in (SESSION_NONE_HEADER, PENDING_NO_CANDIDATE_HEADER):
        assert "the directory this session is working in" in header
        assert "started" not in header and "first observed" not in header


# --- register_session by the call's directory --------------------------------------------

def test_register_uses_entry_first(world):
    entry = _projectless_start(world)
    at_entry = _init(world, entry)
    elsewhere = _init(world, world.base / "x", "x")
    assert world.services.session.register_session(KEY, current_dir=elsewhere.root) == \
        _registered_to(world, at_entry)


def test_register_falls_back_to_current_dir(world):
    entry = _projectless_start(world)
    before = world.row()
    here = _init(world, world.base / "x", "x")
    (world.base / "x" / "sub").mkdir()
    assert world.services.session.register_session(
        KEY, current_dir=str(world.base / "x" / "sub")) == _registered_to(world, here)
    after = world.row()
    assert (after.status, after.project_id) == ("registered", here.id)
    # a registration, not a move: where the session started stays recorded
    assert (after.entry_cwd, after.transcript_path) == (str(entry), before.transcript_path)
    assert world.services.session.moves(KEY) == []


@pytest.mark.parametrize("current_dir", [None, "rel", "/a\x00", "uncovered"])
def test_register_nothing_covers(world, current_dir):
    _projectless_start(world)
    _init(world, world.base / "x", "x")
    if current_dir == "uncovered":
        (world.base / "c").mkdir()
        current_dir = str(world.base / "c")
    before = world.row()
    assert world.services.session.register_session(KEY, current_dir=current_dir) == \
        _no_project(world, "none", SESSION_NONE_HEADER)
    assert world.row() == before


def test_register_degraded_entry_does_not_try_current_dir(world):
    _projectless_start(world)
    here = _init(world, world.base / "x", "x")
    world.sql("UPDATE sessions SET entry_cwd = ?", str(world.base / "gone"))
    before = world.row()
    context = world.services.session.register_session(KEY, current_dir=here.root)
    assert context.state == "degraded" and context.read_write_set.project_id is None
    assert world.row() == before


def test_register_degraded_current_dir(world):
    _projectless_start(world)
    before = world.row()
    context = world.services.session.register_session(
        KEY, current_dir=str(world.base / "missing"))
    assert context.state == "degraded" and context.session_key == KEY
    assert world.row() == before


def test_register_leftover_candidate_does_not_block(world):
    entry = _projectless_start(world)
    world.sql("UPDATE sessions SET candidate_id = ?, candidate_root = ?", world.project.id,
              world.project.root)
    project = _init(world, entry)
    assert world.services.session.register_session(KEY) == _registered_to(world, project)
    assert world.row().project_id == project.id
