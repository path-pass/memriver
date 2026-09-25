"""`SessionStore` over the SQLite database: one row per harness session."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar, get_args

from memriver_core.models import (
    ID_RE,
    PromptEntry,
    Session,
    SessionKey,
    SessionOrigin,
    SessionStatus,
    is_call_id,
    is_timestamp,
)
from memriver_core.models.errors import ProjectUnavailable, StorageFailure

from .database import Database
from .database import dumps_json as _dumps
from .database import loads_json as _loads

SESSION_COLUMNS = ("harness, session_id, status, origin, project_id, candidate_id, "
                   "candidate_root, entry_cwd, branch, transcript_path, started_at, "
                   "last_active_at, ended_at, prompt_count, last_write_prompt_count, "
                   "last_nudge_prompt_count, first_prompt, recent_prompts")
_PLACEHOLDERS = ", ".join("?" for _ in SESSION_COLUMNS.split(","))
_BY_KEY = " WHERE harness = ? AND session_id = ?"
_SELECT = f"SELECT {SESSION_COLUMNS} FROM sessions"
_INSERT = (f"INSERT INTO sessions ({SESSION_COLUMNS}) VALUES ({_PLACEHOLDERS}) "
           "ON CONFLICT(harness, session_id) DO NOTHING")
# every column but the key, rewritten from the validated row
_UPDATE = ("UPDATE sessions SET "
           + ", ".join(f"{column.strip()} = ?" for column in SESSION_COLUMNS.split(",")[2:])
           + _BY_KEY)

_UPSERT_CALL = ("INSERT INTO tool_calls (harness, call_id, session_id, recorded_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(harness, call_id) DO UPDATE SET "
                "session_id = excluded.session_id, recorded_at = excluded.recorded_at")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

_T = TypeVar("_T")


def _entry_object(entry: PromptEntry) -> dict[str, str]:
    if entry.text is not None:
        return {"at": entry.at, "text": entry.text}
    return {"at": entry.at, "omitted": entry.omitted}


def _entry_from_object(value: object) -> PromptEntry:
    if not isinstance(value, dict) or set(value) not in ({"at", "text"}, {"at", "omitted"}):
        raise ValueError("a prompt entry is not {at, text} or {at, omitted}")
    return PromptEntry(**value)


def session_to_row(session: Session) -> tuple:
    first_prompt = None if session.first_prompt is None else _dumps(
        _entry_object(session.first_prompt))
    return (session.key.harness, session.key.session_id, session.status, session.origin,
            session.project_id, session.candidate_id, session.candidate_root,
            session.entry_cwd, session.branch, session.transcript_path, session.started_at,
            session.last_active_at, session.ended_at, session.prompt_count,
            session.last_write_prompt_count, session.last_nudge_prompt_count, first_prompt,
            _dumps([_entry_object(entry) for entry in session.recent_prompts]))


def session_from_row(row: Sequence[object]) -> Session:
    """A validated Session; ValueError for a row memriver could not have written."""
    (harness, session_id, status, origin, project_id, candidate_id, candidate_root,
     entry_cwd, branch, transcript_path, started_at, last_active_at, ended_at,
     prompt_count, last_write_prompt_count, last_nudge_prompt_count, first_prompt,
     recent_prompts) = row
    key = SessionKey(harness, session_id)
    if status not in get_args(SessionStatus) or origin not in get_args(SessionOrigin):
        raise ValueError("unknown status or origin")
    for project in (project_id, candidate_id):
        if project is not None and not (isinstance(project, str) and ID_RE.fullmatch(project)):
            raise ValueError("a project id is not addressable")
    if status == "pending" and (project_id is not None or origin != "first-seen"):
        raise ValueError("a pending row holds a project")
    if not isinstance(entry_cwd, str) or not all(
            value is None or isinstance(value, str)
            for value in (candidate_root, branch, transcript_path)):
        raise ValueError("a text column holds something else")
    if not (is_timestamp(started_at) and is_timestamp(last_active_at)
            and (ended_at is None or is_timestamp(ended_at))):
        raise ValueError("a time column is not a timestamp")
    counters = (prompt_count, last_write_prompt_count, last_nudge_prompt_count)
    if not all(type(counter) is int and counter >= 0 for counter in counters):
        raise ValueError("a counter is not a non-negative integer")
    if last_write_prompt_count > prompt_count or last_nudge_prompt_count > prompt_count:
        raise ValueError("a watermark is above the prompt count")
    recent = _loads(recent_prompts)
    if not isinstance(recent, list):
        raise ValueError("recent_prompts is not a list")  # noqa: TRY004 - a bad row
    return Session(
        key=key, status=status, origin=origin, project_id=project_id,
        candidate_id=candidate_id, candidate_root=candidate_root, entry_cwd=entry_cwd,
        branch=branch, transcript_path=transcript_path, started_at=started_at,
        last_active_at=last_active_at, ended_at=ended_at, prompt_count=prompt_count,
        last_write_prompt_count=last_write_prompt_count,
        last_nudge_prompt_count=last_nudge_prompt_count,
        first_prompt=None if first_prompt is None else _entry_from_object(_loads(first_prompt)),
        recent_prompts=tuple(_entry_from_object(entry) for entry in recent))


def _checked_row(session: Session) -> tuple:
    """The row for `session`; a row the read path would reject is never written."""
    row = session_to_row(session)
    session_from_row(row)
    return row


def _stored(conn: sqlite3.Connection | None, key: SessionKey) -> Session | None:
    if conn is None:
        return None
    row = conn.execute(_SELECT + _BY_KEY, (key.harness, key.session_id)).fetchone()
    if row is None:
        return None
    try:
        return session_from_row(row)
    except ValueError as err:
        raise StorageFailure from err       # damage: the session gets no project access


def _save(conn: sqlite3.Connection, session: Session) -> Session:
    row = _checked_row(session)
    conn.execute(_UPDATE, (*row[2:], *row[:2]))
    return session


def _require_timestamp(at: object) -> None:
    # checked up front: max() with the stored value would hide a bad one
    if not is_timestamp(at):
        raise ValueError("at is not a timestamp")


def _matches(session: Session, needle: str) -> bool:
    prompts = (session.first_prompt, *session.recent_prompts)
    texts = [entry.text for entry in prompts if entry is not None and entry.text is not None]
    texts += [session.entry_cwd, session.branch or ""]
    return any(needle in text.lower() for text in texts)


class SqliteSessionStore:
    def __init__(self, root: Path, *, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self._database = Database(self.root, busy_timeout_ms=busy_timeout_ms)

    def store_exists(self) -> bool:
        return self._database.exists()

    def get(self, key: SessionKey) -> Session | None:
        with self._database.read() as conn:
            return _stored(conn, key)

    def register(self, session: Session) -> Session | None:
        row = _checked_row(session)

        def insert(conn: sqlite3.Connection) -> Session | None:
            conn.execute(_INSERT, row)
            return _stored(conn, session.key)
        return self._write(insert)

    def touch(self, key: SessionKey, at: str, *,
              transcript_path: str | None = None) -> Session | None:
        _require_timestamp(at)

        def touch(conn: sqlite3.Connection) -> Session | None:
            stored = _stored(conn, key)
            if stored is None:
                return None
            return _save(conn, dataclasses.replace(
                stored, last_active_at=max(stored.last_active_at, at),
                transcript_path=(stored.transcript_path if transcript_path is None
                                 else transcript_path)))
        return self._write(touch)

    def add_prompt(self, key: SessionKey, entry: PromptEntry, *, seed: Session,
                   keep_recent: int) -> tuple[Session, bool] | None:
        if seed.key != key:
            raise ValueError("the seed is another session's")
        if type(keep_recent) is not int or keep_recent < 1:
            raise ValueError("keep_recent is not a positive integer")
        seed_row = _checked_row(seed)

        def add(conn: sqlite3.Connection) -> tuple[Session, bool]:
            # the INSERT's own row count: exactly one of several concurrent
            # first prompts sees 1
            created = conn.execute(_INSERT, seed_row).rowcount == 1
            stored = _stored(conn, key)
            session = _save(conn, dataclasses.replace(
                stored, prompt_count=stored.prompt_count + 1,
                first_prompt=entry if stored.first_prompt is None else stored.first_prompt,
                recent_prompts=(*stored.recent_prompts, entry)[-keep_recent:],
                last_active_at=max(stored.last_active_at, entry.at)))
            return session, created
        return self._write(add)

    def end(self, key: SessionKey, at: str) -> None:
        _require_timestamp(at)

        def end(conn: sqlite3.Connection) -> None:
            stored = _stored(conn, key)
            if stored is not None:
                _save(conn, dataclasses.replace(
                    stored, ended_at=at, last_active_at=max(stored.last_active_at, at)))
        self._write(end)

    def nudge_if_due(self, key: SessionKey, at: str, *, min_prompts: int,
                     interval: int) -> bool:
        _require_timestamp(at)

        def nudge(conn: sqlite3.Connection) -> bool:
            stored = _stored(conn, key)
            if stored is None or stored.status != "registered":
                return False
            count = stored.prompt_count
            due = (stored.project_id is not None
                   and count - stored.last_write_prompt_count >= min_prompts
                   and (stored.last_nudge_prompt_count == 0
                        or count - stored.last_nudge_prompt_count >= interval))
            _save(conn, dataclasses.replace(
                stored, last_active_at=max(stored.last_active_at, at),
                last_nudge_prompt_count=count if due else stored.last_nudge_prompt_count))
            return due
        return bool(self._write(nudge))

    def mark_saved(self, key: SessionKey) -> None:
        self._write(lambda conn: conn.execute(
            "UPDATE sessions SET last_write_prompt_count = prompt_count" + _BY_KEY,
            (key.harness, key.session_id)))

    def confirm(self, key: SessionKey) -> Session | None:
        def confirm(conn: sqlite3.Connection) -> Session | None:
            stored = _stored(conn, key)
            if stored is None or stored.status == "registered":
                return stored
            if stored.candidate_id is not None:
                # the filesystem side was checked by the caller; the store's
                # side is decided here, under the write lock
                project = conn.execute("SELECT root, is_global FROM projects WHERE id = ?",
                                       (stored.candidate_id,)).fetchone()
                if project is None or tuple(project) != (stored.candidate_root, 0):
                    raise ProjectUnavailable(reason="candidate-changed")
            return _save(conn, dataclasses.replace(stored, status="registered",
                                                   project_id=stored.candidate_id))
        return self._write(confirm)

    def assign_project(self, key: SessionKey, project_id: str) -> Session | None:
        def assign(conn: sqlite3.Connection) -> Session | None:
            stored = _stored(conn, key)
            # decided under the write lock: a project set meanwhile is never
            # overwritten, and a candidate is session_confirm's to decide
            if stored is None or stored.project_id is not None \
                    or stored.candidate_id is not None:
                return stored
            return _save(conn, dataclasses.replace(stored, status="registered",
                                                   project_id=project_id))
        return self._write(assign)

    def record_call(self, key: SessionKey, call_id: str, at: str, *,
                    retention_s: int) -> None:
        if not is_call_id(call_id):
            raise ValueError("invalid call id")
        _require_timestamp(at)
        if type(retention_s) is not int or retention_s < 1:
            raise ValueError("retention_s is not a positive integer")
        # the same fixed-width form, so the prune compares text chronologically
        expired = (datetime.strptime(at, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
                   - timedelta(seconds=retention_s)).strftime(_TIMESTAMP_FORMAT)

        def record(conn: sqlite3.Connection) -> None:
            conn.execute(_UPSERT_CALL, (key.harness, call_id, key.session_id, at))
            conn.execute("DELETE FROM tool_calls WHERE recorded_at < ?", (expired,))
        self._write(record)

    def session_for_call(self, harness: str, call_id: str) -> SessionKey | None:
        if not is_call_id(call_id):
            return None                     # an id no call can have names no session
        with self._database.read() as conn:
            if conn is None:
                return None
            row = conn.execute("SELECT session_id FROM tool_calls "
                               "WHERE harness = ? AND call_id = ?", (harness, call_id)).fetchone()
        if row is None:
            return None
        try:
            return SessionKey(harness, row[0])
        except ValueError:
            return None                     # a bad row routes nothing

    def search(self, project_id: str | None, query: str, limit: int) -> list[Session]:
        sql, params = _SELECT, ()
        if project_id is not None:
            sql, params = sql + " WHERE project_id = ? AND status = 'registered'", (project_id,)
        needle = query.lower()
        found: list[Session] = []
        with self._database.read() as conn:
            if conn is None:
                return []
            for row in conn.execute(sql + " ORDER BY last_active_at DESC, harness, session_id",
                                    params):
                if len(found) >= limit:
                    break
                try:
                    session = session_from_row(row)
                except ValueError:
                    continue                # a bad row is skipped here, a doctor finding
                if _matches(session, needle):
                    found.append(session)
        return found

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T | None:
        """`operation` in one write transaction; None when the store is absent.

        Nothing here creates a store (spec §3.3): the write opens without
        creating, and a store removed between the check and the connect is the
        same no-op as one that was never there. Any other failure propagates.
        """
        if not self._database.exists():
            return None
        try:
            with self._database.write(create=False) as conn:
                return operation(conn)
        except StorageFailure:
            if not self._database.exists():
                return None
            raise
