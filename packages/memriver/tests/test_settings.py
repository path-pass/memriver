"""memriver.settings: memriver's defaults and fixed values, the executor keys [dream]
and [classifier] share, and the Codex provider-override whitelist -- the exact line of
every refusal, naming the key and a fixed reason, never the value, which could be a
pasted secret."""

from __future__ import annotations

from pathlib import Path

import pytest
from memriver import settings as memriver_settings
from memriver.settings import (
    ClassifierSettings,
    ExecutorSettings,
    check_classifier_table,
    check_codex_overrides,
    load_classifier_settings,
)
from memriver_core.settings import SettingsError, validation_fields
from pydantic import ValidationError

PASTED = "sk-" + "q" * 24                  # what a pasted credential could look like
PROVIDER = {"model_provider": "foundry", "model": "deployment-a",
            "model_providers.foundry.name": "Foundry",
            "model_providers.foundry.base_url": "https://example.invalid/openai/v1",
            "model_providers.foundry.env_key": "FOUNDRY_API_KEY",
            "model_providers.foundry.wire_api": "responses",
            "model_providers.foundry.requires_openai_auth": False}


def test_the_fixed_values_of_the_executors_live_here():
    assert (memriver_settings.KILL_GRACE_S, memriver_settings.DREAM_SCRATCH_PREFIX,
            memriver_settings.CLASSIFIER_SCRATCH_PREFIX,
            memriver_settings.DEFAULT_API_KEY_ENV) == (
        2, "memriver-dream-", "memriver-classifier-", "TYPESAFE_API_KEY")


def test_a_whitelisted_provider_is_returned_as_it_is():
    assert check_codex_overrides(PROVIDER) is PROVIDER
    assert check_codex_overrides({}) == {}


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
    {"model_provider": "foundry", "model_providers.foundry.requires_openai_auth": 1},
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
def test_overrides_outside_the_whitelist_are_refused_without_echoing_values(overrides):
    with pytest.raises(ValueError) as caught:
        check_codex_overrides(overrides)
    assert str(caught.value).startswith("codex_overrides")
    assert PASTED not in str(caught.value)


@pytest.mark.parametrize(("overrides", "line"), [
    (["model", "x"], "codex_overrides must be a table"),
    ({"features.hooks": False}, "codex_overrides: features.hooks is not an allowed key"),
    ({"model_providers.a b.name": "x"}, "codex_overrides: a key is not an allowed key"),
    ({"model_provider": "a", "model_providers.a.requires_openai_auth": "false"},
     "codex_overrides: model_providers.a.requires_openai_auth must be true or false"),
    ({"model": "two\nlines"}, "codex_overrides: model must be a non-empty single-line string"),
    ({"model": True}, "codex_overrides: model must be a non-empty single-line string"),
    ({"model_provider": "a", "model_providers.a.base_url": f"https://example.invalid/v1?key={PASTED}"},
     ("codex_overrides: model_providers.a.base_url must be an http(s) URL without user "
      "information, query or fragment")),
    ({"model_provider": "a", "model_providers.a.env_key": PASTED},
     "codex_overrides: model_providers.a.env_key must name an environment variable"),
    ({"model_provider": "a", "model_providers.a.wire_api": "chat"},
     'codex_overrides: model_providers.a.wire_api must be "responses"'),
    ({"model_provider": "a", "model_providers.b.name": "B"},
     "codex_overrides: provider keys must define the one provider model_provider selects"),
])
def test_each_refusal_is_its_fixed_line(overrides, line):
    with pytest.raises(ValueError) as caught:
        check_codex_overrides(overrides)
    assert str(caught.value) == line


def _fields(table: dict) -> tuple[str, ...]:
    try:
        ExecutorSettings.model_validate(table)
    except ValidationError as err:
        return validation_fields(err)
    return ()


def test_the_executor_keys_are_read_with_their_defaults():
    table = ExecutorSettings(executor="codex", executor_path="/opt/bin/codex")
    assert (table.model, table.claude_settings, table.codex_overrides, table.api_key_env) == (
        None, None, {}, "TYPESAFE_API_KEY")
    assert ExecutorSettings(executor="jev").executor_path is None


