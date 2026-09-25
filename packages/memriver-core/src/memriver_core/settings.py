"""Tunable settings for memriver, shared by the umbrella server and any
future package (dream, sync, vector) that needs the same knobs without
depending on the MCP umbrella.

Settings precedence: CLI override > env > <root>/settings.toml > defaults.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_BUDGET_LINES",
    "DEFAULT_DREAM_IDLE_MINUTES",
    "DEFAULT_DREAM_MAX_CANDIDATES_PER_RUN",
    "DEFAULT_DREAM_MAX_GROUPS_PER_RUN",
    "DEFAULT_DREAM_MAX_SESSIONS_PER_RUN",
    "DEFAULT_DREAM_SCHEDULE_AT",
    "DEFAULT_DREAM_TTL_DAYS",
    "DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX",
    "DEFAULT_DREAM_UNCERTAIN_LIMIT",
    "DEFAULT_MAX_BODY_CHARS",
    "DEFAULT_SEARCH_LIMIT",
    "DEFAULT_SEARCH_LIMIT_MAX",
    "DREAM_CALL_TIMEOUT_S",
    "DREAM_CHUNK_SUMMARY_CHARS",
    "DREAM_CONTEXT_BUDGET_TOKENS",
    "DREAM_DIRECTORY",
    "DREAM_INPUT_MARGIN_TOKENS",
    "DREAM_LAUNCH_AGENT_LABEL",
    "DREAM_LOCK_FILENAME",
    "DREAM_LOG_FILENAME",
    "DREAM_MAX_CALLS_PER_SESSION",
    "DREAM_MAX_QUARANTINE_PER_RUN",
    "DREAM_MAX_ROOM_HALVINGS",
    "DREAM_OUTPUT_RESERVE_TOKENS",
    "DREAM_REASON_CHARS",
    "DREAM_SUMMARY_MAX_CHARS",
    "DREAM_TOOL_OUTPUT_CHARS",
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
    "DreamSettings",
    "Settings",
    "check_codex_overrides",
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

# the [dream] table (spec §10); executor and executor_path have no default:
# memriver dream init writes them
DEFAULT_DREAM_TTL_DAYS = 90
DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX = 5
DEFAULT_DREAM_UNCERTAIN_LIMIT = 2
DEFAULT_DREAM_IDLE_MINUTES = 60
DEFAULT_DREAM_SCHEDULE_AT = "04:00"
DEFAULT_DREAM_MAX_SESSIONS_PER_RUN = 20
DEFAULT_DREAM_MAX_GROUPS_PER_RUN = 20
DEFAULT_DREAM_MAX_CANDIDATES_PER_RUN = 30

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

# dream (spec §10): initial values, revisited after a real-transcript run
DREAM_CONTEXT_BUDGET_TOKENS = 100_000   # one executor call, input and output
DREAM_OUTPUT_RESERVE_TOKENS = 4_000     # kept free for the answer
# the estimate is rough and sees neither the schema, the harness's own prompt nor
# a global AGENTS.md: this much input room is kept unused on top of the reserve
DREAM_INPUT_MARGIN_TOKENS = 16_000
DREAM_SUMMARY_MAX_CHARS = 1_200         # one stored session summary
DREAM_CHUNK_SUMMARY_CHARS = 1_500       # one partial summary of a long session
DREAM_TOOL_OUTPUT_CHARS = 2_000         # one tool output as a transcript record
DREAM_MAX_CALLS_PER_SESSION = 12        # map and reduce calls for one session
DREAM_MAX_ROOM_HALVINGS = 3             # a session's input room, after "too-large" answers
DREAM_CALL_TIMEOUT_S = 300              # one executor call
DREAM_MAX_QUARANTINE_PER_RUN = 1_000    # secret soft-deletes in one run
DREAM_REASON_CHARS = 300                # one change or review reason, as stored
DREAM_DIRECTORY = "dream"               # <root>/dream: the run lock and the run log
DREAM_LOCK_FILENAME = ".lock"
DREAM_LOG_FILENAME = "dream.log"
DREAM_LAUNCH_AGENT_LABEL = "io.github.path-pass.memriver.dream"


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


def _reject_boolean(value: object) -> object:
    """Reject a TOML boolean where an integer is expected.

    pydantic's lax mode reads True as 1 and gt=0 lets it through, so
    'search_limit_max = true' would silently cap every search at one hit.
    TOML has a real boolean type, so this is a plausible typo. Shared by
    Settings and DreamSettings so the two integer validators stay identical.
    """
    if isinstance(value, bool):
        raise ValueError("expected an integer, got a boolean")  # noqa: TRY004
    return value


_SCHEDULE_AT_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
# [dream.codex_overrides] (spec §9.2): the Codex provider keys a user may set, each
# matched by its whole path; everything else -- tools, hooks, MCP servers, whole
# tables, credential fields -- is refused
_CODEX_TOP_KEYS = frozenset({"model_provider", "model"})
_CODEX_PROVIDER_KEY_RE = re.compile(
    r"model_providers\.([A-Za-z0-9_-]{1,64})\."
    r"(name|base_url|env_key|wire_api|requires_openai_auth)")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_PLAIN_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _plain_url(text: str) -> bool:
    """An http(s) URL with a host and nowhere to hide a credential: no user
    information, no query, no fragment."""
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname)
            and parts.username is None and parts.password is None
            and "?" not in text and "#" not in text)


def check_codex_overrides(value: object) -> dict[str, str | bool]:
    """The whitelisted Codex provider overrides, or ValueError naming the key and a
    fixed reason -- never the value, which could be a pasted secret."""
    if not isinstance(value, dict):
        # a TypeError here would escape the field_validator uncaught: pydantic only
        # catches ValueError/AssertionError from a validator, never TypeError
        raise ValueError("codex_overrides must be a table")  # noqa: TRY004
    provider_ids: set[str] = set()
    for key, item in value.items():
        shown = key if isinstance(key, str) and _PLAIN_KEY_RE.fullmatch(key) else "a key"
        match = _CODEX_PROVIDER_KEY_RE.fullmatch(key) if isinstance(key, str) else None
        if key not in _CODEX_TOP_KEYS and match is None:
            raise ValueError(f"codex_overrides: {shown} is not an allowed key")
        field = match.group(2) if match else key
        if field == "requires_openai_auth":
            if not isinstance(item, bool):
                raise ValueError(f"codex_overrides: {shown} must be true or false")
        elif not isinstance(item, str) or not item.strip() or not item.isprintable():
            raise ValueError(f"codex_overrides: {shown} must be a non-empty single-line "
                             "string")
        elif field == "base_url" and not _plain_url(item):
            raise ValueError(f"codex_overrides: {shown} must be an http(s) URL without user "
                             "information, query or fragment")
        elif field == "env_key" and not _ENV_NAME_RE.fullmatch(item):
            raise ValueError(f"codex_overrides: {shown} must name an environment variable")
        elif field == "wire_api" and item != "responses":
            raise ValueError(f'codex_overrides: {shown} must be "responses"')
        if match:
            provider_ids.add(match.group(1))
    if provider_ids and provider_ids != {value.get("model_provider")}:
        raise ValueError("codex_overrides: provider keys must define the one provider "
                         "model_provider selects")
    return value


class DreamSettings(BaseModel):
    """The [dream] table of settings.toml: what memriver dream init writes and dream reads."""

    model_config = ConfigDict(extra="forbid")

    executor: Literal["claude", "codex"]
    executor_path: str
    ttl_days: int = Field(DEFAULT_DREAM_TTL_DAYS, gt=0)
    ttl_read_multiplier_max: int = Field(DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX, gt=0)
    uncertain_limit: int = Field(DEFAULT_DREAM_UNCERTAIN_LIMIT, gt=0)
    idle_minutes: int = Field(DEFAULT_DREAM_IDLE_MINUTES, gt=0)
    schedule_at: str = DEFAULT_DREAM_SCHEDULE_AT
    max_sessions_per_run: int = Field(DEFAULT_DREAM_MAX_SESSIONS_PER_RUN, gt=0)
    max_groups_per_run: int = Field(DEFAULT_DREAM_MAX_GROUPS_PER_RUN, gt=0)
    max_candidates_per_run: int = Field(DEFAULT_DREAM_MAX_CANDIDATES_PER_RUN, gt=0)
    # provider settings the Codex executor passes as -c overrides (spec §9.2): the
    # executor skips config.toml, so a provider defined only there is given here
    codex_overrides: dict[str, str | bool] = Field(default_factory=dict)

    @field_validator("codex_overrides", mode="before")
    @classmethod
    def _codex_whitelist(cls, value: object) -> object:
        return check_codex_overrides(value)

    @field_validator("ttl_days", "ttl_read_multiplier_max", "uncertain_limit", "idle_minutes",
                     "max_sessions_per_run", "max_groups_per_run", "max_candidates_per_run",
                     mode="before")
    @classmethod
    def _no_booleans(cls, value: object) -> object:
        return _reject_boolean(value)

    @field_validator("executor_path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            raise ValueError("executor_path must be absolute")
        return value

    @field_validator("schedule_at")
    @classmethod
    def _hh_mm(cls, value: str) -> str:
        if not _SCHEDULE_AT_RE.fullmatch(value):
            raise ValueError("schedule_at must be HH:MM")
        return value


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
    # unset keeps every memory_reads row; a number of days prunes older rows
    # whenever a new one is written
    memory_reads_retention_days: int | None = Field(None, gt=0)
    dream: DreamSettings | None = None
    _dream_invalid: bool = PrivateAttr(default=False)

    @field_validator("max_body_chars", "search_limit_default", "search_limit_max",
                     "index_budget_lines", "memory_reads_retention_days", mode="before")
    @classmethod
    def _no_booleans(cls, value: object) -> object:
        return _reject_boolean(value)

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

    @property
    def dream_invalid(self) -> bool:
        """A [dream] table exists but was invalid, so `dream` is None."""
        return self._dream_invalid


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


def _dream_settings(table: object) -> tuple[DreamSettings | None, bool]:
    """The [dream] table on its own: an invalid one is dropped alone, with a warning,
    so a typo there never resets the server's settings. (settings, invalid)."""
    if table is None:
        return None, False
    try:
        if not isinstance(table, dict):
            raise TypeError("the dream key is not a table")
        return DreamSettings(**table), False
    except (TypeError, ValidationError):
        # never the exception text: a ValidationError echoes the value back
        log.warning("ignoring the invalid [dream] table in %s", SETTINGS_FILENAME)
        return None, True


def load_settings(root_override: Path | None = None) -> Settings:
    """CLI override > env > <root>/settings.toml > defaults.

    The root is resolved first (override, env, default) so the settings file
    can live inside the store it configures.
    """
    root = Path(root_override) if root_override is not None else storage_root()
    settings_path = root / SETTINGS_FILENAME
    file_values = _read_settings_file(settings_path)
    dream, dream_invalid = _dream_settings(file_values.pop("dream", None))
    # pydantic-settings ranks constructor arguments *above* the env layer, so
    # handing it the file values wholesale would let the file beat the
    # environment. Dropping the keys the environment already sets restores the
    # documented order without a custom settings source.
    env_keys = {k.upper() for k in os.environ}
    file_values = {k: v for k, v in file_values.items()
                   if f"{ENV_PREFIX}{k.upper()}" not in env_keys}
    # built from the merged env + file configuration first: a field valid only once
    # both sources combine (env raises a max, the file lowers a default under it)
    # must reach that combined validation, never a premature env-only construction
    try:
        settings = Settings(root=root, dream=dream, **file_values)
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
        settings = Settings(root=root, dream=dream)
    settings._dream_invalid = dream_invalid
    return settings
