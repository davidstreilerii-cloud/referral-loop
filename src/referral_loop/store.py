"""Four tables: raw_messages, loops, loop_events, mrn_alias_events.

loop_events is append-only and authoritative -- any loop's state is derivable by
replaying it. The loops table is a materialized convenience for the worklist
query and carries no information the event log does not.

mrn_alias_events is the second authoritative log: which patient identifiers have
been retired in favour of which. It is append-only for the same reason, and the
triggers below cover it. A loop's identity is therefore two facts -- the MRN in
its event log, and the alias history that MRN resolves through -- and both have
to survive a restore for the worklist to be correct.

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

from .errors import (
    CircularMergeError,
    LoopNotFoundError,
    ReferralLoopError,
    ReservedStateError,
    StoreUnavailableError,
)
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

-- Which messages have been *applied*, and what content they carried. Both
-- idempotency paths of spec section 6 live in this one table, and it is
-- deliberately not raw_messages.
--
--   * raw_messages answers "has this arrived", which is what the archive is
--     for. applied_messages answers "has this been acted on", which is a
--     strictly later fact. Keying dedup on arrival loses a message whenever a
--     store failure lands between the two: the raw is durable, the engine gets
--     AE, it redelivers under the same MSH-10, and an arrival-keyed check
--     no-ops a message that never produced a transition. A referral loop that
--     silently never opened is exactly the failure this product exists to
--     prevent, arriving through the backpressure mechanism.
--   * content_key is the second path. Many interface engines stamp a fresh
--     control id on retry, so the same result returns with a new MSH-10, sails
--     past the control-id check, and produces a second `resulted` transition or
--     a second orphan. The unique index makes that a database fact rather than
--     a check the listener has to remember, which is also what makes it hold
--     across two connections racing.
--
-- The index is partial because content_key is NULL for messages that carry no
-- identifying content of their own (unknown types, and messages refused before
-- a key could be built). A plain UNIQUE index would collapse all of those into
-- one row on any database engine treating NULLs as equal, and relying on
-- SQLite not doing so is a portability trap for a table whose whole job is not
-- losing messages.
CREATE TABLE IF NOT EXISTS applied_messages (
    control_id   TEXT PRIMARY KEY,
    content_key  TEXT,
    message_type TEXT NOT NULL DEFAULT '',
    applied_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_applied_content_key
    ON applied_messages(content_key) WHERE content_key IS NOT NULL;
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

-- ADT^A40 identity history, in the same event-plus-projection shape as loops.
-- mrn_alias_events is the append-only truth (spec 10.5 reconstruction); it
-- stores the pair the MESSAGE named, not the pair we applied, so a rebuild can
-- recompute the compression from scratch.
CREATE TABLE IF NOT EXISTS mrn_alias_events (
    alias_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type     TEXT NOT NULL,   -- 'established' | 'reversed'
    retired_mrn    TEXT NOT NULL,
    surviving_mrn  TEXT NOT NULL,
    established_at TEXT NOT NULL,
    established_by TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_alias_events_retired ON mrn_alias_events(retired_mrn);

-- The compressed projection. One row per retired identifier, always pointing
-- straight at the identifier that survives it -- never at another retired one.
-- That invariant is maintained on WRITE: recording B->C rewrites every row
-- already pointing at B. Resolution is then a single indexed lookup forever,
-- rather than a walk whose length is set by how many times registration has
-- merged this patient. Chasing at read time would put unbounded work on every
-- inbound message and turn a cycle into a hang; compressing at write time also
-- concentrates the one place a cycle can be detected, which is the reason it is
-- detectable at all.
--
-- Deliberately NOT append-only, and not in the authorizer's deny list:
-- compression rewrites rows, and an administrative reversal rebuilds the whole
-- table. It carries no information mrn_alias_events does not.
CREATE TABLE IF NOT EXISTS mrn_aliases (
    retired_mrn    TEXT PRIMARY KEY,
    surviving_mrn  TEXT NOT NULL,
    established_at TEXT NOT NULL,
    established_by TEXT NOT NULL
);
-- merge_patient asks "what has been retired into this patient" to collect
-- stragglers; that is the reverse direction, so it needs its own index.
CREATE INDEX IF NOT EXISTS idx_aliases_surviving ON mrn_aliases(surviving_mrn);

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
CREATE TRIGGER IF NOT EXISTS mrn_alias_no_delete BEFORE DELETE ON mrn_alias_events
BEGIN SELECT RAISE(ABORT, 'mrn_alias_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mrn_alias_no_update BEFORE UPDATE ON mrn_alias_events
BEGIN SELECT RAISE(ABORT, 'mrn_alias_events is append-only'); END;
-- Append-only for the same reason as the archive: a deleted row is a message
-- that gets applied a second time, and an updated one is a content key
-- reassigned to a message that never carried it.
CREATE TRIGGER IF NOT EXISTS applied_no_delete BEFORE DELETE ON applied_messages
BEGIN SELECT RAISE(ABORT, 'applied_messages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS applied_no_update BEFORE UPDATE ON applied_messages
BEGIN SELECT RAISE(ABORT, 'applied_messages is append-only'); END;
"""

