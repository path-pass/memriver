"""memriver-classifier is a workspace member, so it is importable while these tests run;
every install test sees it absent unless it says otherwise, so the MCP registration
is today's `uvx memriver serve ...` form."""

from __future__ import annotations

import pytest
from memriver.install import editors


@pytest.fixture(autouse=True)
def _no_classifier_package(monkeypatch):
    monkeypatch.setattr(editors, "classifier_installed", lambda: False)
