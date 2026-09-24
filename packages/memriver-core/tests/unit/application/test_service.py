"""MemoryService against in-memory fakes: orchestration without a store."""

from __future__ import annotations

import pytest
from memriver_core.application.service import EMPTY_INDEX, MemoryService
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
# the session collaborators these directory-mode tests never reach
SESSION_ARGUMENTS = {
    "session_store": None, "canonical_directory": lambda path: path,
    "main_tree_path": lambda path: path,
    "current_branch": lambda path: None, "root_is_intact": lambda root: True,
    "session_prompt_chars": 512, "session_recent_prompts": 5,
    "session_prompt_scan_max_bytes": 65536, "stop_nudge_min_prompts": 5,
    "stop_nudge_interval_prompts": 5, "session_search_limit_default": 10,
    "session_search_limit_max": 50,
}


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
    def __init__(self, project_store: FakeProjectStore) -> None:
        self.project_store = project_store
        self.calls: list[tuple] = []
        self.failures: list[Exception] = []     # raised, in order, by record
        self.attempted_ids: list[str] = []

    def record(self, memory, read_write_set):
        self.attempted_ids.append(memory.id)
        if self.failures:
            raise self.failures.pop(0)
        self.calls.append(("record", memory.project_id, read_write_set))
        self.project_store.memories.append(memory)

    def read(self, memory_id, read_write_set):
        self.calls.append(("read", memory_id, read_write_set))
        raise MemoryNotFound(memory_id)

    def update(self, memory_id, read_write_set, *, expected_version, body, description):
        self.calls.append(("update", memory_id, expected_version, body, description))
        raise GlobalReadOnly()

    def delete(self, memory_id, read_write_set, *, expected_version, hard):
        self.calls.append(("delete", memory_id, expected_version, hard))
        return expected_version + 1

    def read_any(self, memory_id, *, include_deleted):
        self.calls.append(("read_any", memory_id, include_deleted))
        raise MemoryNotFound(memory_id)


class FakeDiagnostics:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "report"


def _service(project_store=None, *, budget=100, limit_default=5, limit_max=50):
    project_store = project_store or FakeProjectStore([Project(id=P, name="demo")])
    memory_store = FakeMemoryStore(project_store)
    policy = FakeContentPolicy()
    service = MemoryService(memory_store, project_store, lambda: policy, FakeDiagnostics(),
                            max_body_chars=100, metadata_max_chars=200,
                            search_limit_default=limit_default, search_limit_max=limit_max,
                            index_budget_lines=budget, index_cue_chars=60,
                            header_field_chars=120, project_name_max_chars=120,
                            **SESSION_ARGUMENTS)
    return service, memory_store, project_store, policy


def _memory(project_id, body, updated, description=""):
    m = Memory.new(body=body, type="user", project_id=project_id, source={},
                   description=description)
    m.updated = updated
    return m


# --- project contexts ------------------------------------------------------

def test_open_project_context_registered_names_the_project_and_its_root():
    service, _, project_store, _ = _service()
    project_store.resolution = Resolution("registered",
                                          project=Project(id=P, name="demo", root="/w"))
    project_context = service.open_project_context("/w")
    assert project_context.state == "registered"
    assert project_context.header == f"project: demo [{P}] (root /w)"
    assert project_context.read_write_set == READ_WRITE_SET


def test_open_project_context_none_and_degraded_keep_global_readable_and_write_nothing():
    service, _, project_store, _ = _service()
    assert service.open_project_context("/x").header.startswith("project: none")
    project_store.resolution = Resolution("degraded", diagnostic="/r: matched by more than one project")
    project_context = service.open_project_context("/x")
    assert project_context.state == "degraded" and project_context.read_write_set == NO_PROJECT
    assert "/r: matched by more than one project" in project_context.header
    assert "memriver project explain" in project_context.header


def test_open_project_context_turns_a_storage_failure_into_an_unavailable_project_context():
    service, _, project_store, _ = _service()

    def broken(start, *, ignoring=None):
        raise StorageFailure

    project_store.resolve = broken
    project_context = service.open_project_context("/x")
    assert project_context.state == "unavailable"
    assert project_context.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)


