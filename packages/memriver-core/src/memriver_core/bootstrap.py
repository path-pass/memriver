"""The single composition point: concrete adapters are named only here."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from .application.diagnostics import DiagnosticsService

# EMPTY_INDEX is re-exported (not composed) here: bootstrap is the one
# memriver_core surface, alongside settings/models, that a transport may import.
from .application.service import EMPTY_INDEX, MemoryService

# The store purge is the one data operation outside the facade (the user's
# choice): it destroys the whole storage directory rather than records.
from .repository.directories import (
    PurgePlan,
    PurgeRefusal,
    PurgeResult,
    plan_purge,
    purge,
    root_state,
)
from .repository.sqlite import (
    SqliteMemoryStore,
    SqliteProjectStore,
    SqliteSessionStore,
    SqliteStoreInspector,
)
from .repository.worktree import current_branch, main_tree_path
from .settings import (
    BUSY_TIMEOUT_MS,
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
    Settings,
)

__all__ = [
    "EMPTY_INDEX", "PurgePlan", "PurgeRefusal", "PurgeResult", "build_service", "plan_purge",
    "purge",
]


def _content_policy():
    # imported on first use, never at module level: compiling the scanner's
    # rules is paid by a write, not by a read-only caller such as the Stop hook
    from .content_policy.secret_scanner import SecretScanner

    return SecretScanner()


def build_service(settings: Settings, *, root: Path | None = None,
                  home: Path | None = None) -> MemoryService:
    # an explicit root is authoritative: callers that already resolved it (the
    # CLI, the tests) must not have it replaced by the environment or settings
    store_root = settings.root if root is None else root
    home = Path.home() if home is None else home
    return MemoryService(
        SqliteMemoryStore(store_root, busy_timeout_ms=BUSY_TIMEOUT_MS),
        SqliteProjectStore(store_root, home=home, busy_timeout_ms=BUSY_TIMEOUT_MS),
        _content_policy,
        DiagnosticsService(SqliteStoreInspector(store_root, busy_timeout_ms=BUSY_TIMEOUT_MS)),
        session_store=SqliteSessionStore(store_root, busy_timeout_ms=BUSY_TIMEOUT_MS),
        main_tree_path=partial(main_tree_path, timeout_s=GIT_QUERY_TIMEOUT_S),
        current_branch=partial(current_branch, timeout_s=GIT_QUERY_TIMEOUT_S),
        # the integrity rule resolution applies: an offline root is fine, a
        # re-pointed or uncheckable one is not (spec §5)
        root_is_intact=lambda root: root_state(root) in ("ok", "missing"),
        max_body_chars=settings.max_body_chars,
        # metadata keeps the default budget, so a tightened body limit does
        # not silently change harness/description acceptance
        metadata_max_chars=DEFAULT_MAX_BODY_CHARS,
        search_limit_default=settings.search_limit_default,
        search_limit_max=settings.search_limit_max,
        index_budget_lines=settings.index_budget_lines,
        index_cue_chars=INDEX_CUE_CHARS,
        header_field_chars=HEADER_FIELD_CHARS,
        project_name_max_chars=PROJECT_NAME_MAX_CHARS,
        session_prompt_chars=SESSION_PROMPT_CHARS,
        session_recent_prompts=SESSION_RECENT_PROMPTS,
        session_prompt_scan_max_bytes=SESSION_PROMPT_SCAN_MAX_BYTES,
        stop_nudge_min_prompts=STOP_NUDGE_MIN_PROMPTS,
        stop_nudge_interval_prompts=STOP_NUDGE_INTERVAL_PROMPTS,
        session_search_limit_default=SESSION_SEARCH_LIMIT_DEFAULT,
        session_search_limit_max=SESSION_SEARCH_LIMIT_MAX,
    )
