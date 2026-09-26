"""The [dream] table: load_dream_settings, the Codex override whitelist, and the
dream constants living in memriver_dream.settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from memriver_core.settings import SettingsError, load_settings
from memriver_dream import settings as dream_settings
from memriver_dream.settings import DreamSettings, load_dream_settings
from pydantic import ValidationError

DREAM_TABLE = '[dream]\nexecutor = "codex"\nexecutor_path = "/opt/bin/codex"\n'
PASTED = "sk-" + "q" * 24                  # what a pasted credential could look like


@pytest.fixture(autouse=True)
def _clear_memriver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in os.environ if k.startswith("MEMRIVER_")]:
        monkeypatch.delenv(key, raising=False)


def _root(tmp_path, text: str | None = None) -> Path:
    root = tmp_path / "mem"
    root.mkdir()
    if text is not None:
        (root / "settings.toml").write_text(text, encoding="utf-8")
    return root


def test_no_settings_file_means_no_dream_settings(tmp_path):
    assert load_dream_settings(_root(tmp_path)) is None


def test_no_dream_table_means_no_dream_settings(tmp_path):
    assert load_dream_settings(_root(tmp_path, "max_body_chars = 42\n")) is None


def test_a_dream_table_is_read_with_its_defaults(tmp_path):
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE + "ttl_days = 30\n"))
    assert (dream.executor, dream.executor_path, dream.ttl_days) == ("codex", "/opt/bin/codex", 30)
    assert (dream.ttl_read_multiplier_max, dream.uncertain_limit, dream.idle_minutes,
            dream.schedule_at, dream.max_sessions_per_run, dream.max_groups_per_run,
            dream.max_candidates_per_run) == (5, 2, 60, "04:00", 20, 20, 30)


def test_an_unknown_key_in_the_dream_table_is_ignored(tmp_path):
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE + "unknown_key = 1\n"))
    assert dream.executor == "codex" and not hasattr(dream, "unknown_key")


def test_a_table_key_never_reaches_the_settings_constructor_options(tmp_path, monkeypatch):
    # a [dream] key spelled like a BaseSettings constructor option is just an unknown key
    monkeypatch.setattr("sys.argv", ["memriver", "--executor", "claude"])
    text = DREAM_TABLE + "_cli_parse_args = true\n_secrets_dir = \"/nowhere\"\n"
    assert load_dream_settings(_root(tmp_path, text)).executor == "codex"


def test_the_dream_table_has_no_environment_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR", "claude")
    monkeypatch.setenv("TTL_DAYS", "7")
    dream = load_dream_settings(_root(tmp_path, DREAM_TABLE))
    assert (dream.executor, dream.ttl_days) == ("codex", 90)
    direct = DreamSettings(executor="codex", executor_path="/opt/bin/codex")
    assert (direct.executor, direct.ttl_days) == ("codex", 90)


@pytest.mark.parametrize(("table", "field"), [
    ('[dream]\nexecutor = "gpt"\nexecutor_path = "/opt/bin/codex"\n', "dream.executor"),
    ('[dream]\nexecutor = "codex"\nexecutor_path = "relative/codex"\n', "dream.executor_path"),
    (DREAM_TABLE + "ttl_days = 0\n", "dream.ttl_days"),
    (DREAM_TABLE + "ttl_days = true\n", "dream.ttl_days"),
    (DREAM_TABLE + 'schedule_at = "25:00"\n', "dream.schedule_at"),
    (DREAM_TABLE + f'[dream.codex_overrides]\n"features.hooks" = "{PASTED}"\n',
     "dream.codex_overrides"),
    ('[dream]\nexecutor_path = "/opt/bin/codex"\n', "dream.executor"),
    ("dream = 5\n", "dream"),
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


CODEX_PROVIDER = (
    '[dream.codex_overrides]\n"model_provider" = "foundry"\n"model" = "deployment-a"\n'
    '"model_providers.foundry.name" = "Foundry"\n'
    '"model_providers.foundry.base_url" = "https://example.invalid/openai/v1"\n'
    '"model_providers.foundry.env_key" = "FOUNDRY_API_KEY"\n'
    '"model_providers.foundry.wire_api" = "responses"\n'
    '"model_providers.foundry.requires_openai_auth" = false\n')


def test_codex_overrides_default_to_empty(tmp_path):
    assert load_dream_settings(_root(tmp_path, DREAM_TABLE)).codex_overrides == {}


def test_a_whitelisted_codex_provider_is_read_with_its_types(tmp_path):
    overrides = load_dream_settings(_root(tmp_path, DREAM_TABLE + CODEX_PROVIDER)).codex_overrides
    assert (overrides["model_provider"], overrides["model_providers.foundry.env_key"]) == (
        "foundry", "FOUNDRY_API_KEY")
    assert overrides["model_providers.foundry.requires_openai_auth"] is False
    assert len(overrides) == 7


@pytest.mark.parametrize("overrides", [
    {"features.hooks": False},
    {"mcp_servers.sentinel.command": "sh"},
    {"web_search": "live"},
    {"model_instructions_file": "/tmp/other.md"},
    {"project_doc_max_bytes": "1000"},
    {"model_providers": {"foundry": {"name": "Foundry"}}},            # a whole table
    {"model_provider": "foundry", "model_providers.foundry": {"name": "Foundry"}},
    {"model_provider": "foundry", "model_providers.foundry.name.extra": "x"},  # prefix only
    {"model_provider": "foundry", "model_providers.foundry.experimental_bearer_token": PASTED},
    {"model_provider": "foundry", "model_providers.foundry.http_headers.api-key": PASTED},
    {"model_provider": "foundry", "model_providers.foundry.query_params.key": PASTED},
    {"model": True},
    {"model": "two\nlines"},
    {"model": " "},
    {"model_provider": "foundry", "model_providers.foundry.env_key": PASTED},
    {"model_provider": "foundry", "model_providers.foundry.requires_openai_auth": "false"},
    {"model_provider": "foundry", "model_providers.foundry.wire_api": "chat"},
    {"model_provider": "foundry",
     "model_providers.foundry.base_url": f"https://u:{PASTED}@example.invalid/v1"},
    {"model_provider": "foundry",
     "model_providers.foundry.base_url": f"https://example.invalid/v1?key={PASTED}"},
    {"model_provider": "foundry", "model_providers.foundry.base_url": "https://example.invalid/#x"},
    {"model_provider": "foundry", "model_providers.foundry.base_url": "file:///etc/hosts"},
    {"model_provider": "a", "model_providers.b.name": "B"},           # not the selected one
    {"model_provider": "a", "model_providers.a.name": "A", "model_providers.b.name": "B"},
    {"model_providers.a.name": "A"},                                  # nothing selects it
    ["model", "x"],
])
def test_codex_overrides_outside_the_whitelist_are_refused_without_echoing_values(
        overrides):
    with pytest.raises(ValidationError) as caught:
        DreamSettings(executor="codex", executor_path="/opt/bin/codex",
                      codex_overrides=overrides)
    reasons = [str(error["ctx"]["error"]) for error in caught.value.errors()
               if error["type"] == "value_error"]
    assert reasons and all(reason.startswith("codex_overrides") for reason in reasons)
    assert not any(PASTED in reason for reason in reasons)


DREAM_CONSTANTS = {
    "DREAM_CONTEXT_BUDGET_TOKENS": 100_000, "DREAM_OUTPUT_RESERVE_TOKENS": 4_000,
    "DREAM_INPUT_MARGIN_TOKENS": 16_000, "DREAM_CHUNK_SUMMARY_CHARS": 1_500,
    "DREAM_TOOL_OUTPUT_CHARS": 2_000, "DREAM_MAX_CALLS_PER_SESSION": 12,
    "DREAM_CALL_TIMEOUT_S": 300, "DREAM_MAX_QUARANTINE_PER_RUN": 1_000,
    "DREAM_MAX_ROOM_HALVINGS": 3, "DREAM_REASON_CHARS": 300,
    "DREAM_DIRECTORY": "dream", "DREAM_LOCK_FILENAME": ".lock",
    "DREAM_LOG_FILENAME": "dream.log",
    "DREAM_LAUNCH_AGENT_LABEL": "io.github.path-pass.memriver.dream",
}


def test_the_dream_constants_live_in_dream_settings():
    assert {name: getattr(dream_settings, name) for name in DREAM_CONSTANTS} == DREAM_CONSTANTS
    assert set(DREAM_CONSTANTS) <= set(dream_settings.__all__)


def test_the_dream_defaults_back_the_table_fields():
    fields = DreamSettings.model_fields
    assert (dream_settings.DEFAULT_DREAM_TTL_DAYS, dream_settings.DEFAULT_DREAM_SCHEDULE_AT) == (
        fields["ttl_days"].default, fields["schedule_at"].default)
