"""dream.db, memriver-dream's own records (spec §5): runs, TTL reviews, scope passes,
source re-checks and session summary state.

It never holds a memory body, a full prompt or secret text; the only model text in it
is the policy-checked partial summaries of an unfinished long session
(`session_summaries.progress`). The file is 0600 in a 0700 directory. One connection
per operation, closed at its end; every write is one short BEGIN IMMEDIATE
transaction under core's busy timeout, in SQLite's default journal mode, as core's
own store.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memriver_core.models import is_timestamp
from memriver_core.settings import BUSY_TIMEOUT_MS

from .settings import PROMPT_VERSION

# spec §5, verbatim but for IF NOT EXISTS: every open makes sure the tables are there
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (
      run_id      TEXT PRIMARY KEY,
      started_at  TEXT NOT NULL,
      finished_at TEXT,
      trigger     TEXT NOT NULL CHECK (trigger IN ('schedule','manual')),
      status      TEXT NOT NULL CHECK (status IN ('running','completed','failed','skipped')),
      report_file TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ttl_reviews (
      memory_id        TEXT PRIMARY KEY,
      memory_version   INTEGER NOT NULL,
      decision         TEXT NOT NULL CHECK (decision IN ('keep','uncertain')),
      uncertain_streak INTEGER NOT NULL CHECK (uncertain_streak >= 0),
      decided_at       TEXT NOT NULL,
      next_review_at   TEXT NOT NULL,
      reason           TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS scope_passes (
      scope        TEXT PRIMARY KEY,
      input_digest TEXT NOT NULL,
      finished_at  TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS source_checks (
      memory_id      TEXT PRIMARY KEY,
      input_digest   TEXT NOT NULL,
      checked_at     TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS session_summaries (
      harness            TEXT NOT NULL,
      session_id         TEXT NOT NULL,
      completed_through  TEXT,
      attempted_at       TEXT,
      input_digest       TEXT,
      records            INTEGER,
      complete           INTEGER CHECK (complete IS NULL OR complete IN (0, 1)),
      outcome            TEXT,
      progress           TEXT,
      PRIMARY KEY (harness, session_id)
    )""",
)
_RUN_COLUMNS = "run_id, started_at, finished_at, trigger, status, report_file"
_REVIEW_COLUMNS = ("memory_id, memory_version, decision, uncertain_streak, decided_at, "
                   "next_review_at, reason")
_SUMMARY_COLUMNS = ("harness, session_id, completed_through, attempted_at, input_digest, "
                    "records, complete, outcome, progress")
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"        # the fixed-width form core's now() writes
_EARLIEST = datetime(1, 1, 1, tzinfo=UTC)
_LATEST = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


@dataclass(frozen=True)
class RunRow:
    run_id: str
    started_at: str
    finished_at: str | None
    trigger: str                # 'schedule' | 'manual'
    status: str                 # 'running' | 'completed' | 'failed' | 'skipped'
    report_file: str            # the file name under <root>/dream/reports


@dataclass(frozen=True)
class ReviewRow:
    memory_id: str
    memory_version: int
    decision: str               # 'keep' | 'uncertain'
    uncertain_streak: int
    decided_at: str
    next_review_at: str
    reason: str


@dataclass(frozen=True)
class SummaryRow:
    harness: str
    session_id: str
    completed_through: str | None     # last_active_at covered by the last final outcome
    attempted_at: str | None          # orders candidates only
    input_digest: str | None          # these three describe the last final outcome only
    records: int | None
    complete: bool | None
    outcome: str | None               # the last outcome, for the report
    progress: dict | None             # the checkpoint of an unfinished long session


def input_digest(pairs: Iterable[tuple[str, int]]) -> str:
    """SHA-256 hex over PROMPT_VERSION and the sorted (memory_id, version) pairs: the
    skip key of a scope pass (§6.9) and of a source re-check (§6.6)."""
    material = [PROMPT_VERSION, sorted([memory_id, version] for memory_id, version in pairs)]
    return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()


