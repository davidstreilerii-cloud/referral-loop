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
import re
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
from .events import (
    LABEL_OUTCOME,
    LabelType,
    Loop,
    LoopEvent,
    LoopState,
)

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

-- Spec section 7's flywheel, as a table. Every row is something a coordinator
-- taught the system: an orphan they attached, a match they undid, a dismissal.
--
-- It carries a STRICTER rule than loop_events, and deliberately so. loop_events
-- legitimately holds the MRN, because matching and merges are identifier
-- arithmetic and cannot work without it. A label is training data whose whole
-- point is that it may eventually leave the building -- section 7 contemplates
-- site-local labels contributed back on opt-in -- so it holds only features the
-- matcher actually reasons over, and no identifier of a patient, an actor or a
-- message. What is NOT here is the specification: no mrn, no accession, no
-- placer/filler order number, no actor name, no free-text reason, no control id.
-- record_label has no parameter that accepts any of them, which is the same
-- control audit.py uses and a stronger one than filtering a dict a caller built.
--
-- created_date, not created_at. An exact timestamp on an exportable row is a
-- quasi-identifier -- it pins a label to the hour a study resulted -- and
-- nothing in section 7 needs sub-day resolution: ordering is label_id's job and
-- the temporal bucket that matters for evaluation is pack_version.
--
-- Append-only, under the same triggers as loop_events, because these rows feed
-- a release gate that vetoes a pack on a false-match regression. A deletable
-- label is a gate that can be passed by deleting the evidence. Nothing is lost
-- by that: every label is derivable by replaying loop_events, so this table is
-- an index over the log exactly as `loops` is.
CREATE TABLE IF NOT EXISTS labels (
    label_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    label_type   TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    loop_id      TEXT NOT NULL,
    modality     TEXT NOT NULL DEFAULT '',
    service_code TEXT NOT NULL DEFAULT '',
    tier         INTEGER,
    actor_role   TEXT NOT NULL DEFAULT '',
    pack_version TEXT NOT NULL DEFAULT '',
    created_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labels_outcome ON labels(outcome);

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
CREATE TRIGGER IF NOT EXISTS labels_no_delete BEFORE DELETE ON labels
BEGIN SELECT RAISE(ABORT, 'labels is append-only'); END;
CREATE TRIGGER IF NOT EXISTS labels_no_update BEFORE UPDATE ON labels
BEGIN SELECT RAISE(ABORT, 'labels is append-only'); END;
"""

_ALIAS_ESTABLISHED = "established"
_ALIAS_REVERSED = "reversed"

# A hard floor under purge_retention, at the choke point rather than only in the
# policy that calls it.
#
# retention.py decides which states are terminal, and that is the right place
# for a clinical judgement. But `purge_retention` takes the set as an argument,
# so a caller -- a future config path, a script, a test that gets the argument
# order wrong -- can hand it OPEN and it would delete a clinically open loop.
# This is the same posture append_event takes towards the reserved CLOSED event:
# guard where the write happens, so paths no future caller has written yet are
# covered too. A state here is refused whatever the policy says about it.
#
# It is a floor, not the set. retention.py may narrow it further and does not
# have to justify itself here; it may not widen it. That the two agree is
# asserted by test_the_shipped_policy_stays_inside_the_stores_floor rather than
# trusted -- the same treatment the duplicated label patterns get above.
_NEVER_DELETABLE = frozenset(
    {
        LoopState.OPEN,        # awaiting a result
        LoopState.SCHEDULED,   # awaiting a result, with an appointment
        LoopState.RESULTED,    # a result nobody has acknowledged
        LoopState.ORPHAN,      # a result awaiting a human, which is not a resolution
        LoopState.CLOSED,      # reserved for v2; v1 holds no opinion about deleting it
    }
)

# The two guards a retention purge has to step over, by name. Only DELETE: a
# purge never updates a row, so the UPDATE guards stay armed for its whole
# transaction. Disarming more than the operation needs is how a delete path
# quietly becomes an edit path over an append-only log.
_DELETE_GUARDS = ("loop_events_no_delete", "raw_messages_no_delete")

# How long a purge waits for the write lock before refusing. A live listener is
# a second writer; five seconds is long enough to ride out one message and short
# enough that an operator gets an answer.
_PURGE_BUSY_TIMEOUT_MS = 5000

# SQLite's parameter limit is 999 by default. Deletes are chunked well under it.
_PURGE_CHUNK = 400

# ------------------------------------------------------------- storage stats
#
# `applied_messages` and `mrn_alias_events`/`mrn_aliases` are deliberately
# excluded from retention (see the module docstring above and retention.py),
# which means they have no bound at all on a box this project does not
# administer. `stats()` exists to make that growth visible before it becomes a
# disk-full incident, not to add one -- see `cli.py`'s `stats` mode for the
# operator-facing side of this.
#
# One row per table this report covers, and every column it sums over. The
# audit database is not here: it is a different file, opened by a different
# module, under a different retention policy, and this module never opens it.
#
# Each table's own INTEGER PRIMARY KEY column (event_id, alias_event_id,
# label_id) is left out of its column list. That column IS the table's rowid
# under SQLite's rowid-alias rule and is not stored a second time as a body
# value, so summing LENGTH() over it would report storage that is not really
# there. A TEXT PRIMARY KEY (control_id, loop_id, retired_mrn) is not a rowid
# alias and every one of those stays in its table's list.
STATS_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("raw_messages", ("control_id", "payload", "received_at")),
    ("applied_messages", ("control_id", "content_key", "message_type", "applied_at")),
    ("loops", ("loop_id", "mrn", "state", "placer_order_number", "filler_order_number",
               "service_code", "modality", "ordering_provider", "ordered_at",
               "ack_by", "ack_role", "ack_at")),
    ("loop_events", ("loop_id", "event_type", "occurred_at", "control_id", "detail")),
    ("mrn_alias_events", ("event_type", "retired_mrn", "surviving_mrn", "established_at",
                          "established_by", "detail")),
    ("mrn_aliases", ("retired_mrn", "surviving_mrn", "established_at", "established_by")),
    ("labels", ("label_type", "outcome", "loop_id", "modality", "service_code", "tier",
                "actor_role", "pack_version", "created_date")),
)

# The tables retention.py's module docstring names as permanently excluded.
# stats() flags these in its report so an operator does not have to
# cross-reference retention.py to know which rows never age out.
UNBOUNDED_TABLES = frozenset({"applied_messages", "mrn_alias_events", "mrn_aliases"})

# Same rationale as _PURGE_BUSY_TIMEOUT_MS: a live listener is a second writer,
# and a stats read that cannot get a consistent snapshot quickly should refuse
# rather than block an operator indefinitely.
_STATS_BUSY_TIMEOUT_MS = 5000

# Event types that deliberately carry fields without changing state.
_NON_TRANSITIONAL = frozenset({"merged_in"})

# ---------------------------------------------------------------- label limits
#
# The same control audit.py applies to the audit database, applied to the other
# artifact designed to leave the building. Every value written to `labels` is
# normalised through one of these on the way in; nothing reaches the table as
# the caller supplied it.
#
# Deliberately duplicated rather than imported from audit.py: a change to one
# must not silently widen the other, and test_orphan_attach asserts the two
# patterns still agree, so the duplication is checked rather than trusted.

# Loop ids are minted as f"L-{uuid4().hex[:12]}" (orphans "O-"). Twelve hex
# digits of uuid4 are definitionally non-identifying, which is what makes
# carrying one into an exportable artifact safe. Anything else -- above all a
# string a coordinator's browser put in a URL -- is recorded as unminted.
_LABEL_LOOP_REF_RE = re.compile(r"^[LO]-[0-9a-f]{12}$")
_UNMINTED_LOOP_REF = "unminted"

# A coded clinical value: an HL7 code or a modality abbreviation, which is short
# and drawn from a coded charset. A name, an address or a note is neither, so a
# value that does not look like a code is dropped rather than stored.
#
# Defence in depth, not the primary control. The primary control is that these
# values can only have come from the pack's field_map, and load_pack refuses a
# field_map naming any segment outside the parser allowlist -- so NK1, GT1 and
# NTE cannot reach here at all. This bounds what a *permitted* segment can carry.
_CODED_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._^&+/-]{0,31}$")

# A pack version, from a pack whose Ed25519 signature verified.
_LABEL_PACK_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_UNKNOWN_PACK_VERSION = "unknown"

# A role is the one human-typed value permitted, and it is here for the same
# reason audit.py permits it: a label that cannot say what kind of person made
# the judgement cannot be weighted against one made by someone else. Bounded and
# collapsed to a single line so a pasted note cannot land in it whole. It is not
# filtered further, and the residual risk is stated rather than hidden: a person
# who types a patient's name into the "your role" box puts it here. That is a
# training-time review question for whoever runs the contribution step, not
# something a charset filter can answer -- the same conclusion audit.py reaches
# about the actor field.
_MAX_LABEL_ROLE = 64

# Matching has five tiers (spec section 5); tier 5 is "no match". Anything
# outside that range did not come from the matcher and is recorded as unknown.
_MIN_TIER, _MAX_TIER = 1, 5


def _label_loop_ref(value: object) -> str:
    if isinstance(value, str) and _LABEL_LOOP_REF_RE.match(value):
        return value
    return _UNMINTED_LOOP_REF


def _label_coded(value: object) -> str:
    if isinstance(value, str) and _CODED_VALUE_RE.match(value.strip()):
        return value.strip()
    return ""


def _label_pack_version(value: object) -> str:
    if isinstance(value, str) and _LABEL_PACK_VERSION_RE.match(value):
        return value
    return _UNKNOWN_PACK_VERSION


def _label_role(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_LABEL_ROLE]


def _label_tier(value: object) -> int | None:
    """An int in 1..5, or None. bool is rejected: True would store as tier 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if _MIN_TIER <= value <= _MAX_TIER else None

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
    # An orphan a coordinator attached to a real loop. Terminal, and off every
    # queue -- the coordinator must not be shown the same orphan again -- while
    # the record stays in the log. See LoopState.ATTACHED for why it is not
    # DISMISSED or CANCELLED.
    "attached": LoopState.ATTACHED,
    # A coordinator saying the matcher attached the wrong result. The loop goes
    # back to awaiting one.
    #
    # OPEN rather than "whatever it was before", because state comes from this
    # map alone and the map takes no history. The loss is the appointment fact
    # of a loop that was SCHEDULED, and it costs nothing that matters:
    # open_loops() selects OPEN and SCHEDULED alike, so the loop lands on the
    # same queue, staleness measures from ordered_at either way, and the SIU is
    # still in the event log. A conditional restore would need two event types
    # to say one thing, and would make replay depend on a lookup.
    "unmatched": LoopState.OPEN,
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
        "labels",
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

    # ---------------------------------------------------------------- labels

    def record_label(
        self,
        label_type: LabelType,
        *,
        loop_id: str,
        modality: str = "",
        service_code: str = "",
        tier: object = None,
        actor_role: str = "",
        pack_version: str = "",
    ) -> None:
        """Record what a coordinator taught the system. Spec section 7's flywheel.

        Keyword-only past the label type, and there is no `detail`, no `reason`,
        no `mrn`, no `actor` and no `control_id` parameter anywhere in this
        signature. That absence is the control: a caller holding a patient name
        and wanting it in an exportable artifact has no argument to put it in,
        which is stronger than any filter applied to a dict a caller composed.
        Every argument that *is* accepted is normalised on the way in, so the
        table's contents are a property of this method rather than of its
        callers' care.

        `outcome` is derived from `label_type` through LABEL_OUTCOME rather than
        accepted, so the release gate's false-match count cannot be moved by a
        caller mislabelling a coordinator's slip.

        Raises on an unknown label type. A label the eval harness cannot
        interpret is worse than no label -- it would be counted as *something* --
        and the caller is code, never a message, so the failure is a bug rather
        than bad input.
        """
        if not isinstance(label_type, LabelType):
            raise ReferralLoopError(
                f"Refusing to record a label of unknown type {label_type!r}; "
                f"the type must be a LabelType so its outcome is derivable"
            )
        row = (
            label_type.value,
            LABEL_OUTCOME[label_type].value,
            _label_loop_ref(loop_id),
            _label_coded(modality),
            _label_coded(service_code),
            _label_tier(tier),
            _label_role(actor_role),
            _label_pack_version(pack_version),
            # Day resolution, deliberately. See the schema note.
            datetime.now(timezone.utc).date().isoformat(),
        )
        with self._lock:
            conn = self._guarded()
            try:
                conn.execute(
                    "INSERT INTO labels (label_type, outcome, loop_id, modality, service_code, "
                    "tier, actor_role, pack_version, created_date) VALUES (?,?,?,?,?,?,?,?,?)",
                    row,
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise StoreUnavailableError(f"Label write failed: {exc}") from exc
            finally:
                conn.close()

    def labels(self) -> list[dict]:
        """Every label, oldest first. The artifact a site would contribute back."""
        return [dict(r) for r in self._read("SELECT * FROM labels ORDER BY label_id")]

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
        # Neither identifier and, above all, not the reason. A log record is one
        # of the four artifacts spec test 14 greps, and the reason is free text a
        # human typed about a patient -- the unbounded free-text channel. Both are already
        # in mrn_alias_events, which is append-only and where the audit needs
        # them; this line exists so an operator sees a reversal happened.
        logger.warning(
            "An MRN alias was reversed by %s (%s) under control id %r; the identifiers "
            "and the reason are recorded in mrn_alias_events.", actor, role, control_id,
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

    def loops_in_states(self, states, mrn: str = "", order_numbers=()) -> list[Loop]:
        """Loops in any of the given states, read through idx_loops_state.

        The matcher owns which states may receive a result; this only answers
        the query. Ingest needs that set rather than open_loops(), because an
        ACKNOWLEDGED loop must remain a candidate at the exact-identifier tiers
        or safety rule 2 never fires on live traffic -- a correction would land
        in the orphan queue while the loop it corrects went on reporting
        "handled". all_loops() would answer it too, by replaying every event in
        the file and discarding all but the still-open work.

        `mrn` and `order_numbers` narrow the rows, and they are **OR'd**, which
        is the shape ingest needs and the reason they are one call rather than
        two: the matcher's exact tiers look up an order number without regard to
        whose it is (so that a number belonging to another patient is *seen* and
        reported as a collision, rather than silently missing), and its lower
        tiers look up a patient. Either filter alone would disable one of those.
        Passing neither returns every loop in the states, which is what the
        worklist asks for.

        An empty string in `order_numbers` is dropped rather than matched. A
        loop carrying no placer must not be admitted by a result carrying no
        placer: that is the `"" == ""` equivalence class the matcher refuses at
        every tier, and admitting it here would hand back most of the table
        under the guise of a narrowed query.

        `mrn` is a plain equality test and is correct across merges without
        knowing anything about them: identity is resolved once at ingest and
        `merge_patient` rewrites the loops it moves, so both sides of this
        comparison are already the surviving identifier (registry.py header).
        """
        values = tuple(getattr(s, "value", s) for s in states)
        if not values:
            return []
        clause = f"state IN ({', '.join('?' * len(values))})"
        params: tuple = values

        narrowings: list[str] = []
        if mrn:
            narrowings.append("mrn = ?")
            params += (mrn,)
        numbers = tuple(number for number in order_numbers if number)
        if numbers:
            placeholders = ", ".join("?" * len(numbers))
            narrowings.append(f"placer_order_number IN ({placeholders})")
            narrowings.append(f"filler_order_number IN ({placeholders})")
            params += numbers + numbers
        if narrowings:
            clause += f" AND ({' OR '.join(narrowings)})"

        return self._loops_where(clause, params)

    def all_loops(self) -> list[Loop]:
        return self._loops_where("1 = 1", ())

    # ------------------------------------------------------------- retention
    #
    # The only delete path in this file, and the only one there is going to be.
    # Everything above appends. Spec section 6 makes retention a site policy,
    # and enforcing a policy over PHI means something here has to be able to
    # remove rows from two tables the schema declares append-only.
    #
    # Three properties hold it together, and each is asserted by a test rather
    # than argued here:
    #
    #   1. **The guards come down for one transaction and no longer.** The
    #      triggers are read out of sqlite_master, dropped, and recreated from
    #      exactly the SQL that was found -- so re-arming cannot drift from
    #      arming, because it is the same string. All of it, DDL included, runs
    #      inside one explicit BEGIN IMMEDIATE: SQLite rolls DDL back with
    #      everything else, so a crash mid-purge leaves the file armed. Python's
    #      sqlite3 runs DDL in autocommit unless a transaction is already open,
    #      which is why the BEGIN is explicit and isolation_level is None.
    #
    #   2. **A file already missing a guard is refused.** Those triggers travel
    #      with the file and apply to every connection; a file without them has
    #      been altered outside this module, and running the one sanctioned
    #      delete path over it would destroy the evidence of that.
    #
    #   3. **The delete decision is re-derived from the event log, never read
    #      off the projection.** `loops` is a materialized index and any
    #      connection can write it -- the triggers cover the log, not the index
    #      over it. Selecting candidates through idx_loops_state is a narrowing
    #      step only; every candidate is then replayed, and a row claiming a
    #      terminal state over a log that says OPEN deletes nothing. The
    #      converse error -- a projection that wrongly says OPEN over a
    #      terminal log -- retains a record too long, which is a policy miss
    #      and not a safety one, and is the direction to be wrong in.
    #
    # BEGIN IMMEDIATE also takes the database's write lock for the whole
    # transaction, which is what makes selection and deletion atomic against a
    # listener running in another process. A correction arriving under safety
    # rule 2 either lands before the purge takes the lock -- and the loop is no
    # longer terminal when it is replayed -- or waits behind it.

    _PURGE_BUSY_TIMEOUT_MS = _PURGE_BUSY_TIMEOUT_MS

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """A naive timestamp is read as UTC, never compared against an aware one.

        Comparing the two raises TypeError, and a TypeError inside a purge would
        abort the whole run over one odd row -- most likely a row restored from
        a system that wrote no offset.
        """
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @classmethod
    def _older_than(cls, stamp: object, cutoff: datetime) -> bool | None:
        """True, False, or None when the timestamp cannot be read.

        None is not False, and the caller must not treat it as one. A row whose
        age cannot be established has no age; deleting it on the strength of a
        parse failure is the single outcome here that cannot be undone.
        """
        if not isinstance(stamp, str):
            return None
        try:
            return cls._as_utc(datetime.fromisoformat(stamp)) < cutoff
        except ValueError:
            return None

    def _guards(self, conn: sqlite3.Connection) -> dict[str, str]:
        """The DELETE guards' own DDL, or a refusal naming the missing one."""
        found = {
            r["name"]: r["sql"]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN "
                f"({','.join('?' * len(_DELETE_GUARDS))})",
                _DELETE_GUARDS,
            ).fetchall()
            if r["sql"]
        }
        missing = [name for name in _DELETE_GUARDS if name not in found]
        if missing:
            raise StoreUnavailableError(
                f"Refusing to purge {self.db_path}: the append-only guard(s) "
                f"{', '.join(missing)} are not present on this file. They travel with the "
                "database and apply to every connection, so a file without them has been "
                "altered outside this module -- and a purge is the one operation that would "
                "destroy the evidence of that. Restore the file, or recreate the triggers "
                "deliberately, before purging."
            )
        return found

    def _purge_raw(self, conn, cutoff: datetime, report: dict, dry_run: bool) -> None:
        """Aged archive rows, selected in Python rather than by SQL comparison.

        `received_at < ?` in SQL is a *string* comparison. It happens to be
        right for rows this module wrote, which all carry `+00:00`, and wrong
        for a row restored from a writer that used a different offset or none --
        which would then be deleted or kept by lexicographic accident. The cost
        is holding the doomed control ids in memory for the length of one
        transaction; for a single-site archive that is the cheaper mistake to
        avoid making.
        """
        doomed: list[str] = []
        for row in conn.execute(
            "SELECT control_id, received_at FROM raw_messages"
        ).fetchall():
            older = self._older_than(row["received_at"], cutoff)
            if older is None:
                report["raw_retained_unreadable"] += 1
            elif older:
                doomed.append(row["control_id"])
        report["raw_deleted"] = len(doomed)
        if dry_run:
            return
        for start in range(0, len(doomed), _PURGE_CHUNK):
            chunk = doomed[start:start + _PURGE_CHUNK]
            conn.execute(
                f"DELETE FROM raw_messages WHERE control_id IN ({','.join('?' * len(chunk))})",
                chunk,
            )

    def _doomed_loops(self, conn, cutoff, purgeable_states, report) -> dict[str, list]:
        """Candidates that survive replay, age and the provenance guard."""
        if not purgeable_states:
            # `state IN ()` is a SQLite extension that other engines reject, and
            # "no state is purgeable" has an answer that needs no query.
            return {}
        placeholders = ",".join("?" * len(purgeable_states))
        candidates = [
            r["loop_id"]
            for r in conn.execute(
                f"SELECT loop_id FROM loops WHERE state IN ({placeholders}) ORDER BY loop_id",
                tuple(purgeable_states),
            ).fetchall()
        ]

        doomed: dict[str, list] = {}
        states: dict[str, LoopState] = {}
        for loop_id in candidates:
            rows = conn.execute(self._EVENTS_SQL, (loop_id,)).fetchall()
            if not rows:
                # A projection row with no log behind it. It has no age, so
                # retention has nothing to say about it; counted so it is
                # visible rather than silently skipped.
                report["loops_without_events"] += 1
                continue
            try:
                events = self._rows_to_events(rows)
                loop = self._build_loop(loop_id, events)
            except Exception:  # noqa: BLE001 - any unreadable log retains the loop
                report["retained_unreplayable"] += 1
                continue
            if loop.state.value not in purgeable_states:
                report["retained_projection_disagreed"] += 1
                continue
            ages = [self._older_than(r["occurred_at"], cutoff) for r in rows]
            if any(age is None for age in ages):
                report["retained_unreplayable"] += 1
                continue
            if not all(ages):
                # Age comes from the NEWEST event, so this is "every event is
                # past the window". A loop opened four hundred days ago and
                # acknowledged yesterday is a record the site has been working
                # inside its own window; measuring from creation would delete it
                # the day it resolved. Taking the newest also means a
                # future-dated event -- which the failure matrix accepts --
                # keeps a record rather than deleting one.
                report["retained_recent_activity"] += 1
                continue
            doomed[loop_id] = list(rows)
            states[loop_id] = loop.state

        self._hold_back_live_provenance(conn, doomed, states, report)
        return doomed

    @staticmethod
    def _hold_back_live_provenance(conn, doomed, states, report) -> None:
        """Keep an ATTACHED orphan whose target loop is staying.

        Attaching an orphan writes `attached_from` onto the target's `resulted`
        event, so the target's history names a record this purge would remove.
        Where the target is going too, the pair leaves together and nothing
        dangles. Where it is not -- a correction reopened it under safety rule 2,
        so it is clinically live again -- deleting the orphan would leave an open
        loop whose result came from a record that no longer exists.
        """
        for loop_id in list(doomed):
            if states[loop_id] is not LoopState.ATTACHED:
                continue
            target = ""
            for row in doomed[loop_id]:
                if row["event_type"] == "attached":
                    target = str(json.loads(row["detail"]).get("attached_to", ""))
            if not target or target in doomed:
                continue
            if conn.execute(
                "SELECT 1 FROM loop_events WHERE loop_id = ? LIMIT 1", (target,)
            ).fetchone():
                del doomed[loop_id]
                report["retained_for_provenance"] += 1

    @staticmethod
    def _delete_loops(conn, doomed: dict[str, list]) -> None:
        """A purged loop goes entirely -- events and projection row, one txn.

        Partial is worse than either whole. Events without a projection row are
        resurrected by rebuild_projection; a projection row without events is a
        loop no query can replay, and _loops_where would raise on it forever.
        """
        ids = list(doomed)
        for start in range(0, len(ids), _PURGE_CHUNK):
            chunk = ids[start:start + _PURGE_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM loop_events WHERE loop_id IN ({placeholders})", chunk)
            conn.execute(f"DELETE FROM loops WHERE loop_id IN ({placeholders})", chunk)

    def purge_retention(
        self,
        *,
        raw_cutoff: datetime,
        loop_cutoff: datetime,
        purgeable_states: tuple[str, ...],
        dry_run: bool = False,
        reclaim: bool = False,
    ) -> dict:
        """Delete aged raw messages and aged terminal loops. Returns counts only.

        `purgeable_states` is passed in rather than defined here: which states
        are terminal is a clinical judgement and belongs with the policy, not
        with the storage layer. But it is bounded by _NEVER_DELETABLE, which is
        not negotiable from outside -- see the note on that constant. An empty
        set deletes no loop at all, which is the correct answer to "somebody
        removed every state from the list".

        `dry_run` never lowers a guard and never takes the write lock. A command
        whose entire purpose is to show an operator what a period reaches, before
        they let it reach it, has no business disarming the append-only triggers
        to find out -- and it must be runnable against a live listener, which a
        write lock would prevent.
        """
        if not purgeable_states:
            logger.warning("Purge called with no purgeable states; no loop will be deleted")
        refused = sorted(
            state.value for state in _NEVER_DELETABLE if state.value in purgeable_states
        )
        if refused:
            raise ReferralLoopError(
                f"Refusing to purge loops in {', '.join(refused)}: a loop in any of those "
                "states is not finished. OPEN, SCHEDULED and RESULTED are awaiting a result "
                "or a human; ORPHAN is a result awaiting a human, which is not a resolution; "
                "CLOSED is reserved for v2 and v1 holds no opinion about deleting it. "
                "Retention deletes resolved records, and this refusal does not depend on the "
                "caller's policy being right."
            )

        report = {
            "raw_deleted": 0,
            "raw_retained_unreadable": 0,
            "loops_deleted": 0,
            "events_deleted": 0,
            "loops_without_events": 0,
            "retained_recent_activity": 0,
            "retained_projection_disagreed": 0,
            "retained_unreplayable": 0,
            "retained_for_provenance": 0,
            "reclaimed": False,
        }

        with self._lock:
            conn = self._connect()
            # Manual transaction control. Python's sqlite3 opens a transaction
            # implicitly before DML only, so a DROP TRIGGER would otherwise run
            # in autocommit and survive the rollback that is supposed to undo it.
            conn.isolation_level = None
            guards: dict[str, str] = {}
            try:
                conn.execute(f"PRAGMA busy_timeout = {self._PURGE_BUSY_TIMEOUT_MS}")
                # Freed pages are overwritten rather than merely marked reusable.
                # Without it the purged payloads stay legible in the file to
                # anything that reads it as bytes, and "retention" would mean
                # "unreachable by SQL" rather than "gone".
                conn.execute("PRAGMA secure_delete = ON")
                # IMMEDIATE for a real purge: it takes the database's write lock
                # for the whole transaction, so selection and deletion cannot be
                # split by a listener in another process applying a message.
                # DEFERRED for a dry run, which writes nothing and must not
                # block one.
                conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
                # Checked on a dry run too, and before anything else: an
                # operator asking what a purge would do needs to hear that this
                # file's append-only guards are already off.
                guards = self._guards(conn)
                if not dry_run:
                    for name in guards:
                        conn.execute(f"DROP TRIGGER {name}")

                self._purge_raw(conn, raw_cutoff, report, dry_run)
                doomed = self._doomed_loops(conn, loop_cutoff, purgeable_states, report)
                report["loops_deleted"] = len(doomed)
                report["events_deleted"] = sum(len(rows) for rows in doomed.values())
                if not dry_run:
                    self._delete_loops(conn, doomed)
                    for sql in guards.values():
                        conn.execute(sql)
                conn.execute("ROLLBACK" if dry_run else "COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise StoreUnavailableError(f"Retention purge failed: {exc}") from exc
            except BaseException:
                self._rollback(conn)
                raise
            finally:
                conn.close()

        self._reassert_guards()
        if reclaim and not dry_run:
            report["reclaimed"] = self._reclaim()
        return report

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        """Best effort, and it must not replace the exception that got us here.

        A rollback that raises on the way out of a failure would mask the
        failure -- an operator would be told the connection could not be rolled
        back rather than what the purge actually hit.
        """
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            logger.error("Rolling a failed purge back also failed (%s); the transaction "
                         "was not committed and SQLite discards it on close", type(exc).__name__)

    def _reassert_guards(self) -> None:
        """Check the guards are back, on a fresh connection, after the commit.

        Belt and braces over the rollback semantics rather than a substitute for
        them: if this module ever leaves a file unguarded, an operator has to
        hear about it in the same breath as the delete that did it.
        """
        conn = self._connect()
        try:
            self._guards(conn)
        finally:
            conn.close()

    def _reclaim(self) -> bool:
        """VACUUM, so the purge actually returns the disk. Returns whether it ran.

        Deleting rows only marks pages reusable; the file does not shrink, and
        an archive purged for space would go on occupying it. VACUUM cannot run
        inside a transaction, hence the separate connection in autocommit.

        temp_store = MEMORY because VACUUM builds a temporary copy of the whole
        database, and SQLite's default temp directory is not necessarily on the
        volume the encryption gate attested. A copy of the PHI file landing in
        /tmp would undo the gate on the way to enforcing retention. The cost is
        memory proportional to the file, which for a single-site archive is the
        cheaper of the two.

        **A failure here is logged, not raised.** The deletion has already
        committed, and it is the compliance-relevant half: the PHI is gone and
        its pages are zeroed by secure_delete whether or not the file shrinks.
        Raising would report a completed purge as a failed one -- and would put
        an audit row reading `failure` over a delete that fully succeeded, which
        is the worst of the available lies. Reclaiming is housekeeping; it is
        reported as not done and the next purge will try again.
        """
        conn = None
        try:
            conn = self._connect()
            conn.isolation_level = None
            conn.execute(f"PRAGMA busy_timeout = {self._PURGE_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA secure_delete = ON")
            conn.execute("VACUUM")
            return True
        except (sqlite3.Error, StoreUnavailableError) as exc:
            # Including the failure to open at all: the volume can go away
            # between the commit and the vacuum, and that is still a purge that
            # happened rather than one that did not.
            logger.warning(
                "The purge committed but the space could not be reclaimed (%s). The rows are "
                "deleted and their pages zeroed; the file keeps its size until a later purge "
                "or a manual VACUUM.", type(exc).__name__,
            )
            return False
        finally:
            if conn is not None:
                conn.close()

    # --------------------------------------------------------- storage stats

    def stats(self) -> dict:
        """Row count and an approximate payload-byte figure per STATS_TABLES
        entry, plus the exact whole-file size -- all read from ONE snapshot.

        **Why one snapshot.** A listener is a second writer. Reading each
        table with its own separate SELECT would let a message land between
        two of them -- `applied_messages` counted before it arrived,
        `loop_events` counted after -- and the report would describe a
        combination of table states that was never true of the file at any
        single instant. An explicit deferred transaction fixes the snapshot at
        its first read and holds it there (SQLite's documented transaction
        isolation) until the transaction ends; a concurrent writer is not
        blocked from writing during that window, only from *committing*, and
        is free the moment this method's transaction closes. This is a
        read-only sibling of the same guarantee `purge_retention` leans on,
        just without the write lock a delete needs and this does not.

        **Why COUNT(*) and SUM(LENGTH(...)) rather than reading rows.** Both
        are computed inside SQLite as streaming aggregates over the btree;
        this process never holds more than the running total and the final
        scalar. The cost here does not grow with how much RAM is available the
        way `len(cursor.fetchall())` on the largest table in the file would.

        **What the byte figure is not.** It is a LOWER BOUND, not the on-disk
        footprint. LENGTH() measures stored column bytes; it does not count
        the SQLite record header, btree page overhead, or any of this
        schema's indexes (idx_applied_content_key, idx_alias_events_retired,
        idx_aliases_surviving, idx_loops_mrn, idx_loops_state, idx_events_loop,
        idx_labels_outcome). `dbstat` would give an exact per-table figure and
        is a compile-time SQLite option -- not present in every build,
        including this one -- so the one number reported here as exact is the
        whole file's size (`page_count * page_size`), read inside the same
        snapshot as the row counts so it describes the same instant they do.
        """
        conn = self._connect()
        try:
            conn.isolation_level = None
            conn.execute(f"PRAGMA busy_timeout = {_STATS_BUSY_TIMEOUT_MS}")
            conn.execute("BEGIN")
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                tables: dict[str, dict] = {}
                for name, columns in STATS_TABLES:
                    length_sum = "+".join(f"IFNULL(LENGTH({c}),0)" for c in columns)
                    row_count, payload_bytes = conn.execute(
                        f"SELECT COUNT(*), COALESCE(SUM({length_sum}), 0) FROM {name}"
                    ).fetchone()
                    tables[name] = {
                        "rows": row_count,
                        "payload_bytes": payload_bytes,
                        "unbounded": name in UNBOUNDED_TABLES,
                    }
            finally:
                self._rollback(conn)
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"Stats read failed: {exc}") from exc
        finally:
            conn.close()
        return {"file_bytes": page_count * page_size, "tables": tables}
