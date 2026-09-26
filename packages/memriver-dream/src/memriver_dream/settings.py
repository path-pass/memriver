"""memriver-dream's settings: the [dream] table of <root>/settings.toml, and every
dream default and fixed constant (the rule: each package keeps its defaults in its
one settings module).

The table is read on its own, straight from the file; there is no environment
layer. The Codex override whitelist lives here because the table's keys are the
user's file format: the executor it names is only a value to this package.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from memriver_core.settings import (
    SettingsError,
    reject_boolean,
    settings_file,
    validation_fields,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import TomlConfigSettingsSource

__all__ = [
    "DEFAULT_DREAM_IDLE_MINUTES",
    "DEFAULT_DREAM_MAX_CANDIDATES_PER_RUN",
    "DEFAULT_DREAM_MAX_GROUPS_PER_RUN",
    "DEFAULT_DREAM_MAX_SESSIONS_PER_RUN",
    "DEFAULT_DREAM_SCHEDULE_AT",
    "DEFAULT_DREAM_TTL_DAYS",
    "DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX",
    "DEFAULT_DREAM_UNCERTAIN_LIMIT",
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
    "DREAM_TOOL_OUTPUT_CHARS",
    "DreamSettings",
    "check_codex_overrides",
    "load_dream_settings",
]

DREAM_TABLE = "dream"

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

# fixed values (spec §10): initial values, revisited after a real-transcript run
DREAM_CONTEXT_BUDGET_TOKENS = 100_000   # one executor call, input and output
DREAM_OUTPUT_RESERVE_TOKENS = 4_000     # kept free for the answer
# the estimate is rough and sees neither the schema, the harness's own prompt nor
# a global AGENTS.md: this much input room is kept unused on top of the reserve
DREAM_INPUT_MARGIN_TOKENS = 16_000
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
    """The [dream] table of settings.toml: what memriver dream init writes and dream reads.

    A plain model, not a BaseSettings: the table has one source (the file) and no
    environment layer, and BaseSettings' own constructor would take a table key
    such as `_secrets_dir` or `_cli_parse_args` as one of its options.
    TomlConfigSettingsSource needs only the fields and the config, which a model
    has. extra="ignore", like core: a key it does not know is skipped.
    """

    model_config = ConfigDict(extra="ignore")

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
        return reject_boolean(value)

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


def load_dream_settings(root: Path) -> DreamSettings | None:
    """The [dream] table of <root>/settings.toml; None when there is none.

    An unreadable file, bad TOML or an invalid table raises core's SettingsError:
    dream never runs on a guess. Its fields carry the "dream." prefix, never a
    value, the path or pydantic's text.
    """
    path = settings_file(root)      # raises when the file cannot be read
    if path is None:
        return None
    try:
        table = TomlConfigSettingsSource(DreamSettings,  # type: ignore[arg-type]
                                         toml_file=path, toml_table_header=(DREAM_TABLE,))()
    except KeyError:
        # the file has no [dream] table: dream is simply not configured
        return None
    except AttributeError:
        # `dream = 5`: the key exists but is not a table
        raise SettingsError((DREAM_TABLE,)) from None
    except (OSError, ValueError):
        # permission denied, bad TOML, bad UTF-8: their text repeats the path
        raise SettingsError(unreadable=True) from None
    try:
        return DreamSettings.model_validate(table)
    except ValidationError as err:
        # from None: the cause echoes the rejected value, which could be a secret
        raise SettingsError(validation_fields(err, prefix=f"{DREAM_TABLE}.")) from None
