"""Settings model, the defaults catalog, and storage_root.

Split out of the former tests/test_config.py; the load_settings precedence
cases live next door in test_loader.py.
"""

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
    # remaining signature literal (review_queue's batch cap) is asserted in
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
