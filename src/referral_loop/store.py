"""Three tables: raw_messages, loops, loop_events.

loop_events is append-only and authoritative -- any loop's state is derivable by
replaying it. The loops table is a materialized convenience for the worklist
query and carries no information the event log does not.

"Encrypted" here means a plain SQLite file on an OS-encrypted volume, attested
at startup by encryption_check.verify_encryption_at_rest. Same definition the
rest of the codebase uses.

Scope of the append-only guarantee, for the audit story. Schema triggers reject
DELETE and UPDATE on loop_events and raw_messages from every connection, ours
or any other process's. Two gaps remain, and both are bounded by filesystem
permissions on the database file rather than by anything this module can
express -- say so plainly in any control narrative rather than claiming the
file is tamper-proof:

  * DROP TABLE removes the triggers with the table. SQLite has no in-file DDL
    permission model.
  * REPLACE INTO performs its implicit delete without firing a BEFORE DELETE
    trigger unless recursive_triggers is on, and that pragma is per-connection.
    _connect() sets it, so every path through LoopStore is covered; a foreign
    connection that does not set it is not.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .errors import StoreUnavailableError
from .events import Loop, LoopEvent, LoopState

_SQLITE_DELETE = 9
_SQLITE_UPDATE = 23

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    control_id  TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id     TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    control_id  TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_loop ON loop_events(loop_id);
CREATE TABLE IF NOT EXISTS loops (
    loop_id           TEXT PRIMARY KEY,
    mrn               TEXT NOT NULL,
    state             TEXT NOT NULL,
    placer_order_number TEXT DEFAULT '',
    filler_order_number TEXT DEFAULT '',
    service_code      TEXT DEFAULT '',
    modality          TEXT DEFAULT '',
    ordering_provider TEXT DEFAULT '',
    ordered_at        TEXT,
    ack_by            TEXT DEFAULT '',
    ack_role          TEXT DEFAULT '',
    ack_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_loops_mrn ON loops(mrn);
CREATE INDEX IF NOT EXISTS idx_loops_state ON loops(state);

-- Append-only enforcement lives in the schema, not in the connection.
-- _authorizer only binds to connections LoopStore itself opens; any other
-- process opening this file would bypass it entirely. These triggers travel
-- with the database file and apply to every connection, ours or not.
CREATE TRIGGER IF NOT EXISTS loop_events_no_delete BEFORE DELETE ON loop_events
BEGIN SELECT RAISE(ABORT, 'loop_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS loop_events_no_update BEFORE UPDATE ON loop_events
BEGIN SELECT RAISE(ABORT, 'loop_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS raw_messages_no_delete BEFORE DELETE ON raw_messages
BEGIN SELECT RAISE(ABORT, 'raw_messages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS raw_messages_no_update BEFORE UPDATE ON raw_messages
BEGIN SELECT RAISE(ABORT, 'raw_messages is append-only'); END;
"""

# Event types that deliberately carry fields without changing state.
_NON_TRANSITIONAL = frozenset({"merged_in"})

# Event type -> resulting state. Replay applies these in order.
# No None values: a missing key must mean "unknown", and an unknown event type
# is a replay failure, not a silent no-op. Mapping "merged_in" to None made
# those two cases indistinguishable, so a typo'd event_type left the loop
# silently in its prior state.
_EVENT_STATE = {
    "created": LoopState.OPEN,
    "scheduled": LoopState.SCHEDULED,
    "resulted": LoopState.RESULTED,
    "closed": LoopState.CLOSED,
    "cancelled": LoopState.CANCELLED,
    "orphaned": LoopState.ORPHAN,
    "reopened": LoopState.RESULTED,
}


