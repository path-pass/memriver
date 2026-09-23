"""MemoryService against in-memory fakes: orchestration without a filesystem."""

from __future__ import annotations

import pytest
from memriver_core.application.service import EMPTY_INDEX, MemoryService
from memriver_core.models import ID_RE, AccessContext, Memory, Project
from memriver_core.models.errors import (
    ContentRejected,
    GlobalReadOnly,
    IdCollision,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)

P = "aaaaaaaaaa"
G = "gggggggggg"
CTX = AccessContext(project_id=P, global_project_id=G)
NO_PROJECT = AccessContext(project_id=None, global_project_id=G)


class FakeContentPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def check(self, text: str, max_chars: int) -> None:
        self.calls.append((text, max_chars))
        if not text.strip():
            raise ContentRejected("content is empty; nothing to store")
        if "ghp_" in text:
            raise ContentRejected("looks like a secret")
        if len(text) > max_chars:
            raise ContentRejected("content too large")


class FakeProjectStore:
    def __init__(self, projects=(), global_id: str | None = G) -> None:
        self.projects = {p.id: p for p in projects}
        if global_id is not None:
            self.projects.setdefault(global_id, Project(id=global_id, name="global"))
        self.global_id = global_id
        self.memories: list[Memory] = []
        self.search_calls: list[tuple] = []
        self.failures: list[Exception] = []     # raised, in order, by create/ensure_global
        self.attempted_ids: list[str] = []

    def _next_failure(self):
        if self.failures:
            raise self.failures.pop(0)

    def create(self, project):
        self.attempted_ids.append(project.id)
        self._next_failure()
        self.projects[project.id] = project

    def read(self, project_id):
        if project_id not in self.projects:
            raise ProjectNotFound(project_id)
        return self.projects[project_id]

    def global_project_id(self):
        return self.global_id

    def ensure_global(self):
        self._next_failure()
        if self.global_id is None:
            self.global_id = "nnnnnnnnnn"
        return self.global_id

    def search(self, project_id, ctx, *, query, limit):
        self.search_calls.append((project_id, query, limit))
        if project_id not in ctx.readable():
            return []
        hits = [m for m in self.memories if m.project_id == project_id
                and (query is None or query.lower() in m.body.lower())]
        hits.sort(key=lambda m: (m.updated, m.id), reverse=True)
        return hits if limit is None else hits[:limit]


class FakeMemoryStore:
    def __init__(self, project_store: FakeProjectStore) -> None:
        self.project_store = project_store
        self.calls: list[tuple] = []
        self.failures: list[Exception] = []     # raised, in order, by record
        self.attempted_ids: list[str] = []

    def record(self, memory, ctx):
        self.attempted_ids.append(memory.id)
        if self.failures:
            raise self.failures.pop(0)
        self.calls.append(("record", memory.project_id, ctx))
        self.project_store.memories.append(memory)

    def read(self, memory_id, ctx):
        self.calls.append(("read", memory_id, ctx))
        raise MemoryNotFound(memory_id)

    def update(self, memory_id, ctx, *, body, description):
        self.calls.append(("update", memory_id, body, description))
        raise GlobalReadOnly()

    def delete(self, memory_id, ctx):
        self.calls.append(("delete", memory_id, ctx))


ATTEMPTS = 5


def _service(project_store=None, *, budget=100, limit_default=5, limit_max=50):
    project_store = project_store or FakeProjectStore([Project(id=P, name="demo")])
    memory_store = FakeMemoryStore(project_store)
    policy = FakeContentPolicy()
    service = MemoryService(memory_store, project_store, policy, max_body_chars=100,
                            metadata_max_chars=200, search_limit_default=limit_default,
                            search_limit_max=limit_max, index_budget_lines=budget,
                            id_generation_attempts=ATTEMPTS)
    return service, memory_store, project_store, policy


def _memory(project_id, body, updated, description=""):
    m = Memory.new(body=body, type="user", project_id=project_id, source={},
                   description=description)
    m.updated = updated
    return m


# --- access_context -------------------------------------------------------

def test_access_context_for_an_existing_project():
    service, *_ = _service()
    assert service.access_context(P) == CTX


@pytest.mark.parametrize("project_id", [None, G, "not-an-id", "zzzzzzzzzz"])
def test_access_context_drops_anything_but_an_existing_non_global_project(project_id):
    service, *_ = _service()
    assert service.access_context(project_id) == NO_PROJECT


