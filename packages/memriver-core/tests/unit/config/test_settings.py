"""Settings model, the defaults catalog, and storage_root.

Split out of the former tests/test_config.py; the load_settings precedence
cases live next door in test_loader.py.
"""

import os
from pathlib import Path

import pytest
from memriver_core.config import (
    DEFAULT_BUDGET_LINES,
    DEFAULT_MAX_BODY_CHARS,
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_SEARCH_LIMIT_MAX,
    Settings,
    storage_root,
)
from pydantic import ValidationError


def test_defaults_wired_to_single_source():
    # catches drift between the config catalog and the Settings field defaults
    # it backs. The consumer half of the old assertion -- that these values
    # actually reach the behaviour -- is now injection rather than signature
    # defaults, and is asserted in tests/unit/test_bootstrap.py; the one
    # remaining signature literal (dream's batch cap) is asserted in
    # tests/unit/application/test_service.py.
    assert (DEFAULT_MAX_BODY_CHARS == 8000
            == Settings.model_fields["max_body_chars"].default)
    assert (DEFAULT_SEARCH_LIMIT_MAX == 50
            == Settings.model_fields["search_limit_max"].default)
    assert (DEFAULT_SEARCH_LIMIT == 5
            == Settings.model_fields["search_limit_default"].default)
    assert (DEFAULT_BUDGET_LINES == 100
            == Settings.model_fields["index_budget_lines"].default)


def test_settings_are_constructible_directly():
    # build_server takes a Settings instance; env/file layers must not be needed
    s = Settings(root=Path("/tmp/x"), max_body_chars=10)
    assert s.max_body_chars == 10 and s.search_limit_default == 5


@pytest.mark.parametrize("field", ["max_body_chars", "search_limit_default",
                                   "search_limit_max", "index_budget_lines"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_storage_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "mem"))
    assert storage_root() == tmp_path / "mem"


def test_storage_root_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMRIVER_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert storage_root() == tmp_path / "agent-memory"


def test_settings_root_defaults_to_storage_root(monkeypatch, tmp_path):
    # the field is a default_factory, so the env is read per instantiation
    # rather than once at import time
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "mem"))
    assert Settings().root == tmp_path / "mem"


def test_storage_root_prefers_an_injected_env_over_the_process_environment(
        monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "real"))
    injected = {"MEMRIVER_ROOT": str(tmp_path / "injected")}
    assert storage_root(env=injected) == tmp_path / "injected"


def test_storage_root_falls_back_to_an_injected_home_without_an_env_override(
        monkeypatch, tmp_path):
    monkeypatch.delenv("MEMRIVER_ROOT", raising=False)
    assert storage_root(env={}, home=tmp_path / "injected-home") == (
        tmp_path / "injected-home" / "agent-memory")


# --- a machine with a MEMRIVER_* var already exported must not leak in ---

@pytest.fixture(scope="module")
def _simulated_shell_env():
    """Stands in for a developer machine that already exports MEMRIVER_* in
    its shell profile. Set on the real process environment (not via
    monkeypatch) and at module scope, so it is in place *before* this
    directory's per-test autouse fixture (conftest.py) runs -- proving that
    fixture, not this one, is what clears it for each test."""
    os.environ["MEMRIVER_SEARCH_LIMIT_MAX"] = "1"
    try:
        yield
    finally:
        os.environ.pop("MEMRIVER_SEARCH_LIMIT_MAX", None)


def test_a_preexisting_shell_env_var_does_not_leak_into_defaults(_simulated_shell_env):
    assert Settings().search_limit_max == 50


# --- cross-field validation ---

def test_search_limit_default_may_not_exceed_search_limit_max():
    with pytest.raises(ValidationError, match="search_limit_default"):
        Settings(search_limit_default=100, search_limit_max=50)


def test_search_limit_default_equal_to_the_max_is_allowed():
    assert Settings(search_limit_default=50, search_limit_max=50).search_limit_default == 50
