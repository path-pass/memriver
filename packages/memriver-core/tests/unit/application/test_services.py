"""MemoryService and ProjectService against in-memory fakes: orchestration without a store."""

from __future__ import annotations

import pytest
from memriver_core.application.memory import EMPTY_INDEX, MemoryService
from memriver_core.application.projects import ProjectService
from memriver_core.bootstrap import Services
from memriver_core.models import (
    ID_RE,
    Memory,
    Project,
    ProjectContext,
    ReadWriteSet,
    Resolution,
    RootPlan,
    UnbindPlan,
)
from memriver_core.models.changes import Create, SoftDelete, Update
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
READ_WRITE_SET = ReadWriteSet(project_id=P, global_project_id=G)
NO_PROJECT = ReadWriteSet(project_id=None, global_project_id=G)
CONTEXT = ProjectContext("registered", "", READ_WRITE_SET)
NO_PROJECT_CONTEXT = ProjectContext("none", "", NO_PROJECT)
# the session callbacks these directory-mode tests never need: nothing is
# pending, and there is no session to mark saved
SESSION_CALLBACKS = {"refuse_pending": lambda context: None,
                     "mark_saved": lambda context: None}


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
        self.ensure_global_calls = 0
        self.resolution = Resolution("none")
        self.calls: list[tuple] = []

    def _next_failure(self):
        if self.failures:
            raise self.failures.pop(0)

    def create(self, project, plan):
        self.attempted_ids.append(project.id)
        self._next_failure()
        self.projects[project.id] = Project(id=project.id, name=project.name, root=plan.root)

    def read(self, project_id):
        if project_id not in self.projects:
            raise ProjectNotFound(project_id)
        return self.projects[project_id]

    def list_projects(self):
        return ([p for p in self.projects.values() if p.id != self.global_id]
                + [p for p in self.projects.values() if p.id == self.global_id])

    def global_project_id(self):
        return self.global_id

    def ensure_global(self):
        self.ensure_global_calls += 1
        self._next_failure()
        if self.global_id is None:
            self.global_id = "nnnnnnnnnn"
        return self.global_id

    def search(self, project_id, read_write_set, *, query, limit):
        self.search_calls.append((project_id, query, limit))
        # a None read/write set is the management view: every project readable
        if read_write_set is not None and project_id not in read_write_set.readable():
            return []
        hits = [m for m in self.memories if m.project_id == project_id
                and (query is None or (query != "" and query.lower() in m.body.lower()))]
        hits.sort(key=lambda m: (m.updated, m.id), reverse=True)
        return hits if limit is None else hits[:limit]

    def resolve(self, start, *, ignoring=None):
        return self.resolution

    def plan_root(self, directory, project_id):
        return RootPlan(root=directory, store="/s", nested=(), already_bound=False)

    def bind(self, project_id, plan):
        self.calls.append(("bind", project_id, plan))

    def plan_unbind(self, project_id, root, cwd):
        self.calls.append(("plan_unbind", project_id, root, cwd))
        return UnbindPlan(project_id=project_id, root=root, store="/s"), self.resolution

    def unbind(self, plan):
        self.calls.append(("unbind", plan))


class FakeMemoryStore:
    """Records what the facade asks; a create lands in the project store's memories."""

    def __init__(self, project_store: FakeProjectStore) -> None:
        self.project_store = project_store
        self.calls: list[tuple] = []
        self.failures: list[Exception] = []     # raised, in order, by apply
        self.attempts = 0
        self.stored: dict[str, Memory] = {}

    def write(self, op, *, restriction, changed_by, changed_via, check):
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        self.calls.append((type(op).__name__, op, restriction, changed_by, changed_via))
        if isinstance(op, Update):
            raise GlobalReadOnly()
        if isinstance(op, Create):
            memory = Memory.new(body=op.body, type=op.type, project_id=op.project_id,
                                source={"harness": changed_via or "unknown",
                                        "method": changed_by},
                                sync=op.sync, description=op.description)
            self.stored[memory.id] = memory
            self.project_store.memories.append(memory)
            return memory
        return Memory(id=op.memory_id, project_id=P, type="user", source={}, trust="agent",
                      sync=True, created="c", updated="u", description="", body="b",
                      version=op.expected_version + 1, deleted_at="u")

    def read(self, memory_id, read_write_set):
        self.calls.append(("read", memory_id, read_write_set))
        raise MemoryNotFound(memory_id)

    def read_any(self, memory_id, *, include_deleted):
        self.calls.append(("read_any", memory_id, include_deleted))
        if memory_id in self.stored:
            return self.stored[memory_id]
        raise MemoryNotFound(memory_id)


