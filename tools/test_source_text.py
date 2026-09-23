"""No committed text file carries a raw invisible character.

Copying code that spells ``\\u2028`` as an escape has more than once produced
the raw character instead; it renders as nothing and still changes behavior.
Tab, newline and carriage return are the only control characters allowed.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCANNED = ("packages", "tools", "skills", "docs")
SUFFIXES = {".py", ".md", ".toml", ".json", ".sh"}
INVISIBLE = {"Cc", "Cf", "Zl", "Zp"}


def _offenders(text: str) -> list[str]:
    return [f"U+{ord(c):04X}" for c in text
            if c not in "\t\n\r" and unicodedata.category(c) in INVISIBLE]


def test_the_check_catches_escapes_written_raw():
    sample = "a" + chr(0x2028) + "b" + chr(0) + "c" + chr(0x200B) + "d\tok\n"
    assert _offenders(sample) == ["U+2028", "U+0000", "U+200B"]


def test_no_raw_invisible_characters_in_committed_text():
    found = {}
    for top in SCANNED:
        for path in (REPO / top).rglob("*"):
            if path.suffix in SUFFIXES and path.is_file() and "__pycache__" not in path.parts:
                bad = _offenders(path.read_text(encoding="utf-8"))
                if bad:
                    found[str(path.relative_to(REPO))] = sorted(set(bad))
    assert not found, found
