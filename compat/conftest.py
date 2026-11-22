from __future__ import annotations

import pytest

from . import harness


@pytest.fixture(autouse=True)
def _confined_to_tmp_path(tmp_path, monkeypatch):
    """Every process a test starts must keep its home and store under tmp_path."""
    monkeypatch.setattr(harness, "TMP_PATH", tmp_path)