def _services(project_store=None, *, budget=100, limit_default=5, limit_max=50):
    project_store = project_store or FakeProjectStore([Project(id=P, name="demo")])
    memory_store = FakeMemoryStore(project_store)
    policy = FakeContentPolicy()
    memory = MemoryService(memory_store, project_store, lambda: policy, **SESSION_CALLBACKS,
                           max_body_chars=100, metadata_max_chars=200,
                           search_limit_default=limit_default, search_limit_max=limit_max,
                           index_budget_lines=budget, index_cue_chars=60)
    project = ProjectService(project_store, header_field_chars=120, project_name_max_chars=120)
    # no session or maintenance service: these directory-mode tests reach neither
    services = Services(memory=memory, project=project, session=None, maintenance=None)
    return services, memory_store, project_store, policy


def _memory(project_id, body, updated, description=""):
    m = Memory.new(body=body, type="user", project_id=project_id, source={},
                   description=description)
    m.updated = updated
    return m


# --- project contexts ------------------------------------------------------

def test_open_project_context_registered_names_the_project_and_its_root():
    services, _, project_store, _ = _services()
    project_store.resolution = Resolution("registered",
                                          project=Project(id=P, name="demo", root="/w"))
    project_context = services.project.open_project_context("/w")
    assert project_context.state == "registered"
    assert project_context.header == f"project: demo [{P}] (root /w)"
    assert project_context.read_write_set == READ_WRITE_SET


def test_open_project_context_none_and_degraded_keep_global_readable_and_write_nothing():
    services, _, project_store, _ = _services()
    assert services.project.open_project_context("/x").header.startswith("project: none")
    project_store.resolution = Resolution("degraded", diagnostic="/r: matched by more than one project")
    project_context = services.project.open_project_context("/x")
    assert project_context.state == "degraded" and project_context.read_write_set == NO_PROJECT
    assert "/r: matched by more than one project" in project_context.header
    assert "memriver project explain" in project_context.header


def test_open_project_context_turns_a_storage_failure_into_an_unavailable_project_context():
    services, _, project_store, _ = _services()

    def broken(start, *, ignoring=None):
        raise StorageFailure

    project_store.resolve = broken
    project_context = services.project.open_project_context("/x")
    assert project_context.state == "unavailable"
    assert project_context.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)


def test_header_fields_are_single_line_and_capped():
    services, _, project_store, _ = _services()
    project_store.resolution = Resolution(
        "registered", project=Project(id=P, name="n\nx" + "y" * 200, root="/w\n" + "z" * 200))
    header = services.project.open_project_context("/w").header
    name, root = ("n x" + "y" * 200)[:120], ("/w " + "z" * 200)[:120]
    assert header == f"project: {name} [{P}] (root {root})"
    assert len(name) == len(root) == 120

    project_store.resolution = Resolution("degraded", diagnostic="/r:\n" + "d" * 200)
    header = services.project.open_project_context("/x").header
    diagnostic = ("/r: " + "d" * 200)[:120]
    assert "\n" not in header and len(diagnostic) == 120
    assert f"({diagnostic}); ask the user" in header


def test_open_project_context_turns_an_unreadable_global_into_an_unavailable_project_context():
    services, _, project_store, _ = _services()
    project_store.resolution = Resolution("registered",
                                          project=Project(id=P, name="demo", root="/w"))

    def broken():
        raise StorageFailure

    project_store.global_project_id = broken
    project_context = services.project.open_project_context("/w")
    assert project_context.state == "unavailable"
    assert project_context.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)