@pytest.mark.parametrize(("table", "fields"), [
    ({"executor": "gpt"}, ("executor",)),
    ({}, ("executor",)),
    ({"executor": "claude"}, ("executor_path",)),
    ({"executor": "codex"}, ("executor_path",)),
    ({"executor": "claude", "executor_path": "bin/claude"}, ("executor_path",)),
    ({"executor": "jev", "claude_settings": "auth.json"}, ("claude_settings",)),
    ({"executor": "jev", "model": ""}, ("model",)),
    ({"executor": "jev", "model": "two\nlines"}, ("model",)),
    ({"executor": "jev", "api_key_env": "1BAD"}, ("api_key_env",)),
    ({"executor": "jev", "api_key_env": f"{PASTED} x"}, ("api_key_env",)),
    ({"executor": "jev", "codex_overrides": {"features.hooks": PASTED}}, ("codex_overrides",)),
    ({"executor": "gpt", "model": " ", "claude_settings": "rel", "codex_overrides": [],
      "api_key_env": "1x"},
     ("executor", "model", "claude_settings", "codex_overrides", "api_key_env")),
])
def test_every_bad_executor_key_is_named_together_in_field_order(table, fields):
    assert _fields(table) == fields


def test_the_dream_keys_memriver_adds_have_their_values_here():
    assert (memriver_settings.DEFAULT_DREAM_SCHEDULE_AT,
            memriver_settings.DREAM_LAUNCH_AGENT_LABEL) == (
        "04:00", "io.github.path-pass.memriver.dream")
    assert memriver_settings.SCHEDULE_AT_RE.fullmatch("23:59")
    assert not memriver_settings.SCHEDULE_AT_RE.fullmatch("24:00")


JEV = '[classifier]\nexecutor = "jev"\n'
CLAUDE = '[classifier]\nexecutor = "claude"\nexecutor_path = "/opt/bin/claude"\n'


def _root(tmp_path, text: str | None = None) -> Path:
    root = tmp_path / "mem"
    root.mkdir(parents=True)
    if text is not None:
        (root / "settings.toml").write_text(text, encoding="utf-8")
    return root


def test_no_file_or_no_table_means_no_classifier_settings(tmp_path):
    assert load_classifier_settings(_root(tmp_path)) is None
    assert load_classifier_settings(_root(tmp_path / "b", "max_body_chars = 42\n")) is None


def test_a_jev_table_is_read_with_its_defaults(tmp_path):
    table = load_classifier_settings(_root(tmp_path, JEV))
    assert (table.enabled, table.agent_writes, table.dream_writes) == (True, True, True)
    assert (table.executor, table.executor_path, table.model, table.claude_settings) == (
        "jev", None, None, None)
    assert (table.api_key_env, table.block_threshold, table.timeout) == (
        "TYPESAFE_API_KEY", 0.7, 10)
    assert table.codex_overrides == {}


def test_a_headless_executor_defaults_to_a_sixty_second_timeout(tmp_path):
    table = load_classifier_settings(_root(tmp_path, CLAUDE + "model = \"haiku\"\n"))
    assert (table.executor_path, table.model, table.timeout) == ("/opt/bin/claude", "haiku", 60)
    assert load_classifier_settings(_root(tmp_path / "b", CLAUDE + "timeout_s = 5\n")).timeout == 5


def test_each_switch_set_to_false_is_read_as_false(tmp_path):
    text = JEV + "enabled = false\nagent_writes = false\ndream_writes = false\n"
    table = load_classifier_settings(_root(tmp_path, text))
    assert (table.enabled, table.agent_writes, table.dream_writes) == (False, False, False)


def test_keys_match_case_insensitively_and_unknown_keys_are_ignored(tmp_path):
    table = load_classifier_settings(
        _root(tmp_path, '[classifier]\nExecutor = "jev"\nexecutor = "claude"\nunknown = 1\n'))
    assert table.executor == "jev" and not hasattr(table, "unknown")


def test_the_old_key_names_are_unknown_keys(tmp_path):
    # backend and jev_model were renamed before any release: no alias
    with pytest.raises(SettingsError) as caught:
        load_classifier_settings(_root(tmp_path, '[classifier]\nbackend = "jev"\n'))
    assert caught.value.fields == ("classifier.executor",)
    table = load_classifier_settings(_root(tmp_path / "b", JEV + 'jev_model = "jev-1"\n'))
    assert table.model is None and not hasattr(table, "jev_model")


def test_codex_overrides_are_read_from_their_subtable(tmp_path):
    text = ('[classifier]\nexecutor = "codex"\nexecutor_path = "/opt/bin/codex"\n'
            '[classifier.codex_overrides]\n"model_provider" = "azure"\n'
            '"model_providers.azure.env_key" = "AZURE_KEY"\n')
    table = load_classifier_settings(_root(tmp_path, text))
    assert table.codex_overrides == {"model_provider": "azure",
                                     "model_providers.azure.env_key": "AZURE_KEY"}


