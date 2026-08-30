"""Configuration precedence: CLI override > env > <root>/config.toml > defaults."""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from pydantic import ValidationError

from .settings import ENV_PREFIX, Settings, storage_root

log = logging.getLogger(__name__)

CONFIG_FILENAME = "config.toml"


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
