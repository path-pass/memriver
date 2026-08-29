"""Tunable settings for memriver, shared by the umbrella server and any
future package (dream, sync, vector) that needs the same knobs without
depending on the MCP umbrella.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .gate import MAX_BODY_CHARS
from .render import DEFAULT_BUDGET_LINES
from .scope import storage_root
from .search import MAX_SEARCH_LIMIT

log = logging.getLogger(__name__)

CONFIG_FILENAME = "config.toml"
ENV_PREFIX = "MEMRIVER_"

DEFAULT_SEARCH_LIMIT = 5


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
    max_body_chars: int = Field(MAX_BODY_CHARS, gt=0)
    search_limit_default: int = Field(DEFAULT_SEARCH_LIMIT, gt=0)
    search_limit_max: int = Field(MAX_SEARCH_LIMIT, gt=0)
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


def _read_config_file(path: Path) -> dict:
    """Read a flat TOML file of setting keys. Never raises."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError, ValueError) as err:
        # a broken config file must not stop the server from starting: the
        # defaults are always usable, and stderr carries the reason
        log.warning("ignoring unreadable %s: %s", path, err)
        return {}
    known = set(Settings.model_fields)
    values = {}
    for key, value in data.items():
        if key == "root":
            # chicken and egg: the root is what located this file
            log.warning("ignoring 'root' in %s: set MEMRIVER_ROOT or --root instead",
                        path)
        elif key in known:
            values[key] = value
        else:
            log.warning("ignoring unknown key %r in %s", key, path)
    return values


def load_settings(root_override: Path | None = None) -> Settings:
    """CLI override > env > <root>/config.toml > defaults.

    The root is resolved first (override, env, default) so the config file
    can live inside the store it configures.
    """
    root = Path(root_override) if root_override is not None else storage_root()
    config_path = root / CONFIG_FILENAME
    file_values = _read_config_file(config_path)
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
    except ValidationError as err:
        # a typo'd *value* is as likely as a typo'd key, and neither may stop an
        # agent's memory server from starting. The whole file is dropped rather
        # than the offending key: a partially applied config is harder to reason
        # about than none at all, and the warning names the file to fix.
        log.warning("ignoring %s, falling back to environment and defaults: %s",
                    config_path, err)
        return Settings(root=root)
