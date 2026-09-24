"""MemoryService session operations (spec §5, §5.1, §5.2) over a real SQLite store.

The worktree callables and the root-integrity check are fakes the test steers;
everything else -- the three stores and the secret scanner -- is the real thing.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from memriver_core import bootstrap
from memriver_core.application.service import NONE_HEADER, MemoryService
from memriver_core.content_policy.secret_scanner import SecretScanner
from memriver_core.models import (
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
from memriver_core.repository.directories import canonical_directory
from memriver_core.repository.sqlite import (
    SqliteMemoryStore,
    SqliteProjectStore,
    SqliteSessionStore,
)
from memriver_core.repository.sqlite.database import DATABASE_FILENAME
from memriver_core.settings import BUSY_TIMEOUT_MS, Settings

KEY = SessionKey("claude-code", "session-1")
OTHER_KEY = SessionKey("codex", "session-2")
PENDING_HEADER = ("project: awaiting confirmation — this session is not registered; "
                  "ask the user, then call session_confirm")
UNIDENTIFIED_HEADER = ("project: none — this session is not registered with memriver; "
                       "restart the session after memriver install")
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
        self.service = self.build()

    def build(self) -> MemoryService:
        def intact(root: str) -> bool:
            self.intact_calls.append(root)
            return self.intact

        return MemoryService(
            self.memory_store,
            SqliteProjectStore(self.store, home=self.base / "home",
                               busy_timeout_ms=BUSY_TIMEOUT_MS),
            lambda: self.policy, None,
            session_store=self.session_store,
            canonical_directory=canonical_directory,
            main_tree_path=lambda path: self.main_tree(path),
            current_branch=lambda path: self.branch,
            root_is_intact=intact,
            max_body_chars=8000, metadata_max_chars=8000, search_limit_default=5,
            search_limit_max=50, index_budget_lines=100, index_cue_chars=60,
            header_field_chars=120, project_name_max_chars=120,
            session_prompt_chars=512, session_recent_prompts=5,
            session_prompt_scan_max_bytes=65536, stop_nudge_min_prompts=5,
            stop_nudge_interval_prompts=5, session_search_limit_default=10,
            session_search_limit_max=50)

    def initialize(self) -> None:
        self.global_id = self.service.ensure_global()
        self.project = self.service.init_project("demo",
                                                 self.service.plan_root(str(self.work)))

    def row(self, key: SessionKey = KEY):
        return self.session_store.get(key)

    def sql(self, statement: str, *params) -> None:
        with closing(sqlite3.connect(self.store / DATABASE_FILENAME)) as conn, conn:
            conn.execute(statement, params)

    def start(self, source: str = "startup", entry: Path | None = None, key=KEY,
              transcript: str | None = "/t/1.jsonl") -> ProjectContext:
        return self.service.start_session(key, source=source,
                                          entry_dir=str(entry or self.work),
                                          transcript_path=transcript)

    def prompt(self, text: object = "hello", entry: Path | None = None, key=KEY):
        return self.service.observe_prompt(key, prompt=text, entry_dir=str(entry or self.work),
                                           transcript_path=None)

    def write(self, context: ProjectContext, content: str = "uv manages python"):
        return self.service.record(content=content, type="project", sync=False,
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
    assert world.start(entry=elsewhere) == _no_project(world, "none", NONE_HEADER)
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
    assert storeless.service.store_exists() is False
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
    assert world.service.store_exists() is True


def test_a_prompt_whose_directory_cannot_be_mapped_records_nothing(world):
    world.main_tree = lambda path: None
    context, created = world.prompt()
    assert (context.state, created) == ("degraded", False)
    assert world.row() is None


# --- end_session / stop_decision --------------------------------------------------

def test_end_marks_the_row_and_an_unknown_session_is_left_alone(world):
    world.start()
    before = world.row()
    world.service.end_session(KEY)
    after = world.row()
    assert after.ended_at is not None and after.last_active_at >= before.last_active_at
    world.service.end_session(OTHER_KEY)
    assert world.row(OTHER_KEY) is None


def test_stop_nudges_after_n_unsaved_prompts_then_every_m(world):
    world.start()
    decisions = []
    for _ in range(10):
        world.prompt()
        decisions.append(world.service.stop_decision(KEY))
    assert decisions == [False] * 4 + [True] + [False] * 4 + [True]
    world.write(_registered(world))
    for _ in range(4):
        world.prompt()
        assert world.service.stop_decision(KEY) is False


def test_stop_is_silent_for_pending_projectless_unknown_and_broken_sessions(world):
    for _ in range(6):
        world.prompt()                                  # pending
    assert world.service.stop_decision(KEY) is False
    world.start(entry=world.base, key=OTHER_KEY)        # registered, no project
    for _ in range(6):
        world.prompt(key=OTHER_KEY)
    assert world.service.stop_decision(OTHER_KEY) is False
    assert world.service.stop_decision(SessionKey("codex", "unknown")) is False
    world.session_store = Broken(world.session_store, "nudge_if_due")
    assert world.build().stop_decision(KEY) is False


# --- session_context -----------------------------------------------------------------

def test_session_context_follows_the_stored_row(world):
    assert world.service.session_context(None) == _no_project(
        world, "unidentified", UNIDENTIFIED_HEADER, key=None)
    assert world.service.session_context(KEY) == _no_project(
        world, "unidentified", UNIDENTIFIED_HEADER)
    world.prompt()
    assert world.service.session_context(KEY) == _no_project(world, "pending", PENDING_HEADER)
    world.start(entry=world.base, key=OTHER_KEY)
    assert world.service.session_context(OTHER_KEY) == _no_project(
        world, "none", NONE_HEADER, key=OTHER_KEY)
    third = SessionKey("claude-code", "session-3")
    world.start(key=third)
    assert world.service.session_context(third) == ProjectContext(
        **{**_registered(world).__dict__, "session_key": third})


@pytest.mark.parametrize("project_id", ["zzzzzzzzzz", "global"])
def test_a_session_whose_project_is_gone_or_global_is_degraded(world, project_id):
    world.start()
    world.sql("UPDATE sessions SET project_id = ?",
              world.global_id if project_id == "global" else project_id)
    context = world.service.session_context(KEY)
    assert context == ProjectContext(
        "degraded", context.header, ReadWriteSet(project_id=None,
                                                 global_project_id=world.global_id),
        diagnostic="session project missing", session_key=KEY)
    assert "(session project missing)" in context.header


def test_a_store_failure_is_an_unavailable_session_context(world):
    world.session_store = Broken(world.session_store, "get")
    context = world.build().session_context(KEY)
    assert context.state == "unavailable" and context.session_key == KEY
    assert context.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)


# --- confirm_session ------------------------------------------------------------------

def test_confirming_a_candidate_registers_its_project(world):
    world.prompt()
    assert world.service.confirm_session(KEY) == _registered(world)
    assert world.intact_calls == [world.project.root]
    assert (world.row().status, world.row().project_id) == ("registered", world.project.id)
    # idempotent: a registered row answers unchanged
    assert world.service.confirm_session(KEY) == _registered(world)


def test_confirming_a_null_candidate_registers_no_project(world):
    world.prompt(entry=world.base)
    assert world.service.confirm_session(KEY) == _no_project(world, "none", NONE_HEADER)
    assert world.intact_calls == []
    assert (world.row().status, world.row().project_id) == ("registered", None)


def test_a_candidate_whose_root_is_not_intact_is_refused_and_stays_pending(world):
    world.prompt()
    world.intact = False
    with pytest.raises(ProjectUnavailable) as caught:
        world.service.confirm_session(KEY)
    assert caught.value.reason == "candidate-changed"
    assert world.row().status == "pending"


def test_a_candidate_without_its_root_never_confirms_to_a_rootless_project(world):
    world.prompt()
    world.sql("UPDATE projects SET root = NULL WHERE id = ?", world.project.id)
    world.sql("UPDATE sessions SET candidate_root = NULL")
    with pytest.raises(ProjectUnavailable) as caught:
        world.service.confirm_session(KEY)
    assert caught.value.reason == "candidate-changed"
    assert world.row().status == "pending"


def test_confirming_an_unknown_session_is_unidentified(world):
    with pytest.raises(ProjectUnavailable) as caught:
        world.service.confirm_session(KEY)
    assert caught.value.reason == "unidentified"


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
    service = bootstrap.build_service(Settings(root=base / "store"), root=base / "store",
                                      home=base / "home")
    service.ensure_global()
    project = service.init_project("demo", service.plan_root(str(main / "sub")))
    service.observe_prompt(KEY, prompt="hi", entry_dir=str(base / "wt" / "sub"),
                           transcript_path=None)
    assert service.pending_candidate(service.session_context(KEY)) == project
    (main / "sub").rmdir()          # the main tree's checkout no longer has it
    context = service.confirm_session(KEY)
    assert (context.state, context.read_write_set.project_id) == ("registered", project.id)



def _real_service(base: Path) -> MemoryService:
    service = bootstrap.build_service(Settings(root=base / "store"), root=base / "store",
                                      home=base / "home")
    service.ensure_global()
    return service


def test_a_symlinked_alias_registers_and_proposes_its_targets_project(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    (base / "a" / "sub").mkdir(parents=True)
    (base / "b").mkdir()
    alias = base / "b" / "alias"
    alias.symlink_to(base / "a" / "sub")
    service = _real_service(base)
    target = service.init_project("a", service.plan_root(str(base / "a")))
    service.init_project("b", service.plan_root(str(base / "b")))
    assert service.open_project_context(str(alias)).project == target
    context = service.start_session(KEY, source="startup", entry_dir=str(alias),
                                    transcript_path=None)
    assert context.project == target
    assert service.list_sessions()[0].entry_cwd == str(base / "a" / "sub")
    pending, _ = service.observe_prompt(OTHER_KEY, prompt="hi", entry_dir=str(alias),
                                        transcript_path=None)
    assert pending.state == "pending" and service.pending_candidate(pending) == target


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
    service = _real_service(base)
    project = service.init_project("demo", service.plan_root(str(main)))
    context = service.start_session(KEY, source="startup", entry_dir=str(base / "link"),
                                    transcript_path=None)
    assert context.project == project
    row = service.list_sessions()[0]
    assert (row.entry_cwd, row.branch) == (str(base / "wt" / "sub"), "feature")


def test_an_entry_with_a_nul_is_degraded_and_writes_nothing(tmp_path):
    service = _real_service(Path(os.path.realpath(tmp_path)))
    entry = "/tmp/x" + chr(0) + "y"
    assert service.start_session(KEY, source="startup", entry_dir=entry,
                                 transcript_path=None).state == "degraded"
    context, created = service.observe_prompt(KEY, prompt="hi", entry_dir=entry,
                                              transcript_path=None)
    assert (context.state, created) == ("degraded", False)
    assert service.list_sessions() == []


# --- search_sessions / list_sessions / pending_candidate / entry_of -----------------------

def test_session_search_sees_only_the_writable_projects_registered_rows(world):
    for number in range(12):
        world.start(key=SessionKey("codex", f"s{number}"))
    world.prompt(key=SessionKey("codex", "pending"))
    world.start(entry=world.base, key=SessionKey("codex", "unbound"))
    registered = _registered(world)
    assert len(world.service.search_sessions("", registered)) == 10
    assert len(world.service.search_sessions("", registered, 100)) == 12
    assert len(world.service.search_sessions("", registered, 0)) == 1
    assert {s.key.session_id for s in world.service.search_sessions("", registered, 50)} == {
        f"s{n}" for n in range(12)}
    for context in (world.service.session_context(SessionKey("codex", "pending")),
                    world.service.session_context(SessionKey("codex", "unbound")),
                    world.service.session_context(None)):
        assert world.service.search_sessions("", context) == []


def test_list_sessions_reads_every_row_with_optional_filters(world):
    world.start(key=SessionKey("codex", "a"))
    world.prompt("needle here", key=SessionKey("codex", "b"))
    assert {s.key.session_id for s in world.service.list_sessions()} == {"a", "b"}
    assert [s.key.session_id for s in world.service.list_sessions(query="NEEDLE")] == ["b"]
    assert [s.key.session_id for s in world.service.list_sessions(
        project_id=world.project.id)] == ["a"]
    assert len(world.service.list_sessions(limit=1)) == 1


def test_pending_candidate_and_entry_of_answer_from_the_stored_row(world):
    world.prompt()
    pending = world.service.session_context(KEY)
    assert world.service.pending_candidate(pending) == world.project
    assert world.service.entry_of(pending) == str(world.work)
    world.prompt(entry=world.base, key=OTHER_KEY)
    assert world.service.pending_candidate(world.service.session_context(OTHER_KEY)) is None
    directory = world.service.open_project_context(str(world.work))
    assert world.service.pending_candidate(directory) is None
    assert world.service.entry_of(directory) is None


# --- tools through a session context ------------------------------------------------------

def test_record_and_update_move_the_save_watermark(world):
    context = world.start()
    for _ in range(3):
        world.prompt()
    memory = world.write(context)
    assert world.row().last_write_prompt_count == 3
    world.prompt()
    world.service.update(memory.id, "uv manages all python", context,
                         expected_version=memory.version)
    assert world.row().last_write_prompt_count == 4


def test_a_failed_save_leaves_the_watermark_alone(world):
    context = world.start()
    world.prompt()
    with pytest.raises(ContentRejected):
        world.write(context, SECRET_PROMPT)
    memory = world.write(context)
    world.prompt()
    with pytest.raises(VersionConflict):
        world.service.update(memory.id, "changed", context,
                             expected_version=memory.version + 1)
    assert world.row().last_write_prompt_count == 1


def test_a_failed_watermark_never_fails_the_save(world):
    context = world.start()
    world.session_store = Broken(world.session_store, "mark_saved")
    service = world.build()
    memory = service.record(content="kept", type="user", sync=False, harness="codex",
                            description="", context=context)
    assert service.update(memory.id, "kept too", context,
                          expected_version=memory.version).version == memory.version + 1


def test_delete_never_moves_the_watermark(world):
    context = world.start()
    memory = world.write(context)
    world.prompt()
    world.service.delete(memory.id, context, expected_version=memory.version)
    assert world.row().last_write_prompt_count == 0


def test_only_read_records_last_read_at(world):
    context = world.start()
    memory = world.write(context)
    world.service.index(context)
    world.service.search("uv", context)
    assert world.service.show(memory.id).last_read_at is None
    world.service.read(memory.id, context)
    assert world.service.show(memory.id).last_read_at is not None


def test_a_failed_last_read_at_never_fails_the_read(world):
    context = world.start()
    memory = world.write(context)
    world.memory_store = Broken(world.memory_store, "touch_read")
    assert world.build().read(memory.id, context) == memory


def test_a_pending_session_can_neither_write_update_nor_delete(world):
    registered = world.start(key=OTHER_KEY)
    memory = world.service.record(content="fact", type="user", sync=False, harness="codex",
                                  description="", context=registered)
    world.prompt()
    pending = world.service.session_context(KEY)
    for attempt in (
            lambda: world.write(pending),
            lambda: world.service.update(memory.id, "x", pending,
                                         expected_version=memory.version),
            lambda: world.service.delete(memory.id, pending, expected_version=memory.version)):
        with pytest.raises(ProjectUnavailable) as caught:
            attempt()
        assert caught.value.reason == "pending"
    assert world.service.read(memory.id, registered).version == memory.version
