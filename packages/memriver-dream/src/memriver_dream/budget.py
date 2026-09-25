"""Token budgeting without a tokenizer: a rough estimate, not a guaranteed upper bound.

Combining marks, emoji, schemas and the harness's own prompt cost tokens it
does not see; callers keep DREAM_INPUT_MARGIN_TOKENS unused and shrink the
input when an executor still answers "too-large".
"""

from __future__ import annotations

import math
import unicodedata

_WIDE = ("W", "F")


def estimate_tokens(text: str) -> int:
    """One token per CJK or fullwidth character, one per four others, rounded up."""
    wide = sum(1 for char in text if unicodedata.east_asian_width(char) in _WIDE)
    return wide + math.ceil((len(text) - wide) / 4)


def cut(text: str, budget: int) -> list[str]:
    """`text` in consecutive pieces of at most `budget` tokens, cut at character boundaries."""
    limit = budget * 4                      # counted in quarter tokens
    pieces: list[str] = []
    start = cost = 0
    for index, char in enumerate(text):
        step = 4 if unicodedata.east_asian_width(char) in _WIDE else 1
        if cost + step > limit:
            pieces.append(text[start:index])
            start, cost = index, 0
        cost += step
    pieces.append(text[start:])
    return pieces
