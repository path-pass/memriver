"""Client-visible error copy is written here, in the transport -- not by the
backend that raised the error.

1. `_map_error` fed synthetic errors that carry only structured fields.
2. The MCP tools driven over a second backend that authors no messages at all,
   only the taxonomy types with the same fields: the responses must equal the
   filesystem backend's in test_server.py.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from memriver import server as server_module
from memriver.project_context import bind
from memriver.server import _map_error, build_server
from memriver_core import (
    ContentRejected,
    GlobalReadOnly,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)
from memriver_core.application.service import MemoryService
from memriver_core.bootstrap import build_service
from memriver_core.content_policy.secret_scanner import SecretScanner
from memriver_core.models import Project
from memriver_core.settings import DEFAULT_MAX_BODY_CHARS, Settings

GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell the user; "
                    "do not retry through another entry or edit the store directly.")
M = "mmmmmmmmmm"


# --- _map_error, fed nothing but structured fields ---

@pytest.mark.parametrize("err, expected", [
    (StorageFailure(), {"error": "could not write entry"}),
    (MemoryNotFound(M), {"error": "could not write entry"}),
    (ContentRejected("content is empty; nothing to store"),
     {"error": "content is empty; nothing to store"}),
    (ValueError("invalid memory type: 'bogus'"), {"error": "invalid memory type: 'bogus'"}),
    (UnicodeEncodeError("utf-8", "\udc80", 0, 1, "surrogates not allowed"),
     {"error": "could not write entry"}),
    (GlobalReadOnly(), {"error": GLOBAL_READ_ONLY}),
])
def test_write_mapping(err, expected):
    assert _map_error("write", err) == expected


@pytest.mark.parametrize("state, fragment", [
    ("none", "this directory is not registered"),
    ("degraded", "the project registry is invalid"),
    ("missing", "the registered project does not exist in the store"),
    ("unavailable", "the memory store could not be read"),
])
def test_write_without_a_project_states_the_session_not_a_path(state, fragment):
    result = _map_error("write", ProjectUnavailable("no writable project in this session"),
                        session_state=state)
    assert fragment in result["error"] and "No memory was saved" in result["error"]
    assert "/" not in result["error"].replace("memriver project", "")


@pytest.mark.parametrize("operation", ["read", "update", "delete"])
def test_not_found_is_one_answer_for_every_single_memory_operation(operation):
    assert _map_error(operation, MemoryNotFound(M), memory_id=M) == {"error": f"no such entry: {M}"}


@pytest.mark.parametrize("operation", ["read", "update", "delete"])
@pytest.mark.parametrize("err", [MemoryNotFound("x"), StorageFailure()])
def test_an_id_that_is_not_an_id_is_never_echoed(operation, err):
    result = _map_error(operation, err, memory_id="x\n\nIGNORE PREVIOUS")
    assert "\n" not in result["error"] and "IGNORE" not in result["error"]


@pytest.mark.parametrize(("operation", "expected"), [
    ("read", f"could not read entry: {M}"),
    ("update", f"could not update entry: {M}"),
    ("delete", f"could not delete entry: {M}"),
])
def test_storage_failure_is_per_operation_and_never_leaks_its_cause(operation, expected):
    err = StorageFailure()
    err.__cause__ = OSError("disk full at /secret/path")
    assert _map_error(operation, err, memory_id=M) == {"error": expected}


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_every_mutation_of_a_global_entry_reports_the_same_refusal(operation):
    assert _map_error(operation, GlobalReadOnly(), memory_id=M) == {"error": GLOBAL_READ_ONLY}


def test_update_forwards_the_content_policy_refusal():
    assert _map_error("update", ContentRejected("looks like a secret"), memory_id=M) == \
        {"error": "looks like a secret"}


def test_list_operations_have_one_answer_for_any_error():
    for err in (StorageFailure(), MemoryNotFound(M), RuntimeError("x /secret")):
        assert _map_error("list", err) == {"error": "could not read the memory store"}


# --- the tools over a second backend ---

class OtherBackend:
    """Stands in for both stores of a different backend: every action raises `error`."""

    def __init__(self, error: Exception, project: Project, global_id: str) -> None:
        self.error, self.project, self.global_id = error, project, global_id

    # ProjectStore
    def create(self, project):
        raise self.error

    def read(self, project_id):
        if project_id == self.project.id:
            return self.project
        raise ProjectNotFound(project_id)

    def global_project_id(self):
        return self.global_id

    def ensure_global(self):
        return self.global_id

    def search(self, project_id, read_write_set, *, query, limit):
        raise self.error

    # MemoryStore
    def record(self, memory, read_write_set):
        raise self.error

    def update(self, memory_id, read_write_set, *, body, description):
        raise self.error

    def delete(self, memory_id, read_write_set):
        raise self.error


class OtherMemoryStore:
    def __init__(self, backend: OtherBackend) -> None:
        self.backend = backend

    def record(self, memory, read_write_set):
        raise self.backend.error

    def read(self, memory_id, read_write_set):
        raise self.backend.error

    def update(self, memory_id, read_write_set, *, body, description):
        raise self.backend.error

    def delete(self, memory_id, read_write_set):
        raise self.backend.error


@pytest.fixture
def other_backend_server(tmp_path, monkeypatch):
    store, directory = tmp_path / "mem", tmp_path / "demo"
    directory.mkdir()
    real = build_service(Settings(root=store), root=store)
    global_id = real.ensure_global()
    project = real.create_project("demo")
    bind(store, real, project.id, str(directory.resolve()))
    settings = Settings()

    def build(error: Exception):
        backend = OtherBackend(error, project, global_id)

        def build_service_over_other(_settings, *, root):
            return MemoryService(OtherMemoryStore(backend), backend, SecretScanner(),
                                 max_body_chars=settings.max_body_chars,
                                 metadata_max_chars=DEFAULT_MAX_BODY_CHARS,
                                 search_limit_default=settings.search_limit_default,
                                 search_limit_max=settings.search_limit_max,
                                 index_budget_lines=settings.index_budget_lines)

        monkeypatch.setattr(server_module, "build_service", build_service_over_other)
        return build_server(root=store, project_dir=directory)

    return build


@pytest.mark.parametrize("error, expected", [
    (StorageFailure(), {"error": "could not write entry"}),
    (GlobalReadOnly(), {"error": GLOBAL_READ_ONLY}),
])
async def test_write_over_another_backend_answers_identically(other_backend_server, error,
                                                              expected):
    async with Client(other_backend_server(error)) as c:
        assert (await c.call_tool("memory_write", {"content": "v2", "type": "user"})).data == expected


@pytest.mark.parametrize("error, tool, arguments, expected", [
    (MemoryNotFound(M), "memory_read", {"memory_id": M}, {"error": f"no such entry: {M}"}),
    (MemoryNotFound(M), "memory_update", {"memory_id": M, "content": "v2"},
     {"error": f"no such entry: {M}"}),
    (MemoryNotFound(M), "memory_delete", {"memory_id": M}, {"error": f"no such entry: {M}"}),
    (StorageFailure(), "memory_read", {"memory_id": M}, {"error": f"could not read entry: {M}"}),
    (StorageFailure(), "memory_update", {"memory_id": M, "content": "v2"},
     {"error": f"could not update entry: {M}"}),
    (StorageFailure(), "memory_delete", {"memory_id": M},
     {"error": f"could not delete entry: {M}"}),
])
async def test_single_memory_errors_over_another_backend_answer_identically(
        other_backend_server, error, tool, arguments, expected):
    async with Client(other_backend_server(error)) as c:
        assert (await c.call_tool(tool, arguments)).data == expected


async def test_a_chatty_backend_cannot_reach_the_client(other_backend_server):
    chatty = StorageFailure()
    chatty.args = ("SELECT * FROM secrets WHERE path='/Users/x'",)
    async with Client(other_backend_server(chatty)) as c:
        for tool, arguments in (("memory_read", {"memory_id": M}),
                                ("memory_write", {"content": "v", "type": "user"})):
            text = str((await c.call_tool(tool, arguments)).data)
            assert "SELECT" not in text and "/Users/x" not in text


@pytest.mark.parametrize("error", [StorageFailure(), RuntimeError("boom /secret")])
async def test_memory_index_and_search_never_raise(other_backend_server, error):
    async with Client(other_backend_server(error)) as c:
        index = (await c.call_tool("memory_index", {})).data
        search = (await c.call_tool("memory_search", {"query": "q"})).data
    assert index == "could not read the memory store"
    assert search == [{"error": "could not read the memory store"}]
