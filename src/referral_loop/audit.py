"""Referral audit: a typed, allowlisted wrapper over guardrails/immutable_audit.

Spec section 3 routes referral audit through `guardrails/immutable_audit.py` and
nothing else. It owns its own append-only database and blocks UPDATE and DELETE
at the SQLite authorizer -- the property section 6 requires of `loop_events`
anyway. `db.py` and `audit_trail.py` are excluded, and the reason matters here:
`audit_trail.AuditEvent` carries a free-text `query_summary`, the same channel
shape that leaked through an unbounded free-text field in an earlier system.

`GuardrailAuditEvent` has that shape too. Its `detail` is documented as "JSON
string with additional context" and its `resource_id` is a bare string, so
importing the module and calling it directly would reintroduce exactly the
channel that motivated excluding `audit_trail`. **This module is the control.**

The invariant, written so it can be tested rather than reviewed
---------------------------------------------------------------
**No value written to the audit database is derived from an HL7 message.**
Every field is one of:

  * a loop reference this system minted from `uuid4`, shape-checked on the way
    in (`L-` or `O-` plus twelve hex digits) and replaced by `unminted`
    otherwise;
  * a rule pack version, read from a pack whose Ed25519 signature verified, and
    shape-checked as well;
  * a member of an `Enum` defined in this file;
  * an `int` or a `bool`;
  * the actor name and role a human typed into the worklist form.

Nothing else has a route in. No parameter anywhere in this module accepts an
MRN, a patient name, a note, an `OBX` value, an `MSH-10`, or a coordinator's
free-text reason. The wrapper does not sanitise those values -- it has no way to
receive them. A caller holding a sentinel string and wanting it in the audit
database has no argument to put it in, which is a stronger control than any
amount of filtering applied to a `dict` a caller composes.

That last exclusion is the important one. A dismissal and an acknowledgement
reversal both require a reason, and the reason is the single most useful thing
an auditor could read. It is also free text a human typed about a patient, so it
stays in `loop_events` -- append-only, tamper-triggered, on the same encrypted
volume -- and the audit records only *that* a reason was given. `worklist.py`
already keeps reasons off the page for this reason; the audit database is an
artifact too, and an exportable one, so it gets the same rule. An audit trail
that records PHI is worse than no audit trail, because it creates a second copy
in a place designed to be immutable and exportable -- and immutable means the
mistake cannot be taken back.

The one field carrying human-typed text is the actor, and it is less an
exception to the rule than the reason the rule has a boundary: an audit that
cannot say who acted is not an audit. It is bounded to `_MAX_ACTOR` characters
and collapsed to a single line, so a pasted chart note cannot land in it whole,
and it is already recorded verbatim in `loop_events` -- so the audit database
adds no exposure surface the event log does not already carry. It is not
sanitised beyond that, deliberately: names contain apostrophes and accents, and
a charset filter that mangles them buys nothing, because a determined typo puts
a patient name in the "your name" box whatever the charset.

What is audited, and what deliberately is not
----------------------------------------------
Audited: every action where a human changes clinical-facing state --
acknowledgement, its reversal, orphan dismissal -- plus patient merge and alias
reversal, which are the highest-consequence administrative actions here, plus
the rule pack in force at boot, because the pack decides what matches what.
Refusals are audited alongside successes. A refused acknowledgement on a
preliminary read is precisely the event a risk officer asks about, and an audit
that only records what succeeded cannot answer them.

Not audited: inbound HL7 messages. The raw archive already holds those verbatim
and durably (`raw_messages`, spec section 6), so copying them here would double
the PHI footprint for no added assurance -- and this module's whole claim is
that the audit database holds no message-derived value at all.

Why the guardrail module is loaded by path rather than imported
----------------------------------------------------------------
`from healthcare_rag.guardrails.immutable_audit import ...` executes
`healthcare_rag/guardrails/__init__.py`, which re-exports the whole stack --
including `tenant_isolation`, which spec section 3 says is deliberately **not**
imported, because "importing an unexercised isolation control would suggest a
guarantee the build does not test". A package's convenience re-exports would
otherwise settle an architectural question the spec settled the other way, and
`tests/referral_loop/test_import_closure.py` lists that module as forbidden.

So the one permitted module is loaded from its file. It is registered under its
canonical name first, so a process that also imports the guardrail package
normally binds to the same module object and therefore the same `_db_lock`:
two module objects would mean two locks over one file, which is worse than the
problem being avoided. Both orderings are asserted by test rather than assumed.

Failure policy: an audit failure never blocks the clinical action
------------------------------------------------------------------
Every write here is best-effort. If the audit database is unwritable -- full
disk, wrong permissions, a lock held by a stalled process -- the acknowledgement
still lands and the coordinator still clears the result. The failure is logged
at ERROR and counted (`write_failures`).

This is the opposite of the store's policy, and the asymmetry is the argument.
A `loop_events` write that fails **must** block, because the clinical fact would
otherwise be lost and the engine has to redeliver: spec section 6, never ACK
what cannot be stored. An audit write that fails loses a *duplicate*.
Attribution, role, reason and timestamp are already durable in `loop_events`,
which is itself append-only and under the same tamper triggers, so the audit
database is a second, cross-checkable copy rather than the record of last
resort.

Failing closed would mean an unwritable audit file stops coordinators
acknowledging results. Results then pile up unacknowledged, and the product
manufactures the exact scenario section 1 exists to prevent -- caused by the
control meant to evidence it. Between "a compliance artifact is missing a row,
loudly" and "a safety worklist is wedged", the first is recoverable and the
second is the failure mode this whole product is about.

Silently dropping is not on offer either: nothing here swallows an exception
without an ERROR log naming the action and the exception *type*. The type and
not `str(exc)` -- a SQLite message carries the database path, and log records
are an artifact spec test 14 greps.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# v1 is a single-site install and `guardrails/tenant_isolation.py` is
# deliberately not imported (spec sections 3 and 11): shipping an unexercised
# isolation control would suggest a guarantee the build does not test.
# GuardrailAuditEvent nonetheless requires a tenant_id, so it gets a constant.
#
# The constant is a literal and is deliberately not derived from the hostname,
# the install path or a customer name. Any of those would make it site-
# identifying, which is the one thing a value written to every audit row must
# not be -- and it would also read as a tenancy boundary, which is precisely the
# guarantee this build does not make. It comes in with multi-tenancy or not at
# all; until then it is a placeholder that says so.
REFERRAL_TENANT_ID = "referral-loop-single-site"

# Actors for the two actions no human performs. Literals, so an audit row's
# actor is either one of these or a name a person typed -- never a value
# assembled from a message.
SYSTEM_ACTOR = "referral-loop"          # boot-time pack load
ENGINE_ACTOR = "interface-engine"       # ADT^A40 arriving on the wire
SYSTEM_ROLE = "system"

# What a loop reference that this system did not mint is recorded as. A
# coordinator's browser can POST to /worklist/<anything>/acknowledge, so the
# refusal path receives a caller-controlled string; recording it verbatim would
# hand an audit database designed to be immutable whatever was in the URL.
# "an acknowledgement was refused against an id this system never minted" is
# both the honest fact and more use to an auditor than the string itself.
UNMINTED = "unminted"
UNKNOWN_VERSION = "unknown"

# Loop ids are minted in registry.py as f"L-{uuid4().hex[:12]}" (orphans "O-").
# Twelve hex digits of uuid4 are definitionally non-identifying, which is what
# makes echoing one into an exportable database safe.
_LOOP_REF_RE = re.compile(r"^[LO]-[0-9a-f]{12}$")

# A pack version comes from a pack whose signature verified, so it is already
# tamper-evident; the shape check is defence in depth against a signed-but-odd
# pack rather than against an attacker.
_PACK_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")

# Exception class names are code-derived, never data-derived.
_REFUSAL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

_MAX_ACTOR = 128
_MAX_ROLE = 64


class AuditAction(str, Enum):
    """The closed set of referral actions that reach the audit database."""

    ACKNOWLEDGED = "referral.acknowledged"
    ACKNOWLEDGEMENT_REVERSED = "referral.acknowledgement_reversed"
    ORPHAN_DISMISSED = "referral.orphan_dismissed"
    # A coordinator asserting that an unmatched result belongs to a loop, and a
    # coordinator asserting that the matcher attached one that does not. Both
    # change clinical-facing state on a human's say-so, which is the line this
    # module audits on; both also produce a label (spec section 7), and an
    # auditor asking "who decided this result belongs here" must not have to
    # read the training data to find out.
    ORPHAN_ATTACHED = "referral.orphan_attached"
    MATCH_UNDONE = "referral.match_undone"
    PATIENT_MERGED = "referral.patient_merged"
    MERGE_REVERSED = "referral.merge_reversed"
    PACK_LOADED = "referral.pack_loaded"
    # The one operation in this subsystem that destroys clinical records. It is
    # audited for the reason the others are not enough on their own: after a
    # retention purge runs, the rows that would evidence what it did are exactly
    # what is gone, so the only durable account of it is this one -- in a
    # database that is governed by a different, longer policy and that
    # immutable_audit will not let anything delete from.
    RETENTION_PURGED = "referral.retention_purged"


class Outcome(str, Enum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


class RefusalCode(str, Enum):
    """Refusals whose exception class alone does not say which rule fired.

    `registry.acknowledge` raises a bare `ReferralLoopError` for two different
    reasons, and the difference is the whole safety story: refusing because the
    loop is in the wrong state is bookkeeping, refusing because the read is
    preliminary is spec rule 1. An auditor cannot tell those apart from
    "ReferralLoopError", and the message that would tell them apart is exactly
    what must not be copied here.
    """

    PRELIMINARY_NOT_ACKNOWLEDGEABLE = "preliminary_not_acknowledgeable"
    WRONG_STATE = "wrong_state"
    # An orphan whose OBX-11 could not be read. Refusing the attachment is the
    # safe direction -- the orphan stays visibly queued rather than advancing a
    # loop to RESULTED on a status nobody could read -- and it is a different
    # fact from "wrong state", which is the distinction this enum exists for.
    UNREADABLE_RESULT_STATUS = "unreadable_result_status"


# The keys permitted in the JSON `detail` blob. Enforced at write time rather
# than by review: a contributor adding a key gets an error instead of a leak.
_DETAIL_KEYS = frozenset(
    {
        "action", "actor_role", "pack_version", "loops_moved", "reason_recorded", "refusal",
        # Retention. Four integers and nothing else: two counts of what was
        # removed and the two periods in force when it was, so the row is
        # self-describing to an auditor who cannot see the environment the purge
        # ran in. Counts and day-counts are definitionally non-identifying,
        # which is what makes widening the allowlist for them safe.
        "raw_deleted", "loops_deleted", "raw_retention_days", "resolved_retention_days",
    }
)

_RESOURCE_TYPE = {
    AuditAction.ACKNOWLEDGED: "referral_loop",
    AuditAction.ACKNOWLEDGEMENT_REVERSED: "referral_loop",
    AuditAction.ORPHAN_DISMISSED: "referral_loop",
    AuditAction.ORPHAN_ATTACHED: "referral_loop",
    AuditAction.MATCH_UNDONE: "referral_loop",
    AuditAction.PATIENT_MERGED: "referral_patient_merge",
    AuditAction.MERGE_REVERSED: "referral_patient_merge",
    AuditAction.PACK_LOADED: "referral_rule_pack",
    AuditAction.RETENTION_PURGED: "referral_retention",
}

# A merge touches a set of loops rather than one, and the identifiers that would
# name it -- the retired and surviving MRNs -- are the one part of the alias log
# that must not leave the building (worklist._recent_merges says the same). So
# the resource is the merge itself; `loops_moved` carries the size, and the
# MSH-10 join lives in mrn_alias_events, which the same auditor already has.
_MERGE_RESOURCE_ID = "patient-merge"

# A purge is site-wide, not per-loop, and the loops it names are the ones it
# deleted -- so there is no id to carry and nothing that would want one.
_RETENTION_RESOURCE_ID = "retention-purge"

# immutable_audit's own vocabulary (see GuardrailAuditEvent.event_type). Referral
# actions are all writes to clinical-facing state; the referral action itself is
# in detail["action"], and rows are selected by resource_type LIKE 'referral_%'.
_EVENT_TYPE = {
    Outcome.SUCCESS: "write",
    Outcome.DENIED: "deny",
    Outcome.FAILURE: "deny",
}


# --------------------------------------------------------------------- loading

_MODULE_NAME = "healthcare_rag.guardrails.immutable_audit"
_MODULE_PATH = Path(__file__).resolve().parent.parent / "guardrails" / "immutable_audit.py"

_load_lock = threading.Lock()
_init_lock = threading.Lock()
_initialised_for: str | None = None

_failure_lock = threading.Lock()
_write_failures = 0


def _module():
    """The guardrail audit module, loaded without executing its package.

    Checked against `sys.modules` first and registered there on load, so this
    and a normal `import healthcare_rag.guardrails` bind to one module object
    and therefore one `_db_lock`, in either order.
    """
    existing = sys.modules.get(_MODULE_NAME)
    if existing is not None:
        return existing
    with _load_lock:
        existing = sys.modules.get(_MODULE_NAME)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
        if spec is None or spec.loader is None:  # pragma: no cover - packaging fault
            raise ImportError(f"No loadable module at {_MODULE_PATH}")
        module = importlib.util.module_from_spec(spec)
        # Registered before exec, as the import system does, so a re-entrant
        # import during exec sees the partially initialised module rather than
        # starting a second one.
        sys.modules[_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[_MODULE_NAME]
            raise
        return module


def audit_db_path() -> str:
    """Where audit rows are being written. Useful to an operator and to the
    PHI proof, which greps the file on disk rather than the objects built here."""
    return str(_module().AUDIT_DB)


def set_audit_db(path: str | Path) -> None:
    """Point the audit trail at `path`.

    A deployment hook -- a site may want the audit database on a different
    volume from the package -- and the seam the tests use to keep a suite run
    out of the installed one. Call it at startup, before any audited action:
    it rebinds a module global that `immutable_audit._get_connection` reads per
    call, which is safe to set once and not safe to race with live writes.
    """
    global _initialised_for
    _module().AUDIT_DB = str(path)
    with _init_lock:
        _initialised_for = None


def write_failures() -> int:
    """Audit writes dropped this process. Non-zero means the audit database is
    behind the event log and someone has to look at the ERROR records."""
    return _write_failures


def _note_failure(action: AuditAction, outcome: Outcome, exc: BaseException) -> None:
    global _write_failures
    with _failure_lock:
        _write_failures += 1
        total = _write_failures
    # The exception *type*, never str(exc): a sqlite3 message carries the
    # database path and log records are an artifact spec test 14 greps.
    logger.error(
        "Referral audit write dropped for %s/%s (%s); the action itself succeeded and is "
        "in loop_events. %d audit write(s) dropped this process.",
        action.value, outcome.value, type(exc).__name__, total,
    )


def _ensure_initialised(module) -> None:
    """`init_audit_db` once per database path.

    Idempotent in the module itself -- every statement is CREATE ... IF NOT
    EXISTS -- so this is a cost guard, not a correctness one. Keyed by path so
    `set_audit_db` re-initialises rather than assuming the new file has a
    schema, and cleared on failure so a transient error does not disable
    auditing for the life of the process.
    """
    global _initialised_for
    path = str(module.AUDIT_DB)
    if _initialised_for == path:
        return
    with _init_lock:
        if _initialised_for == path:
            return
        module.init_audit_db()
        _initialised_for = path


# ----------------------------------------------------------------- normalising

def _loop_ref(value: object) -> str:
    if isinstance(value, str) and _LOOP_REF_RE.match(value):
        return value
    return UNMINTED


def _pack_version(value: object) -> str:
    if isinstance(value, str) and _PACK_VERSION_RE.match(value):
        return value
    return UNKNOWN_VERSION


def _refusal(value: object) -> str:
    if isinstance(value, RefusalCode):
        return value.value
    if isinstance(value, str) and _REFUSAL_RE.match(value):
        return value
    return "unspecified"


def _person(value: object, limit: int) -> str:
    """One line, bounded. See the module note on why this is not filtered further."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


