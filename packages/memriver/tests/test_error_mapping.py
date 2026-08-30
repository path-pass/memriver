"""Client-visible error copy is written here, in the transport -- not by the
backend that raised the error.

Two layers of proof:

1. `_map_error` fed synthetic errors that carry only structured fields, with
   the exact response dict asserted for every (operation, error type) pair.
2. The MCP tools driven over a second backend -- a fake that authors no
   messages at all, only the taxonomy types with the same fields. It stands
   in for a future SQLite adapter: the responses must be identical to the
   ones the filesystem backend produces in test_server.py.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from memriver import server as server_module
from memriver.server import _map_error, build_server
from memriver_core.application.errors import (
    ContentRejected,
    InvalidScope,
    MemoryNotFound,
    NameTaken,
    ProjectUnavailable,
    StorageFailure,
    UnreadableMemory,
)
from memriver_core.application.service import MemoryService
from memriver_core.config import DEFAULT_MAX_BODY_CHARS, Settings
from memriver_core.content_policy.secret_scanner import SecretScanner
from memriver_core.models import Memory, ProjectId, Scope

SOURCE = {"harness": "test", "method": "agent"}

EXISTING = Memory.new(body="v1 body", type="user", scope=Scope.global_(),
                      id="n", description="original cue", source=SOURCE)

TAKEN_SAME_SCOPE = ("name 'n' already exists; memory_update it, or choose a "
                    "more precise name if this is a different fact")
TAKEN_ELSEWHERE = ("name 'n' is already used elsewhere in the store; choose "
                   "another name")
TAKEN_UNREADABLE = "name 'n' is taken by a file that is not a readable entry"


# --- _map_error, fed nothing but structured fields ---

@pytest.mark.parametrize("err, expected", [
    (NameTaken("n", existing=None), {"error": TAKEN_ELSEWHERE}),
    (UnreadableMemory("n"), {"error": TAKEN_UNREADABLE}),
    (StorageFailure(), {"error": "could not write entry"}),
    (MemoryNotFound("n"), {"error": "could not write entry"}),
    (ContentRejected("content is empty; nothing to store"),
     {"error": "content is empty; nothing to store"}),
    (InvalidScope("scope 'project:x' is outside the current project; "
                  "use 'project' or 'global'"),
     {"error": "scope 'project:x' is outside the current project; "
               "use 'project' or 'global'"}),
    (ValueError("unknown type: bogus"), {"error": "unknown type: bogus"}),
])
def test_write_mapping(err, expected):
    assert _map_error("write", err) == expected


def test_write_name_taken_echoes_the_existing_memory_from_the_field():
    assert _map_error("write", NameTaken("n", existing=EXISTING)) == {
        "error": TAKEN_SAME_SCOPE,
        "existing": {"id": "n", "type": "user", "scope": "global",
                     "updated": EXISTING.updated, "snippet": "v1 body",
                     "description": "original cue"},
    }


def test_write_outside_a_project_names_the_path_the_transport_resolved(tmp_path):
    assert _map_error("write", ProjectUnavailable("not inside a git project"),
                      project_dir=tmp_path) == {
        "error": f"not inside a git project: {tmp_path}"}


@pytest.mark.parametrize("operation", ["read", "update"])
@pytest.mark.parametrize("err, expected", [
    (MemoryNotFound("n"), "no such entry: n"),
    (UnreadableMemory("n"), "unreadable entry file: n"),
    (StorageFailure(), "unreadable entry file: n"),
])
def test_read_and_update_mapping(operation, err, expected):
    assert _map_error(operation, err, entry_id="n") == {"error": expected}


def test_update_forwards_the_content_policy_refusal():
    # policy copy is authored in the core, where the wording is the rule
    assert _map_error("update", ContentRejected("content is empty; nothing to store"),
                      entry_id="n") == {"error": "content is empty; nothing to store"}


@pytest.mark.parametrize("err, expected", [
    (MemoryNotFound("n"), "no such entry: n"),
    (UnreadableMemory("n"), "could not delete entry: n"),
    (StorageFailure(), "could not delete entry: n"),
])
def test_delete_mapping(err, expected):
    assert _map_error("delete", err, entry_id="n") == {"error": expected}


@pytest.mark.parametrize("operation, kwargs", [
    ("write", {}), ("read", {"entry_id": "n"}),
    ("update", {"entry_id": "n"}), ("delete", {"entry_id": "n"}),
])
def test_no_operation_leaks_the_cause_of_a_storage_failure(operation, kwargs):
    # the adapter keeps its OSError as __cause__ for logs; that text may name
    # an absolute path, a user, or a mount, and must never reach a client
    err = StorageFailure()
    err.__cause__ = OSError(13, "Permission denied: /home/alice/store/.lock")
    assert "alice" not in str(_map_error(operation, err, **kwargs))


# --- the same errors from a second backend, end to end through the tools ---

class OtherBackend:
    """A backend that authors no copy at all: types and fields only.

    Stands in for a future SQLite adapter. Every response a client gets while
    this is installed was therefore composed transport-side.
    """

    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, memory, ctx):
        raise self.error

    def get(self, memory_id, ctx):
        raise self.error

    def update_body(self, memory_id, body, ctx, description=None):
        raise self.error

    def delete(self, memory_id, ctx):
        raise self.error

    def iter_visible(self, ctx):
        return iter(())

    def search(self, query, ctx, limit):
        return []


@pytest.fixture
def other_backend_server(tmp_path, monkeypatch):
    """build_server, but over OtherBackend instead of the filesystem."""
    git_repo = tmp_path / "demo"
    (git_repo / ".git").mkdir(parents=True)
    settings = Settings()

    def build(error: Exception):
        def build_service(_settings, *, root):
            return MemoryService(
                OtherBackend(error), SecretScanner(),
                max_body_chars=settings.max_body_chars,
                metadata_max_chars=DEFAULT_MAX_BODY_CHARS,
                search_limit_default=settings.search_limit_default,
                search_limit_max=settings.search_limit_max,
                index_budget_lines=settings.index_budget_lines)

        monkeypatch.setattr(server_module, "build_service", build_service)
        return build_server(root=tmp_path / "mem", project_dir=git_repo)

    return build


@pytest.mark.parametrize("error, expected", [
    (NameTaken("n", existing=None), {"error": TAKEN_ELSEWHERE}),
    (UnreadableMemory("n"), {"error": TAKEN_UNREADABLE}),
    (StorageFailure(), {"error": "could not write entry"}),
])
async def test_write_over_another_backend_answers_identically(
        other_backend_server, error, expected):
    async with Client(other_backend_server(error)) as c:
        assert (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n",
            "scope": "global"})).data == expected


async def test_write_collision_over_another_backend_echoes_the_same_dict(
        other_backend_server):
    async with Client(other_backend_server(NameTaken("n", existing=EXISTING))) as c:
        assert (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n",
            "scope": "global"})).data == {
            "error": TAKEN_SAME_SCOPE,
            "existing": {"id": "n", "type": "user", "scope": "global",
                         "updated": EXISTING.updated, "snippet": "v1 body",
                         "description": "original cue"}}


@pytest.mark.parametrize("error, expected", [
    (MemoryNotFound("n"), {"error": "no such entry: n"}),
    (UnreadableMemory("n"), {"error": "unreadable entry file: n"}),
    (StorageFailure(), {"error": "unreadable entry file: n"}),
])
async def test_read_and_update_over_another_backend_answer_identically(
        other_backend_server, error, expected):
    async with Client(other_backend_server(error)) as c:
        assert (await c.call_tool("memory_read", {"entry_id": "n"})).data == expected
        assert (await c.call_tool("memory_update", {
            "entry_id": "n", "content": "v2"})).data == expected


@pytest.mark.parametrize("error, expected", [
    (MemoryNotFound("n"), {"error": "no such entry: n"}),
    (UnreadableMemory("n"), {"error": "could not delete entry: n"}),
    (StorageFailure(), {"error": "could not delete entry: n"}),
])
async def test_delete_over_another_backend_answers_identically(
        other_backend_server, error, expected):
    async with Client(other_backend_server(error)) as c:
        assert (await c.call_tool("memory_delete", {"entry_id": "n"})).data == expected


async def test_a_chatty_backend_cannot_reach_the_client(other_backend_server):
    # the worst case the fix exists for: a backend that stuffs its own driver
    # text into the error. The fields decide the response; the text is unused.
    chatty = NameTaken("n", existing=None)
    chatty.args = ("UNIQUE constraint failed: memories.id (/srv/db/mem.sqlite)",)
    async with Client(other_backend_server(chatty)) as c:
        out = (await c.call_tool("memory_write", {
            "content": "v2", "type": "user", "name": "n",
            "scope": "global"})).data
    assert out == {"error": TAKEN_ELSEWHERE}


def test_a_project_scoped_echo_renders_the_scope_as_the_codec_does():
    m = Memory.new(body="b", type="project", scope=Scope.project(ProjectId("demo-000000")),
                   id="p", source=SOURCE)
    assert _map_error("write", NameTaken("p", existing=m))["existing"]["scope"] == (
        "project:demo-000000")
