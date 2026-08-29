from pathlib import Path

import pytest
from memriver_core.config import Settings, load_settings
from pydantic import ValidationError

CONFIG = "config.toml"


def _root(tmp_path, text: str | None = None) -> Path:
    root = tmp_path / "mem"
    root.mkdir()
    if text is not None:
        (root / CONFIG).write_text(text, encoding="utf-8")
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


def test_config_file_in_root_is_read(monkeypatch, tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\nindex_budget_lines = 7\n")
    monkeypatch.delenv("MEMRIVER_MAX_BODY_CHARS", raising=False)
    s = load_settings(root_override=root)
    assert s.max_body_chars == 42 and s.index_budget_lines == 7
    # untouched keys keep their defaults
    assert s.search_limit_default == 5


def test_env_beats_config_file(monkeypatch, tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\nindex_budget_lines = 7\n")
    monkeypatch.setenv("MEMRIVER_MAX_BODY_CHARS", "99")
    s = load_settings(root_override=root)
    assert s.max_body_chars == 99  # env wins
    assert s.index_budget_lines == 7  # file still supplies the rest


def test_root_override_beats_env(monkeypatch, tmp_path):
    override = _root(tmp_path)
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "from-env"))
    assert load_settings(root_override=override).root == override


def test_config_file_is_found_under_the_env_root(monkeypatch, tmp_path):
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


def test_unreadable_config_file_warns_and_does_not_crash(tmp_path, caplog):
    root = _root(tmp_path, "this is not = = valid toml\n")
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 8000 and s.root == root
    assert CONFIG in caplog.text


def test_missing_config_file_is_fine(tmp_path):
    assert load_settings(root_override=tmp_path / "nowhere").max_body_chars == 8000


def test_settings_are_constructible_directly():
    # build_server takes a Settings instance; env/file layers must not be needed
    s = Settings(root=Path("/tmp/x"), max_body_chars=10)
    assert s.max_body_chars == 10 and s.search_limit_default == 5


def test_invalid_value_in_config_file_falls_back_to_defaults(tmp_path, caplog):
    # a typo'd value must never stop the server from starting
    root = _root(tmp_path, 'max_body_chars = "abc"\n')
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 8000 and s.root == root
    assert CONFIG in caplog.text


def test_config_file_table_instead_of_value_falls_back(tmp_path, caplog):
    root = _root(tmp_path, "[max_body_chars]\nnested = 1\n")
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.max_body_chars == 8000


def test_boolean_in_config_file_is_rejected_not_coerced(tmp_path, caplog):
    # pydantic's lax mode reads True as 1, which would silently cap every
    # search at a single hit; the whole file must be refused instead
    root = _root(tmp_path, "search_limit_max = true\n")
    with caplog.at_level("WARNING"):
        s = load_settings(root_override=root)
    assert s.search_limit_max == 50
    assert CONFIG in caplog.text


@pytest.mark.parametrize("field", ["max_body_chars", "search_limit_default",
                                   "search_limit_max", "index_budget_lines"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_valid_config_file_still_wins_after_the_guard(tmp_path):
    root = _root(tmp_path, "max_body_chars = 42\n")
    assert load_settings(root_override=root).max_body_chars == 42
