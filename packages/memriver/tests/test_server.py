import pytest
from fastmcp import Client
from memriver.server import build_server


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def server(tmp_path, project):
    return build_server(root=tmp_path / "mem", project_dir=project)


async def test_write_then_index_and_search(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "本项目 python 包管理用 uv", "type": "fact",
            "scope": "project", "harness": "claude-code"})).data
        assert "id" in r and r["scope"].startswith("project:demo-")
        idx = (await c.call_tool("memory_index", {})).data
        assert "python 包管理用 uv" in idx
        hits = (await c.call_tool("memory_search", {"query": "包管理"})).data
        assert hits[0]["id"] == r["id"]


async def test_write_secret_rejected(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "key AKIAIOSFODNN7EXAMPLE", "type": "fact"})).data
        assert "error" in r and "AKIA" not in r["error"]


async def test_malformed_explicit_scope_returns_error_dict(server):
    async with Client(server) as c:
        r = (await c.call_tool("memory_write", {
            "content": "traversal attempt", "type": "fact",
            "scope": "project:../../etc"})).data
        assert "error" in r


async def test_update_supersedes(server):
    async with Client(server) as c:
        old = (await c.call_tool("memory_write", {
            "content": "旧偏好：用英文回复", "type": "preference",
            "scope": "global"})).data
        new = (await c.call_tool("memory_update", {
            "entry_id": old["id"], "content": "新偏好：用中文回复"})).data
        read_old = (await c.call_tool("memory_read", {"entry_id": old["id"]})).data
        assert read_old["superseded_by"] == new["id"]
        hits = (await c.call_tool("memory_search", {"query": "用中文回复"})).data
        assert {h["id"] for h in hits} == {new["id"]}
