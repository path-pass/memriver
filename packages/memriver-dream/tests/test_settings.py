"""The [dream] table as memriver-dream reads it: load_dream_settings and
check_dream_table, dream's own policy keys, the model a caller validates the table
with, and the dream constants living in memriver_dream.settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from memriver_core.settings import SettingsError, load_settings, validation_fields
from memriver_dream import settings as dream_settings
from memriver_dream.settings import (
    DreamSettings,
    check_dream_table,
    load_dream_settings,
)
from pydantic import ValidationError, field_validator

# the executor keys are the caller's: to dream they are keys it does not know, ignored
DREAM_TABLE = '[dream]\nexecutor = "codex"\nexecutor_path = "/opt/bin/codex"\n'
PASTED = "sk-" + "q" * 24                  # what a pasted credential could look like


@pytest.fixture(autouse=True)
def _clear_memriver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in os.environ if k.startswith("MEMRIVER_")]:
        monkeypatch.delenv(key, raising=False)


def _root(tmp_path, text: str | None = None) -> Path:
    root = tmp_path / "mem"
    root.mkdir(parents=True)
    if text is not None:
        (root / "settings.toml").write_text(text, encoding="utf-8")
    return root


def test_no_settings_file_means_no_dream_settings(tmp_path):
    assert load_dream_settings(_root(tmp_path)) is None


def test_no_dream_table_means_no_dream_settings(tmp_path):
    assert load_dream_settings(_root(tmp_path, "max_body_chars = 42\n")) is None


def test_a_dream_table_is_read_with_its_defaults(tmp_path):
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE + "ttl_days = 7\n"))
    assert (dream.ttl_days, dream.ttl_read_multiplier_max, dream.uncertain_limit,
            dream.report_retention_days, dream.max_sessions_per_run, dream.max_groups_per_run,
            dream.max_candidates_per_run) == (7, 3, 2, 30, 20, 20, 30)


def test_the_executor_keys_and_the_schedule_are_not_dreams(tmp_path):
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE + 'schedule_at = "04:00"\n'
                                                              'model = "m"\n'))
    for key in ("executor", "executor_path", "model", "claude_settings", "codex_overrides",
                "api_key_env", "schedule_at"):
        assert key not in DreamSettings.model_fields and not hasattr(dream, key), key


def test_an_unknown_key_in_the_dream_table_is_ignored(tmp_path):
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE + "unknown_key = 1\n"))
    assert dream.ttl_days == 30 and not hasattr(dream, "unknown_key")


def test_a_table_key_never_reaches_the_settings_constructor_options(tmp_path, monkeypatch):
    # a [dream] key spelled like a BaseSettings constructor option is just an unknown key
    monkeypatch.setattr("sys.argv", ["memriver", "--ttl-days", "7"])
    text = DREAM_TABLE + "_cli_parse_args = true\n_secrets_dir = \"/nowhere\"\n"
    assert load_dream_settings(_root(tmp_path, text)).ttl_days == 30


def test_idle_minutes_is_no_longer_a_setting(tmp_path):
    # a table written for the old run still loads: the key is just unknown now
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE + "idle_minutes = 5\n"))
    assert "idle_minutes" not in DreamSettings.model_fields
    assert not hasattr(dream, "idle_minutes")


def test_the_dream_table_has_no_environment_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("TTL_DAYS", "7")
    monkeypatch.setenv("UNCERTAIN_LIMIT", "9")
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE))
    assert (dream.ttl_days, dream.uncertain_limit) == (30, 2)
    direct = DreamSettings()
    assert (direct.ttl_days, direct.uncertain_limit) == (30, 2)


@pytest.mark.parametrize(("table", "field"), [
    (DREAM_TABLE + "ttl_days = 0\n", "dream.ttl_days"),
    (DREAM_TABLE + "ttl_days = true\n", "dream.ttl_days"),
    (DREAM_TABLE + "report_retention_days = 0\n", "dream.report_retention_days"),
    (DREAM_TABLE + "report_retention_days = true\n", "dream.report_retention_days"),
    (DREAM_TABLE + f'uncertain_limit = "{PASTED}"\n', "dream.uncertain_limit"),
    ("dream = 5\n", "dream"),
    (DREAM_TABLE + "context_budget_tokens = 20000\n", "dream.context_budget_tokens"),
    (DREAM_TABLE + "context_budget_tokens = true\n", "dream.context_budget_tokens"),
])
def test_an_invalid_dream_table_raises_naming_only_the_file_and_the_field(tmp_path, table,
                                                                          field):
    root = _root(tmp_path, "max_body_chars = 42\n" + table)
    with pytest.raises(SettingsError) as caught:
        load_dream_settings(root)
    assert (caught.value.fields, caught.value.env_fields) == ((field,), ())
    assert str(caught.value) == f"settings.toml is invalid: field {field}"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert PASTED not in str(caught.value) and str(root) not in str(caught.value)
    # core's own settings are untouched by a broken [dream] table
    assert load_settings(root_override=root).max_body_chars == 42


@pytest.mark.parametrize("text", ["this is not = = valid toml\n", b"a = '\xff'\n"])
def test_an_unreadable_settings_file_raises_could_not_be_read(tmp_path, text):
    root = _root(tmp_path)
    path = root / "settings.toml"
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    with pytest.raises(SettingsError, match=r"^settings\.toml could not be read$"):
        load_dream_settings(root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_permission_denied_settings_file_never_names_the_path(tmp_path):
    root = _root(tmp_path, DREAM_TABLE)
    (root / "settings.toml").chmod(0o000)
    try:
        with pytest.raises(SettingsError) as caught:
            load_dream_settings(root)
    finally:
        (root / "settings.toml").chmod(0o600)
    assert str(caught.value) == "settings.toml could not be read"


@pytest.mark.parametrize("shape", ["directory", "fifo", "symlink-loop", "dangling-symlink"])
def test_a_settings_path_that_is_not_a_readable_file_is_an_error(tmp_path, shape):
    root = _root(tmp_path)
    path = root / "settings.toml"
    if shape == "directory":
        path.mkdir()
    elif shape == "fifo":
        os.mkfifo(path)
    elif shape == "symlink-loop":
        path.symlink_to(path)
    else:
        path.symlink_to(root / "nowhere.toml")
    with pytest.raises(SettingsError, match=r"^settings\.toml could not be read$"):
        load_dream_settings(root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_root_that_cannot_be_searched_is_an_error_not_no_table(tmp_path):
    root = _root(tmp_path, DREAM_TABLE)
    root.chmod(0o000)
    try:
        with pytest.raises(SettingsError, match="could not be read"):
            load_dream_settings(root)
    finally:
        root.chmod(0o700)


def test_table_keys_match_fields_in_any_case(tmp_path):
    text = "[dream]\nTTL_DAYS = 7\nReport_Retention_Days = 9\n"
    dream = load_dream_settings(_root(tmp_path, text))
    assert (dream.ttl_days, dream.report_retention_days) == (7, 9)


class _Caller(DreamSettings):
    """A caller's own [dream] table: one more key, with a validator of its own."""

    executor: str = "unset"

    @field_validator("executor")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in ("unset", "a", "b"):
            raise ValueError("executor must be a or b")
        return value


