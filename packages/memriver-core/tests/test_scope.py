import hashlib

import pytest
from memriver_core.scope import project_slug, resolve_scope, sanitize_name, storage_root


def test_storage_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "mem"))
    assert storage_root() == tmp_path / "mem"

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

def test_resolve_scope(tmp_path):
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    assert resolve_scope("global", repo) == "global"
    assert resolve_scope("project", repo).startswith("project:demo-")
    with pytest.raises(ValueError):
        resolve_scope("project", tmp_path / "nowhere")


def test_sanitize_passthrough():
    assert sanitize_name("mise-runtime-management") == "mise-runtime-management"


def test_sanitize_normalizes():
    assert sanitize_name("Mise Runtime_Mgmt!") == "mise-runtime-mgmt"
    assert sanitize_name("--weird--") == "weird"


def test_sanitize_caps_length():
    assert len(sanitize_name("a" * 200)) == 64


def test_sanitize_unsalvageable():
    assert sanitize_name("") is None
    assert sanitize_name("!!!") is None
    assert sanitize_name("記憶") is None
