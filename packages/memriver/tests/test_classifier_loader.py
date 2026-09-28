"""How the umbrella finds the optional classifier: built only when memriver-classifier
is importable and a [classifier] table exists; a table without the package is
reported; doctor's one line for each state."""

from __future__ import annotations

import builtins
import sys

import pytest
from memriver import classifier_loader
from memriver.classifier_loader import (
    TABLE_WITHOUT_PACKAGE,
    classifier_state,
    load_classifier,
)
from memriver_core.settings import SettingsError

JEV = '[classifier]\nbackend = "jev"\napi_key_env = "MEMRIVER_TEST_UNSET_KEY"\n'


@pytest.fixture
def root(tmp_path):
    return tmp_path


def _write(root, text: str) -> None:
    (root / "settings.toml").write_text(text, encoding="utf-8")


@pytest.fixture
def absent(monkeypatch):
    """memriver-classifier is a workspace member; this makes it look not installed."""
    monkeypatch.setitem(sys.modules, "memriver_classifier", None)


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    """The loader's warning lines, recorded directly: cli.main() elsewhere in the run
    stops the memriver logger from propagating to caplog."""
    lines: list[str] = []
    monkeypatch.setattr(classifier_loader.logger, "warning",
                        lambda message, *args: lines.append(message % args))
    return lines


def test_without_the_package_and_without_a_table_nothing_is_built_or_said(root, absent,
                                                                         warnings):
    assert load_classifier(root, env={}) is None
    assert warnings == []


def test_a_table_without_the_package_is_one_warning_and_no_classifier(root, absent, warnings):
    _write(root, JEV)
    assert load_classifier(root, env={}) is None
    assert warnings == [f"memriver: {TABLE_WITHOUT_PACKAGE}"]


def test_the_package_without_a_table_builds_nothing(root):
    _write(root, "max_body_chars = 4000\n")
    assert load_classifier(root, env={}) is None


def test_the_package_with_a_table_builds_the_configured_classifier(root):
    _write(root, JEV)
    classifier = load_classifier(root, env={})
    assert classifier.classify("a fact", changed_by="mcp").detail == "no-key"
    _write(root, JEV + "enabled = false\n")
    assert load_classifier(root, env={}) is None


def test_an_invalid_table_is_a_settings_error(root):
    _write(root, '[classifier]\nbackend = "gpt"\n')
    with pytest.raises(SettingsError, match="field classifier.backend"):
        load_classifier(root, env={})


def test_a_broken_installed_package_is_never_taken_for_an_absent_one(root, monkeypatch):
    # an installed package failing on its own missing dependency must not silently turn
    # a configured classifier off
    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "memriver_classifier":
            raise ModuleNotFoundError("No module named 'missing_dependency'",
                                      name="missing_dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "memriver_classifier", raising=False)
    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(ModuleNotFoundError):
        load_classifier(root, env={})


@pytest.mark.parametrize(("text", "installed", "state"), [
    (None, False, "not installed"),
    (JEV, False, f"not installed; {TABLE_WITHOUT_PACKAGE}"),
    (None, True, "installed, not configured"),
    (JEV + "enabled = false\n", True, "off (enabled = false)"),
    ('[classifier]\nbackend = "claude"\nexecutor_path = "/opt/bin/claude"\n', True,
     "claude (/opt/bin/claude)"),
    ('[classifier]\nbackend = "codex"\nexecutor_path = "/opt/bin/codex"\n', True,
     "codex (/opt/bin/codex)"),
    ('[classifier]\nbackend = "jev"\n', True, "jev (model jev-latest, key from TYPESAFE_API_KEY)"),
])
def test_doctor_states_the_classifier_in_one_line(root, monkeypatch, text, installed, state):
    if not installed:
        monkeypatch.setitem(sys.modules, "memriver_classifier", None)
    if text is not None:
        _write(root, text)
    assert classifier_state(root) == state
