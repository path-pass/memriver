"""memriver's settings: every default and fixed value of the memriver package, and the
executor keys its tables share -- the Codex provider-override whitelist among them.

A table is read on its own, straight from settings.toml, with no environment layer; a
bad value is core's SettingsError naming the field, never the value. The keys of
[dream] that memriver-dream owns are memriver_dream's: memriver adds its own to them
where it reads [dream]. This module imports no memriver_dream.
"""

from __future__ import annotations

import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

__all__ = [
    "CLASSIFIER_SCRATCH_PREFIX",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_DREAM_SCHEDULE_AT",
    "DEFAULT_JEV_MODEL",
    "DREAM_LAUNCH_AGENT_LABEL",
    "DREAM_SCRATCH_PREFIX",
    "JEV_BASE_URL",
    "KILL_GRACE_S",
    "SCHEDULE_AT_RE",
    "ExecutorSettings",
    "check_codex_overrides",
]

# the executors (memriver.executor)
KILL_GRACE_S = 2                        # draining a timed-out run's pipes after the kill
DREAM_SCRATCH_PREFIX = "memriver-dream-"            # a dream run's temporary directories
CLASSIFIER_SCRATCH_PREFIX = "memriver-classifier-"  # a classification's
JEV_BASE_URL = "https://api.typesafe.ai"            # fixed: never read from the environment
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_API_KEY_ENV = "TYPESAFE_API_KEY"            # jev: the variable the key is read from

# [dream]: the keys memriver adds to memriver-dream's own
DEFAULT_DREAM_SCHEDULE_AT = "04:00"
DREAM_LAUNCH_AGENT_LABEL = "io.github.path-pass.memriver.dream"
SCHEDULE_AT_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
# [dream.codex_overrides], [classifier.codex_overrides]: the Codex provider keys a user
# may set, each matched by its whole path; everything else -- tools, hooks, MCP
# servers, whole tables, credential fields -- is refused
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
        # a TypeError here would escape a pydantic field_validator uncaught: pydantic
        # only catches ValueError/AssertionError from a validator, never TypeError
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


class ExecutorSettings(BaseModel):
    """The keys that pick and configure an executor, one set for [dream] and
    [classifier]: each table's model adds its own keys to these, so one validation names
    every bad key of the table, the Codex whitelist included. A plain model: one source
    (the file), no environment layer; extra="ignore": a key it does not know is skipped.
    Field order is the order an error line names the fields in.
    """

    model_config = ConfigDict(extra="ignore")

    executor: Literal["claude", "codex", "jev"]
    # declared after executor: its validator reads the executor already validated
    executor_path: str | None = Field(None, validate_default=True)
    # claude --model, codex -c model=, the jev model (jev-latest when unset)
    model: str | None = None
    # passed to claude as --settings, reloaded whole into the call (hooks and
    # environment included): authentication only, never a hook
    claude_settings: str | None = None
    # provider settings the Codex executor passes as -c overrides: it skips
    # config.toml, so a provider defined only there is given here
    codex_overrides: dict[str, str | bool] = Field(default_factory=dict)
    api_key_env: str = DEFAULT_API_KEY_ENV      # jev: the variable's name, never the key

    @field_validator("executor_path")
    @classmethod
    def _executor_path(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            if info.data.get("executor") in ("claude", "codex"):
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

    @field_validator("model")
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

    @field_validator("codex_overrides", mode="before")
    @classmethod
    def _codex_whitelist(cls, value: object) -> object:
        return check_codex_overrides(value)
