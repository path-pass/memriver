"""Keyword search: a query becomes terms, an entry's texts give it a rank.

One rule for both stores: the query and every searched text are folded the
same way (NFKC, then case-folded), an entry matches when any term occurs in
any of its texts, and matches sort by `Rank` (larger first), recency within
equal ranks.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import NamedTuple

from .memory import Memory

MAX_TERMS = 16
# the separators besides whitespace: comma, semicolon, and the ideographic
# comma; the full-width comma and semicolon are listed although NFKC already
# turns them into the ASCII ones
_SEPARATORS = str.maketrans(dict.fromkeys(",;\u3001\uff0c\uff1b", " "))


class Rank(NamedTuple):
    whole: bool           # the whole query (terms joined by one space) occurs in one text
    terms: int            # distinct terms found in any text; 0 is no match
    in_description: int   # terms found in the description (memories; 0 for sessions)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def search_terms(query: str) -> tuple[str, ...]:
    """The query's distinct terms in order, at most MAX_TERMS; () matches nothing.

    Split on any Unicode whitespace (U+3000 included) and the separators. NUL
    is dropped first, so a NUL-only query has no terms.
    """
    pieces = _fold(query.replace("\x00", "")).translate(_SEPARATORS).split()
    return tuple(dict.fromkeys(pieces))[:MAX_TERMS]


def rank_key(terms: tuple[str, ...], texts: Iterable[str], *,
             description: str | None = None) -> Rank:
    """How `texts` answer `terms`. `description` (a memory's, one of `texts`)
    counts the terms it holds on its own; `Rank.terms == 0` is no match."""
    folded = [_fold(text) for text in texts]
    whole = " ".join(terms)
    found = sum(any(term in text for text in folded) for term in terms)
    cue = "" if description is None else _fold(description)
    return Rank(whole=bool(terms) and any(whole in text for text in folded), terms=found,
                in_description=sum(term in cue for term in terms))


def rank_memories(terms: tuple[str, ...], memories: Iterable[Memory]) -> list[Memory]:
    """The memories `terms` match, best first; the given order breaks ties, so
    pass them newest first."""
    ranked = [(rank_key(terms, (m.description, m.body), description=m.description), m)
              for m in memories]
    # stable, reverse included: equal ranks keep the given order
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [m for rank, m in ranked if rank.terms]
