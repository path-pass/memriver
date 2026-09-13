import hashlib

from memriver.project_context import (
    _fit_filename_bytes,
    build_context,
    find_git_root,
    project_slug,
)


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


def test_project_slug_truncates_a_long_basename_to_fit_a_filename(tmp_path):
    # the slug becomes a directory component (projects/<slug>/entries/...);
    # a basename long enough to push it past 255 bytes made every write in
    # that project fail with ENAMETOOLONG. 250 ascii chars is a basename the
    # real filesystem still accepts (the OS's own 255-byte limit), but
    # "-" + a 6-hex digest pushes the slug itself to 257 bytes.
    git_repo = tmp_path / ("x" * 250)
    (git_repo / ".git").mkdir(parents=True)
    slug = project_slug(git_repo)
    assert len(slug.encode()) <= 255
    h = hashlib.sha1(str(git_repo.resolve()).encode()).hexdigest()[:6]
    assert slug.endswith(f"-{h}")


def test_fit_filename_bytes_cuts_multibyte_input_on_a_utf8_boundary():
    # the sanitizer only ever emits ascii, so a real basename can't exercise
    # a mid-codepoint cut -- unit-test the truncation helper itself with a
    # multibyte string long enough to force one, so a future non-ascii
    # source (or a wider slug charset) can't reintroduce a decode crash
    long_multibyte = "€" * 100  # euro sign, 3 bytes each => 300 bytes
    fitted = _fit_filename_bytes(long_multibyte, suffix="-abcdef", limit=255)
    assert len(fitted.encode()) <= 255 - len("-abcdef")
    fitted.encode().decode()  # must not raise: no half codepoint left dangling


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
