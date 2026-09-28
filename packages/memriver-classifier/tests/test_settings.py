"""The [classifier] table: load_classifier_settings, its defaults and every refusal."""

from __future__ import annotations

from pathlib import Path

import pytest
from memriver_classifier import settings as classifier_settings
from memriver_classifier.settings import (
    ClassifierSettings,
    check_classifier_table,
    load_classifier_settings,
)
from memriver_core.settings import SettingsError
from pydantic import ValidationError

JEV = '[classifier]\nbackend = "jev"\n'
CLAUDE = '[classifier]\nbackend = "claude"\nexecutor_path = "/opt/bin/claude"\n'
PASTED = "sk-" + "q" * 24                  # what a pasted credential could look like


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
    assert (table.backend, table.executor_path, table.model, table.claude_settings) == (
        "jev", None, None, None)
    assert (table.jev_model, table.api_key_env, table.block_threshold, table.timeout) == (
        "jev-latest", "TYPESAFE_API_KEY", 0.7, 10)
    assert table.codex_overrides == {}


def test_a_headless_backend_defaults_to_a_sixty_second_timeout(tmp_path):
    table = load_classifier_settings(_root(tmp_path, CLAUDE + "model = \"haiku\"\n"))
    assert (table.executor_path, table.model, table.timeout) == ("/opt/bin/claude", "haiku", 60)
    assert load_classifier_settings(_root(tmp_path / "b", CLAUDE + "timeout_s = 5\n")).timeout == 5


def test_keys_match_case_insensitively_and_unknown_keys_are_ignored(tmp_path):
    table = load_classifier_settings(
        _root(tmp_path, '[classifier]\nBackend = "jev"\nbackend = "claude"\nunknown = 1\n'))
    assert table.backend == "jev" and not hasattr(table, "unknown")


def test_codex_overrides_are_read_from_their_subtable(tmp_path):
    text = ('[classifier]\nbackend = "codex"\nexecutor_path = "/opt/bin/codex"\n'
            '[classifier.codex_overrides]\n"model_provider" = "azure"\n'
            '"model_providers.azure.env_key" = "AZURE_KEY"\n')
    table = load_classifier_settings(_root(tmp_path, text))
    assert table.codex_overrides == {"model_provider": "azure",
                                     "model_providers.azure.env_key": "AZURE_KEY"}


@pytest.mark.parametrize(("text", "field"), [
    ('[classifier]\nbackend = "gpt"\n', "classifier.backend"),
    ("[classifier]\nenabled = true\n", "classifier.backend"),
    ('[classifier]\nbackend = "claude"\n', "classifier.executor_path"),
    ('[classifier]\nbackend = "codex"\n', "classifier.executor_path"),
    ('[classifier]\nbackend = "claude"\nexecutor_path = "bin/claude"\n',
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
    (JEV + 'jev_model = ""\n', "classifier.jev_model"),
    (JEV + f'[classifier.codex_overrides]\n"features.hooks" = "{PASTED}"\n',
     "classifier.codex_overrides"),
    ("classifier = 5\n", "classifier"),
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


def test_an_unreadable_file_is_unreadable(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_classifier_settings(_root(tmp_path, "[classifier\n"))
    assert caught.value.unreadable


def test_check_classifier_table_raises_a_validation_error():
    with pytest.raises(ValidationError):
        check_classifier_table({"backend": "claude"})
    assert check_classifier_table({"BACKEND": "jev"}).backend == "jev"


def test_the_defaults_live_in_the_settings_module_and_back_the_fields():
    fields = ClassifierSettings.model_fields
    assert (classifier_settings.DEFAULT_JEV_MODEL, classifier_settings.DEFAULT_API_KEY_ENV,
            classifier_settings.DEFAULT_BLOCK_THRESHOLD) == (
        fields["jev_model"].default, fields["api_key_env"].default,
        fields["block_threshold"].default) == ("jev-latest", "TYPESAFE_API_KEY", 0.7)
    assert (classifier_settings.DEFAULT_HEADLESS_TIMEOUT_S,
            classifier_settings.DEFAULT_JEV_TIMEOUT_S, classifier_settings.KILL_GRACE_S,
            classifier_settings.JEV_URL) == (60, 10, 2, "https://api.typesafe.ai/v1/systemone")
