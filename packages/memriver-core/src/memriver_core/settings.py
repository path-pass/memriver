"""Tunable settings for memriver, shared by the umbrella server and any
future package (dream, sync, vector) that needs the same knobs without
depending on the MCP umbrella.

Settings precedence: CLI override > env > <root>/settings.toml > defaults.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_BUDGET_LINES",
    "DEFAULT_MAX_BODY_CHARS",
    "DEFAULT_SEARCH_LIMIT",
    "DEFAULT_SEARCH_LIMIT_MAX",
    "GIT_QUERY_TIMEOUT_S",
    "HEADER_FIELD_CHARS",
    "INDEX_CUE_CHARS",
    "PROJECT_NAME_MAX_CHARS",
    "SEARCH_SNIPPET_CHARS",
    "SESSION_PROMPT_CHARS",
    "SESSION_PROMPT_SCAN_MAX_BYTES",
    "SESSION_RECENT_PROMPTS",
    "SESSION_SEARCH_LIMIT_DEFAULT",
    "SESSION_SEARCH_LIMIT_MAX",
    "STOP_NUDGE_INTERVAL_PROMPTS",
    "STOP_NUDGE_MIN_PROMPTS",
    "TOOL_CALL_RETENTION_S",
    "Settings",
    "load_settings",
    "storage_root",
]

log = logging.getLogger(__name__)

ENV_PREFIX = "MEMRIVER_"
SETTINGS_FILENAME = "settings.toml"

# Canonical home of every user-configurable behavior default, each backed by
# a Settings field below. The fixed, non-configurable values (e.g. the index
# cue length) live in the constants block further down instead.
DEFAULT_MAX_BODY_CHARS = 8000
DEFAULT_SEARCH_LIMIT_MAX = 50
DEFAULT_SEARCH_LIMIT = 5
DEFAULT_BUDGET_LINES = 100

# Fixed values that are not user-configurable but still live here, with every
# other default (the user's rule: no default constants in feature modules).
# bootstrap injects them: models, application and repository never import
# settings.
INDEX_CUE_CHARS = 60           # one index line's cue
SEARCH_SNIPPET_CHARS = 60      # one memory_search hit's body snippet
HEADER_FIELD_CHARS = 120       # one field of the project header
PROJECT_NAME_MAX_CHARS = 120   # a project name
BUSY_TIMEOUT_MS = 5000         # SQLite's bounded wait for the write lock
# harness sessions (spec §8)
SESSION_PROMPT_CHARS = 512              # one recorded prompt's text
SESSION_RECENT_PROMPTS = 5              # prompts kept per session, newest last
SESSION_PROMPT_SCAN_MAX_BYTES = 65536   # a larger prompt is omitted unscanned
STOP_NUDGE_MIN_PROMPTS = 5              # unsaved prompts before the first Stop nudge
STOP_NUDGE_INTERVAL_PROMPTS = 5         # prompts between two Stop nudges
GIT_QUERY_TIMEOUT_S = 2                 # one git call mapping a worktree
SESSION_SEARCH_LIMIT_DEFAULT = 10
SESSION_SEARCH_LIMIT_MAX = 50
TOOL_CALL_RETENTION_S = 3600            # how long a Claude Code call -> session mapping is kept


def storage_root(env: Mapping[str, str] | None = None,
                 home: Path | None = None) -> Path:
    """The memory storage root: ``$MEMRIVER_ROOT`` or ``<home>/agent-memory``.

    ``env``/``home`` default to the real process environment/home so every
    existing zero-argument caller (the ``root`` field's ``default_factory``,
    ``load_settings``) is unaffected; a caller that already has its own
    injected env/home (``memriver uninstall``, tested against a fake root)
    passes them through instead of reading the real process state.
    """
    value = (os.environ if env is None else env).get("MEMRIVER_ROOT")
    return Path(value) if value else (home or Path.home()) / "agent-memory"


class Settings(BaseSettings):
    """Behaviour knobs, read from MEMRIVER_* env vars and <root>/settings.toml.

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

    @model_validator(mode="after")
    def _default_within_max(self) -> Settings:
        # a default above the max would make the untouched, common case (no
        # explicit limit on a search call) silently ask for more hits than
        # the store is configured to ever return
        if self.search_limit_default > self.search_limit_max:
            raise ValueError(
                f"search_limit_default ({self.search_limit_default}) must not "
                f"exceed search_limit_max ({self.search_limit_max})")
        return self


def _read_settings_file(path: Path) -> dict:
    """Read a flat TOML file of setting keys. Never raises.

    Every warning below names only ``SETTINGS_FILENAME`` (never ``path``, which
    may be an absolute, permission-denied, or otherwise sensitive location)
    and never an exception's own text -- an OSError's str() routinely repeats
    the absolute path, so it is dropped rather than logged. This mirrors the
    fieldless discipline `StorageFailure` already applies to that boundary.
    """
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        # a broken settings file must not stop the server from starting: the
        # defaults are always usable, and stderr carries the reason
        log.warning("ignoring unreadable %s", SETTINGS_FILENAME)
        return {}
    known = set(Settings.model_fields)
    values = {}
    for key, value in data.items():
        if key == "root":
            # chicken and egg: the root is what located this file
            log.warning("ignoring 'root' in %s: set MEMRIVER_ROOT or --root instead",
                        SETTINGS_FILENAME)
        elif key in known:
            values[key] = value
        else:
            log.warning("ignoring unknown key %r in %s", key, SETTINGS_FILENAME)
    return values


def load_settings(root_override: Path | None = None) -> Settings:
    """CLI override > env > <root>/settings.toml > defaults.

    The root is resolved first (override, env, default) so the settings file
    can live inside the store it configures.
    """
    root = Path(root_override) if root_override is not None else storage_root()
    settings_path = root / SETTINGS_FILENAME
    file_values = _read_settings_file(settings_path)
    # pydantic-settings ranks constructor arguments *above* the env layer, so
    # handing it the file values wholesale would let the file beat the
    # environment. Dropping the keys the environment already sets restores the
    # documented order without a custom settings source.
    env_keys = {k.upper() for k in os.environ}
    file_values = {k: v for k, v in file_values.items()
                   if f"{ENV_PREFIX}{k.upper()}" not in env_keys}
    if not file_values:
        return Settings(root=root)
    try:
        return Settings(root=root, **file_values)
    except ValidationError:
        # a typo'd *value* is as likely as a typo'd key, and neither may stop an
        # agent's memory server from starting. The whole file is dropped rather
        # than the offending key: a partially applied settings file is harder
        # to reason about than none at all. The warning names the file to fix,
        # but not `settings_path` (absolute) or the exception text --
        # ValidationError echoes back the offending value, which could itself
        # be a path.
        log.warning("ignoring %s, falling back to environment and defaults",
                    SETTINGS_FILENAME)
        return Settings(root=root)
