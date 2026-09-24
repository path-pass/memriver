"""No committed text file carries a raw invisible character.

Copying code that spells ``\\u2028`` as an escape has more than once produced
the raw character instead; it renders as nothing and still changes behavior.
Tab, newline and carriage return are the only control characters allowed.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCANNED = ("packages", "tools", "skills", "docs")
SUFFIXES = {".py", ".md", ".toml", ".json", ".sh"}
INVISIBLE = {"Cc", "Cf", "Zl", "Zp"}


def _offenders(text: str) -> list[str]:
    return [f"U+{ord(c):04X}" for c in text
            if c not in "\t\n\r" and unicodedata.category(c) in INVISIBLE]


def _tracked_files() -> list[Path]:
    """Files git tracks under SCANNED; skips the test if git is unusable."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", *SCANNED],
            cwd=REPO, check=True, capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git unavailable: {exc}")
    return [REPO / name for name in result.stdout.decode().split("\0") if name]


def _scan() -> dict[str, list[str]]:
    found = {}
    for path in _tracked_files():
        if path.suffix in SUFFIXES and path.exists():
            bad = _offenders(path.read_text(encoding="utf-8"))
            if bad:
                found[str(path.relative_to(REPO))] = sorted(set(bad))
    return found


def test_the_check_catches_escapes_written_raw():
    sample = "a" + chr(0x2028) + "b" + chr(0) + "c" + chr(0x200B) + "d\tok\n"
    assert _offenders(sample) == ["U+2028", "U+0000", "U+200B"]


def test_no_raw_invisible_characters_in_committed_text():
    found = _scan()
    assert not found, found


def test_scan_ignores_untracked_files(tmp_path, monkeypatch):
    offender = tmp_path / "untracked.py"
    offender.write_text("bad" + chr(0x2028) + "\n", encoding="utf-8")

    monkeypatch.setattr(sys.modules[__name__], "_tracked_files", lambda: [Path(__file__)])

    assert _scan() == {}
