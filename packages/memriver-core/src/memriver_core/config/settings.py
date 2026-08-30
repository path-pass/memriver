"""Tunable settings for memriver, shared by the umbrella server and any
future package (dream, sync, vector) that needs the same knobs without
depending on the MCP umbrella.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "MEMRIVER_"

# Canonical home of every user-configurable behavior default (each backed by
# a Settings field below). Interface defaults that aren't user-configurable
# (e.g. memory_dream's limit=3) live at their own function signatures instead.
DEFAULT_MAX_BODY_CHARS = 8000
DEFAULT_SEARCH_LIMIT_MAX = 50
DEFAULT_SEARCH_LIMIT = 5
DEFAULT_BUDGET_LINES = 100


def storage_root() -> Path:
    env = os.environ.get("MEMRIVER_ROOT")
    return Path(env) if env else Path.home() / "agent-memory"


class Settings(BaseSettings):
    """Behaviour knobs, read from MEMRIVER_* env vars and <root>/config.toml.

    Every default reproduces the behaviour memriver had before the settings
    existed, so an unconfigured install is unchanged.
    """

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX)

    # storage_root() also honours MEMRIVER_ROOT, so this default agrees with the
    # env layer above it; calling it lazily keeps Path.home() out of import time
    root: Path = Field(default_factory=storage_root)
    # every knob is a count or a budget: zero and negative values are never
    # meaningful, and gt=0 turns them into a startup error instead of a server
    # that silently answers nothing
    max_body_chars: int = Field(DEFAULT_MAX_BODY_CHARS, gt=0)
    search_limit_default: int = Field(DEFAULT_SEARCH_LIMIT, gt=0)
    search_limit_max: int = Field(DEFAULT_SEARCH_LIMIT_MAX, gt=0)
    index_budget_lines: int = Field(DEFAULT_BUDGET_LINES, gt=0)

    @field_validator("max_body_chars", "search_limit_default", "search_limit_max",
                     "index_budget_lines", mode="before")
    @classmethod
    def _no_booleans(cls, value: object) -> object:
        # pydantic's lax mode reads True as 1 and gt=0 lets it through, so
        # 'search_limit_max = true' would silently cap every search at one hit.
        # TOML has a real boolean type, so this is a plausible typo.
        if isinstance(value, bool):
            raise ValueError("expected an integer, got a boolean")  # noqa: TRY004
        return value
