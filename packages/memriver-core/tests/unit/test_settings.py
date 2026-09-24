"""Settings model, the defaults catalog, storage_root, and load_settings
precedence: CLI override > env > <root>/settings.toml > defaults.
"""

import os
from pathlib import Path

import pytest
from memriver_core.settings import (
    DEFAULT_BUDGET_LINES,
    DEFAULT_MAX_BODY_CHARS,
    DEFAULT_SEARCH_LIMIT,
    DEFAULT_SEARCH_LIMIT_MAX,
    Settings,
    load_settings,
    storage_root,
)
from pydantic import ValidationError

SETTINGS = "settings.toml"


@pytest.fixture(autouse=True)
def _clear_memriver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env isolation for every test in this module.

    A developer machine may already export MEMRIVER_* (e.g. MEMRIVER_ROOT in a
    shell profile); each precedence/defaults case below sets only the vars it
    means to test, so a value inherited from the real process environment must
    not leak in and change the outcome.
    """
    for key in [k for k in os.environ if k.startswith("MEMRIVER_")]:
        monkeypatch.delenv(key, raising=False)


def test_defaults_wired_to_single_source():
    # catches drift between the defaults catalog and the Settings field defaults
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
    its shell profile. Set on the real process environment at module scope,
    so it is in place *before* this module's per-test autouse fixture
    (_clear_memriver_env) runs -- proving that fixture, not this one, is what
    clears it for each test. Its own `MonkeyPatch` rather than a bare assignment for
    the same reason the machine is worth simulating at all: pytest may have
    inherited a real value for this var, and popping it on teardown would run
    the rest of the session without it."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MEMRIVER_SEARCH_LIMIT_MAX", "1")
        yield


def test_a_preexisting_shell_env_var_does_not_leak_into_defaults(_simulated_shell_env):
    assert Settings().search_limit_max == 50


# --- cross-field validation ---

def test_search_limit_default_may_not_exceed_search_limit_max():
    with pytest.raises(ValidationError, match="search_limit_default"):
        Settings(search_limit_default=100, search_limit_max=50)


def test_search_limit_default_equal_to_the_max_is_allowed():
    assert Settings(search_limit_default=50, search_limit_max=50).search_limit_default == 50


# --- load_settings precedence ---

def _root(tmp_path, text: str | None = None) -> Path:
    root = tmp_path / "mem"
    root.mkdir()
    if text is not None:
        (root / SETTINGS).write_text(text, encoding="utf-8")
    return root


def test_defaults_match_current_behaviour(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMRIVER_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    s = load_settings()
    assert s.root == tmp_path / "agent-memory"
    assert s.max_body_chars == 8000
    assert s.search_limit_default == 5
    assert s.search_limit_max == 50
    assert s.index_budget_lines == 100


def test_env_overrides_default(monkeypatch, tmp_path):
    root = _root(tmp_path)
    monkeypatch.setenv("MEMRIVER_ROOT", str(root))
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "123")
    s = load_settings()
    assert s.root == root and s.max_body_chars == 123


def test_settings_file_in_root_is_read(monkeypatch, tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\nindex_budget_lines = 7\n")
    monkeypatch.delenv("MEMRIVER_MAX_BODY_CHARS", raising=False)
    s = load_settings(root_override=root)
    assert s.max_body_chars == 42 and s.index_budget_lines == 7
    # untouched keys keep their defaults
    assert s.search_limit_default == 5


def test_env_beats_settings_file(monkeypatch, tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\nindex_budget_lines = 7\n")
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "99")
    s = load_settings(root_override=root)
    assert s.max_body_chars == 99  # env wins
    assert s.index_budget_lines == 7  # file still supplies the rest


def test_root_override_beats_env(monkeypatch, tmp_path):
    override = _root(tmp_path)
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "from-env"))
    assert load_settings(root_override=override).root == override


def test_settings_file_is_found_under_the_env_root(monkeypatch, tmp_path):
    root = _root(tmp_path, "search_limit_max = 11\n")
    monkeypatch.setenv("MEMRIVER_ROOT", str(root))
    assert load_settings().search_limit_max == 11


def test_unknown_key_warns_and_does_not_crash(monkeypatch, tmp_path, caplog):
    root = _root(tmp_path, "max_body_chars = 42\nnot_a_setting = 1\n")
    monkeypatch.delenv("MEMRIVER_MAX_BODY_CHARS", raising=False)
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 42
    assert "not_a_setting" in caplog.text


def test_unreadable_settings_file_warns_and_does_not_crash(tmp_path, caplog):
    root = _root(tmp_path, "this is not = = valid toml\n")
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 8000 and s.root == root
    assert SETTINGS in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_settings_file_never_logs_the_absolute_path(tmp_path, caplog):
    """R5: a permission-denied settings.toml is a plausible real-world case (a
    locked-down store root), and its OSError text routinely repeats the
    absolute path -- the warning must name only SETTINGS, never `root`."""
    root = _root(tmp_path, "max_body_chars = 42\n")
    (root / SETTINGS).chmod(0o000)
    try:
        with caplog.at_level("WARNING"):
            s = load_settings(root_override=root)
    finally:
        (root / SETTINGS).chmod(0o600)
    assert s.max_body_chars == 8000
    assert SETTINGS in caplog.text
    assert str(root) not in caplog.text


def test_missing_settings_file_is_fine(tmp_path):
    assert load_settings(root_override=tmp_path / "nowhere").max_body_chars == 8000


def test_invalid_value_in_settings_file_falls_back_to_defaults(tmp_path, caplog):
    # a typo'd value must never stop the server from starting
    root = _root(tmp_path, 'max_body_chars = "abc"\n')
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 8000 and s.root == root
    assert SETTINGS in caplog.text


def test_settings_file_table_instead_of_value_falls_back(tmp_path, caplog):
    root = _root(tmp_path, "[max_body_chars]\nnested = 1\n")
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 8000


def test_boolean_in_settings_file_is_rejected_not_coerced(tmp_path, caplog):
    # pydantic's lax mode reads True as 1, which would silently cap every
    # search at a single hit; the whole file must be refused instead
    root = _root(tmp_path, "search_limit_max = true\n")
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.search_limit_max == 50
    assert SETTINGS in caplog.text


def test_valid_settings_file_still_wins_after_the_guard(tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\n")
    assert load_settings(root_override=root).max_body_chars == 42


def test_root_key_in_settings_file_is_ignored_and_warns(tmp_path, caplog):
    # chicken and egg: the root is what located this file, so a 'root' key
    # inside it can never take effect -- it is dropped, not applied
    root = _root(tmp_path, 'root = "/somewhere/else"\nmax_body_chars = 42\n')
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.root == root
    assert s.max_body_chars == 42
    assert "root" in caplog.text
    assert "MEMRIVER_ROOT" in caplog.text


def test_the_fixed_length_and_timeout_constants_live_in_settings():
    from memriver_core import settings
    assert (settings.INDEX_CUE_CHARS, settings.SEARCH_SNIPPET_CHARS, settings.HEADER_FIELD_CHARS,
            settings.PROJECT_NAME_MAX_CHARS, settings.BUSY_TIMEOUT_MS) == (60, 60, 120, 120, 5000)
    for name in ("INDEX_CUE_CHARS", "SEARCH_SNIPPET_CHARS", "HEADER_FIELD_CHARS",
                 "PROJECT_NAME_MAX_CHARS", "BUSY_TIMEOUT_MS"):
        assert name in settings.__all__