def test_header_fields_are_single_line_and_capped():
    service, _, project_store, _ = _service()
    project_store.resolution = Resolution(
        "registered", project=Project(id=P, name="n\nx" + "y" * 200, root="/w\n" + "z" * 200))
    header = service.open_project_context("/w").header
    name, root = ("n x" + "y" * 200)[:120], ("/w " + "z" * 200)[:120]
    assert header == f"project: {name} [{P}] (root {root})"
    assert len(name) == len(root) == 120

    project_store.resolution = Resolution("degraded", diagnostic="/r:\n" + "d" * 200)
    header = service.open_project_context("/x").header
    diagnostic = ("/r: " + "d" * 200)[:120]
    assert "\n" not in header and len(diagnostic) == 120
    assert f"({diagnostic}); ask the user" in header


def test_open_project_context_turns_an_unreadable_global_into_an_unavailable_project_context():
    service, _, project_store, _ = _service()
    project_store.resolution = Resolution("registered",
                                          project=Project(id=P, name="demo", root="/w"))

    def broken():
        raise StorageFailure

    project_store.global_project_id = broken
    project_context = service.open_project_context("/w")
    assert project_context.state == "unavailable"
    assert project_context.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)


# --- record -----------------------------------------------------------------

def test_record_targets_the_read_write_set_project_with_a_generated_id():
    service, memory_store, *_ = _service()
    memory = service.record(content="uv manages python", type="project", sync=False,
                            harness="claude-code", description="cue", context=CONTEXT)
    assert ID_RE.fullmatch(memory.id)
    assert (memory.project_id, memory.sync, memory.description) == (P, False, "cue")
    assert memory.source == {"harness": "claude-code", "method": "agent"}
    assert memory.trust == "agent"
    assert memory_store.calls == [("record", P, READ_WRITE_SET)]


def test_record_without_a_project_is_refused_before_any_check():
    service, memory_store, _, policy = _service()
    with pytest.raises(ProjectUnavailable):
        service.record(content="x", type="user", sync=True, harness="h", description="",
                       context=NO_PROJECT_CONTEXT)
    assert memory_store.calls == [] and policy.calls == []


@pytest.mark.parametrize(("field", "kwargs"), [
    ("content", {"content": "token ghp_abc"}),
    ("harness", {"harness": "ghp_abc"}),
    ("description", {"description": "ghp_abc"}),
])
def test_record_runs_the_content_policy_on_every_stored_text(field, kwargs):
    service, memory_store, *_ = _service()
    args = {"content": "fine", "type": "user", "sync": True, "harness": "h",
            "description": "", "context": CONTEXT, **kwargs}
    with pytest.raises(ContentRejected):
        service.record(**args)
    assert memory_store.calls == []


@pytest.mark.parametrize("harness", ["", "has space", "x" * 65, "a/b"])
def test_record_refuses_a_malformed_harness(harness):
    service, *_ = _service()
    with pytest.raises(ContentRejected):
        service.record(content="c", type="user", sync=True, harness=harness,
                       description="", context=CONTEXT)


def test_record_takes_no_name_argument():
    service, *_ = _service()
    with pytest.raises(TypeError):
        service.record(content="c", type="user", sync=True, harness="h", description="",
                       context=CONTEXT, name="n")  # type: ignore[call-arg]


def test_the_content_policy_is_built_only_when_a_write_needs_it():
    built: list[int] = []
    project_store = FakeProjectStore([Project(id=P, name="demo")])
    service = MemoryService(FakeMemoryStore(project_store), project_store,
                            lambda: built.append(1) or FakeContentPolicy(), FakeDiagnostics(),
                            max_body_chars=100, metadata_max_chars=200, search_limit_default=5,
                            search_limit_max=50, index_budget_lines=100, index_cue_chars=60,
                            header_field_chars=120, project_name_max_chars=120,
                            **SESSION_ARGUMENTS)
    service.index(CONTEXT)
    service.open_project_context("/x")
    assert built == []
    service.record(content="c", type="user", sync=True, harness="h", description="",
                   context=CONTEXT)
    service.record(content="d", type="user", sync=True, harness="h", description="",
                   context=CONTEXT)
    assert built == [1]


# --- read / update / delete ---------------------------------------------------

def test_read_update_delete_delegate_with_the_read_write_set():
    service, memory_store, *_ = _service()
    with pytest.raises(MemoryNotFound):
        service.read("X", CONTEXT)
    with pytest.raises(GlobalReadOnly):
        service.update("X", "new body", CONTEXT, expected_version=1, description=None)
    assert service.delete("X", CONTEXT, expected_version=1) == 2
    assert memory_store.calls == [("read", "X", READ_WRITE_SET),
                                  ("update", "X", 1, "new body", None),
                                  ("delete", "X", 1, False)]


