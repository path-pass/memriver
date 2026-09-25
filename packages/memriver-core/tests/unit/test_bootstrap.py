"""bootstrap: the only place the concrete adapters are named."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from memriver_core import bootstrap
from memriver_core.application.service import MemoryService
from memriver_core.repository.sqlite import (
    SqliteMemoryStore,
    SqliteProjectStore,
    SqliteSessionStore,
    SqliteStoreInspector,
)
from memriver_core.settings import (
    DEFAULT_MAX_BODY_CHARS,
    GIT_QUERY_TIMEOUT_S,
    HEADER_FIELD_CHARS,
    INDEX_CUE_CHARS,
    PROJECT_NAME_MAX_CHARS,
    SESSION_PROMPT_CHARS,
    SESSION_PROMPT_SCAN_MAX_BYTES,
    SESSION_RECENT_PROMPTS,
    SESSION_SEARCH_LIMIT_DEFAULT,
    SESSION_SEARCH_LIMIT_MAX,
    STOP_NUDGE_INTERVAL_PROMPTS,
    STOP_NUDGE_MIN_PROMPTS,
    TOOL_CALL_RETENTION_S,
    Settings,
)


def test_uses_the_settings_root_by_default(tmp_path):
    service = bootstrap.build_service(Settings(root=tmp_path / "from-settings"))
    assert service._project_store.root == tmp_path / "from-settings"
    assert service._memory_store.root == tmp_path / "from-settings"


def test_an_explicit_root_wins_over_the_settings_root(tmp_path):
    service = bootstrap.build_service(Settings(root=tmp_path / "from-settings"),
                                      root=tmp_path / "explicit")
    assert service._project_store.root == tmp_path / "explicit"


def test_composes_the_sqlite_adapters(tmp_path):
    service = bootstrap.build_service(Settings(root=tmp_path), home=tmp_path / "home")
    assert isinstance(service._memory_store, SqliteMemoryStore)
    assert isinstance(service._project_store, SqliteProjectStore)
    assert isinstance(service._diagnostics._inspector, SqliteStoreInspector)
    assert service._project_store._home == tmp_path / "home"


def test_injects_the_configured_limits_and_the_fixed_constants(tmp_path):
    settings = Settings(root=tmp_path, max_body_chars=10, search_limit_default=3,
                        search_limit_max=7, index_budget_lines=9)
    service = bootstrap.build_service(settings)
    assert (service._max_body_chars, service._metadata_max_chars,
            service._search_limit_default, service._search_limit_max,
            service._index_budget_lines) == (10, DEFAULT_MAX_BODY_CHARS, 3, 7, 9)
    assert (service._index_cue_chars, service._header_field_chars,
            service._project_name_max_chars) == (INDEX_CUE_CHARS, HEADER_FIELD_CHARS,
                                                 PROJECT_NAME_MAX_CHARS)


def test_composes_the_session_store_and_the_directory_callables(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    service = bootstrap.build_service(Settings(root=base / "store"), home=base / "home")
    assert isinstance(service._session_store, SqliteSessionStore)
    assert service._session_store.root == base / "store"
    for query in (service._main_tree_path, service._current_branch):
        assert query.keywords == {"timeout_s": GIT_QUERY_TIMEOUT_S}
    plain = base / "plain"
    plain.mkdir()
    assert service._canonical_directory(str(plain)) == str(plain)
    assert service._main_tree_path(str(plain)) == str(plain)
    assert service._current_branch(str(plain)) is None
    (base / "link").symlink_to(plain)
    # "ok" and "missing" are intact; a re-pointed root is not
    assert service._root_is_intact(str(plain)) is True
    assert service._root_is_intact(str(base / "gone")) is True
    assert service._root_is_intact(str(base / "link")) is False


def test_injects_the_session_constants(tmp_path):
    service = bootstrap.build_service(Settings(root=tmp_path))
    assert (service._session_prompt_chars, service._session_recent_prompts,
            service._session_prompt_scan_max_bytes, service._stop_nudge_min_prompts,
            service._stop_nudge_interval_prompts, service._session_search_limit_default,
            service._session_search_limit_max, service._tool_call_retention_s) == (
        SESSION_PROMPT_CHARS, SESSION_RECENT_PROMPTS, SESSION_PROMPT_SCAN_MAX_BYTES,
        STOP_NUDGE_MIN_PROMPTS, STOP_NUDGE_INTERVAL_PROMPTS, SESSION_SEARCH_LIMIT_DEFAULT,
        SESSION_SEARCH_LIMIT_MAX, TOOL_CALL_RETENTION_S)


def test_returns_the_facade(tmp_path):
    assert isinstance(bootstrap.build_service(Settings(root=tmp_path)), MemoryService)


def test_bootstrap_exports_the_facade_builder_the_empty_index_and_the_purge():
    assert set(bootstrap.__all__) == {"EMPTY_INDEX", "PurgePlan", "PurgeRefusal", "PurgeResult",
                                      "build_service", "plan_purge", "purge"}
    for gone in ("build_diagnostics_service", "store_lock", "replace_file"):
        assert not hasattr(bootstrap, gone)


def test_building_the_service_never_loads_the_secret_scanner(tmp_path):
    script = ("import sys; from memriver_core.bootstrap import build_service; "
              "from memriver_core.settings import Settings; "
              f"build_service(Settings(root={str(tmp_path)!r})); "
              "print('memriver_core.content_policy.secret_scanner' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == "False"