def test_access_context_of_an_uninitialized_store_has_no_global():
    service, *_ = _service(FakeProjectStore([Project(id=P, name="demo")], global_id=None))
    assert service.access_context(P) == AccessContext(project_id=P, global_project_id=None)


# --- record -----------------------------------------------------------------

def test_record_targets_the_context_project_with_a_generated_id():
    service, memory_store, *_ = _service()
    memory = service.record(content="uv manages python", type="project", sync=False,
                            harness="claude-code", description="cue", ctx=CTX)
    assert ID_RE.fullmatch(memory.id)
    assert (memory.project_id, memory.sync, memory.description) == (P, False, "cue")
    assert memory.source == {"harness": "claude-code", "method": "agent"}
    assert memory.trust == "agent"
    assert memory_store.calls == [("record", P, CTX)]


def test_record_without_a_project_is_refused_before_any_check():
    service, memory_store, _, policy = _service()
    with pytest.raises(ProjectUnavailable):
        service.record(content="x", type="user", sync=True, harness="h", description="",
                       ctx=NO_PROJECT)
    assert memory_store.calls == [] and policy.calls == []


@pytest.mark.parametrize(("field", "kwargs"), [
    ("content", {"content": "token ghp_abc"}),
    ("harness", {"harness": "ghp_abc"}),
    ("description", {"description": "ghp_abc"}),
])
def test_record_runs_the_content_policy_on_every_stored_text(field, kwargs):
    service, memory_store, *_ = _service()
    args = {"content": "fine", "type": "user", "sync": True, "harness": "h",
            "description": "", "ctx": CTX, **kwargs}
    with pytest.raises(ContentRejected):
        service.record(**args)
    assert memory_store.calls == []


@pytest.mark.parametrize("harness", ["", "has space", "x" * 65, "a/b"])
def test_record_refuses_a_malformed_harness(harness):
    service, *_ = _service()
    with pytest.raises(ContentRejected):
        service.record(content="c", type="user", sync=True, harness=harness,
                       description="", ctx=CTX)


def test_record_takes_no_name_argument():
    service, *_ = _service()
    with pytest.raises(TypeError):
        service.record(content="c", type="user", sync=True, harness="h", description="",
                       ctx=CTX, name="n")  # type: ignore[call-arg]


# --- read / update / delete ---------------------------------------------------

def test_read_update_delete_delegate_with_the_context():
    service, memory_store, *_ = _service()
    with pytest.raises(MemoryNotFound):
        service.read("X", CTX)
    with pytest.raises(GlobalReadOnly):
        service.update("X", "new body", CTX, description=None)
    service.delete("X", CTX)
    assert memory_store.calls == [("read", "X", CTX), ("update", "X", "new body", None),
                                  ("delete", "X", CTX)]


def test_update_runs_the_content_policy_first():
    service, memory_store, *_ = _service()
    with pytest.raises(ContentRejected):
        service.update("X", "ghp_abc", CTX)
    with pytest.raises(ContentRejected):
        service.update("X", "fine", CTX, description="ghp_abc")
    assert memory_store.calls == []


# --- projects -----------------------------------------------------------------

def test_create_project_generates_an_id_and_stores_it():
    service, *_ = _service()
    project = service.create_project("  work ")
    assert ID_RE.fullmatch(project.id) and project.name == "work"
    assert service.read_project(project.id) == project


def test_create_project_refuses_a_bad_name():
    service, *_ = _service()
    with pytest.raises(ValueError):
        service.create_project("\n")


def test_ensure_global_delegates():
    service, *_ = _service(FakeProjectStore([], global_id=None))
    assert service.ensure_global() == "nnnnnnnnnn"
    assert service.global_project_id() == "nnnnnnnnnn"


# --- fresh ids on collision --------------------------------------------------

def _record(service):
    return service.record(content="fact", type="user", sync=True, harness="h",
                          description="", ctx=CTX)


def test_record_draws_a_fresh_id_after_a_collision():
    service, memory_store, *_ = _service()
    memory_store.failures = [IdCollision("x"), IdCollision("y")]
    memory = _record(service)
    assert len(memory_store.attempted_ids) == 3
    assert len(set(memory_store.attempted_ids)) == 3
    assert memory.id == memory_store.attempted_ids[-1]


