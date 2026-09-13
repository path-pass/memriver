"""Tests for the write side of sync_gitleaks_rules.py, offline only.

Run with: uv run pytest tools/ -- this directory sits outside the workspace's
`testpaths` (packages/*/tests), so it is not part of the default `pytest
packages/` invocation and never touches the network.
"""
import importlib.util
import os
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent / "sync_gitleaks_rules.py"
_spec = importlib.util.spec_from_file_location("sync_gitleaks_rules", _MODULE_PATH)
sync_gitleaks_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_gitleaks_rules)


def test_atomic_write_bytes_replaces_target_content(tmp_path):
    target = tmp_path / "gitleaks.toml"
    target.write_bytes(b"old content")

    sync_gitleaks_rules._atomic_write_bytes(target, b"new content")

    assert target.read_bytes() == b"new content"
    # no leftover temp sibling
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_bytes_leaves_original_intact_on_interruption(tmp_path, monkeypatch):
    target = tmp_path / "gitleaks.toml"
    target.write_bytes(b"original content")

    def boom(fd, mode):
        os.close(fd)
        raise OSError("simulated disk-full during write")

    monkeypatch.setattr(sync_gitleaks_rules.os, "fdopen", boom)

    with pytest.raises(OSError):
        sync_gitleaks_rules._atomic_write_bytes(target, b"new content" * 100)

    # the original file was never touched, and the failed temp write left no trace
    assert target.read_bytes() == b"original content"
    assert list(tmp_path.iterdir()) == [target]
