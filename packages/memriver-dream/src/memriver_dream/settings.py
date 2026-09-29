"""memriver-dream's settings: dream's own keys of the [dream] table of
<root>/settings.toml, and every dream default and fixed constant (the rule: each
package keeps its defaults in its one settings module).

The table is read on its own, straight from the file; there is no environment
layer. Only dream's policy lives here: which executor runs, and how, is the caller's
to define. The caller validates the table with a subclass that adds those keys (the
`model` argument of load_dream_settings and check_dream_table), in the same validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from memriver_core.settings import (
    SettingsError,
    reject_boolean,
    settings_file,
    validation_fields,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import InitSettingsSource, TomlConfigSettingsSource

__all__ = [
    "DEFAULT_DREAM_CONTEXT_BUDGET_TOKENS",
    "DEFAULT_DREAM_MAX_CANDIDATES_PER_RUN",
    "DEFAULT_DREAM_MAX_GROUPS_PER_RUN",
    "DEFAULT_DREAM_MAX_SESSIONS_PER_RUN",
    "DEFAULT_DREAM_REPORT_RETENTION_DAYS",
    "DEFAULT_DREAM_TTL_DAYS",
    "DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX",
    "DEFAULT_DREAM_UNCERTAIN_LIMIT",
    "DREAM_CALL_TIMEOUT_S",
    "DREAM_CHUNK_SUMMARY_CHARS",
    "DREAM_DB_FILENAME",
    "DREAM_DIRECTORY",
    "DREAM_INPUT_MARGIN_TOKENS",
    "DREAM_LOCK_FILENAME",
    "DREAM_LOG_FILENAME",
    "DREAM_MAX_CALLS_PER_SESSION",
    "DREAM_MAX_ROOM_HALVINGS",
    "DREAM_OUTPUT_RESERVE_TOKENS",
    "DREAM_REASON_CHARS",
    "DREAM_REPORTS_DIRECTORY",
    "DREAM_TOOL_OUTPUT_CHARS",
    "PROMPT_VERSION",
    "DreamSettings",
    "check_dream_table",
    "load_dream_settings",
]

DREAM_TABLE = "dream"

# dream's keys of the [dream] table (spec §7)
DEFAULT_DREAM_TTL_DAYS = 30
DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX = 3
DEFAULT_DREAM_UNCERTAIN_LIMIT = 2
DEFAULT_DREAM_REPORT_RETENTION_DAYS = 30    # report files and their runs only
DEFAULT_DREAM_MAX_SESSIONS_PER_RUN = 20
DEFAULT_DREAM_MAX_GROUPS_PER_RUN = 20
DEFAULT_DREAM_MAX_CANDIDATES_PER_RUN = 30
DEFAULT_DREAM_CONTEXT_BUDGET_TOKENS = 200_000   # one executor call, input and output

# fixed values (spec §10): initial values, revisited after a real-transcript run
DREAM_OUTPUT_RESERVE_TOKENS = 4_000     # kept free for the answer
# the estimate is rough and sees neither the schema, the harness's own prompt nor
# a global AGENTS.md: this much input room is kept unused on top of the reserve
DREAM_INPUT_MARGIN_TOKENS = 16_000
DREAM_CHUNK_SUMMARY_CHARS = 1_500       # one partial summary of a long session
DREAM_TOOL_OUTPUT_CHARS = 2_000         # one tool output as a transcript record
DREAM_MAX_CALLS_PER_SESSION = 12        # map and reduce calls for one session
DREAM_MAX_ROOM_HALVINGS = 3             # a session's input room, after "too-large" answers
DREAM_CALL_TIMEOUT_S = 300              # one executor call
DREAM_REASON_CHARS = 300                # one change or review reason, as stored
DREAM_DIRECTORY = "dream"               # <root>/dream: lock, dream.db, reports, run log
DREAM_LOCK_FILENAME = ".lock"
DREAM_LOG_FILENAME = "dream.log"
DREAM_DB_FILENAME = "dream.db"          # dream's own records (runs, reviews, passes)
DREAM_REPORTS_DIRECTORY = "reports"     # <root>/dream/reports/<run_id>.txt
PROMPT_VERSION = "dream-4"              # in every input digest; bump it when a prompt changes


class DreamSettings(BaseModel):
    """Dream's own keys of the [dream] table: what the run reads.

    A plain model, not a BaseSettings: the table has one source (the file) and no
    environment layer, and BaseSettings' own constructor would take a table key
    such as `_secrets_dir` or `_cli_parse_args` as one of its options.
    TomlConfigSettingsSource needs only the fields and the config, which a model
    has. extra="ignore", like core: a key it does not know -- the caller's among
    them -- is skipped. A caller that owns more keys of the table subclasses it.
    """

    model_config = ConfigDict(extra="ignore")

    ttl_days: int = Field(DEFAULT_DREAM_TTL_DAYS, gt=0)
    ttl_read_multiplier_max: int = Field(DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX, gt=0)
    uncertain_limit: int = Field(DEFAULT_DREAM_UNCERTAIN_LIMIT, gt=0)
    report_retention_days: int = Field(DEFAULT_DREAM_REPORT_RETENTION_DAYS, gt=0)
    max_sessions_per_run: int = Field(DEFAULT_DREAM_MAX_SESSIONS_PER_RUN, gt=0)
    max_groups_per_run: int = Field(DEFAULT_DREAM_MAX_GROUPS_PER_RUN, gt=0)
    max_candidates_per_run: int = Field(DEFAULT_DREAM_MAX_CANDIDATES_PER_RUN, gt=0)
    # one executor call's tokens, input and output; must leave room after the answer's
    # reserve and the estimate's margin
    context_budget_tokens: int = Field(
        DEFAULT_DREAM_CONTEXT_BUDGET_TOKENS,
        gt=DREAM_OUTPUT_RESERVE_TOKENS + DREAM_INPUT_MARGIN_TOKENS)

    @field_validator("ttl_days", "ttl_read_multiplier_max", "uncertain_limit",
                     "report_retention_days", "max_sessions_per_run", "max_groups_per_run",
                     "max_candidates_per_run", "context_budget_tokens", mode="before")
    @classmethod
    def _no_booleans(cls, value: object) -> object:
        return reject_boolean(value)


def check_dream_table(table: Mapping[str, object], *,
                      model: type[DreamSettings] = DreamSettings) -> DreamSettings:
    """A raw [dream] table validated exactly as load_dream_settings reads it: keys are
    matched to fields case-insensitively, the first spelling in the table winning.
    Raises ValidationError. memriver dream init checks the table as it will stand
    with this, so it never accepts a table the run then refuses. `model` is the class
    it is validated with: a subclass may add keys and validators, which then run in
    the same pass as every field's own."""
    # typed for a BaseSettings, but reads only model_fields and model_config
    fields = InitSettingsSource(model, dict(table))()  # type: ignore[arg-type]
    return model.model_validate(fields)


def load_dream_settings(root: Path, *,
                        model: type[DreamSettings] = DreamSettings) -> DreamSettings | None:
    """The [dream] table of <root>/settings.toml, validated with `model` (see
    check_dream_table); None when there is none.

    An unreadable file, bad TOML or an invalid table raises core's SettingsError:
    dream never runs on a guess. Its fields carry the "dream." prefix, never a
    value, the path or pydantic's text.
    """
    path = settings_file(root)      # raises when the file cannot be read
    if path is None:
        return None
    try:
        table = TomlConfigSettingsSource(model,  # type: ignore[arg-type]
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
        return check_dream_table(table, model=model)
    except ValidationError as err:
        # from None: the cause echoes the rejected value, which could be a secret
        raise SettingsError(validation_fields(err, prefix=f"{DREAM_TABLE}.")) from None
