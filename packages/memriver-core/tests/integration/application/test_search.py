"""Keyword search through the composed services: one budget for the agent's
search, one ranked list for the human's."""

from __future__ import annotations


def _bodies(memories) -> list[str]:
    return [m.body for m in memories]


def test_search_all_ranks_across_projects_as_one_list(world):
    create = world["create"]
    create("uv pip in mine", description="")
    create("pip in theirs, newest", project_id=world["theirs"], description="")
    create("uv pip in global", project_id=world["global"], description="")
    create("uv only, newest of all", description="")
    assert _bodies(world["memory"].search_all("uv pip")) == [
        "uv pip in global", "uv pip in mine", "uv only, newest of all",
        "pip in theirs, newest"]
    assert _bodies(world["memory"].search_all("uv pip", limit=1)) == ["uv pip in global"]
    assert _bodies(world["memory"].search_all("uv pip", world["theirs"])) == [
        "pip in theirs, newest"]
    assert world["memory"].search_all(" ,; ") == []


def test_agent_search_ranks_project_then_global_in_one_budget(world):
    create = world["create"]
    create("uv pip global", project_id=world["global"], description="")
    create("uv project", description="")
    create("uv pip project", description="")
    create("pip global newest", project_id=world["global"], description="")
    memory, context = world["memory"], world["context"]
    assert _bodies(memory.search("uv pip", context)) == [
        "uv pip project", "uv project",                 # the project first, ranked
        "uv pip global", "pip global newest"]           # then global, ranked on its own
    assert _bodies(memory.search("uv pip", context, limit=3)) == [
        "uv pip project", "uv project", "uv pip global"]
    assert _bodies(memory.search("executor classifier", context)) == []
