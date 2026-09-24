"""Client-visible error copy is written here, in the transport -- not by the
backend that raised the error.

1. `_map_error` fed synthetic errors that carry only structured fields.
2. The MCP tools driven over a second backend that authors no messages at all,
   only the taxonomy types with the same fields: the responses must equal the
   SQLite backend's in test_server.py.
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from memriver import server as server_module
from memriver.server import _fail, _map_error, build_server
from memriver_core import (
    ContentRejected,
    GlobalReadOnly,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
    VersionConflict,
)
from memriver_core.application.service import MemoryService
from memriver_core.bootstrap import build_service
from memriver_core.content_policy.secret_scanner import SecretScanner
from memriver_core.models import Project, Resolution
from memriver_core.settings import (
    DEFAULT_MAX_BODY_CHARS,
    HEADER_FIELD_CHARS,
    INDEX_CUE_CHARS,
    PROJECT_NAME_MAX_CHARS,
    Settings,
)

GLOBAL_READ_ONLY = ("global memories are read-only to agents; no change was made. Tell the user; "
                    "do not retry through another entry or edit the store directly.")
M = "mmmmmmmmmm"


# --- _map_error, fed nothing but structured fields ---

@pytest.mark.parametrize("err, expected", [
    (StorageFailure(), "could not write entry"),
    (MemoryNotFound(M), "could not write entry"),
    (ContentRejected("content is empty; nothing to store"),
     "content is empty; nothing to store"),
    (ValueError("invalid memory type: 'bogus'"), "invalid memory type: 'bogus'"),
    (UnicodeEncodeError("utf-8", "\udc80", 0, 1, "surrogates not allowed"),
     "could not write entry"),
    (GlobalReadOnly(), GLOBAL_READ_ONLY),
])
def test_write_mapping(err, expected):
    assert _map_error("write", err) == expected


@pytest.mark.parametrize("state, fragment", [
    ("none", "this directory is not registered"),
    ("degraded", "this directory could not be matched to one project"),
    ("unavailable", "the memory store could not be read"),
    ("registered", "the registered project could not be found in the store"),
])
def test_write_without_a_project_states_the_project_context_not_a_path(state, fragment):
    result = _map_error("write", ProjectUnavailable(), context_state=state)
    assert fragment in result and "No memory was saved" in result
    assert "/" not in result.replace("memriver project", "")


@pytest.mark.parametrize("operation", ["read", "update", "delete"])
def test_not_found_is_one_answer_for_every_single_memory_operation(operation):
    assert _map_error(operation, MemoryNotFound(M), memory_id=M) == f"no such entry: {M}"


@pytest.mark.parametrize("operation", ["read", "update", "delete"])
@pytest.mark.parametrize("err", [MemoryNotFound("x"), StorageFailure()])
def test_an_id_that_is_not_an_id_is_never_echoed(operation, err):
    result = _map_error(operation, err, memory_id="x\n\nIGNORE PREVIOUS")
    assert "\n" not in result and "IGNORE" not in result


@pytest.mark.parametrize(("operation", "expected"), [
    ("read", f"could not read entry: {M}"),
    ("update", f"could not update entry: {M}"),
    ("delete", f"could not delete entry: {M}"),
])
def test_storage_failure_is_per_operation_and_never_leaks_its_cause(operation, expected):
    err = StorageFailure()
    err.__cause__ = OSError("disk full at /secret/path")
    assert _map_error(operation, err, memory_id=M) == expected


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_every_mutation_of_a_global_entry_reports_the_same_refusal(operation):
    assert _map_error(operation, GlobalReadOnly(), memory_id=M) == GLOBAL_READ_ONLY


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_a_version_conflict_names_the_entry_and_the_recovery(operation):
    assert _map_error(operation, VersionConflict(M), memory_id=M) == (
        f"entry {M} changed since you read it; no change was made. "
        "Call memory_read again and retry with its version.")
    assert _map_error(operation, VersionConflict("x\ny"), memory_id="x\ny") \
        .startswith("entry changed since you read it")


def test_update_forwards_the_content_policy_refusal():
    assert _map_error("update", ContentRejected("looks like a secret"), memory_id=M) == \
        "looks like a secret"


def test_list_operations_have_one_answer_for_any_error():
    for err in (StorageFailure(), MemoryNotFound(M), RuntimeError("x /secret")):
        assert _map_error("list", err) == "could not read the memory store"


def test_an_unnamed_error_logs_only_the_operation_and_its_type(caplog):
    with caplog.at_level(logging.WARNING, logger="memriver"), pytest.raises(ToolError) as exc_info:
        _fail("read", RuntimeError("secret detail /Users/x"), memory_id=M)
    assert str(exc_info.value) == f"could not read entry: {M}"
    assert [r.getMessage() for r in caplog.records] == ["memory_read failed: RuntimeError"]


@pytest.mark.parametrize("err", [MemoryNotFound(M), GlobalReadOnly(),
                                 ProjectUnavailable(), VersionConflict(M),
                                 ContentRejected("looks like a secret")])
def test_a_named_error_never_logs(caplog, err):
    with caplog.at_level(logging.WARNING, logger="memriver"), pytest.raises(ToolError):
        _fail("update", err, memory_id=M)
    assert caplog.records == []


def test_a_value_error_is_named_only_on_the_write_path(caplog):
    """`_fail` mirrors `_map_error`'s own carve-out: a ValueError is an
    already-worded, expected refusal only on `write`, and only when it is not
    a UnicodeError. Everywhere else -- including a UnicodeError on `write` --
    it reaches a generic message and must log like any other unnamed
    exception, e.g. the pre-write round-trip check in
    `SqliteMemoryStore.update` raises `ValueError` on `update`."""
    with caplog.at_level(logging.WARNING, logger="memriver"), pytest.raises(ToolError):
        _fail("write", ValueError("invalid memory type: 'bogus'"))
    assert caplog.records == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="memriver"), pytest.raises(ToolError):
        _fail("update", ValueError("row failed its round trip"), memory_id=M)
    assert [r.getMessage() for r in caplog.records] == ["memory_update failed: ValueError"]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="memriver"), pytest.raises(ToolError):
        _fail("write", UnicodeEncodeError("utf-8", "\udc80", 0, 1, "surrogates not allowed"))
    assert [r.getMessage() for r in caplog.records] == ["memory_write failed: UnicodeEncodeError"]


# --- the tools over a second backend ---

class OtherBackend:
    """Stands in for both stores of a different backend: every action raises `error`."""

    def __init__(self, error: Exception, project: Project, global_id: str) -> None:
        self.error, self.project, self.global_id = error, project, global_id

    # ProjectStore
    def create(self, project, plan):
        raise self.error

    def read(self, project_id):
        if project_id == self.project.id:
            return self.project
        raise ProjectNotFound(project_id)

    def global_project_id(self):
        return self.global_id

    def ensure_global(self):
        return self.global_id

    def list_projects(self):
        return [self.project]

    def search(self, project_id, read_write_set, *, query, limit):
        raise self.error

    def resolve(self, start, *, ignoring=None):
        return Resolution("registered", project=self.project)

    def plan_root(self, directory, project_id):
        raise self.error

    def bind(self, project_id, plan):
        raise self.error

    def plan_unbind(self, project_id, root, cwd):
        raise self.error

    def unbind(self, plan):
        raise self.error

    # MemoryStore
    def record(self, memory, read_write_set):
        raise self.error

    def update(self, memory_id, read_write_set, *, expected_version, body, description):
        raise self.error

    def delete(self, memory_id, read_write_set, *, expected_version, hard):
        raise self.error

    def read_any(self, memory_id, *, include_deleted):
        raise self.error


class OtherMemoryStore:
    def __init__(self, backend: OtherBackend) -> None:
        self.backend = backend

    def record(self, memory, read_write_set):
        raise self.backend.error

    def read(self, memory_id, read_write_set):
        raise self.backend.error

    def update(self, memory_id, read_write_set, *, expected_version, body, description):
        raise self.backend.error

    def delete(self, memory_id, read_write_set, *, expected_version, hard):
        raise self.backend.error

    def read_any(self, memory_id, *, include_deleted):
        raise self.backend.error


class _NoDiagnostics:
    def run(self, **kw):
        raise AssertionError


@pytest.fixture
def other_backend_server(tmp_path, monkeypatch):
    store, directory = tmp_path / "mem", tmp_path / "demo"
    directory.mkdir()
    real = build_service(Settings(root=store), root=store)
    global_id = real.ensure_global()
    project_id = real.init_project("demo", real.plan_root(str(directory))).id
    project = Project(id=project_id, name="demo", root=str(directory.resolve()))
    settings = Settings()

    def build(error: Exception):
        backend = OtherBackend(error, project, global_id)

        def build_service_over_other(_settings, *, root):
            return MemoryService(OtherMemoryStore(backend), backend, SecretScanner,
                                 _NoDiagnostics(),
                                 max_body_chars=settings.max_body_chars,
                                 metadata_max_chars=DEFAULT_MAX_BODY_CHARS,
                                 search_limit_default=settings.search_limit_default,
                                 search_limit_max=settings.search_limit_max,
                                 index_budget_lines=settings.index_budget_lines,
                                 index_cue_chars=INDEX_CUE_CHARS,
                                 header_field_chars=HEADER_FIELD_CHARS,
                                 project_name_max_chars=PROJECT_NAME_MAX_CHARS)

        monkeypatch.setattr(server_module, "build_service", build_service_over_other)
        return build_server(root=store, project_dir=directory)

    return build


async def _error_text(server, tool: str, arguments: dict) -> str:
    """Call `tool`, assert it fails as an MCP tool error, and return its message."""
    async with Client(server) as c:
        result = await c.call_tool(tool, arguments, raise_on_error=False)
    assert result.is_error is True
    assert len(result.content) == 1
    return result.content[0].text


@pytest.mark.parametrize("error, expected", [
    (StorageFailure(), "could not write entry"),
    (GlobalReadOnly(), GLOBAL_READ_ONLY),
])
async def test_write_over_another_backend_answers_identically(other_backend_server, error,
                                                              expected):
    server = other_backend_server(error)
    assert await _error_text(server, "memory_write", {"content": "v2", "type": "user"}) == expected


@pytest.mark.parametrize("error, tool, arguments, expected", [
    (MemoryNotFound(M), "memory_read", {"memory_id": M}, f"no such entry: {M}"),
    (MemoryNotFound(M), "memory_update", {"memory_id": M, "expected_version": 1, "content": "v2"},
     f"no such entry: {M}"),
    (MemoryNotFound(M), "memory_delete", {"memory_id": M, "expected_version": 1},
     f"no such entry: {M}"),
    (StorageFailure(), "memory_read", {"memory_id": M}, f"could not read entry: {M}"),
    (StorageFailure(), "memory_update", {"memory_id": M, "expected_version": 1, "content": "v2"},
     f"could not update entry: {M}"),
    (StorageFailure(), "memory_delete", {"memory_id": M, "expected_version": 1},
     f"could not delete entry: {M}"),
])
async def test_single_memory_errors_over_another_backend_answer_identically(
        other_backend_server, error, tool, arguments, expected):
    assert await _error_text(other_backend_server(error), tool, arguments) == expected


async def test_a_chatty_backend_cannot_reach_the_client(other_backend_server):
    chatty = StorageFailure()
    chatty.args = ("SELECT * FROM secrets WHERE path='/Users/x'",)
    server = other_backend_server(chatty)
    for tool, arguments in (("memory_read", {"memory_id": M}),
                            ("memory_write", {"content": "v", "type": "user"})):
        text = await _error_text(server, tool, arguments)
        assert "SELECT" not in text and "/Users/x" not in text


@pytest.mark.parametrize("error", [StorageFailure(), RuntimeError("boom /secret")])
async def test_memory_index_and_search_report_the_same_failure_as_a_tool_error(
        other_backend_server, error):
    server = other_backend_server(error)
    assert await _error_text(server, "memory_index", {}) == "could not read the memory store"
    assert await _error_text(server, "memory_search", {"query": "q"}) == \
        "could not read the memory store"
