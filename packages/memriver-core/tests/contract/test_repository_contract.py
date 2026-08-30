"""The MemoryRepository contract every backend must satisfy.

Nothing here knows how a repository stores anything. Every registered backend
runs this whole suite: to add one (SQLite, say), write a `BackendHarness` and
put a single entry in `BACKENDS` below -- no test in this file changes, and the
new backend is held to the same behaviour from that one line on.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from memriver_core.application.errors import (
    InvalidScope,
    MemoryNotFound,
    NameTaken,
    UnreadableMemory,
)
from memriver_core.models import AccessContext, Memory, ProjectId, Scope
from memriver_core.repository.filesystem import FileMemoryRepository
from memriver_core.repository.protocol import MemoryRepository

SOURCE = {"harness": "test", "session": "s", "method": "agent"}

MINE = ProjectId("mine-000000")
OTHER = ProjectId("other-000000")

GLOBAL = Scope.global_()

CTX = AccessContext(project_id=MINE)
GLOBAL_ONLY = AccessContext(project_id=None)
OTHER_CTX = AccessContext(project_id=OTHER)


@dataclass(frozen=True)
class BackendHarness:
    """Everything a backend must supply to be held to this contract.

    `make_repository` called twice with the SAME storage_dir must return two
    INDEPENDENT repository instances over the same underlying storage -- that
    is the seam the concurrency contract races against, standing in for two
    processes sharing one store.

    `occupy_unreadably` plants something undecodable at a global name, so the
    third collision outcome can be exercised without this file knowing what
    "undecodable" means for the backend.
    """

    make_repository: Callable[[Path], MemoryRepository]
    occupy_unreadably: Callable[[Path, str], None]


def _plant_unreadable_file(storage_dir: Path, memory_id: str) -> None:
    d = storage_dir / "global" / "entries"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{memory_id}.md").write_text("hand-written notes\n", encoding="utf-8")


BACKENDS: dict[str, BackendHarness] = {
    "filesystem": BackendHarness(
        make_repository=FileMemoryRepository,
        occupy_unreadably=_plant_unreadable_file,
    ),
}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request) -> BackendHarness:
    return BACKENDS[request.param]


@pytest.fixture
def storage_dir(tmp_path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def occupy_unreadably(backend, storage_dir):
    return lambda memory_id: backend.occupy_unreadably(storage_dir, memory_id)


@pytest.fixture
def memory_repository(backend, storage_dir) -> MemoryRepository:
    return backend.make_repository(storage_dir)


def _m(body="内容", type="project", scope=GLOBAL, id=None, description=""):
    return Memory.new(body=body, type=type, scope=scope, source=SOURCE, id=id,
                      description=description)


# --- CRUD ---

def test_create_then_get_round_trip(memory_repository):
    m = _m(id="n", description="a cue")
    memory_repository.create(m, CTX)
    assert memory_repository.get("n", CTX) == m


def test_create_and_get_in_the_current_project_scope(memory_repository):
    m = _m(scope=Scope.project(MINE), id="p")
    memory_repository.create(m, CTX)
    assert memory_repository.get("p", CTX) == m


def test_get_missing_raises_memory_not_found(memory_repository):
    with pytest.raises(MemoryNotFound) as err:
        memory_repository.get("nope", CTX)
    assert err.value.memory_id == "nope"


def test_update_body_rewrites_in_place(memory_repository):
    m = _m(body="old", id="n")
    memory_repository.create(m, CTX)
    updated = memory_repository.update_body("n", "new", CTX)
    assert updated.id == "n" and updated.body == "new"
    assert updated.updated >= m.updated
    again = memory_repository.get("n", CTX)
    assert again.body == "new" and again.created == m.created
    assert len(list(memory_repository.iter_visible(CTX))) == 1


def test_update_body_description_none_keeps_string_replaces_empty_clears(memory_repository):
    memory_repository.create(_m(body="old", id="n", description="original cue"), CTX)
    assert memory_repository.update_body("n", "new", CTX).description == "original cue"
    assert memory_repository.update_body("n", "n2", CTX, description="new cue").description == "new cue"
    assert memory_repository.update_body("n", "n3", CTX, description="").description == ""


def test_update_body_missing_raises_memory_not_found(memory_repository):
    with pytest.raises(MemoryNotFound):
        memory_repository.update_body("nope", "x", CTX)


def test_delete_removes_and_second_delete_raises(memory_repository):
    memory_repository.create(_m(id="n"), CTX)
    memory_repository.delete("n", CTX)
    with pytest.raises(MemoryNotFound):
        memory_repository.get("n", CTX)
    with pytest.raises(MemoryNotFound):
        memory_repository.delete("n", CTX)


# --- scope isolation ---

def test_another_projects_memory_is_invisible(memory_repository):
    memory_repository.create(_m(body="foreign", scope=Scope.project(OTHER), id="foreign"), OTHER_CTX)
    with pytest.raises(MemoryNotFound):
        memory_repository.get("foreign", CTX)
    assert [m.id for m in memory_repository.iter_visible(CTX)] == []


def test_global_memories_are_visible_to_every_context(memory_repository):
    memory_repository.create(_m(id="shared"), OTHER_CTX)
    assert [m.id for m in memory_repository.iter_visible(CTX)] == ["shared"]
    assert [m.id for m in memory_repository.iter_visible(GLOBAL_ONLY)] == ["shared"]


def test_iter_visible_returns_global_plus_current_project(memory_repository):
    memory_repository.create(_m(body="g", id="g"), CTX)
    memory_repository.create(_m(body="p", scope=Scope.project(MINE), id="p"), CTX)
    memory_repository.create(_m(body="f", scope=Scope.project(OTHER), id="f"), OTHER_CTX)
    assert {m.id for m in memory_repository.iter_visible(CTX)} == {"g", "p"}
    assert {m.id for m in memory_repository.iter_visible(GLOBAL_ONLY)} == {"g"}


def test_a_project_may_reuse_a_name_another_project_owns(memory_repository):
    memory_repository.create(_m(body="a", scope=Scope.project(OTHER), id="shared-name"), OTHER_CTX)
    memory_repository.create(_m(body="b", scope=Scope.project(MINE), id="shared-name"), CTX)
    assert memory_repository.get("shared-name", CTX).body == "b"
    assert memory_repository.get("shared-name", OTHER_CTX).body == "a"


def test_create_refuses_a_project_scope_outside_the_context(memory_repository):
    # the collision check searches the caller's scopes, but the write routes by
    # the memory's own scope: without a binding check the two disagree and a
    # foreign project's entry is replaced without ever being looked at
    memory_repository.create(_m(body="foreign secret plan", scope=Scope.project(OTHER), id="n"),
                OTHER_CTX)
    with pytest.raises(InvalidScope) as err:
        memory_repository.create(_m(body="v2", scope=Scope.project(OTHER), id="n"), CTX)
    assert "foreign secret plan" not in str(err.value)
    assert memory_repository.get("n", OTHER_CTX).body == "foreign secret plan"


def test_create_outside_the_context_writes_nothing_even_without_a_collision(memory_repository):
    with pytest.raises(InvalidScope):
        memory_repository.create(_m(body="planted", scope=Scope.project(OTHER), id="fresh"), CTX)
    assert [m.id for m in memory_repository.iter_visible(OTHER_CTX)] == []


def test_create_of_a_global_memory_is_unaffected(memory_repository):
    # every context sees the global scope, so global writes keep working from
    # a project context and from a project-less one alike
    memory_repository.create(_m(id="g"), CTX)
    memory_repository.create(_m(id="g2"), GLOBAL_ONLY)
    assert {m.id for m in memory_repository.iter_visible(GLOBAL_ONLY)} == {"g", "g2"}


# --- collisions ---
#
# The three outcomes are told apart by TYPE and FIELDS, never by wording: the
# client-visible copy is the transport's (see memriver's error-mapping tests),
# so a backend that phrases its own messages differently -- or not at all --
# still satisfies this contract and still produces identical MCP responses.

def test_same_scope_collision_raises_name_taken_with_the_existing_memory(memory_repository):
    first = _m(body="v1", id="n", description="original cue")
    memory_repository.create(first, CTX)
    with pytest.raises(NameTaken) as err:
        memory_repository.create(_m(body="v2", id="n"), CTX)
    assert err.value.memory_id == "n"
    assert err.value.existing == first


def test_global_write_refused_when_another_project_holds_the_name(memory_repository):
    memory_repository.create(_m(body="foreign secret plan", scope=Scope.project(OTHER), id="n"),
                OTHER_CTX)
    with pytest.raises(NameTaken) as err:
        memory_repository.create(_m(body="v2", id="n"), CTX)
    assert err.value.memory_id == "n"
    # existing=None is the cross-scope refusal: the field carries no trace of
    # the memory holding the name, so no transport can echo one
    assert err.value.existing is None
    assert "foreign secret plan" not in str(err.value)
    # the foreign memory is untouched and no global memory was created
    assert memory_repository.get("n", OTHER_CTX).body == "foreign secret plan"
    assert [m.id for m in memory_repository.iter_visible(GLOBAL_ONLY)] == []


def test_collision_with_an_undecodable_item_raises_unreadable_memory(
        memory_repository, occupy_unreadably):
    occupy_unreadably("notes")
    with pytest.raises(UnreadableMemory) as err:
        memory_repository.create(_m(body="v1", id="notes"), CTX)
    assert err.value.memory_id == "notes"


# --- concurrency ---
#
# Only create carries a concurrency clause. The port gives create the atomic
# name reservation, and a check-then-insert backend passes every serial test
# above while producing two winners here. update_body and delete reserve
# nothing: their documented outcome under a race is last-writer-wins over an
# existing name, which no observable end state can tell apart from a
# serialized implementation -- both leave one whole memory or none. A clause
# there would only assert what a plain atomic replace already gives, so it is
# deferred until the port promises something stronger (ordering, or a
# compare-and-swap on updated). The filesystem lock spanning those two
# read-modify-writes is verified in the filesystem integration tests instead.

_RACE_ROUNDS = 20


def _race_create(backend, storage_dir, memory_id):
    """Two independent repositories create one id at once; report both outcomes."""
    candidates = {name: _m(body=f"body from {name}", id=memory_id)
                  for name in ("a", "b")}
    repositories = {name: backend.make_repository(storage_dir)
                    for name in ("a", "b")}
    barrier = threading.Barrier(2)
    outcomes: dict[str, Exception | None] = {}

    def attempt(name: str) -> None:
        barrier.wait(timeout=5)  # release both threads into create together
        try:
            repositories[name].create(candidates[name], CTX)
            outcomes[name] = None
        except Exception as err:  # noqa: BLE001
            outcomes[name] = err

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()
    return candidates, outcomes


def test_concurrent_create_of_one_id_yields_exactly_one_winner(backend, storage_dir):
    # two independent repository instances over one store, standing in for two
    # processes. A backend that checks the name free and then inserts lets both
    # through: two winners, or a half-written entry the read below cannot parse
    for round_no in range(_RACE_ROUNDS):
        memory_id = f"race-{round_no}"
        candidates, outcomes = _race_create(backend, storage_dir, memory_id)

        assert set(outcomes) == {"a", "b"}  # both attempts reached create
        winners = [n for n, outcome in outcomes.items() if outcome is None]
        assert len(winners) == 1, f"round {round_no}: {outcomes}"
        winner = winners[0]
        loser = "b" if winner == "a" else "a"

        conflict = outcomes[loser]
        assert isinstance(conflict, NameTaken), f"round {round_no}: {conflict!r}"
        assert conflict.memory_id == memory_id

        # the store holds exactly the winner's memory: whole, parseable, and
        # with none of the loser's content mixed into it
        stored = backend.make_repository(storage_dir).get(memory_id, CTX)
        assert stored == candidates[winner]
        # `existing` may be None -- the loser can lose before the winner's
        # write lands -- but when it is set it is the winner's memory, never a
        # partial or losing one
        if conflict.existing is not None:
            assert conflict.existing == stored


# --- search ---

def _seed_search(memory_repository):
    memory_repository.create(_m(body="mise manages every runtime on this machine", type="user",
                   id="mise-runtime-management"), CTX)
    memory_repository.create(_m(body="项目使用 uv workspace 管理依赖", id="uv-layout"), CTX)
    memory_repository.create(_m(body="unrelated body text", type="user", id="cue-memory",
                   description="a distinctive recall cue"), CTX)


def test_search_matches_body_case_insensitively(memory_repository):
    _seed_search(memory_repository)
    assert [h.id for h in memory_repository.search("MISE", CTX, 5)] == ["mise-runtime-management"]


def test_search_matches_a_cjk_substring(memory_repository):
    _seed_search(memory_repository)
    assert [h.id for h in memory_repository.search("依赖", CTX, 5)] == ["uv-layout"]


def test_search_matches_the_memory_name(memory_repository):
    _seed_search(memory_repository)
    assert [h.id for h in memory_repository.search("uv-layout", CTX, 5)] == ["uv-layout"]


def test_search_matches_the_description(memory_repository):
    _seed_search(memory_repository)
    assert [h.id for h in memory_repository.search("distinctive", CTX, 5)] == ["cue-memory"]


def test_search_returns_scope_and_type_on_every_hit(memory_repository):
    memory_repository.create(_m(body="scoped hit", type="user", scope=Scope.project(MINE),
                   id="hit"), CTX)
    hit = memory_repository.search("scoped", CTX, 5)[0]
    assert hit.scope == Scope.project(MINE) and hit.type == "user"


def test_search_ignores_memories_outside_the_context(memory_repository):
    memory_repository.create(_m(body="foreign match", scope=Scope.project(OTHER), id="f"), OTHER_CTX)
    assert memory_repository.search("foreign", CTX, 5) == []


def test_search_empty_and_nul_queries_return_nothing(memory_repository):
    _seed_search(memory_repository)
    assert memory_repository.search("", CTX, 5) == []
    assert memory_repository.search("\x00", CTX, 5) == []


def test_search_strips_nul_from_the_query(memory_repository):
    _seed_search(memory_repository)
    assert [h.id for h in memory_repository.search("mi\x00se", CTX, 5)] == ["mise-runtime-management"]


def test_search_returns_newest_first(memory_repository):
    for i, updated in enumerate(["2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z",
                                 "2026-08-01T00:00:00Z"]):
        m = _m(body=f"aged fact {i}", id=f"aged-{i}")
        m.updated = updated
        memory_repository.create(m, CTX)
    assert [h.id for h in memory_repository.search("aged fact", CTX, 5)] == [
        "aged-2", "aged-1", "aged-0"]


def test_search_honours_the_limit_it_is_given(memory_repository):
    for i in range(3):
        memory_repository.create(_m(body=f"repeated fact {i}", id=f"rep-{i}"), CTX)
    assert len(memory_repository.search("repeated", CTX, 2)) == 2


def test_search_snippet_is_truncated_with_an_ellipsis(memory_repository):
    memory_repository.create(_m(body="x" * 200, type="user", id="long"), CTX)
    memory_repository.create(_m(body="y" * 60, type="user", id="exact"), CTX)
    long_hit = memory_repository.search("xxx", CTX, 5)[0]
    assert long_hit.snippet == "x" * 60 + "…"
    assert memory_repository.search("yyy", CTX, 5)[0].snippet == "y" * 60
