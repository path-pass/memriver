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
    SettingsError,
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


def test_an_unknown_key_and_a_foreign_table_are_ignored_silently(monkeypatch, tmp_path,
                                                                caplog):
    # another package's table ([dream]) and a key core does not know are skipped
    # without a word: the file is shared, and core owns only its top-level keys
    root = _root(tmp_path, "max_body_chars = 42\nnot_a_setting = 1\n"
                           '[dream]\nexecutor = "codex"\n[other]\nx = 1\n')
    with caplog.at_level("DEBUG"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 42
    assert caplog.text == ""
    assert not hasattr(s, "dream") and not hasattr(s, "not_a_setting")


def test_a_file_key_spelled_like_a_constructor_option_is_just_unknown(monkeypatch, tmp_path):
    # BaseSettings' constructor takes _cli_parse_args/_secrets_dir/...; a file key
    # must never reach it
    monkeypatch.setattr("sys.argv", ["memriver", "--max_body_chars", "7"])
    root = _root(tmp_path, '_cli_parse_args = true\n_secrets_dir = "/nowhere"\n')
    assert load_settings(root_override=root).max_body_chars == 8000


def test_precedence_is_init_then_env_then_file_then_defaults(monkeypatch, tmp_path):
    # through the one TOML settings source: each layer sets what those above leave
    root = _root(tmp_path, "max_body_chars = 3\nsearch_limit_max = 30\n"
                           "index_budget_lines = 7\n")
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "2")
    monkeypatch.setenv("MEMRIVER_SEARCH_LIMIT_MAX", "20")
    s = load_settings(root_override=root)
    assert (s.root, s.max_body_chars, s.search_limit_max, s.index_budget_lines,
            s.search_limit_default) == (root, 2, 20, 7, 5)


def test_a_direct_construction_reads_no_settings_file(tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\n")
    assert Settings(root=root).max_body_chars == 8000


def _raised(root: Path) -> SettingsError:
    with pytest.raises(SettingsError) as caught:
        load_settings(root_override=root)
    # never chained: a ValidationError echoes the value, an OSError the path
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    return caught.value


@pytest.mark.parametrize("content", [b"this is not = = valid toml\n", b"a = '\xff'\n"])
def test_an_unreadable_settings_file_is_an_error(tmp_path, content):
    root = _root(tmp_path)
    (root / SETTINGS).write_bytes(content)
    error = _raised(root)
    assert str(error) == "settings.toml could not be read"
    assert (error.fields, error.env_fields, error.unreadable) == ((), (), True)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_permission_denied_settings_file_is_an_error_that_never_names_the_path(tmp_path):
    """A locked-down store root is a plausible real-world case, and its OSError text
    repeats the absolute path -- the error names only SETTINGS, never `root`."""
    root = _root(tmp_path, "max_body_chars = 42\n")
    (root / SETTINGS).chmod(0o000)
    try:
        error = _raised(root)
    finally:
        (root / SETTINGS).chmod(0o600)
    assert str(error) == "settings.toml could not be read"
    assert str(root) not in str(error)


def test_missing_settings_file_is_fine(tmp_path):
    assert load_settings(root_override=tmp_path / "nowhere").max_body_chars == 8000


@pytest.mark.parametrize(("text", "field"), [
    ('max_body_chars = "/secret/abc"\n', "max_body_chars"),      # a typo'd value
    ("[max_body_chars]\nnested = 1\n", "max_body_chars"),        # a table, not a value
    # pydantic's lax mode reads True as 1, which would silently cap every search at
    # a single hit
    ("search_limit_max = true\n", "search_limit_max"),
    ("search_limit_default = 100\n", "search_limit_default"),    # above the max
    ("memory_reads_retention_days = 0\n", "memory_reads_retention_days"),
])
def test_an_invalid_value_in_the_settings_file_is_an_error_naming_the_field(tmp_path, text,
                                                                            field):
    error = _raised(_root(tmp_path, text))
    assert str(error) == f"settings.toml is invalid: field {field}"
    assert (error.fields, error.env_fields) == ((field,), ())
    assert "/secret/abc" not in str(error)


def test_every_invalid_field_is_named_once(tmp_path):
    error = _raised(_root(tmp_path, 'max_body_chars = 0\nindex_budget_lines = "x"\n'))
    assert str(error) == "settings.toml is invalid: field max_body_chars, index_budget_lines"


def test_an_invalid_environment_value_is_an_error_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "/secret/abc")
    error = _raised(_root(tmp_path, "index_budget_lines = 7\n"))
    assert str(error) == "environment variable MEMRIVER_MAX_BODY_CHARS is invalid"
    assert (error.fields, error.env_fields) == ((), ("max_body_chars",))


def test_a_cross_field_failure_from_the_environment_alone_blames_the_environment(
        monkeypatch, tmp_path):
    # no settings.toml at all: the env max falls under the default default
    monkeypatch.setenv("MEMRIVER_SEARCH_LIMIT_MAX", "3")
    error = _raised(_root(tmp_path))
    assert str(error) == "environment variable MEMRIVER_SEARCH_LIMIT_MAX is invalid"
    assert (error.fields, error.env_fields) == ((), ("search_limit_max",))


def test_a_cross_field_failure_from_the_file_names_the_files_max(tmp_path):
    error = _raised(_root(tmp_path, "search_limit_max = 3\n"))
    assert str(error) == "settings.toml is invalid: field search_limit_max"