def _parsed_progress(raw: str) -> object:
    """`raw` decoded, or the raw text itself when it is not even valid JSON (truncated
    or hand-edited data): a non-dict sentinel summarize's `_valid_progress` discards,
    rather than a `JSONDecodeError` that would fail the run before it gets the chance."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def shift_days(stamp: str, days: float) -> str:
    """`stamp`, in the fixed-width form core's now() writes, moved by `days` (negative:
    earlier), in the same form -- so the result compares with stored times as text.

    Saturates instead of overflowing: every positive day count is a valid setting, so
    a huge one lands on the earliest/latest timestamp rather than failing the run.
    The year is padded by hand: strftime's %Y is not four digits below 1000 everywhere.
    """
    start = datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
    try:
        moved = start + timedelta(days=days)
    except OverflowError:
        moved = _LATEST if days > 0 else _EARLIEST
    return f"{moved.year:04d}" + moved.strftime("-%m-%dT%H:%M:%S.%fZ")


class DreamStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # created 0600 before SQLite opens it (its journal takes the same mode);
        # O_NOFOLLOW: a symlink planted at the path is refused, never followed
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        with self._write() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, isolation_level=None,
                                     timeout=BUSY_TIMEOUT_MS / 1000)) as conn:
            yield conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _execute(self, statement: str, *params: object) -> None:
        with self._write() as conn:
            conn.execute(statement, params)

    def _rows(self, statement: str, *params: object) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute(statement, params).fetchall()

    def _runs(self, clause: str, *params: object) -> list[RunRow]:
        return [RunRow(*row) for row in self._rows(
            f"SELECT {_RUN_COLUMNS} FROM runs {clause}", *params)]

    def start_run(self, run: RunRow) -> None:
        self._execute(f"INSERT INTO runs ({_RUN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                      *astuple(run))

    def finish_run(self, run_id: str, *, status: str, finished_at: str) -> None:
        self._execute("UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                      status, finished_at, run_id)

    def running(self) -> list[RunRow]:
        return self._runs("WHERE status = 'running' ORDER BY started_at, run_id")

    def runs(self, limit: int) -> list[RunRow]:
        return self._runs("ORDER BY started_at DESC, run_id DESC LIMIT ?", limit)

    def run(self, run_id: str) -> RunRow | None:
        rows = self._runs("WHERE run_id = ?", run_id)
        return rows[0] if rows else None

    def runs_before(self, before: str) -> list[RunRow]:
        """Runs started before `before`, oldest first, for retention to delete one by one.
        A run still `running` is left out: the next run marks it failed and closes its
        report first."""
        return self._runs("WHERE started_at < ? AND status != 'running' "
                          "ORDER BY started_at, run_id", before)

    def delete_run(self, run_id: str) -> None:
        self._execute("DELETE FROM runs WHERE run_id = ?", run_id)

    def review(self, memory_id: str) -> ReviewRow | None:
        rows = self._rows(f"SELECT {_REVIEW_COLUMNS} FROM ttl_reviews WHERE memory_id = ?",
                          memory_id)
        if not rows:
            return None
        row = ReviewRow(*rows[0])
        # hand-edited or otherwise corrupt data (the CHECK constraint lets a stored
        # TEXT streak like 'x' through): treated as no review at all, never as a
        # raw-string time compare or an int arithmetic error reaching the run
        if not (is_timestamp(row.decided_at) and is_timestamp(row.next_review_at)
                and isinstance(row.uncertain_streak, int) and row.uncertain_streak >= 0):
            return None
        return row

    def put_review(self, row: ReviewRow) -> None:
        self._execute(f"INSERT OR REPLACE INTO ttl_reviews ({_REVIEW_COLUMNS}) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", *astuple(row))

    def scope_digest(self, scope: str) -> str | None:
        rows = self._rows("SELECT input_digest FROM scope_passes WHERE scope = ?", scope)
        return rows[0][0] if rows else None

    def put_scope_pass(self, scope: str, digest: str, at: str) -> None:
        self._execute("INSERT OR REPLACE INTO scope_passes (scope, input_digest, finished_at) "
                      "VALUES (?, ?, ?)", scope, digest, at)

    def source_check(self, memory_id: str) -> str | None:
        rows = self._rows("SELECT input_digest FROM source_checks WHERE memory_id = ?",
                          memory_id)
        return rows[0][0] if rows else None

    def put_source_check(self, memory_id: str, digest: str, at: str) -> None:
        self._execute("INSERT OR REPLACE INTO source_checks (memory_id, input_digest, "
                      "checked_at) VALUES (?, ?, ?)", memory_id, digest, at)

    def summary(self, harness: str, session_id: str) -> SummaryRow | None:
        rows = self._rows(f"SELECT {_SUMMARY_COLUMNS} FROM session_summaries "
                          "WHERE harness = ? AND session_id = ?", harness, session_id)
        if not rows:
            return None
        *head, complete, outcome, progress = rows[0]
        return SummaryRow(*head, None if complete is None else bool(complete), outcome,
                          None if progress is None else _parsed_progress(progress))

    def put_summary(self, row: SummaryRow) -> None:
        self._execute(
            f"INSERT OR REPLACE INTO session_summaries ({_SUMMARY_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row.harness, row.session_id, row.completed_through, row.attempted_at,
            row.input_digest, row.records, None if row.complete is None else int(row.complete),
            row.outcome,
            None if row.progress is None else json.dumps(row.progress, ensure_ascii=False))
