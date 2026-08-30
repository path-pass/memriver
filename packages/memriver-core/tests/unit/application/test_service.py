"""MemoryService against in-memory fakes: the whole write/read pipeline
without a filesystem, a gate engine, or a transport.
"""

from __future__ import annotations

import inspect

import pytest
from memriver_core.application.errors import (
    ContentRejected,
    InvalidScope,
    MemoryNotFound,
    NameTaken,
    ProjectUnavailable,
)
from memriver_core.application.service import MemoryService
from memriver_core.models import (
    ID_RE,
    AccessContext,
    Memory,
    ProjectId,
    Scope,
    SearchHit,
)

PID = ProjectId("demo-abc123")
CTX = AccessContext(project_id=PID)
GLOBAL_ONLY = AccessContext(project_id=None)


class FakeContentPolicy:
    """Records every (text, max_chars) call and enforces only the budget."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def check(self, text: str, max_chars: int) -> None:
        self.calls.append((text, max_chars))
        if not text.strip():
            raise ContentRejected("content is empty; nothing to store")
        if len(text) > max_chars:
            raise ContentRejected(f"content too large ({len(text)} > {max_chars} chars)")


class FakeMemoryRepository:
    def __init__(self, memories: list[Memory] | None = None) -> None:
        self.memories = list(memories or [])
        self.created: list[Memory] = []
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.calls: list[tuple] = []
        self.search_result: list[SearchHit] = []
        self.updated_memory: Memory | None = None

    def create(self, memory: Memory, ctx: AccessContext) -> None:
        self.calls.append(("create", memory.id, ctx))
        if self.create_error is not None:
            raise self.create_error
        self.created.append(memory)
        self.memories.append(memory)

    def get(self, memory_id: str, ctx: AccessContext) -> Memory:
        self.calls.append(("get", memory_id, ctx))
        if self.get_error is not None:
            raise self.get_error
        for m in self.memories:
            if m.id == memory_id:
                return m
        raise MemoryNotFound(memory_id)

    def update_body(self, memory_id: str, body: str, ctx: AccessContext,
                    description: str | None = None) -> Memory:
        self.calls.append(("update_body", memory_id, body, description))
        self.updated_memory = memory(memory_id, body=body)
        return self.updated_memory

    def delete(self, memory_id: str, ctx: AccessContext) -> None:
        self.calls.append(("delete", memory_id, ctx))

    def iter_visible(self, ctx: AccessContext):
        self.calls.append(("iter_visible", ctx))
        return iter(list(self.memories))

    def search(self, query: str, ctx: AccessContext, limit: int) -> list[SearchHit]:
        self.calls.append(("search", query, ctx, limit))
        return self.search_result


def memory(memory_id: str, *, updated: str = "2026-01-01T00:00:00Z", body: str = "b",
           description: str = "", type: str = "project",
           scope: Scope | None = None) -> Memory:
    return Memory(id=memory_id, type=type, scope=scope or Scope.global_(), sync=True,
                  created=updated, updated=updated, source={}, trust="agent",
                  description=description, body=body)


def build(memory_repository=None, content_policy=None, **overrides) -> MemoryService:
    limits = {"max_body_chars": 8000, "metadata_max_chars": 8000,
              "search_limit_default": 5, "search_limit_max": 50,
              "index_budget_lines": 100}
    limits.update(overrides)
    return MemoryService(memory_repository or FakeMemoryRepository(),
                         content_policy or FakeContentPolicy(), **limits)


def write(service, ctx=CTX, **overrides):
    kwargs = {"content": "a durable fact", "type": "project", "name": "",
              "scope": "project", "sync": True, "harness": "claude-code",
              "description": "", "ctx": ctx}
    kwargs.update(overrides)
    return service.create(**kwargs)


# --- create: policy calls ---

def test_policy_call_order_and_arguments():
    content_policy, memory_repository = FakeContentPolicy(), FakeMemoryRepository()
    service = build(memory_repository, content_policy, max_body_chars=100)
    write(service, content="body text", name="My Name", description="a cue")
    assert content_policy.calls == [("claude-code", 8000), ("body text", 100),
                            ("a cue", 8000), ("My Name", 8000)]


def test_empty_description_and_name_are_not_gated():
    content_policy = FakeContentPolicy()
    write(build(content_policy=content_policy), description="   ", name="  ")
    assert content_policy.calls == [("claude-code", 8000), ("a durable fact", 8000)]


def test_metadata_budget_is_independent_of_the_body_budget():
    # a tightened body limit must not tighten metadata acceptance
    content_policy, memory_repository = FakeContentPolicy(), FakeMemoryRepository()
    service = build(memory_repository, content_policy, max_body_chars=10, metadata_max_chars=8000)
    write(service, content="short", description="d" * 20, name="n" * 20)
    assert content_policy.calls == [("claude-code", 8000), ("short", 10),
                            ("d" * 20, 8000), ("n" * 20, 8000)]
    assert len(memory_repository.created) == 1


def test_content_is_gated_with_the_configured_body_budget():
    with pytest.raises(ContentRejected):
        write(build(max_body_chars=10), content="x" * 11)


def test_rejected_content_never_reaches_the_repository():
    memory_repository = FakeMemoryRepository()
    with pytest.raises(ContentRejected):
        write(build(memory_repository, max_body_chars=10), content="x" * 11)
    assert memory_repository.created == []


# --- create: harness shape ---

@pytest.mark.parametrize("harness", ["bad harness", "", "a" * 65, "ghp/x"])
def test_invalid_harness_shape_is_refused_before_any_policy_call(harness):
    content_policy, memory_repository = FakeContentPolicy(), FakeMemoryRepository()
    with pytest.raises(ContentRejected) as err:
        write(build(memory_repository, content_policy), harness=harness)
    assert str(err.value) == ("invalid harness identifier "
                              "(allowed: letters, digits, ., _, -, max 64 chars)")
    assert content_policy.calls == [] and memory_repository.created == []


def test_harness_shape_accepts_the_documented_charset():
    content_policy = FakeContentPolicy()
    write(build(content_policy=content_policy), harness="Claude_Code-1.0")
    assert content_policy.calls[0] == ("Claude_Code-1.0", 8000)


# --- create: scope resolution ---

def test_global_scope_resolves_to_the_global_scope_value():
    memory_repository = FakeMemoryRepository()
    write(build(memory_repository), scope="global")
    assert memory_repository.created[0].scope == Scope.global_()


def test_project_scope_uses_the_context_project():
    memory_repository = FakeMemoryRepository()
    write(build(memory_repository), scope="project")
    assert memory_repository.created[0].scope == Scope.project(PID)


def test_explicit_current_project_scope_is_allowed():
    memory_repository = FakeMemoryRepository()
    write(build(memory_repository), scope=f"project:{PID}")
    assert memory_repository.created[0].scope == Scope.project(PID)


def test_project_scope_without_a_project_is_unavailable_and_path_free():
    memory_repository = FakeMemoryRepository()
    with pytest.raises(ProjectUnavailable) as err:
        write(build(memory_repository), ctx=GLOBAL_ONLY, scope="project")
    assert "/" not in str(err.value)  # the transport owns the path text
    assert memory_repository.created == []


def test_global_scope_still_works_without_a_project():
    memory_repository = FakeMemoryRepository()
    write(build(memory_repository), ctx=GLOBAL_ONLY, scope="global")
    assert memory_repository.created[0].scope == Scope.global_()


def test_foreign_project_scope_is_refused():
    memory_repository = FakeMemoryRepository()
    with pytest.raises(InvalidScope) as err:
        write(build(memory_repository), scope="project:other-000000")
    assert str(err.value) == ("scope 'project:other-000000' is outside the current "
                              "project; use 'project' or 'global'")
    assert memory_repository.created == []


def test_any_project_scope_is_refused_when_the_context_has_no_project():
    with pytest.raises(InvalidScope):
        write(build(), ctx=GLOBAL_ONLY, scope="project:other-000000")


def test_malformed_project_scope_keeps_the_outside_project_message():
    with pytest.raises(InvalidScope) as err:
        write(build(), scope="project:")
    assert str(err.value) == ("scope 'project:' is outside the current "
                              "project; use 'project' or 'global'")


def test_scope_outside_the_grammar_keeps_the_invalid_scope_message():
    with pytest.raises(InvalidScope) as err:
        write(build(), scope="team:x")
    assert str(err.value) == "invalid scope: 'team:x'"


# --- create: naming and collisions ---

def test_name_proposal_is_sanitized_into_the_id():
    memory_repository = FakeMemoryRepository()
    write(build(memory_repository), name="Mise Runtime_Mgmt!")
    assert memory_repository.created[0].id == "mise-runtime-mgmt"


@pytest.mark.parametrize("name", ["", "記憶"])
def test_unsalvageable_name_falls_back_to_a_ulid(name):
    memory_repository = FakeMemoryRepository()
    write(build(memory_repository), name=name)
    new_id = memory_repository.created[0].id
    assert len(new_id) == 26 and ID_RE.fullmatch(new_id)


def test_created_memory_is_returned_and_carries_its_source():
    memory_out = write(build(), harness="claude-code")
    assert memory_out.source == {"harness": "claude-code", "method": "agent"}
    assert memory_out.body == "a durable fact"


def test_name_collision_from_the_repository_propagates():
    memory_repository = FakeMemoryRepository()
    existing = memory("taken")
    memory_repository.create_error = NameTaken("taken", existing=existing)
    with pytest.raises(NameTaken) as err:
        write(build(memory_repository), name="taken")
    # the service passes the error through untouched: fields and all
    assert err.value.memory_id == "taken"
    assert err.value.existing is existing


# --- read / update / delete ---

def test_read_delegates_to_the_repository():
    memory_repository = FakeMemoryRepository([memory("known")])
    assert build(memory_repository).read("known", CTX).id == "known"


def test_read_propagates_not_found():
    with pytest.raises(MemoryNotFound):
        build(FakeMemoryRepository()).read("nope", CTX)


def test_update_gates_body_then_description():
    content_policy, memory_repository = FakeContentPolicy(), FakeMemoryRepository()
    service = build(memory_repository, content_policy, max_body_chars=100)
    service.update("known", "new body", CTX, description="new cue")
    assert content_policy.calls == [("new body", 100), ("new cue", 8000)]
    assert memory_repository.calls == [("update_body", "known", "new body", "new cue")]


@pytest.mark.parametrize("description", [None, "", "   "])
def test_update_skips_an_absent_or_empty_description(description):
    content_policy, memory_repository = FakeContentPolicy(), FakeMemoryRepository()
    build(memory_repository, content_policy).update("known", "new body", CTX, description=description)
    assert content_policy.calls == [("new body", 8000)]
    assert memory_repository.calls == [("update_body", "known", "new body", description)]


def test_update_rejects_an_oversized_body_before_touching_storage():
    memory_repository = FakeMemoryRepository()
    with pytest.raises(ContentRejected):
        build(memory_repository, max_body_chars=10).update("known", "x" * 11, CTX)
    assert memory_repository.calls == []


def test_delete_delegates_to_the_repository():
    memory_repository = FakeMemoryRepository()
    build(memory_repository).delete("known", CTX)
    assert memory_repository.calls == [("delete", "known", CTX)]


# --- search ---

def test_search_uses_the_configured_default_limit():
    memory_repository = FakeMemoryRepository()
    build(memory_repository, search_limit_default=5).search("q", CTX)
    assert memory_repository.calls == [("search", "q", CTX, 5)]


def test_search_passes_a_limit_inside_the_range_through():
    memory_repository = FakeMemoryRepository()
    build(memory_repository).search("q", CTX, limit=7)
    assert memory_repository.calls[0][3] == 7


@pytest.mark.parametrize("limit", [0, -1])
def test_search_clamps_the_limit_up_to_one(limit):
    memory_repository = FakeMemoryRepository()
    build(memory_repository).search("q", CTX, limit=limit)
    assert memory_repository.calls[0][3] == 1


def test_search_clamps_the_limit_down_to_the_maximum():
    memory_repository = FakeMemoryRepository()
    build(memory_repository, search_limit_max=50).search("q", CTX, limit=10 ** 9)
    assert memory_repository.calls[0][3] == 50


def test_search_returns_the_repository_hits():
    memory_repository = FakeMemoryRepository()
    memory_repository.search_result = [SearchHit(id="a", scope=Scope.global_(), type="user",
                                    snippet="s")]
    assert build(memory_repository).search("q", CTX) == memory_repository.search_result


# --- dream ---

def test_dream_limit_is_required():
    limit = inspect.signature(MemoryService.dream).parameters["limit"]
    assert limit.default is inspect.Parameter.empty


def test_dream_batch_cap_is_a_signature_literal():
    cap = inspect.signature(MemoryService.dream).parameters["max_limit"]
    assert cap.default == 10  # fixed internal guard, not config-backed


def test_dream_returns_the_least_recently_confirmed_first():
    memory_repository = FakeMemoryRepository([memory("entry-2", updated="2026-08-01T00:00:00Z"),
                           memory("entry-0", updated="2026-01-01T00:00:00Z"),
                           memory("entry-1", updated="2026-06-01T00:00:00Z")])
    assert [m.id for m in build(memory_repository).dream(CTX, 3)] == [
        "entry-0", "entry-1", "entry-2"]


def test_dream_breaks_ties_by_id():
    memory_repository = FakeMemoryRepository([memory(i) for i in ["b", "a", "c"]])
    assert [m.id for m in build(memory_repository).dream(CTX, 3)] == ["a", "b", "c"]


def test_dream_clamps_the_limit_up_from_zero():
    memory_repository = FakeMemoryRepository([memory("entry-0", updated="2026-01-01T00:00:00Z"),
                           memory("entry-1", updated="2026-06-01T00:00:00Z")])
    hits = build(memory_repository).dream(CTX, 0)
    assert [m.id for m in hits] == ["entry-0"]


def test_dream_clamps_the_limit_down_to_the_batch_cap():
    memory_repository = FakeMemoryRepository([memory(f"entry-{i:02d}", updated=f"2026-01-{i + 1:02d}"
                                  "T00:00:00Z") for i in range(15)])
    assert len(build(memory_repository).dream(CTX, 10 ** 9)) == 10


def test_dream_of_an_empty_store_is_empty():
    assert build(FakeMemoryRepository()).dream(CTX, 5) == []


# --- index ---

def test_index_lines_and_budget():
    memory_repository = FakeMemoryRepository([memory(f"entry-{i}", updated=f"2026-01-0{i + 1}T00:00:00Z",
                                  body=f"记忆条目内容 {i}") for i in range(5)])
    lines = build(memory_repository, index_budget_lines=3).index(CTX).splitlines()
    assert len(lines) == 4  # 3 entries + 1 omitted-notice line
    assert lines[0].startswith("- [project] ")
    assert "2 more entries omitted; use memory_search" in lines[-1]
    assert "index_budget_lines" not in lines[-1]  # agents cannot change this knob


def test_index_of_an_empty_store():
    assert "no memories yet" in build(FakeMemoryRepository()).index(CTX)


def test_index_tolerates_an_empty_body():
    # entry files are hand-editable, so an empty body can reach the store
    # without passing through the write gate; it must not break the index
    memory_repository = FakeMemoryRepository([memory("hand-edited", body="")])
    assert "hand-edited" in build(memory_repository).index(CTX)


def test_index_orders_newest_first_with_ulid_tiebreak():
    memory_repository = FakeMemoryRepository([
        memory("01" + "A" * 24, updated="2026-08-01T00:00:00Z", body="older entry"),
        memory("01" + "B" * 24, updated="2026-08-01T00:00:00Z",
               body="tied but larger id"),
        memory("01" + "C" * 24, updated="2026-08-02T00:00:00Z", body="newest entry"),
    ])
    lines = build(memory_repository).index(CTX).splitlines()
    assert "newest entry" in lines[0]
    assert "tied but larger id" in lines[1]  # ULID tiebreak within a timestamp tie
    assert "older entry" in lines[2]


def test_index_line_leads_with_name():
    memory_repository = FakeMemoryRepository([memory("mise-runtimes", type="user",
                                  body="runtimes are managed by mise")])
    out = build(memory_repository).index(CTX)
    assert out.splitlines()[0].startswith("- [user] mise-runtimes: runtimes")


def test_index_line_ends_with_the_updated_date():
    memory_repository = FakeMemoryRepository([memory("mise-runtimes", updated="2026-08-02T11:22:33Z")])
    assert build(memory_repository).index(CTX).splitlines()[0].endswith(" (2026-08-02)")


def test_index_prefers_description_over_body_first_line():
    memory_repository = FakeMemoryRepository([memory("mise-runtimes", type="user",
                                  body="the full body text",
                                  description="mise manages every runtime")])
    line = build(memory_repository).index(CTX).splitlines()[0]
    assert "mise manages every runtime" in line
    assert "the full body text" not in line


def test_index_falls_back_to_the_body_line_when_description_is_empty():
    memory_repository = FakeMemoryRepository([memory("mise-runtimes", type="user",
                                  body="runtimes are managed by mise\nsecond line")])
    line = build(memory_repository).index(CTX).splitlines()[0]
    assert "runtimes are managed by mise" in line
    assert "second line" not in line


def test_index_truncates_the_cue_to_60_chars():
    memory_repository = FakeMemoryRepository([memory("n", type="user", body="b", description="d" * 100)])
    line = build(memory_repository).index(CTX).splitlines()[0]
    assert "d" * 60 in line
    assert "d" * 61 not in line