def test_a_caller_model_validates_the_table_in_the_same_pass(tmp_path):
    text = '[dream]\nreport_retention_days = 0\nEXECUTOR = "c"\n'
    with pytest.raises(SettingsError) as caught:
        load_dream_settings(_root(tmp_path, text), model=_Caller)
    # one line, every bad key of the table, in the model's field order
    assert caught.value.fields == ("dream.report_retention_days", "dream.executor")
    with pytest.raises(ValidationError) as caught:
        check_dream_table({"Executor": "c", "ttl_days": 0}, model=_Caller)
    assert validation_fields(caught.value) == ("ttl_days", "executor")
    good = load_dream_settings(_root(tmp_path / "b", '[dream]\nexecutor = "a"\n'), model=_Caller)
    assert type(good) is _Caller and good.executor == "a"
    # the default model: the caller's key is just an unknown key
    plain = load_dream_settings(_root(tmp_path / "c", '[dream]\nEXECUTOR = "c"\n'))
    assert type(plain) is DreamSettings and not hasattr(plain, "executor")


DREAM_CONSTANTS = {
    "DEFAULT_DREAM_CONTEXT_BUDGET_TOKENS": 200_000, "DREAM_OUTPUT_RESERVE_TOKENS": 4_000,
    "DREAM_INPUT_MARGIN_TOKENS": 16_000, "DREAM_CHUNK_SUMMARY_CHARS": 1_500,
    "DREAM_TOOL_OUTPUT_CHARS": 2_000, "DREAM_MAX_CALLS_PER_SESSION": 12,
    "DREAM_CALL_TIMEOUT_S": 300, "DREAM_MAX_ROOM_HALVINGS": 3, "DREAM_REASON_CHARS": 300,
    "DREAM_DIRECTORY": "dream", "DREAM_LOCK_FILENAME": ".lock",
    "DREAM_LOG_FILENAME": "dream.log", "DREAM_DB_FILENAME": "dream.db",
    "DREAM_REPORTS_DIRECTORY": "reports", "PROMPT_VERSION": "dream-4",
}
# gone from dream: the executor's keys, the schedule, the kill grace and the hints are
# the caller's
REMOVED = ("DEFAULT_DREAM_IDLE_MINUTES", "DREAM_MAX_QUARANTINE_PER_RUN",
           "DREAM_CONTEXT_BUDGET_TOKENS", "DREAM_KILL_GRACE_S", "DEFAULT_DREAM_SCHEDULE_AT",
           "DREAM_LAUNCH_AGENT_LABEL", "DREAM_FAILURE_HINTS", "check_codex_overrides",
           "_CODEX_TOP_KEYS", "_CODEX_PROVIDER_KEY_RE", "_plain_url", "_SCHEDULE_AT_RE")


def test_the_dream_constants_live_in_dream_settings():
    assert {name: getattr(dream_settings, name) for name in DREAM_CONSTANTS} == DREAM_CONSTANTS
    assert set(DREAM_CONSTANTS) <= set(dream_settings.__all__)
    assert not [name for name in REMOVED
                if hasattr(dream_settings, name) or name in dream_settings.__all__]


def test_the_dream_defaults_back_the_table_fields():
    fields = DreamSettings.model_fields
    defaults = (dream_settings.DEFAULT_DREAM_TTL_DAYS,
                dream_settings.DEFAULT_DREAM_TTL_READ_MULTIPLIER_MAX,
                dream_settings.DEFAULT_DREAM_REPORT_RETENTION_DAYS)
    assert defaults == (fields["ttl_days"].default, fields["ttl_read_multiplier_max"].default,
                        fields["report_retention_days"].default)
    assert defaults == (30, 3, 30)


def test_the_context_budget_defaults_to_200k_and_must_exceed_the_reserve(tmp_path):
    assert load_dream_settings(_root(tmp_path, DREAM_TABLE)).context_budget_tokens == 200_000
    assert DreamSettings.model_fields["context_budget_tokens"].default == \
        dream_settings.DEFAULT_DREAM_CONTEXT_BUDGET_TOKENS
    table = load_dream_settings(_root(tmp_path / "b", DREAM_TABLE
                                      + "context_budget_tokens = 20001\n"))
    assert table.context_budget_tokens == 20_001
