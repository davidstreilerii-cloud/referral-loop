"""
Append-only audit enforcement — SQLite-backed audit trail.

What this module guarantees
---------------------------
Every connection it hands out carries a **deny-by-default** SQLite authorizer:
an action is refused unless it is on a short allowlist that covers exactly what
this module does -- read, append, and the transaction machinery those need.
UPDATE, DELETE, DROP, ALTER TABLE, ATTACH and every PRAGMA are refused with
`sqlite3.DatabaseError` ("not authorized"), as is any action code the allowlist
has never heard of.

Deny-by-default is the fix for a real bypass, not a stylistic preference. Until
2026-07-31 the authorizer named the two actions it disliked (DELETE, UPDATE) and
allowed the rest, and gated even those on `arg1 == "audit_events"`. That let an
in-process caller drop both triggers, `ALTER TABLE audit_events RENAME TO
tmp_ae`, delete from the renamed table -- a name the comparison did not
recognise -- rename it back and recreate the triggers, leaving a file that
looked fully armed. `PRAGMA writable_schema=ON` was a shorter route to the same
place. Inverting the check reverses which way that comparison fails: a name the
authorizer does not recognise is now refused rather than waved through.

The table also carries BEFORE UPDATE, BEFORE DELETE and BEFORE INSERT triggers
that RAISE. The authorizer does not travel with the file, so the triggers are
what a connection opened elsewhere in this process -- one that never called
`_get_connection` -- runs into. They are the second layer, not the first.

An authorizer alone was not enough even on this module's own connections, which
is why the trigger layer is not decoration. SQLite reports `REPLACE` and
`INSERT OR REPLACE` as action 18 (INSERT) and nothing else -- no DELETE, no
UPDATE -- so a caller supplying an `id` that already existed overwrote an audit
row in place with every action allowed. Two independent changes close it, and
both are here because they cover different connections: `_get_connection` turns
`PRAGMA recursive_triggers` on, so the conflict resolution's implicit delete
reaches BEFORE DELETE; and `audit_no_overwrite` refuses, on the INSERT itself,
any id that already exists or that runs ahead of the AUTOINCREMENT sequence. A
caller stopped by either gets `sqlite3.IntegrityError` rather than the
authorizer's `DatabaseError`.

What it does not guarantee, and where the boundary actually is
--------------------------------------------------------------
**This is not a promise that audit records cannot be modified or removed.** Any
process that can open `AUDIT_DB` can open it without an authorizer, and once
there the triggers are the only obstacle -- and a connection with no authorizer
can drop them. A process that can write the file at all can also truncate it,
replace it, or edit its bytes with the database closed.

Nor does it stop a *deliberate* caller inside this process. `set_authorizer` is
an ordinary method on the connection object this module returns:
`conn.set_authorizer(None)` is one line of Python, and after it the whole chain
above succeeds. The authorizer is a guard against accident, not against someone
who has already decided to remove it -- and nothing in-process can be otherwise,
because whatever disarms the guard runs with the same privileges as the guard.

The real boundary is therefore **filesystem permissions on the database file**:
the audit trail is as immutable as the operating system makes that file, and no
more. What this module actually provides is that code holding one of *its*
connections -- the ordinary case, and the one a bug or a careless refactor
arrives through -- cannot modify or remove a row **by accident or through
ordinary SQL**. The deliberate in-process adversary is outside the boundary and
is stated here as such rather than left to be discovered. Deployments that need
a stronger claim need write-once storage or an off-host copy; that is an install
decision, not something a callback in this process can supply.

`referral_loop/retention.py` cites this module's guarantee when it explains why
a purge never touches the audit trail. That reasoning still holds -- the purge
runs in this process and through this module -- but it holds for the reason
above, not because the file is tamper-proof.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

# The audit database does not live inside the package. A package directory is read-only
# in a container and may be on a different volume from the one the encryption gate
# attests; putting PHI-adjacent state there would put it outside the boundary that gate
# checks.
#
# The default below is the repo-root `data/` of a source checkout -- the same directory
# `cli.py` puts `referral_loops.db` in by default -- reached by walking up out of
# `src/referral_loop/`. That walk is only meaningful in a checkout: from an installed
# package it lands beside site-packages, which is neither writable nor anywhere a
# deployment wants PHI-adjacent state. So an installed deployment sets
# REFERRAL_AUDIT_DB, and the Dockerfile's `/app/data` is where it points.
#
# In the monorepo this was `dirname(dirname(abspath(__file__)))/../data`, which resolved
# to the repo root only because the file sat two directories down. It is spelled with
# `Path` and `.resolve()` rather than an interpolated parent-directory segment, so the
# path is normalised at import rather than carrying an unresolved segment into every
# `sqlite3.connect` and every operator-facing log line -- and so `audit_db_path()`
# returns something an operator can act on. The test for this greps the source for that
# segment, so do not reintroduce one even inside a comment.
_DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "data"
AUDIT_DB = os.environ.get(
    "REFERRAL_AUDIT_DB",
    str(_DEFAULT_AUDIT_DIR / "audit_trail.db"),
)

_db_lock = threading.Lock()


@dataclass
class GuardrailAuditEvent:
    event_type: str       # "read" | "write" | "redact" | "deny" | "confirm" | "step_up"
    resource_type: str    # "patient_record", "Observation", etc.
    resource_id: str
    tenant_id: str
    actor: str            # API key hash or agent identifier
    outcome: str          # "success" | "failure" | "denied" | "pending_confirmation"
    detail: str           # JSON string with additional context
    timestamp: str        # ISO 8601


# --------------------------------------------------------------- action codes

def _action_code(name: str, documented: int) -> int:
    """A SQLite authorizer action code, from the stdlib where it exposes one.

    `sqlite3.SQLITE_*` action codes landed in Python 3.11 and this package
    supports 3.10 (pyproject `requires-python`), so the documented numeric value
    from the SQLite C API is the fallback. Where both exist they are checked
    against each other, so a mistyped literal is an import-time failure rather
    than a silently wrong allowlist -- and a wrong allowlist here fails *open*
    for whichever action the typo landed on.
    """
    exposed = getattr(sqlite3, name, None)
    if exposed is None:
        return documented
    if exposed != documented:  # pragma: no cover - the C API cannot renumber these
        raise RuntimeError(f"{name}: stdlib says {exposed}, this module says {documented}")
    return exposed


_SQLITE_CREATE_INDEX = _action_code("SQLITE_CREATE_INDEX", 1)
_SQLITE_CREATE_TABLE = _action_code("SQLITE_CREATE_TABLE", 2)
_SQLITE_CREATE_TEMP_INDEX = _action_code("SQLITE_CREATE_TEMP_INDEX", 3)
_SQLITE_CREATE_TEMP_TABLE = _action_code("SQLITE_CREATE_TEMP_TABLE", 4)
_SQLITE_CREATE_TEMP_TRIGGER = _action_code("SQLITE_CREATE_TEMP_TRIGGER", 5)
_SQLITE_CREATE_TEMP_VIEW = _action_code("SQLITE_CREATE_TEMP_VIEW", 6)
_SQLITE_CREATE_TRIGGER = _action_code("SQLITE_CREATE_TRIGGER", 7)
_SQLITE_CREATE_VIEW = _action_code("SQLITE_CREATE_VIEW", 8)
_SQLITE_DELETE = _action_code("SQLITE_DELETE", 9)
_SQLITE_DROP_INDEX = _action_code("SQLITE_DROP_INDEX", 10)
_SQLITE_DROP_TABLE = _action_code("SQLITE_DROP_TABLE", 11)
_SQLITE_DROP_TEMP_INDEX = _action_code("SQLITE_DROP_TEMP_INDEX", 12)
_SQLITE_DROP_TEMP_TABLE = _action_code("SQLITE_DROP_TEMP_TABLE", 13)
_SQLITE_DROP_TEMP_TRIGGER = _action_code("SQLITE_DROP_TEMP_TRIGGER", 14)
_SQLITE_DROP_TEMP_VIEW = _action_code("SQLITE_DROP_TEMP_VIEW", 15)
_SQLITE_DROP_TRIGGER = _action_code("SQLITE_DROP_TRIGGER", 16)
_SQLITE_DROP_VIEW = _action_code("SQLITE_DROP_VIEW", 17)
_SQLITE_INSERT = _action_code("SQLITE_INSERT", 18)
_SQLITE_PRAGMA = _action_code("SQLITE_PRAGMA", 19)
_SQLITE_READ = _action_code("SQLITE_READ", 20)
_SQLITE_SELECT = _action_code("SQLITE_SELECT", 21)
_SQLITE_TRANSACTION = _action_code("SQLITE_TRANSACTION", 22)
_SQLITE_UPDATE = _action_code("SQLITE_UPDATE", 23)
_SQLITE_ATTACH = _action_code("SQLITE_ATTACH", 24)
_SQLITE_DETACH = _action_code("SQLITE_DETACH", 25)
_SQLITE_ALTER_TABLE = _action_code("SQLITE_ALTER_TABLE", 26)
_SQLITE_REINDEX = _action_code("SQLITE_REINDEX", 27)
_SQLITE_CREATE_VTABLE = _action_code("SQLITE_CREATE_VTABLE", 29)
_SQLITE_DROP_VTABLE = _action_code("SQLITE_DROP_VTABLE", 30)
_SQLITE_FUNCTION = _action_code("SQLITE_FUNCTION", 31)

# SQLite's own schema table. Renamed to `sqlite_schema` in 3.33; the authorizer
# still reports `sqlite_master` on the builds measured here, so both are named
# rather than betting the schema-init window on which one a site's build says.
_SCHEMA_TABLES = frozenset({"sqlite_master", "sqlite_schema"})


# ---------------------------------------------------------------- authorizers

# What an ordinary connection may do. Derived by instrumenting this module's own
# three operations with a logging authorizer and recording every action code
# SQLite asked about: init, append, and each shape of export between them use
# INSERT, READ, SELECT and TRANSACTION and nothing else.
#
# FUNCTION is the one entry the three current operations do not reach, and it is
# here because "current" is doing too much work in that sentence: SQLite issues
# it for any function call in a query, `SELECT COUNT(*) FROM audit_events` is
# measurably one (action 31, alongside READ and SELECT), and the
# `inserted_at DEFAULT (datetime('now'))` column means a build that authorizes
# DEFAULT expressions would issue it on every append. Leaving it out would make
# a one-word query change refuse an audit write -- and on the guardrail path
# that refusal is invisible, because `guardrails/middleware.py` ends its audit
# write in `except Exception: pass` with no log at all. (The referral path is
# not the silent one and should not be cited as though it were:
# `referral_loop/audit.py` logs ERROR with the exception type and increments
# `write_failures()`, deliberately.)
# It widens nothing reachable: extension loading is off by default in
# Python's sqlite3 and this module registers no functions, so the callable set
# is SQLite builtins, none of which touch the schema or the filesystem.
#
# INSERT is the entry that cannot be tightened and is worth knowing about: it is
# also what SQLite reports for `REPLACE`, which destroys a row. No value of this
# set can distinguish the two, which is why that case is answered by triggers
# and a pragma instead -- see `_get_connection` and `audit_no_overwrite`.
_ALLOWED_ACTIONS = frozenset({
    _SQLITE_SELECT,
    _SQLITE_READ,
    _SQLITE_INSERT,
    _SQLITE_TRANSACTION,   # BEGIN / COMMIT / ROLLBACK around an append
    _SQLITE_FUNCTION,
})

# Refused ahead of any allowlist, on every connection including the one that
# creates the schema. Redundant today -- deny-by-default already refuses each of
# these -- and kept because it is what makes the schema-init window below
# provably unable to reopen the 2026-07-31 bypass: that window widens the
# allowlist, and a widening cannot reach past this set. It is also the guard
# against a later contributor adding a code here without seeing what it enables.
#
# SQLITE_PRAGMA is refused as a whole class rather than by matching the pragma
# name. Two pragmas have to be refused (`writable_schema`, which makes the
# schema an ordinary writable table, and `journal_mode`, which can be used to
# discard a journal mid-transaction), this module needs none at all, and
# matching on a name string is the exact pattern that failed here before.
_NEVER_ALLOWED = frozenset({
    _SQLITE_DELETE, _SQLITE_UPDATE,
    _SQLITE_DROP_TABLE, _SQLITE_DROP_INDEX, _SQLITE_DROP_TRIGGER, _SQLITE_DROP_VIEW,
    _SQLITE_DROP_TEMP_TABLE, _SQLITE_DROP_TEMP_INDEX, _SQLITE_DROP_TEMP_TRIGGER,
    _SQLITE_DROP_TEMP_VIEW, _SQLITE_DROP_VTABLE,
    _SQLITE_ALTER_TABLE,
    _SQLITE_ATTACH, _SQLITE_DETACH,
    _SQLITE_PRAGMA,
    # A view or a temp table is somewhere to stage a copy of the rows, and a
    # second name for them that no table-name comparison anywhere would match.
    _SQLITE_CREATE_VIEW, _SQLITE_CREATE_TEMP_VIEW,
    _SQLITE_CREATE_TEMP_TABLE, _SQLITE_CREATE_TEMP_INDEX, _SQLITE_CREATE_TEMP_TRIGGER,
    _SQLITE_CREATE_VTABLE,
})

# What `init_audit_db` adds, for the CREATE statements in `_create_schema` and
# nothing else. REINDEX is on the list because SQLite issues it while building
# idx_audit_tenant, not because anything here reindexes.
_SCHEMA_INIT_ACTIONS = frozenset({
    _SQLITE_CREATE_TABLE,   # audit_events, plus sqlite_sequence for AUTOINCREMENT
    _SQLITE_CREATE_INDEX,
    _SQLITE_CREATE_TRIGGER,
    _SQLITE_REINDEX,
})


def _sqlite_authorizer(action_code: int, arg1, arg2, db_name, trigger_or_view):
    """Deny by default: refuse anything not on `_ALLOWED_ACTIONS`.

    Installed on every connection this module hands out. An action code added to
    a future SQLite -- one nobody here has considered -- is refused rather than
    permitted, which is the property the previous named-denials version did not
    have.
    """
    if action_code in _NEVER_ALLOWED:
        return sqlite3.SQLITE_DENY
    if action_code in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _schema_init_authorizer(action_code: int, arg1, arg2, db_name, trigger_or_view):
    """`_sqlite_authorizer` plus the CREATEs `init_audit_db` needs.

    Installed on one connection, immediately before the CREATE statements run,
    and replaced by `_sqlite_authorizer` immediately after them -- see
    `init_audit_db`. It is never installed on a connection a caller receives.

    Note what it still refuses: every member of `_NEVER_ALLOWED`, because it
    delegates rather than returning OK on a miss. So even inside the window
    there is no DROP TRIGGER, no ALTER TABLE, no ATTACH and no PRAGMA -- the
    widening is CREATE-shaped, and the 2026-07-31 chain needs a DROP and a
    RENAME it cannot get here.
    """
    # SQLite rewrites its own schema table while creating a table, an index or a
    # trigger, and reports that as an UPDATE against `sqlite_master`. It is the
    # one UPDATE this module ever needs and it is admitted by name, above the
    # delegation, because UPDATE is otherwise refused outright. The name
    # comparison fails safe in this direction: a name that does not match lands
    # in `_sqlite_authorizer`, which denies it.
    if action_code == _SQLITE_UPDATE and arg1 in _SCHEMA_TABLES:
        return sqlite3.SQLITE_OK
    if action_code in _SCHEMA_INIT_ACTIONS:
        return sqlite3.SQLITE_OK
    return _sqlite_authorizer(action_code, arg1, arg2, db_name, trigger_or_view)


def _get_connection() -> sqlite3.Connection:
    """Create a connection with recursive triggers on and the authorizer installed.

    **The order of these two statements is load-bearing and is not a mistake.**
    `PRAGMA recursive_triggers` has to be set *before* `set_authorizer`: after
    it, the authorizer refuses SQLITE_PRAGMA as a class and the statement raises
    `not authorized` (measured), so a reader who "tidies" the two lines into a
    more natural order breaks every connection this module makes rather than
    quietly weakening one. Moving the pragma into `init_audit_db`, where the
    rest of the schema setup lives, is the dangerous edit -- that one is silent,
    because it leaves connections working and reopens the REPLACE bypass below.
    It is a per-connection setting, not a property of the file, so it belongs
    here in the one place connections are made rather than once at startup.

    Why it is needed at all: SQLite reports `REPLACE` / `INSERT OR REPLACE` to
    the authorizer as action 18 (INSERT) only -- no DELETE, no UPDATE -- so the
    allowlist cannot see it coming. And with recursive triggers off (the
    default) the conflict resolution's implicit delete does not fire
    `audit_no_delete` either. Both defences missed it, and a caller supplying an
    existing `id` overwrote an audit row in place with nothing denied. With this
    on, that implicit delete reaches the trigger and aborts.
    """
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.set_authorizer(_sqlite_authorizer)
    return conn


def init_audit_db() -> None:
    """Create audit_events, its index and its two immutability triggers.

    Idempotent — every statement is CREATE ... IF NOT EXISTS — because
    `referral_loop/audit.py` re-runs it whenever `set_audit_db` moves the path.

    The CREATEs need permissions no other code path here gets, so they run
    inside a window: `_schema_init_authorizer` is installed on this one local
    connection just before them and `_sqlite_authorizer` is put back in a
    `finally` just after, before the commit and before anything else can run on
    it. The connection never leaves this function and is closed either way, so
    the widened authorizer has no lifetime beyond `_create_schema` and no other
    connection -- in this thread or any other -- is affected by it. Nothing
    inside the window is caller-supplied: the statements are literals.
    """
    os.makedirs(os.path.dirname(AUDIT_DB), exist_ok=True)
    with _db_lock:
        conn = _get_connection()
        try:
            conn.set_authorizer(_schema_init_authorizer)   # window opens
            try:
                _create_schema(conn)
            finally:
                conn.set_authorizer(_sqlite_authorizer)    # window closes
            conn.commit()
        finally:
            conn.close()


def _create_schema(conn: sqlite3.Connection) -> None:
    """The CREATE statements, factored out so the widened window in
    `init_audit_db` is one call wide and cannot accidentally grow to cover
    anything a later edit adds to that function."""
    # CHECK (id > 0) is the invariant "all stored ids are positive", and it is
    # what makes the auto-assign sentinel unreachable -- see audit_no_overwrite
    # below for why that matters. A CHECK on an INTEGER PRIMARY KEY sees the
    # *assigned* rowid rather than the pre-assignment sentinel (measured), so
    # ordinary appends satisfy it and only a caller-chosen non-positive id does
    # not.
    #
    # It does not reach databases that already exist: CREATE TABLE IF NOT EXISTS
    # does nothing to a table that is already there, and retrofitting a CHECK in
    # SQLite means rebuilding the table -- a DROP and a RENAME, the two things
    # this module exists to refuse. Those files are covered by the trigger's
    # own positivity gate instead, which is sufficient for the failure that
    # matters; the difference is recorded in the test for it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT CHECK (id > 0),
            event_type TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            inserted_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_tenant
        ON audit_events (tenant_id, timestamp)
    """)
    # The second layer. The authorizer does not travel with the file, so these
    # triggers are what a connection opened without this module runs into --
    # and dropping them is step one of the 2026-07-31 bypass, which is why
    # SQLITE_DROP_TRIGGER is in _NEVER_ALLOWED rather than merely off the
    # allowlist.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'IMMUTABLE: audit_events cannot be updated');
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'IMMUTABLE: audit_events cannot be deleted');
        END
    """)
    # The second half of the REPLACE fix, and the half that covers a connection
    # this module did not open. `recursive_triggers` is per-connection, so a
    # foreign connection's REPLACE still never reaches audit_no_delete; this
    # fires on the INSERT itself, which every REPLACE is, whatever the pragma
    # says. Either defence alone closes the hole -- both are here because they
    # cover different connections.
    #
    # It tests the *effect* of an id rather than whether one was syntactically
    # supplied, because `NEW.id` inside a BEFORE INSERT trigger holds a sentinel
    # for an auto-assigned row -- measured as -1 on SQLite 3.49.1 -- whose value
    # SQLite does not document. An ABORT keyed on that sentinel would abort
    # every legitimate append on any build that changes it.
    #
    # `NEW.id > 0` is the gate that makes the two clauses after it mean what
    # they say, and it is not optional. Without it, the sentinel is a value the
    # clauses can see: one accepted INSERT at id = -1 planted a row that clause
    # 1 then matched on *every* subsequent append (EXISTS ... WHERE id = -1),
    # turning the trail permanently unwritable -- and unrepairable, since the
    # poison row cannot be deleted through the authorizer or past
    # audit_no_delete. That was H-1, and it was worse than the REPLACE hole this
    # trigger was added to close.
    #
    # The general lesson is in the CHECK above: **all stored ids are positive**
    # is the invariant, and the gate is that invariant applied to the sentinel.
    # Enumerating the attacks anyone has thought of is what produced H-1; the
    # clauses below are only sound because no sentinel value can reach them.
    #
    #   clause 1  the id already exists -- an overwrite, the H4 harm exactly
    #   clause 2  the id is beyond what AUTOINCREMENT would hand out next. A row
    #             claiming the largest possible integer leaves the sequence with
    #             nowhere to go and SQLite answers `database or disk is full` to
    #             every later append: denial of service against an audit trail
    #             is the same harm as erasing it, reached from the other end.
    #
    # What it accepts, deliberately: exactly `seq + 1`, the id an ordinary
    # append would have been given. That is an append, not an overwrite, and it
    # is already available to anyone who can call log_guardrail_event.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_no_overwrite
        BEFORE INSERT ON audit_events
        WHEN NEW.id > 0
         AND (EXISTS (SELECT 1 FROM audit_events WHERE id = NEW.id)
              OR NEW.id > IFNULL((SELECT seq FROM sqlite_sequence
                                  WHERE name = 'audit_events'), 0) + 1)
        BEGIN
            SELECT RAISE(ABORT, 'IMMUTABLE: audit_events assigns its own ids');
        END
    """)


