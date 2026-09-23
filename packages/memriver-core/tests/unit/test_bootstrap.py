"""bootstrap: the only place the concrete adapters are named."""

from __future__ import annotations

from memriver_core import bootstrap
from memriver_core.application.diagnostics import DiagnosticsService
from memriver_core.application.service import MemoryService
from memriver_core.config import (
    DEFAULT_MAX_BODY_CHARS,
    ID_GENERATION_ATTEMPTS,
    Settings,
)
from memriver_core.repository.filesystem import (
    FileMemoryStore,
    FileProjectStore,
    FilesystemStoreInspector,
)


def test_uses_the_settings_root_by_default(tmp_path):
    service = bootstrap.build_service(Settings(root=tmp_path / "from-settings"))
    assert service._project_store.root == tmp_path / "from-settings"
    assert service._memory_store.root == tmp_path / "from-settings"


def test_an_explicit_root_wins_over_the_settings_root(tmp_path):
    service = bootstrap.build_service(Settings(root=tmp_path / "from-settings"),
                                      root=tmp_path / "explicit")
    assert service._project_store.root == tmp_path / "explicit"


def test_the_memory_store_checks_projects_through_the_same_project_store(tmp_path):
    service = bootstrap.build_service(Settings(root=tmp_path))
    assert isinstance(service._memory_store, FileMemoryStore)
    assert isinstance(service._project_store, FileProjectStore)
    assert service._memory_store._project_store is service._project_store


def test_injects_the_configured_limits_and_the_fixed_internal_ones(tmp_path):
    settings = Settings(root=tmp_path, max_body_chars=10, search_limit_default=3,
                        search_limit_max=7, index_budget_lines=9)
    service = bootstrap.build_service(settings)
    assert (service._max_body_chars, service._metadata_max_chars,
            service._search_limit_default, service._search_limit_max,
            service._index_budget_lines, service._id_generation_attempts) == \
        (10, DEFAULT_MAX_BODY_CHARS, 3, 7, 9, ID_GENERATION_ATTEMPTS)
    assert ID_GENERATION_ATTEMPTS == 5
    assert "id_generation_attempts" not in Settings.model_fields


def test_returns_the_facade(tmp_path):
    assert isinstance(bootstrap.build_service(Settings(root=tmp_path)), MemoryService)


def test_build_diagnostics_service_uses_explicit_root(tmp_path):
    service = bootstrap.build_diagnostics_service(Settings(root=tmp_path / "s"),
                                                  root=tmp_path / "explicit")
    assert isinstance(service, DiagnosticsService)
    assert isinstance(service._inspector, FilesystemStoreInspector)
    assert service._inspector.root == tmp_path / "explicit"


def test_bootstrap_reexports_store_lock_and_the_empty_index():
    from memriver_core.application.service import EMPTY_INDEX
    from memriver_core.repository.filesystem.locking import store_lock

    assert bootstrap.store_lock is store_lock
    assert bootstrap.EMPTY_INDEX == EMPTY_INDEX
