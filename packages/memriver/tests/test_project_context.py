import hashlib

from memriver.project_context import build_context, find_git_root, project_slug


def test_git_root_walks_up_from_a_subdirectory(tmp_path):
    git_repo = tmp_path / "repo"
    (git_repo / ".git").mkdir(parents=True)
    sub = git_repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert find_git_root(sub) == git_repo.resolve()


def test_git_root_outside_a_repository_is_none(tmp_path):
    assert find_git_root(tmp_path) is None


def test_project_slug_from_git_root(tmp_path):
    git_repo = tmp_path / "My_Repo"
    (git_repo / ".git").mkdir(parents=True)
    sub = git_repo / "src" / "deep"
    sub.mkdir(parents=True)
    slug = project_slug(sub)
    h = hashlib.sha1(str(git_repo.resolve()).encode()).hexdigest()[:6]
    assert slug == f"my-repo-{h}"


def test_project_slug_non_git_returns_none(tmp_path):
    assert project_slug(tmp_path) is None


def test_build_context_in_a_git_project_carries_its_id(tmp_path):
    git_repo = tmp_path / "demo"
    (git_repo / ".git").mkdir(parents=True)
    ctx = build_context(git_repo)
    assert ctx.project_id == project_slug(git_repo)
    assert [s.to_storage() for s in ctx.visible_scopes()] == [
        "global", f"project:{project_slug(git_repo)}"]


def test_build_context_outside_a_git_project_is_global_only(tmp_path):
    ctx = build_context(tmp_path)
    assert ctx.project_id is None
    assert [s.to_storage() for s in ctx.visible_scopes()] == ["global"]