def test_an_env_failure_is_never_blamed_on_a_file_without_the_field(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_SEARCH_LIMIT_DEFAULT", "9")
    error = _raised(_root(tmp_path, "search_limit_max = 8\nindex_budget_lines = 7\n"))
    # the env default is what exceeds the file's max: the variable is named
    assert str(error) == "environment variable MEMRIVER_SEARCH_LIMIT_DEFAULT is invalid"


def test_env_and_file_failures_are_both_reported_env_first(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "abc")
    error = _raised(_root(tmp_path, 'index_budget_lines = "x"\n'))
    assert str(error) == ("environment variable MEMRIVER_MAX_BODY_CHARS is invalid; "
                          "settings.toml is invalid: field index_budget_lines")
    assert (error.fields, error.env_fields) == (("index_budget_lines",), ("max_body_chars",))


def test_an_error_naming_no_field_is_invalid_not_unreadable():
    assert str(SettingsError()) == "the settings are invalid"
    assert str(SettingsError(unreadable=True)) == "settings.toml could not be read"


def test_valid_settings_file_still_wins_after_the_guard(tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\n")
    assert load_settings(root_override=root).max_body_chars == 42


@pytest.mark.parametrize("value", ['"/somewhere/else"', "5"])
def test_root_key_in_settings_file_is_ignored(tmp_path, value):
    # chicken and egg: the root is what located this file, so a 'root' key
    # inside it can never take effect -- not even an invalid one
    root = _root(tmp_path, f"root = {value}\nmax_body_chars = 42\n")
    s = load_settings(root_override=root)
    assert (s.root, s.max_body_chars) == (root, 42)


def test_a_root_key_never_beats_the_env_root(monkeypatch, tmp_path):
    root = _root(tmp_path, 'root = "/somewhere/else"\n')
    monkeypatch.setenv("MEMRIVER_ROOT", str(root))
    assert load_settings().root == root


@pytest.mark.parametrize("with_dream", [False, True], ids=["no_dream", "with_dream"])
def test_env_and_file_fields_combine_before_cross_field_validation(monkeypatch, tmp_path,
                                                                    with_dream):
    # regression: a field valid only once env and file combine (env lowers the max,
    # the file lowers the default under it) is validated on the merged configuration,
    # never on a premature env-only one
    text = "search_limit_default = 2\n" + ('[dream]\nexecutor = "codex"\n' if with_dream else "")
    monkeypatch.setenv("MEMRIVER_SEARCH_LIMIT_MAX", "3")
    settings = load_settings(root_override=_root(tmp_path, text))
    assert (settings.search_limit_default, settings.search_limit_max) == (2, 3)


def test_memriver_dream_is_not_a_config_entry(tmp_path, monkeypatch):
    # regression: a Settings field named "dream" made MEMRIVER_DREAM a config entry
    # pydantic-settings tried to parse, so a value like "notjson" raised
    monkeypatch.setenv("MEMRIVER_DREAM", "notjson")
    assert not hasattr(Settings(root=tmp_path), "dream")
    assert not hasattr(load_settings(root_override=_root(tmp_path)), "dream")


def test_loading_one_root_leaves_no_file_behind_for_the_next_construction(tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\n")
    assert load_settings(root_override=root).max_body_chars == 42
    assert Settings(root=root).max_body_chars == 8000
    (root / SETTINGS).write_text("max_body_chars = 0\n", encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(root_override=root)
    assert Settings(root=root).max_body_chars == 8000


def test_the_fixed_length_and_timeout_constants_live_in_settings():
    from memriver_core import settings
    assert (settings.INDEX_CUE_CHARS, settings.SEARCH_SNIPPET_CHARS, settings.HEADER_FIELD_CHARS,
            settings.PROJECT_NAME_MAX_CHARS, settings.BUSY_TIMEOUT_MS) == (60, 60, 120, 120, 5000)
    for name in ("INDEX_CUE_CHARS", "SEARCH_SNIPPET_CHARS", "HEADER_FIELD_CHARS",
                 "PROJECT_NAME_MAX_CHARS", "BUSY_TIMEOUT_MS"):
        assert name in settings.__all__


SESSION_CONSTANTS = {
    "SESSION_PROMPT_CHARS": 512, "SESSION_RECENT_PROMPTS": 5,
    "SESSION_PROMPT_SCAN_MAX_BYTES": 65536, "STOP_NUDGE_MIN_PROMPTS": 5,
    "STOP_NUDGE_INTERVAL_PROMPTS": 5, "GIT_QUERY_TIMEOUT_S": 2,
    "SESSION_SEARCH_LIMIT_DEFAULT": 10, "SESSION_SEARCH_LIMIT_MAX": 50,
    "TOOL_CALL_RETENTION_S": 3600, "SESSION_SUMMARY_MAX_CHARS": 1_200,
}


def test_the_session_constants_live_in_settings():
    from memriver_core import settings
    assert {name: getattr(settings, name) for name in SESSION_CONSTANTS} == SESSION_CONSTANTS
    assert set(SESSION_CONSTANTS) <= set(settings.__all__)


def test_memory_reads_retention_is_unset_by_default_and_positive_when_set(tmp_path):
    assert Settings(root=tmp_path).memory_reads_retention_days is None
    assert Settings(root=tmp_path, memory_reads_retention_days=30).memory_reads_retention_days \
        == 30
    for bad in (0, -1, True):
        with pytest.raises(ValidationError):
            Settings(root=tmp_path, memory_reads_retention_days=bad)


def test_memory_reads_retention_is_read_from_the_settings_file(tmp_path):
    root = _root(tmp_path, "memory_reads_retention_days = 60\n")
    assert load_settings(root_override=root).memory_reads_retention_days == 60