_ALIAS_ESTABLISHED = "established"
_ALIAS_REVERSED = "reversed"

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
        "mrn_alias_events",
        "applied_messages",
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

    def raw_payloads(self) -> list[str]:
        """Every archived message, in receipt order.

        Spec section 7 evaluates a rule-pack revision by replaying the archive
        and measuring the delta, so the archive has to be readable as a stream
        of messages rather than only countable. Ordered by received_at then
        control_id so a replay is deterministic across runs even when two
        messages share a timestamp.
        """
        return [
            r["payload"]
            for r in self._read(
                "SELECT payload FROM raw_messages ORDER BY received_at, control_id"
            )
        ]

    # ---------------------------------------------------------- applied messages

    def control_id_applied(self, control_id: str) -> bool:
        """Has this MSH-10 already been acted on (not merely archived)?"""
        if not control_id:
            return False
        return bool(
            self._read("SELECT 1 FROM applied_messages WHERE control_id = ?", (control_id,))
        )

    def content_key_owner(self, content_key: str) -> str | None:
        """The control id that already applied this content, if any."""
        if not content_key:
            return None
        rows = self._read(
            "SELECT control_id FROM applied_messages WHERE content_key = ?", (content_key,)
        )
        return rows[0]["control_id"] if rows else None

    def record_applied(
        self, control_id: str, content_key: str | None, message_type: str = ""
    ) -> bool:
        """Mark a message applied. False if this control id or content was already.

        Written *after* the transition it describes, deliberately. Claiming the
        key first would give at-most-once delivery: a crash between the claim
        and the write leaves a message permanently marked applied that produced
        no transition, and the result is silently gone. Writing afterwards gives
        at-least-once -- a crash in the same window costs a duplicate transition
        on redelivery, which is visible in the event log and correctable. A
        swallowed result is neither.
        """
        if not control_id:
            raise StoreUnavailableError(
                "Refusing to mark a message applied with no control id (MSH-10): "
                "idempotency cannot be guaranteed without it"
            )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO applied_messages (control_id, content_key, message_type, "
                    "applied_at) VALUES (?, ?, ?, ?)",
                    (control_id, content_key or None, message_type,
                     datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Either this control id or this content key is already on file.
                # Both mean "somebody else got here first", which is the whole
                # point of the constraint -- including when the somebody else is
                # a second process this lock does not cover.
                conn.rollback()
                logger.info(
                    "Message %s was already marked applied (content key present: %s)",
                    control_id, content_key is not None,
                )
                return False
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreUnavailableError(f"Applied-message write failed: {exc}") from exc
            finally:
                conn.close()

    def applied_count(self) -> int:
        return self._read("SELECT COUNT(*) FROM applied_messages")[0][0]

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

    # ------------------------------------------------------------- MRN aliases

    _RESOLVE_SQL = "SELECT surviving_mrn FROM mrn_aliases WHERE retired_mrn = ?"

    def resolve_mrn(self, mrn: str) -> str:
        """The identifier that survives this one, or this one. ONE lookup.

        Called once per inbound message, at ingest, before the registry or the
        matcher sees anything -- so that everything downstream works in surviving
        identifiers and there is exactly one place that decides who a message is
        about. Resolving at each call site instead gives two implementations that
        eventually disagree, and the disagreement shows up as a loop on a
        worklist nobody reads.

        No walk and no depth cap, because mrn_aliases is compressed on write and
        can never contain a row pointing at another retired identifier.

        Measured, since it is on the hot path: 0.27 ms against a 10,000-row
        alias table, and 0.27 ms at the head of a 500-deep merge chain -- the
        same number, which is the whole point of compressing. An empty MRN costs
        0.0001 ms because it never reaches the database.
        """
        if not mrn:
            return mrn
        rows = self._read(self._RESOLVE_SQL, (mrn,))
        return rows[0]["surviving_mrn"] if rows else mrn

    def retired_into(self, mrn: str) -> list[str]:
        """Every identifier retired in favour of this one. One indexed lookup."""
        if not mrn:
            return []
        return [r["retired_mrn"] for r in self._read(
            "SELECT retired_mrn FROM mrn_aliases WHERE surviving_mrn = ? ORDER BY retired_mrn",
            (mrn,),
        )]

    def aliases(self) -> list[tuple[str, str]]:
        return [(r["retired_mrn"], r["surviving_mrn"]) for r in self._read(
            "SELECT retired_mrn, surviving_mrn FROM mrn_aliases ORDER BY retired_mrn"
        )]

    def alias_count(self) -> int:
        return self._read("SELECT COUNT(*) FROM mrn_aliases")[0][0]

    def alias_events(self) -> list[sqlite3.Row]:
        return self._read("SELECT * FROM mrn_alias_events ORDER BY alias_event_id")

    @staticmethod
    def _resolve_on(conn: sqlite3.Connection, mrn: str) -> str:
        row = conn.execute(LoopStore._RESOLVE_SQL, (mrn,)).fetchone()
        return row["surviving_mrn"] if row else mrn

    @classmethod
    def _apply_alias(
        cls,
        conn: sqlite3.Connection,
        retired_mrn: str,
        surviving_mrn: str,
        established_at: str,
        established_by: str,
    ) -> tuple[str, str] | None:
        """Compress one message's claim into the projection. Caller owns the txn.

        Returns the pair actually applied, or None if it was already in force.
        Raises CircularMergeError if applying it would make an identity cyclic.

        Both endpoints resolve first, in one lookup each. A40s arrive against
        whatever identifier the sending system still knows, so "A merges into C"
        can arrive after A was already merged into B; recording the raw pair
        would leave B's loops on B while new orders on A went to C -- one patient
        split across two identifiers, which is the failure this table exists to
        prevent, reintroduced by the fix for it.
        """
        source = cls._resolve_on(conn, retired_mrn)
        target = cls._resolve_on(conn, surviving_mrn)

        if source == target:
            if cls._resolve_on(conn, retired_mrn) == surviving_mrn:
                # The message's own claim is already in force -- the same A40
                # delivered twice, which an engine does after a timeout.
                return None
            # The opposite direction is on file: this message says B retires
            # into A while A is already retired into B. Refuse; see
            # CircularMergeError. Nothing is written, including no event.
            raise CircularMergeError(
                f"Refusing ADT^A40 {retired_mrn} -> {surviving_mrn}: {surviving_mrn} already "
                f"resolves to {target}, so applying this would make the identity cyclic. "
                "Both claims cannot hold and choosing between them would strand every loop "
                "on the losing side. The alias table is unmodified and no loop moved; "
                "registration must correct this and a human must review it."
            )

        # Compression. Every row already pointing at the source now points past
        # it, so no row ever references a retired identifier and resolution stays
        # a single lookup no matter how often this patient is merged. The cost
        # moves here, and grows with how many identifiers already resolve to the
        # source -- two or three for a real patient. Paid once per A40 rather
        # than once per inbound message, which is the trade being made.
        conn.execute(
            "UPDATE mrn_aliases SET surviving_mrn = ? WHERE surviving_mrn = ?",
            (target, source),
        )
        conn.execute(
            "INSERT OR REPLACE INTO mrn_aliases (retired_mrn, surviving_mrn, established_at, "
            "established_by) VALUES (?, ?, ?, ?)",
            (source, target, established_at, established_by),
        )

        # The post-condition, checked rather than argued. The reasoning above
        # says no row can now point at itself; this is what makes that a fact
        # about the file instead of a fact about my reasoning.
        if conn.execute(
            "SELECT 1 FROM mrn_aliases WHERE retired_mrn = surviving_mrn LIMIT 1"
        ).fetchone():
            raise CircularMergeError(
                f"Refusing ADT^A40 {retired_mrn} -> {surviving_mrn}: compressing it would "
                "leave an identifier pointing at itself. Refused whole; a human must review."
            )
        return source, target

    def record_alias(
        self,
        retired_mrn: str,
        surviving_mrn: str,
        established_at: datetime,
        established_by: str,
    ) -> tuple[str, str] | None:
        """Record that retired_mrn is retired in favour of surviving_mrn.

        The event and the compressed projection land in ONE transaction: an
        alias visible in the log but missing from the projection would redirect
        nothing, which is the whole failure this table closes.

        Aliases do not expire. An alias records a fact the hospital asserted --
        these are one patient -- and that does not decay; an expiring one would
        silently resume stranding loops at an arbitrary future moment with no
        event to explain it. A merge recorded in error is undone by
        reverse_alias, an explicit administrative act with a named actor, never
        by deletion and never by time passing.
        """
        if not retired_mrn or not surviving_mrn:
            raise StoreUnavailableError(
                f"Refusing to record an alias with an empty MRN: "
                f"retired={retired_mrn!r} surviving={surviving_mrn!r}"
            )
        if retired_mrn == surviving_mrn:
            raise CircularMergeError(f"Refusing to alias MRN {retired_mrn!r} to itself")

        with self._lock:
            conn = self._guarded()
            try:
                applied = self._apply_alias(
                    conn, retired_mrn, surviving_mrn,
                    established_at.isoformat(), established_by,
                )
                if applied is None:
                    conn.rollback()
                    return None
                conn.execute(
                    "INSERT INTO mrn_alias_events (event_type, retired_mrn, surviving_mrn, "
                    "established_at, established_by, detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _ALIAS_ESTABLISHED, retired_mrn, surviving_mrn,
                        established_at.isoformat(), established_by,
                        # The pair the message named is the event; the pair we
                        # applied after resolving both ends is derived, and is
                        # recorded so an auditor can see the compression.
                        json.dumps({"applied_retired": applied[0], "applied_surviving": applied[1]},
                                   sort_keys=True),
                    ),
                )
                conn.commit()
                return applied
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreUnavailableError(f"Alias write failed: {exc}") from exc
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def reverse_alias(
        self, retired_mrn: str, actor: str, role: str, reason: str, control_id: str = ""
    ) -> tuple[str, str]:
        """Undo a merge administratively. Returns the pair that was in force.

        Aliases never expire, so this is the only way back from an A40 sent in
        error -- and without it "no expiry" would mean a mistyped merge is
        permanent. Recorded as an event, exactly like the acknowledgement
        reversal in rule 4: appended, never a deletion, so the merge and its
        undoing both stay in the log and an auditor sees that a human decided.

        The projection is rebuilt from the log rather than edited, because
        compression is lossy -- rows that were rewritten to point past this alias
        cannot be un-rewritten in place.
        """
        if not actor or not role or not reason:
            raise ReferralLoopError(
                "An alias reversal needs a named actor, role and reason: it re-splits two "
                "patient records and 'someone reversed it' is not an answer to why"
            )
        surviving = self.resolve_mrn(retired_mrn)
        if surviving == retired_mrn:
            raise ReferralLoopError(
                f"MRN {retired_mrn} is not retired; there is no merge to reverse"
            )
        with self._lock:
            conn = self._guarded()
            try:
                conn.execute(
                    "INSERT INTO mrn_alias_events (event_type, retired_mrn, surviving_mrn, "
                    "established_at, established_by, detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _ALIAS_REVERSED, retired_mrn, surviving,
                        datetime.now(timezone.utc).isoformat(), control_id,
                        json.dumps(
                            {"reversed_by": actor, "reversed_role": role,
                             "reversed_reason": reason},
                            sort_keys=True,
                        ),
                    ),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreUnavailableError(f"Alias reversal failed: {exc}") from exc
            finally:
                conn.close()
        self.rebuild_alias_projection()
        logger.warning(
            "MRN alias %s -> %s reversed by %s (%s): %s",
            retired_mrn, surviving, actor, role, reason,
        )
        return retired_mrn, surviving

    def rebuild_alias_projection(self) -> int:
        """Recompute mrn_aliases from mrn_alias_events. Spec 10.5.

        The projection is compressed and therefore derived; the log holds the
        pairs the messages actually named. Replaying them through the same
        compression reproduces the table exactly, which is what makes a restore
        of loop_events plus mrn_alias_events sufficient.
        """
        with self._lock:
            conn = self._guarded()
            try:
                rows = conn.execute(
                    "SELECT * FROM mrn_alias_events ORDER BY alias_event_id"
                ).fetchall()
                reversed_mrns = {
                    r["retired_mrn"] for r in rows if r["event_type"] == _ALIAS_REVERSED
                }
                conn.execute("DELETE FROM mrn_aliases")
                applied = 0
                for row in rows:
                    if row["event_type"] != _ALIAS_ESTABLISHED:
                        continue
                    if row["retired_mrn"] in reversed_mrns:
                        continue
                    if self._apply_alias(
                        conn, row["retired_mrn"], row["surviving_mrn"],
                        row["established_at"], row["established_by"],
                    ):
                        applied += 1
                conn.commit()
                return applied
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreUnavailableError(f"Alias projection rebuild failed: {exc}") from exc
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def loops_for_mrn(self, mrn: str) -> list[Loop]:
        """Every loop currently attributed to this MRN, in any state.

        Answers the one question ADT^A40 asks. all_loops() answers it too, by
        replaying every event in the file and discarding all but a handful --
        and registry.merge_patient runs under the registry lock, so that cost is
        paid with every other message on the interface blocked behind it.
        Measured on this schema with 10,000 single-event loops: all_loops()
        answers in 340ms, this in 1.3ms, because idx_loops_mrn turns a full
        replay into a lookup. Production loops carry several events each, so the
        gap only widens.

        Not a different answer, only a cheaper one: _loops_where reads ids from
        the projection and still rebuilds each Loop from its events, and
        _materialize writes the projection's mrn from that same replay.
        """
        return self._loops_where("mrn = ?", (mrn,))

    def loops_in_states(self, states) -> list[Loop]:
        """Loops in any of the given states, read through idx_loops_state.

        The matcher owns which states may receive a result; this only answers
        the query. Ingest needs that set rather than open_loops(), because an
        ACKNOWLEDGED loop must remain a candidate at the exact-identifier tiers
        or safety rule 2 never fires on live traffic -- a correction would land
        in the orphan queue while the loop it corrects went on reporting
        "handled". all_loops() would answer it too, by replaying every event in
        the file and discarding all but the still-open work.
        """
        values = tuple(getattr(s, "value", s) for s in states)
        if not values:
            return []
        placeholders = ", ".join("?" * len(values))
        return self._loops_where(f"state IN ({placeholders})", values)

    def all_loops(self) -> list[Loop]:
        return self._loops_where("1 = 1", ())
