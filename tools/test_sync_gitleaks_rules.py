"""Tests for the write side of sync_gitleaks_rules.py, offline only.

`tools` is part of the workspace's `testpaths`, so the default `pytest`
invocation runs these. The download is always stubbed out here: the sync
script is the only part of the project allowed to touch the network, and its
tests are not.
"""
import importlib.util
import os
import stat
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


def test_atomic_write_bytes_keeps_the_mode_the_target_already_had(tmp_path):
    """`os.replace` hands the temp file's inode to the target's name, so the
    world-readable rules file a checkout ships would silently become 0600 --
    unreadable to a packaging job or a second user on a shared checkout --
    unless the existing mode is copied onto the temp file first."""
    target = tmp_path / "gitleaks.toml"
    target.write_bytes(b"old content")
    target.chmod(0o644)

    sync_gitleaks_rules._atomic_write_bytes(target, b"new content")

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_atomic_write_bytes_leaves_a_brand_new_file_private(tmp_path):
    """There is no mode to preserve when the target does not exist yet, and
    mkstemp's own 0600 is the right default to fall back to."""
    target = tmp_path / "gitleaks.toml"

    sync_gitleaks_rules._atomic_write_bytes(target, b"new content")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def _run_sync(monkeypatch, tmp_path, payload: bytes) -> Path:
    """Point the script's output at `tmp_path` and feed it `payload`."""
    target = tmp_path / "gitleaks.toml"
    target.write_bytes(b'[[rules]]\nid = "known-good"\nregex = "AKIA[0-9A-Z]{16}"\n')
    target.chmod(0o644)
    monkeypatch.setattr(sync_gitleaks_rules, "OUTPUT", target)

    class _Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sync_gitleaks_rules.urllib.request, "urlopen",
                        lambda url, timeout: _Response())
    monkeypatch.setattr("sys.argv", ["sync_gitleaks_rules.py"])
    return target


def test_sync_refuses_a_ruleset_the_scanner_cannot_load(monkeypatch, tmp_path):
    """Legal TOML with a rules array is not a legal ruleset. A rule without an
    `id` parses fine and would replace the live file, and only then would the
    scanner's own loader raise -- breaking every process that imports it. The
    structural check has to run against the bytes on disk before the replace,
    through the same loader the runtime uses."""
    target = _run_sync(monkeypatch, tmp_path, b'[[rules]]\nregex = "token"\n')
    before = target.read_bytes()

    with pytest.raises(KeyError):
        sync_gitleaks_rules.main()

    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_sync_replaces_the_ruleset_when_the_scanner_can_load_it(monkeypatch,
                                                                tmp_path, capsys):
    fresh = b'[[rules]]\nid = "fresh"\nregex = "ghp_[0-9A-Za-z]{36}"\n'
    target = _run_sync(monkeypatch, tmp_path, fresh)

    sync_gitleaks_rules.main()

    assert target.read_bytes() == fresh
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "1 upstream rules" in capsys.readouterr().out