def test_update_runs_the_content_policy_first():
    service, memory_store, *_ = _service()
    with pytest.raises(ContentRejected):
        service.update("X", "ghp_abc", CONTEXT, expected_version=1)
    with pytest.raises(ContentRejected):
        service.update("X", "fine", CONTEXT, expected_version=1, description="ghp_abc")
    assert memory_store.calls == []


def test_update_and_delete_pass_the_expected_version_through():
    service, memory_store, _, _ = _service()
    with pytest.raises(GlobalReadOnly):
        service.update("m", "body", CONTEXT, expected_version=3)
    assert service.delete("m", CONTEXT, expected_version=3, hard=True) == 4
    assert memory_store.calls[-2:] == [("update", "m", 3, "body", None),
                                       ("delete", "m", 3, True)]


# --- projects -----------------------------------------------------------------

def test_ensure_global_delegates():
    service, *_ = _service(FakeProjectStore([], global_id=None))
    assert service.ensure_global() == "nnnnnnnnnn"
    assert service.global_project_id() == "nnnnnnnnnn"


def test_init_project_validates_the_name_with_the_injected_cap():
    service, *_ = _service()
    plan = service.plan_root("/w")
    project = service.init_project("demo", plan)
    assert (project.name, project.root) == ("demo", "/w")
    with pytest.raises(ValueError, match="120"):
        service.init_project("x" * 121, plan)


def test_init_project_reports_a_collision_as_storage_failure():
    service, _, project_store, _ = _service()
    project_store.failures = [IdCollision("x")]
    with pytest.raises(StorageFailure):
        service.init_project("demo", service.plan_root("/w"))


def test_binding_calls_delegate_to_the_project_store():
    service, _, project_store, _ = _service()
    plan = service.plan_root("/w", P)
    service.adopt(P, plan)
    unbind_plan, resolution = service.plan_unbind(P, "/w", "/w/sub")
    service.unbind(unbind_plan)
    assert resolution == project_store.resolution
    assert project_store.calls == [("bind", P, plan), ("plan_unbind", P, "/w", "/w/sub"),
                                   ("unbind", unbind_plan)]


# --- a collision surfaces as StorageFailure, on the first store call ---------

def _record(service):
    return service.record(content="fact", type="user", sync=True, harness="h",
                          description="", context=CONTEXT)


def test_record_surfaces_a_collision_as_storage_failure_after_one_call():
    service, memory_store, *_ = _service()
    memory_store.failures = [IdCollision("x")]
    with pytest.raises(StorageFailure) as exc_info:
        _record(service)
    assert len(memory_store.attempted_ids) == 1
    assert isinstance(exc_info.value.__cause__, IdCollision)


def test_a_non_collision_failure_is_final_on_the_first_call_too():
    service, memory_store, *_ = _service()
    memory_store.failures = [StorageFailure()]
    with pytest.raises(StorageFailure):
        _record(service)
    assert len(memory_store.attempted_ids) == 1


def test_ensure_global_surfaces_a_collision_the_same_way():
    service, _, project_store, _ = _service(FakeProjectStore([], global_id=None))
    project_store.failures = [IdCollision("y")]
    with pytest.raises(StorageFailure) as exc_info:
        service.ensure_global()
    assert project_store.ensure_global_calls == 1
    assert isinstance(exc_info.value.__cause__, IdCollision)


# --- search -------------------------------------------------------------------

@pytest.mark.parametrize(("asked", "normalized"), [(None, 5), (0, 1), (-3, 1), (7, 7), (999, 50)])
def test_one_clamp_normalizes_every_limit(asked, normalized):
    service, _, project_store, _ = _service()
    assert service.normalize_search_limit(asked) == normalized
    service.search("q", CONTEXT, asked)
    assert project_store.search_calls == [(P, "q", normalized), (G, "q", normalized)]