@pytest.mark.parametrize(("text", "field"), [
    ('[classifier]\nexecutor = "gpt"\n', "classifier.executor"),
    ("[classifier]\nenabled = true\n", "classifier.executor"),
    ('[classifier]\nexecutor = "claude"\n', "classifier.executor_path"),
    ('[classifier]\nexecutor = "codex"\n', "classifier.executor_path"),
    ('[classifier]\nexecutor = "claude"\nexecutor_path = "bin/claude"\n',
     "classifier.executor_path"),
    (CLAUDE + 'claude_settings = "auth.json"\n', "classifier.claude_settings"),
    (CLAUDE + 'model = ""\n', "classifier.model"),
    (JEV + "timeout_s = 0\n", "classifier.timeout_s"),
    (JEV + "timeout_s = true\n", "classifier.timeout_s"),
    (JEV + "block_threshold = 0\n", "classifier.block_threshold"),
    (JEV + "block_threshold = 1.5\n", "classifier.block_threshold"),
    (JEV + "block_threshold = true\n", "classifier.block_threshold"),
    (JEV + 'api_key_env = "1BAD"\n', "classifier.api_key_env"),
    (JEV + f'api_key_env = "{PASTED} x"\n', "classifier.api_key_env"),
    (JEV + 'model = ""\n', "classifier.model"),
    (JEV + f'[classifier.codex_overrides]\n"features.hooks" = "{PASTED}"\n',
     "classifier.codex_overrides"),
    ("classifier = 5\n", "classifier"),
    (JEV + "enabled = 1\n", "classifier.enabled"),
    (JEV + 'enabled = "no"\n', "classifier.enabled"),
    (JEV + "agent_writes = 1\n", "classifier.agent_writes"),
    (JEV + 'agent_writes = "no"\n', "classifier.agent_writes"),
    (JEV + "dream_writes = 1\n", "classifier.dream_writes"),
    (JEV + 'dream_writes = "no"\n', "classifier.dream_writes"),
])
def test_an_invalid_table_raises_naming_only_the_file_and_the_field(tmp_path, text, field):
    root = _root(tmp_path, "max_body_chars = 42\n" + text)
    with pytest.raises(SettingsError) as caught:
        load_classifier_settings(root)
    assert (caught.value.fields, caught.value.env_fields) == ((field,), ())
    assert str(caught.value) == f"settings.toml is invalid: field {field}"
    # nothing chained that could carry the value: no cause, and any context suppressed
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__
    assert PASTED not in str(caught.value) and str(root) not in str(caught.value)


def test_every_bad_classifier_key_is_named_together_in_one_line(tmp_path):
    # a declared change (spec §9, item 6): the executor keys come first; c005153 named
    # enabled, agent_writes, dream_writes, backend, model, claude_settings, timeout_s,
    # codex_overrides, api_key_env, block_threshold
    text = ('[classifier]\nenabled = 1\nagent_writes = 1\ndream_writes = 1\nexecutor = "gpt"\n'
            'model = " "\nclaude_settings = "rel"\ntimeout_s = 0\napi_key_env = "1x"\n'
            'block_threshold = 2\n[classifier.codex_overrides]\n"features.hooks" = false\n')
    with pytest.raises(SettingsError) as caught:
        load_classifier_settings(_root(tmp_path, text))
    assert caught.value.fields == (
        "classifier.executor", "classifier.model", "classifier.claude_settings",
        "classifier.codex_overrides", "classifier.api_key_env", "classifier.enabled",
        "classifier.agent_writes", "classifier.dream_writes", "classifier.timeout_s",
        "classifier.block_threshold")


def test_an_unreadable_file_is_unreadable(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_classifier_settings(_root(tmp_path, "[classifier\n"))
    assert caught.value.unreadable


def test_check_classifier_table_raises_a_validation_error():
    with pytest.raises(ValidationError):
        check_classifier_table({"executor": "claude"})
    assert check_classifier_table({"EXECUTOR": "jev"}).executor == "jev"


def test_the_classifier_defaults_live_here_and_back_the_fields():
    fields = ClassifierSettings.model_fields
    assert (memriver_settings.DEFAULT_API_KEY_ENV, memriver_settings.DEFAULT_BLOCK_THRESHOLD) \
        == (fields["api_key_env"].default, fields["block_threshold"].default) \
        == ("TYPESAFE_API_KEY", 0.7)
    assert (memriver_settings.DEFAULT_HEADLESS_TIMEOUT_S, memriver_settings.DEFAULT_JEV_TIMEOUT_S,
            memriver_settings.DEFAULT_JEV_MODEL, memriver_settings.JEV_BASE_URL,
            memriver_settings.CLASSIFIER_TABLE) == (
        60, 10, "jev-latest", "https://api.typesafe.ai", "classifier")
