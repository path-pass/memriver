import hashlib

from memriver.project_context import _git_root, build_context, project_slug


def test_git_root_walks_up_from_a_subdirectory(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert _git_root(sub) == repo.resolve()


def test_git_root_outside_a_repository_is_none(tmp_path):
    assert _git_root(tmp_path) is None


def test_project_slug_from_git_root(tmp_path):
    repo = tmp_path / "My_Repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    slug = project_slug(sub)
    h = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:6]
    assert slug == f"my-repo-{h}"


def test_project_slug_non_git_returns_none(tmp_path):
    assert project_slug(tmp_path) is None


def test_build_context_in_a_git_project_carries_its_id(tmp_path):
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    ctx = build_context(repo)
    assert ctx.project_id == project_slug(repo)
    assert [s.to_storage() for s in ctx.visible_scopes()] == [
        "global", f"project:{project_slug(repo)}"]


def test_build_context_outside_a_git_project_is_global_only(tmp_path):
    ctx = build_context(tmp_path)
    assert ctx.project_id is None
    assert [s.to_storage() for s in ctx.visible_scopes()] == ["global"]
