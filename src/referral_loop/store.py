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

  * DDL disarms the log. DROP TRIGGER removes a guard while leaving the table
    looking intact; DROP TABLE removes both at once; ALTER TABLE ... RENAME
    moves the rows out from under the triggers. All three were confirmed to
    succeed from a foreign connection. SQLite has no in-file DDL permission
    model, so none of them can be blocked from inside the file.
  * REPLACE INTO performs its implicit delete without firing a BEFORE DELETE
    trigger unless recursive_triggers is on, and that pragma is per-connection.
    _connect() sets it, so every path through LoopStore is covered; a foreign
    connection that does not set it is not.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .errors import LoopNotFoundError, ReservedStateError, StoreUnavailableError
from .events import Loop, LoopEvent, LoopState

logger = logging.getLogger(__name__)

# States that mean "still waiting on a result". Derived from LoopState rather
# than written out as SQL string literals, so renaming a state cannot leave a
# stale literal silently matching nothing.
_OPEN_STATES = (LoopState.OPEN, LoopState.SCHEDULED)

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
    "acknowledged": LoopState.ACKNOWLEDGED,
    "cancelled": LoopState.CANCELLED,
    "orphaned": LoopState.ORPHAN,
    "reopened": LoopState.RESULTED,
    "reversed": LoopState.RESULTED,   # a coordinator undoing their own ack
    "dismissed": LoopState.DISMISSED,
}

# No event type maps to LoopState.CLOSED, and that is the whole of the v1
# guarantee: state comes only from replaying this mapping, so a state absent
# from its values cannot be reached by any message, coordinator action or
# replay path. Asserted by test_no_event_type_maps_to_closed and swept for by
# spec test 5, not by a bare `assert` here -- an assert vanishes under python -O,
# and a mutation of this table has to fail a test rather than an import.

# Event types that would enter a state reserved for v2. Refused by name so the
# failure is legible: "closed" is what code written before the ACKNOWLEDGED /
# CLOSED split emits, and a generic unknown-event-type error would send its
# author looking for a typo rather than reading spec section 4.
_RESERVED_V2_EVENTS = {"closed"}


