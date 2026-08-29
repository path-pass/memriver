from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .entry import Entry

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(
    id UNINDEXED, scope UNINDEXED, type UNINDEXED, active UNINDEXED, body,
    tokenize = 'trigram'
);
"""

# The trigram tokenizer indexes no term shorter than 3 characters, so a shorter
# query can never be answered through MATCH and falls back to a substring scan.
_MIN_TRIGRAM = 3
_SNIPPET_CHARS = 60


def _like_pattern(query: str) -> str:
    """Build a LIKE pattern that matches `query` literally (ESCAPE '\\')."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _truncate(body: str) -> str:
    return body if len(body) <= _SNIPPET_CHARS else body[:_SNIPPET_CHARS] + "…"


@dataclass
class SearchHit:
    id: str
    scope: str
    type: str
    snippet: str
    score: float


class IndexBackend(Protocol):
    def add(self, entry: Entry) -> None: ...
    def mark_superseded(self, entry_id: str) -> None: ...
    def search(self, query: str, scopes: list[str], limit: int) -> list[SearchHit]: ...
    def rebuild(self, store) -> None: ...


class FtsIndex:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)

    def add(self, entry: Entry) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM entries WHERE id = ?", (entry.id,))
            self.conn.execute(
                "INSERT INTO entries (id, scope, type, active, body) VALUES (?,?,?,?,?)",
                (entry.id, entry.scope, entry.type, 1, entry.body))

    def mark_superseded(self, entry_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE entries SET active = 0 WHERE id = ?", (entry_id,))

    def search(self, query: str, scopes: list[str], limit: int = 5) -> list[SearchHit]:
        marks = ",".join("?" for _ in scopes)
        if len(query) < _MIN_TRIGRAM:
            sql = (f"SELECT id, scope, type, body FROM entries "
                   f"WHERE active = 1 AND scope IN ({marks}) "
                   f"AND body LIKE ? ESCAPE '\\' LIMIT ?")
            rows = self.conn.execute(sql, (*scopes, _like_pattern(query), limit)).fetchall()
            return [SearchHit(id=r[0], scope=r[1], type=r[2], snippet=_truncate(r[3]),
                              score=0.0) for r in rows]
        # double any embedded quote so the whole query stays one FTS5 phrase
        # literal and cannot terminate it to inject operators
        escaped = query.replace('"', '""')
        sql = (f"SELECT id, scope, type, snippet(entries, 4, '', '', '…', 20), rank "
               f"FROM entries WHERE entries MATCH ? AND active = 1 "
               f"AND scope IN ({marks}) ORDER BY rank LIMIT ?")
        rows = self.conn.execute(sql, (f'"{escaped}"', *scopes, limit)).fetchall()
        return [SearchHit(id=r[0], scope=r[1], type=r[2], snippet=r[3], score=-r[4])
                for r in rows]

    def rebuild(self, store) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM entries")
        for e in store.iter_entries(include_superseded=False):
            self.add(e)
