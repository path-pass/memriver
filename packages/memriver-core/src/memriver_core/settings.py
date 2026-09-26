"""memriver-core's settings: the top level of <root>/settings.toml, the
MEMRIVER_* environment, and the fixed constants core injects through bootstrap.

Settings precedence: CLI override > env > <root>/settings.toml > defaults.
Other packages read their own table of the same file (memriver-dream reads
[dream]); core ignores every key and table it does not own.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path

from pydantic import Field, ValidationError, ValidationInfo, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

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
    "SESSION_SUMMARY_MAX_CHARS",
    "SETTINGS_FILENAME",
    "STOP_NUDGE_INTERVAL_PROMPTS",
    "STOP_NUDGE_MIN_PROMPTS",
    "TOOL_CALL_RETENTION_S",
    "Settings",
    "SettingsError",
    "load_settings",
    "reject_boolean",
    "settings_file",
    "storage_root",
    "validation_fields",
]

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
# other core default (the rule: each package keeps its defaults in its one
# settings module, never in feature modules). bootstrap injects them: models,
# application and repository never import settings.
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
SESSION_SUMMARY_MAX_CHARS = 1_200       # one stored session summary

# the settings file load_settings is building from, or None: a direct
# Settings(...) construction reads no file. A ContextVar rather than a class
# attribute so two threads (or tasks) loading different roots never see each
# other's file.
_settings_file: ContextVar[Path | None] = ContextVar("_settings_file", default=None)


class SettingsError(Exception):
    """settings.toml or a MEMRIVER_* variable cannot be used; nothing was loaded.

    Fields only, like the storage errors: `fields` names the settings.toml fields
    that failed validation, `env_fields` the fields whose MEMRIVER_* variable did,
    and `unreadable` says the file could not be read at all. Its str() is one line
    safe to print -- never the path, the rejected value, or pydantic's own text,
    which echoes the value back. Both sources failing are reported env first.
    """

    def __init__(self, fields: tuple[str, ...] = (), env_fields: tuple[str, ...] = (), *,
                 unreadable: bool = False) -> None:
        parts = []
        if unreadable:
            parts.append(f"{SETTINGS_FILENAME} could not be read")
        if env_fields:
            names = ", ".join(f"{ENV_PREFIX}{name.upper()}" for name in env_fields)
            parts.append(f"environment variable {names} is invalid")
        if fields:
            parts.append(f"{SETTINGS_FILENAME} is invalid: field {', '.join(fields)}")
        # a validation error that names no field still is one, never "could not be read"
        super().__init__("; ".join(parts) or "the settings are invalid")
        self.fields = fields
        self.env_fields = env_fields
        self.unreadable = unreadable


def validation_fields(error: ValidationError, prefix: str = "") -> tuple[str, ...]:
    """The top-level field names a ValidationError names, in order, once each.

    Only the first location element: a field name the model itself declares,
    never a user-written key or value.
    """
    return tuple(dict.fromkeys(f"{prefix}{item['loc'][0]}" for item in error.errors()
                               if item["loc"]))


def settings_file(root: Path) -> Path | None:
    """<root>/settings.toml when it is a regular file to read; None only when the
    path is genuinely absent. Anything else -- a directory, a FIFO, a link that
    dangles or loops, a root that cannot be searched -- raises SettingsError
    ("could not be read"): an unreadable file never means "no settings".

    The type is checked with stat before anything opens the path, so a FIFO never
    blocks; `Path.is_file()` is not used because it turns a stat failure (on some
    Python versions even EACCES) into a quiet False.
    """
    path = Path(root) / SETTINGS_FILENAME
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        try:
            path.lstat()          # a link whose target is missing is broken, not absent
        except FileNotFoundError:
            return None
        except OSError:
            pass
        raise SettingsError(unreadable=True) from None
    except OSError:               # ELOOP, EACCES, EIO, ENOTDIR, ...
        raise SettingsError(unreadable=True) from None
    if not stat.S_ISREG(mode):
        raise SettingsError(unreadable=True) from None
    return path


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


def reject_boolean(value: object) -> object:
    """Reject a TOML boolean where an integer is expected.

    pydantic's lax mode reads True as 1 and gt=0 lets it through, so
    'search_limit_max = true' would silently cap every search at one hit.
    TOML has a real boolean type, so this is a plausible typo. Public so
    memriver-dream's settings reuse it and the integer validators stay identical.
    """
    if isinstance(value, bool):
        raise ValueError("expected an integer, got a boolean")  # noqa: TRY004
    return value


class Settings(BaseSettings):
    """Behaviour knobs, read from MEMRIVER_* env vars and <root>/settings.toml.

    Every default reproduces the behaviour memriver had before the settings
    existed, so an unconfigured install is unchanged. A direct construction
    reads init arguments, the environment and defaults; only load_settings adds
    the settings file.
    """

    # extra="ignore": a key or table core does not own (another package's
    # [dream], a key from a newer version) is skipped silently, in the file and
    # in the environment alike -- so MEMRIVER_DREAM is never a config entry
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="ignore")

    # storage_root() also honours MEMRIVER_ROOT, so this default agrees with the
    # env layer above it; calling it lazily keeps Path.home() out of import time
    root: Path = Field(default_factory=storage_root)
    # every knob is a count or a budget: zero and negative values are never
    # meaningful, and gt=0 turns them into a startup error instead of a server
    # that silently answers nothing
    max_body_chars: int = Field(DEFAULT_MAX_BODY_CHARS, gt=0)
    # the max before the default: the default's validator reads the validated max
    search_limit_max: int = Field(DEFAULT_SEARCH_LIMIT_MAX, gt=0)
    search_limit_default: int = Field(DEFAULT_SEARCH_LIMIT, gt=0)
    index_budget_lines: int = Field(DEFAULT_BUDGET_LINES, gt=0)
    # unset keeps every memory_reads row; a number of days prunes older rows
    # whenever a new one is written
    memory_reads_retention_days: int | None = Field(None, gt=0)

    @classmethod
    def settings_customise_sources(
            cls, settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # pydantic-settings' own order, with the file last: init > env > file > defaults
        sources = (init_settings, env_settings, dotenv_settings, file_secret_settings)
        path = _settings_file.get()
        if path is None:
            return sources
        # the top level only: the table header () reads every top-level key, and
        # extra="ignore" drops the ones that are not fields
        return (*sources, TomlConfigSettingsSource(settings_cls, toml_file=path))

    @field_validator("max_body_chars", "search_limit_default", "search_limit_max",
                     "index_budget_lines", "memory_reads_retention_days", mode="before")
    @classmethod
    def _no_booleans(cls, value: object) -> object:
        return reject_boolean(value)

    @field_validator("search_limit_default")
    @classmethod
    def _default_within_max(cls, value: int, info: ValidationInfo) -> int:
        # a default above the max would make the untouched, common case (no
        # explicit limit on a search call) silently ask for more hits than
        # the store is configured to ever return. A field validator, not a model
        # one, so the error names the field a user would lower
        maximum = info.data.get("search_limit_max")
        if maximum is not None and value > maximum:
            raise ValueError(f"search_limit_default ({value}) must not exceed "
                             f"search_limit_max ({maximum})")
        return value


def _env_set(name: str) -> bool:
    # pydantic-settings matches env names case-insensitively by default
    return f"{ENV_PREFIX}{name}".upper() in {key.upper() for key in os.environ}


def _attribute(error: ValidationError, path: Path | None) -> SettingsError:
    """Each failing field blamed on the source that set it: its MEMRIVER_* variable,
    else settings.toml when the file holds it, else the environment. A default
    that only fails against the max (the cross-field rule) is blamed on whichever
    source set search_limit_max, by that name."""
    try:
        in_file = set(TomlConfigSettingsSource(Settings, toml_file=path)()) if path else set()
    except (OSError, ValueError):     # changed since it was read: blame no file field
        in_file = set()
    fields: list[str] = []
    env_fields: list[str] = []
    for name in validation_fields(error):
        if not _env_set(name) and name not in in_file and name == "search_limit_default":
            name = "search_limit_max"
        if _env_set(name) or name not in in_file:
            env_fields.append(name)
        else:
            fields.append(name)
    return SettingsError(tuple(dict.fromkeys(fields)), tuple(dict.fromkeys(env_fields)))


def load_settings(root_override: Path | None = None) -> Settings:
    """CLI override > env > <root>/settings.toml > defaults, or SettingsError.

    The root is resolved first (override, env, default) so the settings file
    can live inside the store it configures; it is passed as an init argument,
    so a `root` key in the file can never move it. The environment and the file
    are validated together, once: a field valid only once both combine (env
    raises a max, the file lowers a default under it) is accepted.

    An unreadable file, bad TOML or an invalid value raises SettingsError --
    nothing falls back to the defaults, so a typo is never silently ignored.
    """
    root = Path(root_override) if root_override is not None else storage_root()
    path = settings_file(root)     # None: no file; raises when it cannot be read
    token = _settings_file.set(path)
    try:
        return Settings(root=root)
    except ValidationError as err:
        # from None: the cause echoes the rejected value, which could be a secret
        raise _attribute(err, path) from None
    except (OSError, ValueError):
        # OSError: permission denied and the like; ValueError: TOMLDecodeError
        # and UnicodeDecodeError. Their text repeats the path, so it is dropped.
        raise SettingsError(unreadable=True) from None
    finally:
        _settings_file.reset(token)