# --- record -----------------------------------------------------------------

def test_record_builds_one_create_restricted_to_the_read_write_set():
    services, memory_store, *_ = _services()
    memory = services.memory.record(content="uv manages python", type="project", sync=False,
                                    harness="claude-code", description="cue", context=CONTEXT)
    assert ID_RE.fullmatch(memory.id)
    assert (memory.project_id, memory.sync, memory.description) == (P, False, "cue")
    assert memory.source == {"harness": "claude-code", "method": "mcp"}
    assert memory.trust == "agent"
    [(name, op, restriction, changed_by, changed_via)] = memory_store.calls
    assert (name, op.project_id, op.trust, restriction, changed_by, changed_via) == \
        ("Create", P, "agent", READ_WRITE_SET, "mcp", "claude-code")
    assert memory is memory_store.stored[memory.id]      # the store's answer, not a re-read


def test_record_without_a_project_is_refused_before_any_check():
    services, memory_store, _, policy = _services()
    with pytest.raises(ProjectUnavailable):
        services.memory.record(content="x", type="user", sync=True, harness="h", description="",
                       context=NO_PROJECT_CONTEXT)
    assert memory_store.calls == [] and policy.calls == []


@pytest.mark.parametrize(("field", "kwargs"), [
    ("content", {"content": "token ghp_abc"}),
    ("harness", {"harness": "ghp_abc"}),
    ("description", {"description": "ghp_abc"}),
])
def test_record_runs_the_content_policy_on_every_stored_text(field, kwargs):
    services, memory_store, *_ = _services()
    args = {"content": "fine", "type": "user", "sync": True, "harness": "h",
            "description": "", "context": CONTEXT, **kwargs}
    with pytest.raises(ContentRejected):
        services.memory.record(**args)
    assert memory_store.calls == []


@pytest.mark.parametrize("harness", ["", "has space", "x" * 65, "a/b"])
def test_record_refuses_a_malformed_harness(harness):
    services, *_ = _services()
    with pytest.raises(ContentRejected):
        services.memory.record(content="c", type="user", sync=True, harness=harness,
                       description="", context=CONTEXT)


def test_record_takes_no_name_argument():
    services, *_ = _services()
    with pytest.raises(TypeError):
        services.memory.record(content="c", type="user", sync=True, harness="h", description="",
                       context=CONTEXT, name="n")  # type: ignore[call-arg]


def test_the_content_policy_is_built_only_when_a_write_needs_it():
    built: list[int] = []
    project_store = FakeProjectStore([Project(id=P, name="demo")])
    memory_service = MemoryService(
        FakeMemoryStore(project_store), project_store,
        lambda: built.append(1) or FakeContentPolicy(), **SESSION_CALLBACKS,
        max_body_chars=100, metadata_max_chars=200, search_limit_default=5,
        search_limit_max=50, index_budget_lines=100, index_cue_chars=60)
    memory_service.index(CONTEXT)
    assert built == []
    memory_service.record(content="c", type="user", sync=True, harness="h", description="",
                          context=CONTEXT)
    memory_service.record(content="d", type="user", sync=True, harness="h", description="",
                          context=CONTEXT)
    assert built == [1]


# --- read / update / delete ---------------------------------------------------

def test_read_delegates_with_the_read_write_set():
    services, memory_store, *_ = _services()
    with pytest.raises(MemoryNotFound):
        services.memory.read("X", CONTEXT)
    assert memory_store.calls == [("read", "X", READ_WRITE_SET)]


def test_update_runs_the_content_policy_first():
    services, memory_store, *_ = _services()
    with pytest.raises(ContentRejected):
        services.memory.update("X", "ghp_abc", CONTEXT, expected_version=1)
    with pytest.raises(ContentRejected):
        services.memory.update("X", "fine", CONTEXT, expected_version=1, description="ghp_abc")
    assert memory_store.calls == []


