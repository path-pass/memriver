"""Env isolation for every test in this directory.

A developer machine may already export MEMRIVER_* (e.g. MEMRIVER_ROOT in a
shell profile); each precedence/defaults case below sets only the vars it
means to test, so a value inherited from the real process environment must
not leak in and change the outcome.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_memriver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in os.environ if k.startswith("MEMRIVER_")]:
        monkeypatch.delenv(key, raising=False)
