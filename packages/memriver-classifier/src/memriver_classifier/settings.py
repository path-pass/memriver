"""memriver-classifier's settings: the [classifier] table of <root>/settings.toml, and
every default and fixed constant of this package.

The table is read on its own, straight from the file, with no environment layer, the
way memriver dream reads [dream]. A bad value is core's SettingsError naming the field,
never the value; nothing falls back to a default. The Codex provider-override
whitelist lives here, where the table is validated.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from memriver_core.settings import (
    SettingsError,
    reject_boolean,
    settings_file,
    validation_fields,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

__all__ = [
    "CLASSIFIER_TABLE",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_BLOCK_THRESHOLD",
    "DEFAULT_HEADLESS_TIMEOUT_S",
    "DEFAULT_JEV_MODEL",
    "DEFAULT_JEV_TIMEOUT_S",
    "JEV_URL",
    "KILL_GRACE_S",
    "ClassifierSettings",
    "check_classifier_table",
    "check_codex_overrides",
    "load_classifier_settings",
]

CLASSIFIER_TABLE = "classifier"

# the [classifier] table's defaults; backend (and executor_path for claude/codex) has none
DEFAULT_HEADLESS_TIMEOUT_S = 60         # one claude/codex classification
DEFAULT_JEV_TIMEOUT_S = 10              # one jev request
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_BLOCK_THRESHOLD = 0.7           # jev: block when P(plants instructions) >= this

# fixed values
JEV_URL = "https://api.typesafe.ai/v1/systemone"
KILL_GRACE_S = 2                        # draining a timed-out run's pipes after the kill

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

# [classifier.codex_overrides]: a copy of memriver_dream.settings.check_codex_overrides
# and its patterns (this package may not import memriver dream); a fix to one is checked
# against the other
_CODEX_TOP_KEYS = frozenset({"model_provider", "model"})
_CODEX_PROVIDER_KEY_RE = re.compile(
    r"model_providers\.([A-Za-z0-9_-]{1,64})\."
    r"(name|base_url|env_key|wire_api|requires_openai_auth)")
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
        # pydantic only catches ValueError/AssertionError from a validator
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


class ClassifierSettings(BaseModel):
    """The [classifier] table. A plain model, like DreamSettings: one source (the file),
    no environment layer. extra="ignore": a key it does not know is skipped."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True                # false: off, the table kept
    agent_writes: bool = True           # check changed_by "mcp"
    dream_writes: bool = True           # check changed_by "dream"
    backend: Literal["claude", "codex", "jev"]
    # declared after backend: its validator reads the backend already validated
    executor_path: str | None = Field(None, validate_default=True)
    model: str | None = None            # claude/codex
    claude_settings: str | None = None  # passed to claude as --settings
    timeout_s: int | None = Field(None, gt=0)
    codex_overrides: dict[str, str | bool] = Field(default_factory=dict)
    jev_model: str = DEFAULT_JEV_MODEL
    api_key_env: str = DEFAULT_API_KEY_ENV
    block_threshold: float = Field(DEFAULT_BLOCK_THRESHOLD, gt=0, le=1)

    @property
    def timeout(self) -> int:
        """timeout_s, else the backend's default."""
        if self.timeout_s is not None:
            return self.timeout_s
        return DEFAULT_JEV_TIMEOUT_S if self.backend == "jev" else DEFAULT_HEADLESS_TIMEOUT_S

    @field_validator("executor_path")
    @classmethod
    def _executor_path(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            if info.data.get("backend") in ("claude", "codex"):
                raise ValueError("executor_path is required for claude and codex")
            return None
        if not os.path.isabs(value):
            raise ValueError("executor_path must be absolute")
        return value

    @field_validator("claude_settings")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is not None and not os.path.isabs(value):
            raise ValueError("claude_settings must be absolute")
        return value

    @field_validator("model", "jev_model")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        if value is not None and not (value.strip() and value.isprintable()):
            raise ValueError("must be a non-empty single-line string")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, value: str) -> str:
        if not _ENV_NAME_RE.fullmatch(value):
            raise ValueError("api_key_env must name an environment variable")
        return value

    @field_validator("timeout_s", "block_threshold", mode="before")
    @classmethod
    def _no_booleans(cls, value: object) -> object:
        return reject_boolean(value)

    @field_validator("codex_overrides", mode="before")
    @classmethod
    def _codex_whitelist(cls, value: object) -> object:
        return check_codex_overrides(value)


def check_classifier_table(table: Mapping[str, object]) -> ClassifierSettings:
    """A raw [classifier] table validated: its keys matched to fields
    case-insensitively, the first spelling in the table winning (as core and [dream]
    read theirs). Raises ValidationError."""
    fields: dict[str, object] = {}
    for key, value in table.items():
        fields.setdefault(key.lower(), value)
    return ClassifierSettings.model_validate(fields)


def load_classifier_settings(root: Path) -> ClassifierSettings | None:
    """The [classifier] table of <root>/settings.toml; None when there is none.

    An unreadable file, bad TOML or an invalid table raises core's SettingsError:
    its fields carry the "classifier." prefix, never a value, the path or pydantic's
    text.
    """
    path = settings_file(root)          # raises when the file cannot be read
    if path is None:
        return None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # permission denied, bad TOML, bad UTF-8: their text repeats the path
        raise SettingsError(unreadable=True) from None
    table = document.get(CLASSIFIER_TABLE)
    if table is None:
        return None
    if not isinstance(table, dict):
        raise SettingsError((CLASSIFIER_TABLE,))     # `classifier = 5`
    try:
        return check_classifier_table(table)
    except ValidationError as err:
        # from None: the cause echoes the rejected value, which could be a secret
        raise SettingsError(validation_fields(err, prefix=f"{CLASSIFIER_TABLE}.")) from None