# ----------------------------------------------------------------------- write

class AuditScope:
    """The only writable surface a caller gets inside an audited block.

    A closed set of typed attributes and nothing else -- `__slots__` makes one
    more an AttributeError rather than a field that quietly reaches `detail`.
    There is no attribute here that takes prose, and that is the point: the
    wrapper is a control because of what it cannot be handed, not because of
    what it filters. Every slot beyond `pack_version` and `refusal` holds an
    integer, so widening this set stays provably non-identifying.
    """

    __slots__ = (
        "loops_moved", "pack_version", "refusal",
        "raw_deleted", "loops_deleted", "raw_retention_days", "resolved_retention_days",
    )

    def __init__(self) -> None:
        self.loops_moved: int | None = None
        self.pack_version: str = ""
        self.refusal: RefusalCode | None = None
        # Retention (spec section 6). Counts of what a purge removed and the
        # periods in force when it did.
        self.raw_deleted: int | None = None
        self.loops_deleted: int | None = None
        self.raw_retention_days: int | None = None
        self.resolved_retention_days: int | None = None


def _emit(
    action: AuditAction,
    outcome: Outcome,
    *,
    loop_id: str,
    actor: str,
    role: str,
    reason_required: bool,
    scope: AuditScope,
    refusal: object = None,
) -> bool:
    """Append one row. Returns whether it landed. Never raises -- see the module
    note on why an audit failure must not block a coordinator."""
    try:
        module = _module()
        _ensure_initialised(module)

        if action in (AuditAction.PATIENT_MERGED, AuditAction.MERGE_REVERSED):
            resource_id = _MERGE_RESOURCE_ID
        elif action is AuditAction.RETENTION_PURGED:
            resource_id = _RETENTION_RESOURCE_ID
        elif action is AuditAction.PACK_LOADED:
            resource_id = _pack_version(scope.pack_version)
        else:
            resource_id = _loop_ref(loop_id)

        detail: dict[str, object] = {"action": action.value, "actor_role": _person(role, _MAX_ROLE)}
        if scope.pack_version:
            detail["pack_version"] = _pack_version(scope.pack_version)
        if scope.loops_moved is not None:
            detail["loops_moved"] = int(scope.loops_moved)
        for name in ("raw_deleted", "loops_deleted",
                     "raw_retention_days", "resolved_retention_days"):
            value = getattr(scope, name)
            if value is not None:
                # int(), so a value that is not a number raises here and drops
                # the row rather than writing whatever it was.
                detail[name] = int(value)
        if reason_required:
            # That a reason was given, never the reason. It is in loop_events.
            detail["reason_recorded"] = outcome is Outcome.SUCCESS
        if outcome is not Outcome.SUCCESS:
            detail["refusal"] = _refusal(scope.refusal or refusal)

        unknown = set(detail) - _DETAIL_KEYS
        if unknown:
            # A contributor added a key without adding it to the allowlist. Not
            # silently written: the allowlist is the control, and a control that
            # can be widened by forgetting is not one.
            raise ValueError(f"detail keys outside the allowlist: {sorted(unknown)}")

        module.log_guardrail_event(
            module.GuardrailAuditEvent(
                event_type=_EVENT_TYPE[outcome],
                resource_type=_RESOURCE_TYPE[action],
                resource_id=resource_id,
                tenant_id=REFERRAL_TENANT_ID,
                actor=_person(actor, _MAX_ACTOR) or SYSTEM_ACTOR,
                outcome=outcome.value,
                detail=json.dumps(detail, sort_keys=True, separators=(",", ":")),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        return True
    except BaseException as exc:  # noqa: BLE001 - deliberate, see module docstring
        _note_failure(action, outcome, exc)
        return False


@contextmanager
def audited(
    action: AuditAction,
    *,
    loop_id: str = "",
    actor: str = "",
    role: str = "",
    reason_required: bool = False,
):
    """Record exactly one row for one attempt, whatever the attempt does.

    Wrapping rather than calling at the end means a refusal cannot be forgotten
    -- and refusals are half of what this exists to record. The row is written
    after the body, so a success is only ever claimed once the append committed.

    `ReferralLoopError` is a refusal (`denied`); anything else is a fault
    (`failure`). Both are re-raised untouched: this observes the action, it does
    not handle it.
    """
    from .errors import ReferralLoopError

    scope = AuditScope()
    try:
        yield scope
    except ReferralLoopError as exc:
        _emit(action, Outcome.DENIED, loop_id=loop_id, actor=actor, role=role,
              reason_required=reason_required, scope=scope, refusal=type(exc).__name__)
        raise
    except BaseException as exc:
        _emit(action, Outcome.FAILURE, loop_id=loop_id, actor=actor, role=role,
              reason_required=reason_required, scope=scope, refusal=type(exc).__name__)
        raise
    _emit(action, Outcome.SUCCESS, loop_id=loop_id, actor=actor, role=role,
          reason_required=reason_required, scope=scope)


def referral_audit_entries() -> list[dict]:
    """Every referral row, oldest first. The export an auditor reads.

    Filtered on `resource_type` rather than tenant: the tenant is a constant
    here (see REFERRAL_TENANT_ID) and would select any other subsystem that ever
    picked the same string, whereas the resource types are this module's own.
    """
    module = _module()
    _ensure_initialised(module)
    return [
        row
        for row in module.export_audit_trail(tenant_id=REFERRAL_TENANT_ID)
        if str(row.get("resource_type", "")).startswith("referral_")
    ]


__all__ = [
    "REFERRAL_TENANT_ID",
    "SYSTEM_ACTOR",
    "ENGINE_ACTOR",
    "SYSTEM_ROLE",
    "UNMINTED",
    "UNKNOWN_VERSION",
    "AuditAction",
    "AuditScope",
    "Outcome",
    "RefusalCode",
    "audited",
    "audit_db_path",
    "referral_audit_entries",
    "set_audit_db",
    "write_failures",
]
