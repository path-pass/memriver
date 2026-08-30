"""bootstrap.build_service: the only place the concrete adapters are named."""

from __future__ import annotations

from pathlib import Path

from memriver_core import bootstrap
from memriver_core.application.service import MemoryService
from memriver_core.config import DEFAULT_MAX_BODY_CHARS, Settings


class Recorder:
    """Stands in for MemoryService, keeping what bootstrap injected."""

    def __init__(self, repository, policy, **limits):
        self.repository = repository
        self.policy = policy
        self.limits = limits


def _capture(monkeypatch) -> list[Path]:
    roots: list[Path] = []
    monkeypatch.setattr(bootstrap, "FileMemoryRepository",
                        lambda root: roots.append(root) or f"repo@{root}")
    monkeypatch.setattr(bootstrap, "MemoryService", Recorder)
    return roots


def test_uses_the_settings_root_by_default(monkeypatch, tmp_path):
    roots = _capture(monkeypatch)
    bootstrap.build_service(Settings(root=tmp_path / "from-settings"))
    assert roots == [tmp_path / "from-settings"]


def test_an_explicit_root_wins_over_the_settings_root(monkeypatch, tmp_path):
    roots = _capture(monkeypatch)
    bootstrap.build_service(Settings(root=tmp_path / "from-settings"),
                            root=tmp_path / "explicit")
    assert roots == [tmp_path / "explicit"]


def test_injects_the_configured_body_limit_and_the_default_metadata_limit(
        monkeypatch, tmp_path):
    _capture(monkeypatch)
    settings = Settings(root=tmp_path, max_body_chars=10, search_limit_default=3,
                        search_limit_max=7, index_budget_lines=9)
    service = bootstrap.build_service(settings)
    assert service.limits == {"max_body_chars": 10,
                              "metadata_max_chars": DEFAULT_MAX_BODY_CHARS,
                              "search_limit_default": 3, "search_limit_max": 7,
                              "index_budget_lines": 9}
    # a tightened body budget must not tighten metadata acceptance
    assert service.limits["metadata_max_chars"] == 8000


def test_returns_the_facade_not_a_concrete_adapter(tmp_path):
    assert isinstance(bootstrap.build_service(Settings(root=tmp_path)), MemoryService)
