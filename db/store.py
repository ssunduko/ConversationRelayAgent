"""SQLite storage: who called, what was said, and what they agreed to.

This is what gives the agent memory across calls. On a second call from the
same number it can open with what it already knows instead of asking again.

Everything is keyed by the caller's phone number in E.164. Writes happen on a
worker thread so a slow disk can never stall a live call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT, SETTINGS

log = logging.getLogger("relay.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS callers (
    phone        TEXT PRIMARY KEY,
    first_name   TEXT,
    last_name    TEXT,
    email        TEXT,
    experience   TEXT,          -- what they said about their texting/calling tools
    notes        TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    phone        TEXT NOT NULL,
    call_sid     TEXT,
    direction    TEXT,          -- inbound | outbound
    mode         TEXT,          -- answering | demo
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    summary      TEXT,
    transcript   TEXT,          -- JSON array of {role, text}
    -- Knowledge-base lookups: the queries the agent wrote, what came back,
    -- and whether anything cleared the score threshold. This is what tells
    -- you which prospect questions the index cannot answer.
    searches     TEXT           -- JSON array of {query, hits, best_score, top, at}
);

CREATE TABLE IF NOT EXISTS appointments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    phone          TEXT NOT NULL,
    first_name     TEXT,
    last_name      TEXT,
    email          TEXT,
    requested_time TEXT NOT NULL,   -- as the caller said it, plus any ISO parse
    notes          TEXT,
    call_sid       TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_phone ON conversations(phone);
CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or SETTINGS.database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        """Commit on success, and always close.

        `with sqlite3.connect(...)` only manages the transaction — it leaves
        the connection open. Relying on that leaks a file handle per query.
        """
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns that CREATE TABLE IF NOT EXISTS won't add to an old table."""
        wanted = {"conversations": {"searches": "TEXT"}}
        for table, columns in wanted.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                    log.info("added %s.%s to the existing database", table, name)

    # --- synchronous core (run off-thread by the async wrappers) ---------

    def _get_caller(self, phone: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM callers WHERE phone = ?", (phone,)).fetchone()
            if row is None:
                return None
            caller = dict(row)

            caller["appointments"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT requested_time, notes, created_at FROM appointments"
                    " WHERE phone = ? ORDER BY created_at DESC LIMIT 5",
                    (phone,),
                )
            ]
            caller["previous_calls"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT started_at, mode, summary FROM conversations"
                    " WHERE phone = ? AND summary IS NOT NULL ORDER BY started_at DESC LIMIT 5",
                    (phone,),
                )
            ]
            return caller

    def _upsert_caller(self, phone: str, **fields: Any) -> None:
        known = {"first_name", "last_name", "email", "experience", "notes"}
        updates = {k: v for k, v in fields.items() if k in known and v}
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO callers (phone, first_seen, last_seen) VALUES (?, ?, ?)"
                " ON CONFLICT(phone) DO UPDATE SET last_seen = excluded.last_seen",
                (phone, now, now),
            )
            if updates:
                # Only overwrite with non-empty values, so a later call that
                # skips a field doesn't erase what an earlier one learned.
                assignments = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE callers SET {assignments} WHERE phone = ?",
                    (*updates.values(), phone),
                )

    def _start_conversation(self, phone: str, call_sid: str, direction: str, mode: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (phone, call_sid, direction, mode, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (phone, call_sid, direction, mode, _now()),
            )
            return int(cur.lastrowid)

    def _finish_conversation(
        self,
        conversation_id: int,
        transcript: list[dict],
        summary: str | None,
        searches: list[dict] | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET ended_at = ?, transcript = ?, summary = ?, searches = ?"
                " WHERE id = ?",
                (
                    _now(),
                    json.dumps(transcript, ensure_ascii=False),
                    summary,
                    json.dumps(searches or [], ensure_ascii=False),
                    conversation_id,
                ),
            )

    def _save_appointment(self, phone: str, **fields: Any) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO appointments"
                " (phone, first_name, last_name, email, requested_time, notes, call_sid, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    phone,
                    fields.get("first_name"),
                    fields.get("last_name"),
                    fields.get("email"),
                    fields.get("requested_time"),
                    fields.get("notes"),
                    fields.get("call_sid"),
                    _now(),
                ),
            )
            return int(cur.lastrowid)

    # --- async wrappers --------------------------------------------------
    # sqlite3 is blocking; a live call must never wait on the filesystem.

    async def get_caller(self, phone: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_caller, phone)

    async def upsert_caller(self, phone: str, **fields: Any) -> None:
        await asyncio.to_thread(self._upsert_caller, phone, **fields)

    async def start_conversation(self, phone: str, call_sid: str, direction: str, mode: str) -> int:
        return await asyncio.to_thread(self._start_conversation, phone, call_sid, direction, mode)

    async def finish_conversation(
        self,
        conversation_id: int,
        transcript: list[dict],
        summary: str | None = None,
        searches: list[dict] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._finish_conversation, conversation_id, transcript, summary, searches
        )

    async def save_appointment(self, phone: str, **fields: Any) -> int:
        return await asyncio.to_thread(self._save_appointment, phone, **fields)


STORE = Store()


def describe_caller(caller: dict[str, Any] | None) -> str:
    """Turn a stored caller record into a line the agent can act on."""
    if not caller:
        return ""

    known: list[str] = []
    name = " ".join(x for x in (caller.get("first_name"), caller.get("last_name")) if x)
    if name:
        known.append(f"name {name}")
    if caller.get("email"):
        known.append(f"email {caller['email']}")
    if caller.get("experience"):
        known.append(f"tools they mentioned: {caller['experience']}")

    lines = ["You have spoken with this caller before."]
    if known:
        lines.append("Already on file: " + "; ".join(known) + ".")
        lines.append(
            "Do not ask for anything already on file — confirm it instead, and only"
            " if it matters for what they're asking."
        )
    for appt in caller.get("appointments", [])[:2]:
        lines.append(f"Existing appointment request: {appt['requested_time']} (booked {appt['created_at'][:10]}).")
    for call in caller.get("previous_calls", [])[:2]:
        if call.get("summary"):
            lines.append(f"Earlier call ({call['started_at'][:10]}): {call['summary']}")
    return "\n".join(lines)