def _authorizer(action_code: int, arg1, arg2, *_args):
    """Block UPDATE and DELETE on loop_events. Raw archive stays immutable too.

    Defence in depth only. The schema triggers above are the real guarantee --
    an authorizer is per-connection and stops nothing that opens the file
    without one.
    """
    if action_code in (_SQLITE_DELETE, _SQLITE_UPDATE) and arg1 in ("loop_events", "raw_messages"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class LoopStore:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        conn = None
        try:
            conn = self._connect()
            conn.executescript(_SCHEMA)
            conn.commit()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"Cannot initialize store at {self.db_path}: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Pinned, not left to SQLite's defaults. These currently match the
        # defaults, which means nothing would fail if a later change flipped
        # them -- and persist-before-ACK would quietly stop holding.
        conn.execute("PRAGMA synchronous = FULL")       # fsync on commit
        conn.execute("PRAGMA recursive_triggers = ON")  # so REPLACE fires BEFORE DELETE
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _guarded(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.set_authorizer(_authorizer)
        return conn

    def record_raw(self, control_id: str, payload: str) -> bool:
        """Durably persist the raw message. Returns False if already seen.

        This must complete before any ACK. Acknowledging then crashing during
        parse means the engine considers the message delivered and it is gone.
        """
        if not control_id:
            # SQLite permits repeated NULLs in a TEXT primary key, so a missing
            # MSH-10 would insert a fresh row every time and dedup would fail
            # silently. A message we cannot key is a message we cannot promise
            # not to double-process; the listener must answer AE, not AA.
            raise StoreUnavailableError(
                "Refusing to store a message with an empty control id (MSH-10): "
                "idempotency cannot be guaranteed without it"
            )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO raw_messages (control_id, payload, received_at) VALUES (?, ?, ?)",
                    (control_id, payload, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError as exc:
                # Only a genuine duplicate control id is a no-op. Any other
                # constraint failure (NOT NULL, etc.) means nothing was stored,
                # and returning False would let the listener ACK a message that
                # does not exist. Confirm the row is really there before we
                # claim "already seen".
                seen = conn.execute(
                    "SELECT 1 FROM raw_messages WHERE control_id = ?", (control_id,)
                ).fetchone()
                if seen is None:
                    raise StoreUnavailableError(f"Durable write failed: {exc}") from exc
                return False
            except sqlite3.Error as exc:
                raise StoreUnavailableError(f"Durable write failed: {exc}") from exc
            finally:
                conn.close()

    def raw_count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
        finally:
            conn.close()

    def append_event(self, event: LoopEvent) -> None:
        if event.event_type not in _EVENT_STATE and event.event_type not in _NON_TRANSITIONAL:
            # Validate before the insert, not after. replay() also rejects
            # unknown types, but by then the event is committed and the log is
            # append-only -- a single typo would leave the loop permanently
            # unreplayable with no way to correct it.
            raise StoreUnavailableError(
                f"Refusing to append unknown event_type {event.event_type!r}: "
                "it would make loop " + f"{event.loop_id} permanently unreplayable"
            )
        with self._lock:
            conn = self._guarded()
            try:
                conn.execute(
                    "INSERT INTO loop_events (loop_id, event_type, occurred_at, control_id, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        event.loop_id,
                        event.event_type,
                        event.occurred_at.isoformat(),
                        event.control_id,
                        json.dumps(event.detail, sort_keys=True),
                    ),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise StoreUnavailableError(f"Event append failed: {exc}") from exc
            finally:
                conn.close()
            self._materialize(event.loop_id)

    def events_for(self, loop_id: str) -> list[LoopEvent]:
        """Events in arrival order (event_id), deliberately not occurred_at.

        Each event was accepted by the registry, which validated the transition
        at the moment it was applied, so arrival order IS the authoritative
        accepted sequence and replay must reproduce exactly what the system did.
        Reordering by occurred_at would make replay show a history that never
        happened. Rejecting a clinically-older message that arrives late is the
        registry's job, before the event is ever appended -- not the store's.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM loop_events WHERE loop_id = ? ORDER BY event_id", (loop_id,)
            ).fetchall()
        finally:
            conn.close()
        return [
            LoopEvent(
                loop_id=r["loop_id"],
                event_type=r["event_type"],
                occurred_at=datetime.fromisoformat(r["occurred_at"]),
                control_id=r["control_id"],
                detail=json.loads(r["detail"]),
            )
            for r in rows
        ]

    def replay(self, loop_id: str) -> Loop:
        """Reconstruct a loop from its events alone. Spec test 10."""
        events = self.events_for(loop_id)
        if not events:
            raise KeyError(f"No events for loop {loop_id}")

        state = LoopState.OPEN
        attrs: dict = {}
        for event in events:
            # No falsy filter. An event's detail contains exactly the fields it
            # intends to change, so updating wholesale leaves everything else
            # alone -- and lets an event deliberately CLEAR a field. Filtering
            # out "" made it impossible for a corrected result to clear a prior
            # acknowledgement, which is safety rule 2.
            attrs.update(event.detail)
            if event.event_type in _NON_TRANSITIONAL:
                continue
            if event.event_type not in _EVENT_STATE:
                raise StoreUnavailableError(
                    f"Unknown event_type {event.event_type!r} in loop {loop_id}; "
                    "the event log cannot be replayed and state is not derivable"
                )
            state = _EVENT_STATE[event.event_type]

        # "" means the field was deliberately cleared; treat it as absent rather
        # than handing an empty string to fromisoformat.
        ordered_at = attrs.get("ordered_at")
        ack_at = attrs.get("ack_at")
        return Loop(
            loop_id=loop_id,
            mrn=attrs.get("mrn", ""),
            state=state,
            placer_order_number=attrs.get("placer_order_number", ""),
            filler_order_number=attrs.get("filler_order_number", ""),
            service_code=attrs.get("service_code", ""),
            modality=attrs.get("modality", ""),
            ordering_provider=attrs.get("ordering_provider", ""),
            ordered_at=datetime.fromisoformat(ordered_at) if ordered_at else None,
            ack_by=attrs.get("ack_by", ""),
            ack_role=attrs.get("ack_role", ""),
            ack_at=datetime.fromisoformat(ack_at) if ack_at else None,
        )

    def _materialize(self, loop_id: str) -> None:
        """Refresh the loops row from the event log. Never the source of truth."""
        loop = self.replay(loop_id)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO loops (loop_id, mrn, state, placer_order_number, "
                "filler_order_number, service_code, modality, ordering_provider, ordered_at, "
                "ack_by, ack_role, ack_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    loop.loop_id, loop.mrn, loop.state.value, loop.placer_order_number,
                    loop.filler_order_number, loop.service_code, loop.modality,
                    loop.ordering_provider,
                    loop.ordered_at.isoformat() if loop.ordered_at else None,
                    loop.ack_by, loop.ack_role,
                    loop.ack_at.isoformat() if loop.ack_at else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def rebuild_projection(self) -> int:
        """Rebuild every loops row from the event log. Returns loops rebuilt.

        The loops table is a projection, so it must be reconstructible. Without
        this, a restore that replays loop_events into a fresh file leaves the
        worklist empty -- open_loops() returns nothing while the events are
        sitting right there. A loop vanishing silently from the worklist is the
        exact failure this product exists to prevent.
        """
        conn = self._connect()
        try:
            loop_ids = [
                r["loop_id"]
                for r in conn.execute(
                    "SELECT DISTINCT loop_id FROM loop_events ORDER BY loop_id"
                ).fetchall()
            ]
        finally:
            conn.close()
        with self._lock:
            for loop_id in loop_ids:
                self._materialize(loop_id)
        return len(loop_ids)

    def open_loops(self, mrn: str | None = None) -> list[Loop]:
        query = "SELECT loop_id FROM loops WHERE state IN ('OPEN', 'SCHEDULED')"
        params: tuple = ()
        if mrn:
            query += " AND mrn = ?"
            params = (mrn,)
        conn = self._connect()
        try:
            ids = [r["loop_id"] for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()
        return [self.replay(i) for i in ids]

    def all_loops(self) -> list[Loop]:
        conn = self._connect()
        try:
            ids = [r["loop_id"] for r in conn.execute("SELECT loop_id FROM loops").fetchall()]
        finally:
            conn.close()
        return [self.replay(i) for i in ids]
