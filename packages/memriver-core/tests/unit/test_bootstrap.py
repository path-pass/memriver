"""bootstrap: the only place the concrete adapters are named."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from memriver_core import bootstrap
from memriver_core.application.maintenance import MaintenanceService
from memriver_core.application.memory import MemoryService
from memriver_core.application.projects import ProjectService
from memriver_core.application.sessions import SessionService
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
    services = bootstrap.build_services(Settings(root=tmp_path / "from-settings"))
    assert services.project._project_store.root == tmp_path / "from-settings"
    assert services.memory._memory_store.root == tmp_path / "from-settings"
    assert services.session._session_store.root == tmp_path / "from-settings"


def test_an_explicit_root_wins_over_the_settings_root(tmp_path):
    services = bootstrap.build_services(Settings(root=tmp_path / "from-settings"),
                                        root=tmp_path / "explicit")
    assert services.project._project_store.root == tmp_path / "explicit"
    assert services.memory._memory_store.root == tmp_path / "explicit"


def test_composes_the_four_services(tmp_path):
    services = bootstrap.build_services(Settings(root=tmp_path))
    assert isinstance(services.memory, MemoryService)
    assert isinstance(services.project, ProjectService)
    assert isinstance(services.session, SessionService)
    assert isinstance(services.maintenance, MaintenanceService)


def test_composes_the_sqlite_adapters(tmp_path):
    services = bootstrap.build_services(Settings(root=tmp_path), home=tmp_path / "home")
    assert isinstance(services.memory._memory_store, SqliteMemoryStore)
    assert isinstance(services.project._project_store, SqliteProjectStore)
    assert isinstance(services.session._session_store, SqliteSessionStore)
    assert isinstance(services.maintenance._inspector, SqliteStoreInspector)
    assert services.project._project_store._home == tmp_path / "home"
    # one project store over the root, shared by the services that read projects
    assert services.memory._project_store is services.project._project_store
    assert services.session._project_store is services.project._project_store


def test_memory_writes_ask_the_session_service_through_its_callbacks(tmp_path):
    # no service constructs another: bootstrap hands MemoryService the session
    # service's two bound methods
    services = bootstrap.build_services(Settings(root=tmp_path))
    assert services.memory._mark_saved == services.session.mark_saved
    assert services.memory._refuse_pending == services.session.refuse_pending


def test_the_two_text_checking_services_share_one_content_policy(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(bootstrap, "_content_policy", lambda: built.append(1) or object())
    services = bootstrap.build_services(Settings(root=tmp_path))
    assert services.memory._policy() is services.session._policy()
    assert built == [1]


def test_injects_the_configured_limits_and_the_fixed_constants(tmp_path):
    settings = Settings(root=tmp_path, max_body_chars=10, search_limit_default=3,
                        search_limit_max=7, index_budget_lines=9)
    services = bootstrap.build_services(settings)
    memory = services.memory
    assert (memory._max_body_chars, memory._metadata_max_chars,
            memory._search_limit_default, memory._search_limit_max,
            memory._index_budget_lines, memory._index_cue_chars) == (
        10, DEFAULT_MAX_BODY_CHARS, 3, 7, 9, INDEX_CUE_CHARS)
    assert (services.project._header_field_chars,
            services.project._project_name_max_chars) == (HEADER_FIELD_CHARS,
                                                          PROJECT_NAME_MAX_CHARS)
    assert services.session._header_field_chars == HEADER_FIELD_CHARS


def test_composes_the_directory_callables(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    session = bootstrap.build_services(Settings(root=base / "store"),
                                       home=base / "home").session
    for query in (session._main_tree_path, session._current_branch):
        assert query.keywords == {"timeout_s": GIT_QUERY_TIMEOUT_S}
    plain = base / "plain"
    plain.mkdir()
    assert session._canonical_directory(str(plain)) == str(plain)
    assert session._main_tree_path(str(plain)) == str(plain)
    assert session._current_branch(str(plain)) is None
    (base / "link").symlink_to(plain)
    # "ok" and "missing" are intact; a re-pointed root is not
    assert session._root_is_intact(str(plain)) is True
    assert session._root_is_intact(str(base / "gone")) is True
    assert session._root_is_intact(str(base / "link")) is False


def test_injects_the_session_constants(tmp_path):
    session = bootstrap.build_services(Settings(root=tmp_path)).session
    assert (session._session_prompt_chars, session._session_recent_prompts,
            session._session_prompt_scan_max_bytes, session._stop_nudge_min_prompts,
            session._stop_nudge_interval_prompts, session._session_search_limit_default,
            session._session_search_limit_max, session._tool_call_retention_s) == (
        SESSION_PROMPT_CHARS, SESSION_RECENT_PROMPTS, SESSION_PROMPT_SCAN_MAX_BYTES,
        STOP_NUDGE_MIN_PROMPTS, STOP_NUDGE_INTERVAL_PROMPTS, SESSION_SEARCH_LIMIT_DEFAULT,
        SESSION_SEARCH_LIMIT_MAX, TOOL_CALL_RETENTION_S)


def test_bootstrap_exports_the_services_builder_the_empty_index_and_the_purge():
    assert set(bootstrap.__all__) == {"EMPTY_INDEX", "PurgePlan", "PurgeRefusal", "PurgeResult",
                                      "Services", "build_services", "plan_purge", "purge"}
    # the single-facade builder is gone: every caller builds the four services
    for gone in ("build_service", "build_diagnostics_service", "store_lock", "replace_file"):
        assert not hasattr(bootstrap, gone)


def test_building_the_services_never_loads_the_secret_scanner(tmp_path):
    script = ("import sys; from memriver_core.bootstrap import build_services; "
              "from memriver_core.settings import Settings; "
              f"build_services(Settings(root={str(tmp_path)!r})); "
              "print('memriver_core.content_policy.secret_scanner' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == "False"