def _authorizer(action_code: int, arg1, arg2, *_args):
    """Block UPDATE and DELETE on loop_events. Raw archive stays immutable too.

    Defence in depth only. The schema triggers above are the real guarantee --
    an authorizer is per-connection and stops nothing that opens the file
    without one.
    """
    if action_code in (sqlite3.SQLITE_DELETE, sqlite3.SQLITE_UPDATE) and arg1 in (
        "loop_events",
        "raw_messages",
    ):
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
        """Open a connection, or raise StoreUnavailableError. Never a raw sqlite3 error.

        Every failure this module can suffer has to reach the listener as a
        StoreUnavailableError, because that is what Task 10 catches to answer AE
        and let the engine queue and retry. sqlite3.connect() is lazy -- the file
        is really opened by the first statement, which is the PRAGMA below -- so
        disk full, unmounted volume and permission denied all surface here.
        Those are precisely the cases the AE path exists for, and an untyped
        OperationalError would sail straight past its handler.
        """
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            # Pinned, not left to SQLite's defaults. These currently match the
            # defaults, which means nothing would fail if a later change flipped
            # them -- and persist-before-ACK would quietly stop holding.
            conn.execute("PRAGMA synchronous = FULL")       # fsync on commit
            conn.execute("PRAGMA recursive_triggers = ON")  # so REPLACE fires BEFORE DELETE
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise StoreUnavailableError(f"Cannot open store at {self.db_path}: {exc}") from exc

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

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a read and close the connection, typing any failure."""
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"Store read failed: {exc}") from exc
        finally:
            conn.close()

    def raw_count(self) -> int:
        return self._read("SELECT COUNT(*) FROM raw_messages")[0][0]

    def append_event(self, event: LoopEvent) -> None:
        """Append an event and refresh its projection in ONE transaction.

        The event log is authoritative, but open_loops() reads ids from the
        loops projection. Committing the event and the projection separately
        left a window where a crash produced a brand-new referral loop that was
        durably in the log yet invisible on the coordinator's worklist -- the
        exact failure this product exists to prevent. Same connection, one
        commit: they land together or not at all.
        """
        if event.event_type in _RESERVED_V2_EVENTS:
            # Failure matrix: attempted transition to CLOSED is refused and
            # logged. This is the choke point every event passes through, from
            # the registry or from anywhere else, so guarding here covers paths
            # no future caller has written yet.
            logger.error(
                "Refused reserved event_type %r for loop %s: CLOSED is v1-unreachable",
                event.event_type, event.loop_id,
            )
            raise ReservedStateError(
                f"Event type {event.event_type!r} would enter a state reserved for v2. "
                "v1 stops at ACKNOWLEDGED, which claims a coordinator matched the result "
                "to the loop; CLOSED claims a clinically responsible actor dispositioned "
                "the finding, and nothing in v1 observes that. Use 'acknowledged'."
            )
        if event.event_type not in _EVENT_STATE and event.event_type not in _NON_TRANSITIONAL:
            # Validate before the insert, not after. replay() also rejects
            # unknown types, but by then the event is committed and the log is
            # append-only -- a single typo would leave the loop permanently
            # unreplayable with no way to correct it.
            raise StoreUnavailableError(
                f"Refusing to append unknown event_type {event.event_type!r}: "
                f"it would make loop {event.loop_id} permanently unreplayable"
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
                # Same connection, so this joins the transaction the INSERT
                # opened and is covered by the single commit below.
                self._materialize(event.loop_id, conn)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreUnavailableError(f"Event append failed: {exc}") from exc
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    _EVENTS_SQL = "SELECT * FROM loop_events WHERE loop_id = ? ORDER BY event_id"

    @staticmethod
    def _rows_to_events(rows) -> list[LoopEvent]:
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

    def events_for(self, loop_id: str) -> list[LoopEvent]:
        """Events in arrival order (event_id), deliberately not occurred_at.

        Each event was accepted by the registry, which validated the transition
        at the moment it was applied, so arrival order IS the authoritative
        accepted sequence and replay must reproduce exactly what the system did.
        Reordering by occurred_at would make replay show a history that never
        happened. Rejecting a clinically-older message that arrives late is the
        registry's job, before the event is ever appended -- not the store's.
        """
        return self._rows_to_events(self._read(self._EVENTS_SQL, (loop_id,)))

    def _replay_on(self, conn: sqlite3.Connection, loop_id: str) -> Loop:
        """Replay through a caller-owned connection, so an in-flight
        transaction sees its own uncommitted event."""
        rows = conn.execute(self._EVENTS_SQL, (loop_id,)).fetchall()
        return self._build_loop(loop_id, self._rows_to_events(rows))

    def replay(self, loop_id: str) -> Loop:
        """Reconstruct a loop from its events alone. Spec test 10."""
        return self._build_loop(loop_id, self.events_for(loop_id))

    def _build_loop(self, loop_id: str, events: list[LoopEvent]) -> Loop:
        if not events:
            raise LoopNotFoundError(f"No events for loop {loop_id}")

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
            if event.event_type in _RESERVED_V2_EVENTS:
                # A restored or foreign-written log. Refusing to replay is the
                # point: silently mapping it to ACKNOWLEDGED would let a v2 claim
                # in the log be read back as v1's weaker one.
                raise ReservedStateError(
                    f"Loop {loop_id} holds a reserved event_type {event.event_type!r}; "
                    "CLOSED is not reachable in v1 and this log cannot be replayed"
                )
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

    def _materialize(self, loop_id: str, conn: sqlite3.Connection) -> None:
        """Refresh the loops row from the event log. Never the source of truth.

        The caller owns the transaction and the commit, so this can be enlisted
        in the same one as the event insert that prompted it.
        """
        loop = self._replay_on(conn, loop_id)
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

    def rebuild_projection(self) -> int:
        """Rebuild every loops row from the event log. Returns loops rebuilt.

        The loops table is a projection, so it must be reconstructible. Without
        this, a restore that replays loop_events into a fresh file leaves the
        worklist empty -- open_loops() returns nothing while the events are
        sitting right there. A loop vanishing silently from the worklist is the
        exact failure this product exists to prevent.
        """
        with self._lock:
            conn = self._connect()
            try:
                loop_ids = [
                    r["loop_id"]
                    for r in conn.execute(
                        "SELECT DISTINCT loop_id FROM loop_events ORDER BY loop_id"
                    ).fetchall()
                ]
                for loop_id in loop_ids:
                    self._materialize(loop_id, conn)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreUnavailableError(f"Projection rebuild failed: {exc}") from exc
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return len(loop_ids)

    def _loops_where(self, clause: str, params: tuple) -> list[Loop]:
        conn = self._connect()
        try:
            ids = [
                r["loop_id"]
                for r in conn.execute(
                    f"SELECT loop_id FROM loops WHERE {clause}", params
                ).fetchall()
            ]
            return [self._replay_on(conn, i) for i in ids]
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"Store read failed: {exc}") from exc
        finally:
            conn.close()

    def open_loops(self, mrn: str | None = None) -> list[Loop]:
        """Loops still awaiting a result. Not the whole worklist -- a RESULTED
        loop nobody has acknowledged yet is in resulted_unacknowledged()."""
        placeholders = ", ".join("?" * len(_OPEN_STATES))
        clause = f"state IN ({placeholders})"
        params: tuple = tuple(s.value for s in _OPEN_STATES)
        if mrn:
            clause += " AND mrn = ?"
            params += (mrn,)
        return self._loops_where(clause, params)

    def resulted_unacknowledged(self, mrn: str | None = None) -> list[Loop]:
        """Resulted loops with no acknowledgement, including reopened ones.

        "reopened" maps to RESULTED, which open_loops() does not select, so a
        loop whose acknowledgement was just cleared by safety rule 2 -- a
        corrected result, the population most needing a human -- was returned by
        nothing but unfiltered all_loops(). ack_at is '' when an event cleared
        it and NULL when it was never set; both mean unacknowledged.
        """
        clause = "state = ? AND (ack_at IS NULL OR ack_at = '')"
        params: tuple = (LoopState.RESULTED.value,)
        if mrn:
            clause += " AND mrn = ?"
            params += (mrn,)
        return self._loops_where(clause, params)

    def all_loops(self) -> list[Loop]:
        return self._loops_where("1 = 1", ())
