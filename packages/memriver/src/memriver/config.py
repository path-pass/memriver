"""Tunable settings for the memriver MCP server.

Only the umbrella package depends on pydantic-settings; memriver-core stays on
python-frontmatter + python-ulid + the standard library and takes every tunable
as a plain function or constructor parameter.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from memriver_core.gate import MAX_BODY_CHARS
from memriver_core.index_fts import MAX_SEARCH_LIMIT
from memriver_core.scope import storage_root
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

CONFIG_FILENAME = "config.toml"
ENV_PREFIX = "MEMRIVER_"

# render_index's own default; kept here rather than imported because render.py
# spells it as a signature default and core needs no constant for it
DEFAULT_INDEX_BUDGET_LINES = 100
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
    max_body_chars: int = MAX_BODY_CHARS
    search_limit_default: int = DEFAULT_SEARCH_LIMIT
    search_limit_max: int = MAX_SEARCH_LIMIT
    index_budget_lines: int = DEFAULT_INDEX_BUDGET_LINES


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
    file_values = _read_config_file(root / CONFIG_FILENAME)
    # pydantic-settings ranks constructor arguments *above* the env layer, so
    # handing it the file values wholesale would let the file beat the
    # environment. Dropping the keys the environment already sets restores the
    # documented order without a custom settings source.
    env_keys = {k.upper() for k in os.environ}
    file_values = {k: v for k, v in file_values.items()
                   if f"{ENV_PREFIX}{k.upper()}" not in env_keys}
    return Settings(root=root, **file_values)
