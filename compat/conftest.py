from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from . import harness

SESSION: Path | None = None             # the home and the store of the session lie in it
STARTED_WITH: dict[str, str] = {}       # what pytest was started with, put back at the end


def pytest_configure(config):
    """Before any case is collected, pytest's own process gets a home and a store in a
    temporary directory and loses every other `MEMRIVER_*` variable: the top of a case's
    module and a fixture of a wider scope run before the fixture below, and would
    otherwise find the developer's own."""
    global SESSION
    if SESSION is not None:
        return
    SESSION = Path(tempfile.mkdtemp(prefix="memriver-compat-")).resolve()
    for name in [name for name in os.environ
                 if name == "HOME" or name.upper().startswith("MEMRIVER_")]:
        STARTED_WITH[name] = os.environ.pop(name)
    os.environ["HOME"] = str(SESSION / "home")
    os.environ["MEMRIVER_ROOT"] = str(SESSION / "store")


def pytest_unconfigure(config):
    global SESSION
    if SESSION is None:
        return
    for name in [name for name in os.environ
                 if name == "HOME" or name.upper().startswith("MEMRIVER_")]:
        del os.environ[name]
    os.environ.update(STARTED_WITH)
    STARTED_WITH.clear()
    shutil.rmtree(SESSION, ignore_errors=True)
    SESSION = None


@pytest.fixture(autouse=True)
def _confined_to_tmp_path(tmp_path, monkeypatch):
    """Every process a test starts must keep its home and store under tmp_path -- and so
    must the test's own process: the suite imports the Python implementation, and a call
    of it that names no store would otherwise find the session's."""
    monkeypatch.setattr(harness, "TMP_PATH", tmp_path)
    for name in [name for name in os.environ if name.upper().startswith("MEMRIVER_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEMRIVER_ROOT", str(tmp_path / "store"))