def test_update_and_delete_are_one_restricted_op_each_with_the_expected_version():
    services, memory_store, *_ = _services()
    with pytest.raises(GlobalReadOnly):
        services.memory.update("m", "body", CONTEXT, expected_version=3)
    assert services.memory.delete("m", CONTEXT, expected_version=3) == 4
    (_, update, *update_rest), (_, delete, *delete_rest) = memory_store.calls
    assert update == Update("m", 3, description=None, body="body")
    assert delete == SoftDelete("m", 3)
    assert update_rest == delete_rest == [READ_WRITE_SET, "mcp", None]


# --- projects -----------------------------------------------------------------

def test_ensure_global_delegates():
    services, *_ = _services(FakeProjectStore([], global_id=None))
    assert services.project.ensure_global() == "nnnnnnnnnn"
    assert services.project.global_project_id() == "nnnnnnnnnn"


def test_init_project_validates_the_name_with_the_injected_cap():
    services, *_ = _services()
    plan = services.project.plan_root("/w")
    project = services.project.init_project("demo", plan)
    assert (project.name, project.root) == ("demo", "/w")
    with pytest.raises(ValueError, match="120"):
        services.project.init_project("x" * 121, plan)


def test_init_project_reports_a_collision_as_storage_failure():
    services, _, project_store, _ = _services()
    project_store.failures = [IdCollision("x")]
    with pytest.raises(StorageFailure):
        services.project.init_project("demo", services.project.plan_root("/w"))


def test_binding_calls_delegate_to_the_project_store():
    services, _, project_store, _ = _services()
    plan = services.project.plan_root("/w", P)
    services.project.adopt(P, plan)
    unbind_plan, resolution = services.project.plan_unbind(P, "/w", "/w/sub")
    services.project.unbind(unbind_plan)
    assert resolution == project_store.resolution
    assert project_store.calls == [("bind", P, plan), ("plan_unbind", P, "/w", "/w/sub"),
                                   ("unbind", unbind_plan)]


# --- a collision surfaces as StorageFailure, on the first store call ---------

def _record(services):
    return services.memory.record(content="fact", type="user", sync=True, harness="h",
                          description="", context=CONTEXT)


def test_record_surfaces_a_collision_as_storage_failure_after_one_call():
    services, memory_store, *_ = _services()
    memory_store.failures = [IdCollision("x")]
    with pytest.raises(StorageFailure) as exc_info:
        _record(services)
    assert memory_store.attempts == 1
    assert isinstance(exc_info.value.__cause__, IdCollision)


def test_a_non_collision_failure_is_final_on_the_first_call_too():
    services, memory_store, *_ = _services()
    memory_store.failures = [StorageFailure()]
    with pytest.raises(StorageFailure):
        _record(services)
    assert memory_store.attempts == 1


def test_ensure_global_surfaces_a_collision_the_same_way():
    services, _, project_store, _ = _services(FakeProjectStore([], global_id=None))
    project_store.failures = [IdCollision("y")]
    with pytest.raises(StorageFailure) as exc_info:
        services.project.ensure_global()
    assert project_store.ensure_global_calls == 1
    assert isinstance(exc_info.value.__cause__, IdCollision)


# --- search -------------------------------------------------------------------

@pytest.mark.parametrize(("asked", "normalized"), [(None, 5), (0, 1), (-3, 1), (7, 7), (999, 50)])
def test_one_clamp_normalizes_every_limit(asked, normalized):
    services, _, project_store, _ = _services()
    assert services.memory.normalize_search_limit(asked) == normalized
    services.memory.search("q", CONTEXT, asked)
    assert project_store.search_calls == [(P, "q", normalized), (G, "q", normalized)]


def test_search_is_project_first_then_global_in_one_budget():
    project_store = FakeProjectStore([Project(id=P, name="demo")])
    services, _, _, _ = _services(project_store, limit_default=2)
    project_store.memories = [_memory(P, "hit one", "2026-01-02"),
                              _memory(G, "hit two", "2026-01-03"),
                              _memory(G, "hit three", "2026-01-01")]
    hits = services.memory.search("hit", CONTEXT)
    assert [m.body for m in hits] == ["hit one", "hit two"]
    assert [c[0] for c in project_store.search_calls] == [P, G]