def test_search_is_project_first_then_global_in_one_budget():
    project_store = FakeProjectStore([Project(id=P, name="demo")])
    service, _, _, _ = _service(project_store, limit_default=2)
    project_store.memories = [_memory(P, "hit one", "2026-01-02"),
                              _memory(G, "hit two", "2026-01-03"),
                              _memory(G, "hit three", "2026-01-01")]
    hits = service.search("hit", CONTEXT)
    assert [m.body for m in hits] == ["hit one", "hit two"]
    assert [c[0] for c in project_store.search_calls] == [P, G]


def test_a_spent_budget_skips_global():
    project_store = FakeProjectStore([Project(id=P, name="demo")])
    service, _, _, _ = _service(project_store)
    project_store.memories = [_memory(P, "hit", "2026-01-02"), _memory(G, "hit", "2026-01-03")]
    assert len(service.search("hit", CONTEXT, limit=1)) == 1
    assert [c[0] for c in project_store.search_calls] == [P]


# --- index --------------------------------------------------------------------

def test_empty_index_is_the_sentinel():
    service, *_ = _service()
    assert service.index(CONTEXT) == EMPTY_INDEX
    assert service.index(ProjectContext("unavailable", "", ReadWriteSet(project_id=None, global_project_id=None))) == EMPTY_INDEX


def test_index_lists_the_project_then_global_with_a_global_tag():
    service, _, project_store, _ = _service()
    mine = _memory(P, "mine body", "2026-09-01T00:00:00.000000Z", description="my cue")
    shared = _memory(G, "global body\nsecond line", "2026-09-05T00:00:00.000000Z")
    project_store.memories += [mine, shared]
    assert service.index(CONTEXT).splitlines() == [
        f"- [user] {mine.id}: my cue (2026-09-01)",
        f"- [user, global] {shared.id}: global body (2026-09-05)",
    ]


def test_index_uses_two_single_project_searches():
    service, _, project_store, _ = _service()
    service.index(CONTEXT)
    assert project_store.search_calls == [(P, None, None), (G, None, None)]


def test_index_without_a_project_lists_global_only():
    service, _, project_store, _ = _service()
    project_store.memories.append(_memory(G, "g", "2026-09-05T00:00:00.000000Z"))
    assert service.index(NO_PROJECT_CONTEXT).startswith("- [user, global] ")
    assert project_store.search_calls == [(G, None, None)]


def test_index_fills_one_budget_project_first_and_counts_what_it_dropped():
    service, _, project_store, _ = _service(budget=3)
    project_store.memories += [_memory(P, f"p{i}", f"2026-09-0{i}T00:00:00.000000Z")
                               for i in range(1, 5)]
    project_store.memories += [_memory(G, "g", "2026-09-09T00:00:00.000000Z")]
    lines = service.index(CONTEXT).splitlines()
    assert [line.split(": ", 1)[1] for line in lines[:3]] == \
        ["p4 (2026-09-04)", "p3 (2026-09-03)", "p2 (2026-09-02)"]
    assert lines[3] == "… (2 more entries omitted; use memory_search)"


def test_index_lines_are_single_line_and_capped():
    service, _, project_store, _ = _service()
    project_store.memories.append(_memory(P, "x", "2026-09-01T00:00:00.000000Z",
                                          description="line\none " + "y" * 100))
    line = service.index(CONTEXT)
    assert "\n" not in line
    assert len(line.split(": ", 1)[1].rsplit(" (", 1)[0]) == 60


# --- management reads ---------------------------------------------------------

def test_management_reads_use_the_unrestricted_views():
    service, memory_store, project_store, _ = _service()
    project_store.memories = [_memory(P, "mine", "2026-01-02"), _memory(G, "global", "2026-01-03")]
    assert [m.body for m in service.search_all("")] == []
    assert [m.body for m in service.search_all("l")] == ["global"]
    assert [m.body for m in service.search_all("n")] == ["mine"]
    listed = service.list_memories()
    assert [p.id for p, _ in listed] == [P, G]
    with pytest.raises(MemoryNotFound):
        service.show("m", include_deleted=True)
    assert memory_store.calls[-1] == ("read_any", "m", True)


def test_diagnose_delegates_to_the_diagnostics_service():
    service, _, _, _ = _service()
    assert service.diagnose(stale_days=5) == "report"
    assert service._diagnostics.calls == [{"now": None, "stale_days": 5,
                                           "jaccard_threshold": 0.6}]


def test_there_is_no_dream_and_no_create():
    service, *_ = _service()
    assert not hasattr(service, "dream") and not hasattr(service, "create")