def log_guardrail_event(event: GuardrailAuditEvent) -> None:
    """Append an audit event. The only write this module performs.

    The columns are named explicitly and `id` is not among them: every row's id
    comes from AUTOINCREMENT. A caller reaching past this function to choose an
    id is constrained but not forbidden -- `CHECK (id > 0)` and
    `audit_no_overwrite` between them refuse any id that is non-positive, that
    already exists, or that runs ahead of the sequence. The one id still
    accepted is exactly the next one, which is what an ordinary append would
    have been given anyway.

    A caller reaching for UPDATE or DELETE on one of these connections gets
    `sqlite3.DatabaseError` ("not authorized") from the authorizer. A caller
    reaching for both at once via `REPLACE` or `INSERT OR REPLACE` gets neither
    -- SQLite reports that as an INSERT and nothing else -- and is stopped
    instead by a trigger, so the exception there is `sqlite3.IntegrityError`.
    Callers that distinguish the two should expect either; both are
    `sqlite3.Error`. See the module docstring for what this does and does not
    guarantee.
    """
    with _db_lock:
        conn = _get_connection()
        try:
            conn.execute(
                """
                INSERT INTO audit_events
                    (event_type, resource_type, resource_id, tenant_id, actor, outcome, detail, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_type,
                    event.resource_type,
                    event.resource_id,
                    event.tenant_id,
                    event.actor,
                    event.outcome,
                    event.detail,
                    event.timestamp,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def export_audit_trail(
    tenant_id: str | None = None,
    since: str | None = None,
    format: str = "json",
) -> list[dict]:
    """Export audit trail, optionally filtered by tenant and time.

    Returns list of event dicts.
    """
    clauses: list[str] = []
    params: list[str] = []

    if tenant_id:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _db_lock:
        conn = _get_connection()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"SELECT * FROM audit_events {where} ORDER BY id ASC", params
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