def test_a_spent_budget_skips_global():
    project_store = FakeProjectStore([Project(id=P, name="demo")])
    services, _, _, _ = _services(project_store)
    project_store.memories = [_memory(P, "hit", "2026-01-02"), _memory(G, "hit", "2026-01-03")]
    assert len(services.memory.search("hit", CONTEXT, limit=1)) == 1
    assert [c[0] for c in project_store.search_calls] == [P]


# --- index --------------------------------------------------------------------

def test_empty_index_is_the_sentinel():
    services, *_ = _services()
    assert services.memory.index(CONTEXT) == EMPTY_INDEX
    assert services.memory.index(ProjectContext("unavailable", "", ReadWriteSet(project_id=None, global_project_id=None))) == EMPTY_INDEX


def test_index_lists_the_project_then_global_with_a_global_tag():
    services, _, project_store, _ = _services()
    mine = _memory(P, "mine body", "2026-09-01T00:00:00.000000Z", description="my cue")
    shared = _memory(G, "global body\nsecond line", "2026-09-05T00:00:00.000000Z")
    project_store.memories += [mine, shared]
    assert services.memory.index(CONTEXT).splitlines() == [
        f"- [user] {mine.id}: my cue (2026-09-01)",
        f"- [user, global] {shared.id}: global body (2026-09-05)",
    ]


def test_index_uses_two_single_project_searches():
    services, _, project_store, _ = _services()
    services.memory.index(CONTEXT)
    assert project_store.search_calls == [(P, None, None), (G, None, None)]


def test_index_without_a_project_lists_global_only():
    services, _, project_store, _ = _services()
    project_store.memories.append(_memory(G, "g", "2026-09-05T00:00:00.000000Z"))
    assert services.memory.index(NO_PROJECT_CONTEXT).startswith("- [user, global] ")
    assert project_store.search_calls == [(G, None, None)]


def test_index_fills_one_budget_project_first_and_counts_what_it_dropped():
    services, _, project_store, _ = _services(budget=3)
    project_store.memories += [_memory(P, f"p{i}", f"2026-09-0{i}T00:00:00.000000Z")
                               for i in range(1, 5)]
    project_store.memories += [_memory(G, "g", "2026-09-09T00:00:00.000000Z")]
    lines = services.memory.index(CONTEXT).splitlines()
    assert [line.split(": ", 1)[1] for line in lines[:3]] == \
        ["p4 (2026-09-04)", "p3 (2026-09-03)", "p2 (2026-09-02)"]
    assert lines[3] == "… (2 more entries omitted; use memory_search)"


def test_index_lines_are_single_line_and_capped():
    services, _, project_store, _ = _services()
    project_store.memories.append(_memory(P, "x", "2026-09-01T00:00:00.000000Z",
                                          description="line\none " + "y" * 100))
    line = services.memory.index(CONTEXT)
    assert "\n" not in line
    assert len(line.split(": ", 1)[1].rsplit(" (", 1)[0]) == 60


# --- management reads ---------------------------------------------------------

def test_management_reads_use_the_unrestricted_views():
    services, memory_store, project_store, _ = _services()
    project_store.memories = [_memory(P, "mine", "2026-01-02"), _memory(G, "global", "2026-01-03")]
    assert [m.body for m in services.memory.search_all("")] == []
    assert [m.body for m in services.memory.search_all("l")] == ["global"]
    assert [m.body for m in services.memory.search_all("n")] == ["mine"]
    listed = services.memory.list_memories()
    assert [p.id for p, _ in listed] == [P, G]
    with pytest.raises(MemoryNotFound):
        services.memory.show("m", include_deleted=True)
    assert memory_store.calls[-1] == ("read_any", "m", True)


def test_there_is_no_dream_and_no_create():
    services, *_ = _services()
    assert not hasattr(services.memory, "dream") and not hasattr(services.memory, "create")
