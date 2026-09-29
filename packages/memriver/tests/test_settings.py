"""memriver.settings: memriver's defaults and fixed values, the executor keys [dream]
and [classifier] share, and the Codex provider-override whitelist -- the exact line of
every refusal, naming the key and a fixed reason, never the value, which could be a
pasted secret."""

from __future__ import annotations

import pytest
from memriver import settings as memriver_settings
from memriver.settings import ExecutorSettings, check_codex_overrides
from memriver_core.settings import validation_fields
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
