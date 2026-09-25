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


SESSION_CONSTANTS = {
    "SESSION_PROMPT_CHARS": 512, "SESSION_RECENT_PROMPTS": 5,
    "SESSION_PROMPT_SCAN_MAX_BYTES": 65536, "STOP_NUDGE_MIN_PROMPTS": 5,
    "STOP_NUDGE_INTERVAL_PROMPTS": 5, "GIT_QUERY_TIMEOUT_S": 2,
    "SESSION_SEARCH_LIMIT_DEFAULT": 10, "SESSION_SEARCH_LIMIT_MAX": 50,
    "TOOL_CALL_RETENTION_S": 3600,
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


# --- the [dream] table ---

DREAM_TABLE = '[dream]\nexecutor = "codex"\nexecutor_path = "/opt/bin/codex"\n'


def test_no_dream_table_means_no_dream_settings(tmp_path):
    settings = load_settings(root_override=_root(tmp_path, "max_body_chars = 42\n"))
    assert (settings.dream, settings.dream_invalid) == (None, False)


def test_a_dream_table_is_read_with_its_defaults(tmp_path):
    settings = load_settings(root_override=_root(tmp_path, DREAM_TABLE + "ttl_days = 30\n"))
    dream = settings.dream
    assert (dream.executor, dream.executor_path, dream.ttl_days) == ("codex", "/opt/bin/codex", 30)
    assert (dream.ttl_read_multiplier_max, dream.uncertain_limit, dream.idle_minutes,
            dream.schedule_at, dream.max_sessions_per_run, dream.max_groups_per_run,
            dream.max_candidates_per_run) == (5, 2, 60, "04:00", 20, 20, 30)


@pytest.mark.parametrize("dream_table", [None, DREAM_TABLE], ids=["no_dream", "with_dream"])
def test_env_and_file_fields_combine_before_cross_field_validation(monkeypatch, tmp_path,
                                                                    dream_table):
    # regression: load_settings used to build a Settings from the root/dream/env layer
    # alone before merging the file values in. A field valid only once env and file
    # combine (env raises the max, the file lowers the default under it) raised
    # ValidationError from that premature construction instead of ever reaching the
    # merged one.
    text = "search_limit_default = 2\n" + (dream_table or "")
    root = _root(tmp_path, text)
    monkeypatch.setenv("MEMRIVER_SEARCH_LIMIT_MAX", "3")
    settings = load_settings(root_override=root)
    assert (settings.search_limit_default, settings.search_limit_max) == (2, 3)


@pytest.mark.parametrize("table", [
    '[dream]\nexecutor = "gpt"\nexecutor_path = "/opt/bin/codex"\n',
    '[dream]\nexecutor = "codex"\nexecutor_path = "relative/codex"\n',
    DREAM_TABLE + "ttl_days = 0\n",
    DREAM_TABLE + "ttl_days = true\n",
    DREAM_TABLE + 'schedule_at = "25:00"\n',
    DREAM_TABLE + "unknown_key = 1\n",
    DREAM_TABLE + '[dream.codex_overrides]\n"features.hooks" = false\n',
    "dream = 5\n",
])
def test_an_invalid_dream_table_is_dropped_alone_and_flagged(tmp_path, caplog, table):
    text = "max_body_chars = 42\n" + table
    with caplog.at_level("WARNING"):
        settings = load_settings(root_override=_root(tmp_path, text))
    assert (settings.dream, settings.dream_invalid, settings.max_body_chars) == (None, True, 42)
    assert "[dream]" in caplog.text


def test_a_dream_table_missing_its_executor_is_invalid(tmp_path):
    settings = load_settings(root_override=_root(tmp_path, "[dream]\nttl_days = 30\n"))
    assert (settings.dream, settings.dream_invalid) == (None, True)


CODEX_PROVIDER = (
    '[dream.codex_overrides]\n"model_provider" = "foundry"\n"model" = "deployment-a"\n'
    '"model_providers.foundry.name" = "Foundry"\n'
    '"model_providers.foundry.base_url" = "https://example.invalid/openai/v1"\n'
    '"model_providers.foundry.env_key" = "FOUNDRY_API_KEY"\n'
    '"model_providers.foundry.wire_api" = "responses"\n'
    '"model_providers.foundry.requires_openai_auth" = false\n')


def test_codex_overrides_default_to_empty(tmp_path):
    assert load_settings(root_override=_root(tmp_path, DREAM_TABLE)).dream.codex_overrides == {}


def test_a_whitelisted_codex_provider_is_read_with_its_types(tmp_path):
    settings = load_settings(root_override=_root(tmp_path, DREAM_TABLE + CODEX_PROVIDER))
    overrides = settings.dream.codex_overrides
    assert (overrides["model_provider"], overrides["model_providers.foundry.env_key"]) == (
        "foundry", "FOUNDRY_API_KEY")
    assert overrides["model_providers.foundry.requires_openai_auth"] is False
    assert len(overrides) == 7


PASTED = "sk-" + "q" * 24                  # what a pasted credential could look like


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
    from memriver_core.settings import DreamSettings

    with pytest.raises(ValidationError) as caught:
        DreamSettings(executor="codex", executor_path="/opt/bin/codex",
                      codex_overrides=overrides)
    reasons = [str(error["ctx"]["error"]) for error in caught.value.errors()
               if error["type"] == "value_error"]
    assert reasons and all(reason.startswith("codex_overrides") for reason in reasons)
    assert not any(PASTED in reason for reason in reasons)


DREAM_CONSTANTS = {
    "DREAM_CONTEXT_BUDGET_TOKENS": 100_000, "DREAM_OUTPUT_RESERVE_TOKENS": 4_000,
    "DREAM_INPUT_MARGIN_TOKENS": 16_000,
    "DREAM_SUMMARY_MAX_CHARS": 1_200, "DREAM_CHUNK_SUMMARY_CHARS": 1_500,
    "DREAM_TOOL_OUTPUT_CHARS": 2_000, "DREAM_MAX_CALLS_PER_SESSION": 12,
    "DREAM_CALL_TIMEOUT_S": 300, "DREAM_MAX_QUARANTINE_PER_RUN": 1_000,
    "DREAM_MAX_ROOM_HALVINGS": 3,
}


def test_the_dream_constants_live_in_settings():
    from memriver_core import settings
    assert {name: getattr(settings, name) for name in DREAM_CONSTANTS} == DREAM_CONSTANTS
    assert set(DREAM_CONSTANTS) <= set(settings.__all__)