def test_record_gives_up_after_the_configured_attempts():
    service, memory_store, *_ = _service()
    memory_store.failures = [IdCollision(str(i)) for i in range(ATTEMPTS)]
    with pytest.raises(StorageFailure):
        _record(service)
    assert len(memory_store.attempted_ids) == ATTEMPTS


def test_only_a_collision_is_retried():
    service, memory_store, *_ = _service()
    memory_store.failures = [StorageFailure()]
    with pytest.raises(StorageFailure):
        _record(service)
    assert len(memory_store.attempted_ids) == 1


def test_create_project_and_ensure_global_retry_collisions_the_same_way():
    service, _, project_store, _ = _service(FakeProjectStore([], global_id=None))
    project_store.failures = [IdCollision("x")]
    project = service.create_project("work")
    assert len(project_store.attempted_ids) == 2 and project.id == project_store.attempted_ids[-1]
    project_store.failures = [IdCollision("y"), IdCollision("z")]
    assert service.ensure_global() == "nnnnnnnnnn"
    project_store.failures = [IdCollision(str(i)) for i in range(ATTEMPTS)]
    with pytest.raises(StorageFailure):
        service.create_project("again")


# --- search -------------------------------------------------------------------

@pytest.mark.parametrize(("asked", "normalized"), [(None, 5), (0, 1), (-3, 1), (7, 7), (999, 50)])
def test_one_clamp_normalizes_every_limit(asked, normalized):
    service, _, project_store, _ = _service()
    assert service.normalize_search_limit(asked) == normalized
    service.search(P, "q", CTX, asked)
    assert project_store.search_calls == [(P, "q", normalized)]


# --- index --------------------------------------------------------------------

def test_empty_index_is_the_sentinel():
    service, *_ = _service()
    assert service.index(CTX) == EMPTY_INDEX
    assert service.index(AccessContext(project_id=None, global_project_id=None)) == EMPTY_INDEX


def test_index_lists_the_project_then_global_with_a_global_tag():
    service, _, project_store, _ = _service()
    mine = _memory(P, "mine body", "2026-09-01T00:00:00.000000Z", description="my cue")
    shared = _memory(G, "global body\nsecond line", "2026-09-05T00:00:00.000000Z")
    project_store.memories += [mine, shared]
    assert service.index(CTX).splitlines() == [
        f"- [user] {mine.id}: my cue (2026-09-01)",
        f"- [user, global] {shared.id}: global body (2026-09-05)",
    ]


def test_index_uses_two_single_project_searches():
    service, _, project_store, _ = _service()
    service.index(CTX)
    assert project_store.search_calls == [(P, None, None), (G, None, None)]


def test_index_without_a_project_lists_global_only():
    service, _, project_store, _ = _service()
    project_store.memories.append(_memory(G, "g", "2026-09-05T00:00:00.000000Z"))
    assert service.index(NO_PROJECT).startswith("- [user, global] ")
    assert project_store.search_calls == [(G, None, None)]


def test_index_fills_one_budget_project_first_and_counts_what_it_dropped():
    service, _, project_store, _ = _service(budget=3)
    project_store.memories += [_memory(P, f"p{i}", f"2026-09-0{i}T00:00:00.000000Z")
                               for i in range(1, 5)]
    project_store.memories += [_memory(G, "g", "2026-09-09T00:00:00.000000Z")]
    lines = service.index(CTX).splitlines()
    assert [line.split(": ", 1)[1] for line in lines[:3]] == \
        ["p4 (2026-09-04)", "p3 (2026-09-03)", "p2 (2026-09-02)"]
    assert lines[3] == "… (2 more entries omitted; use memory_search)"


def test_index_lines_are_single_line_and_capped():
    service, _, project_store, _ = _service()
    project_store.memories.append(_memory(P, "x", "2026-09-01T00:00:00.000000Z",
                                          description="line\none " + "y" * 100))
    line = service.index(CTX)
    assert "\n" not in line
    assert len(line.split(": ", 1)[1].rsplit(" (", 1)[0]) == 60


def test_there_is_no_dream_and_no_create():
    service, *_ = _service()
    assert not hasattr(service, "dream") and not hasattr(service, "create")
