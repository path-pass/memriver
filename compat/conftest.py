from __future__ import annotations

import os

import pytest

from . import harness


@pytest.fixture(autouse=True)
def _confined_to_tmp_path(tmp_path, monkeypatch):
    """Every process a test starts must keep its home and store under tmp_path -- and so
    must the test's own process: the suite imports the Python implementation, and a call
    of it that names no store would otherwise find the developer's own."""
    monkeypatch.setattr(harness, "TMP_PATH", tmp_path)
    for name in [name for name in os.environ if name.upper().startswith("MEMRIVER_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "store"))
