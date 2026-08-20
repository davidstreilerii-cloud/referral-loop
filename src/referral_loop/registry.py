"""The loop state machine.

A loop is an expectation that a result returns. Three transitions here are
clinical safety decisions rather than engineering choices, and all three are
enforced structurally rather than by convention:

  1. A preliminary read (OBX-11 = P) may reach RESULTED but never ACKNOWLEDGED.
  2. A corrected read (OBX-11 = C) returns an ACKNOWLEDGED loop to RESULTED.
  4. A coordinator may reverse their own acknowledgement.

ACKNOWLEDGED is v1's terminal state; CLOSED is reserved for v2 and unreachable.
The two are different claims (spec section 4). A coordinator can support "this
result belongs to this order"; only a clinically responsible actor can support
"this finding has been dispositioned", and nothing in v1 observes that. So
acknowledge() yields ACKNOWLEDGED and no path here reaches CLOSED -- the store
refuses the event type outright, and test 5 sweeps the state space to prove it.

Rule 1 is written as an allowlist, not as "not P". Only an explicitly final or
corrected read may be acknowledged, so an absent, unrecognised or future-dialect
OBX-11 fails safe instead of resolving the loop by default.

Rule 2 is generalised: *any* result arriving on an ACKNOWLEDGED loop reopens it
and clears the acknowledgement. A resent final that only moved ACKNOWLEDGED ->
RESULTED while leaving ack_at set would sit in neither open_loops() nor
resulted_unacknowledged() -- a loop on no worklist at all, which is the failure
this product exists to prevent.

Rule 4 exists because rule 2 only recovers the machine's error. A coordinator
who acknowledged the wrong loop had no way back: that loop stayed resolved while
the real one stayed open. reverse_acknowledgement appends, never mutates, so the
mistake and its correction both remain in the history.

Ordering. store.replay() orders events by arrival (event_id), because every
event was validated here at the moment it was applied, so arrival order is the
authoritative accepted sequence. That makes rejecting a clinically-older message
this module's job and nothing else's: see _refuse_if_stale.

An ADT^A40 is exempt from that guard, deliberately, in both directions. It is an
administrative correction about identity, not a clinical observation, and the
three consequences of treating it as one are all wrong:

  * A merge that advanced the watermark would make a result whose MSH-7 predates
    it look stale. Registration merges a patient at 14:00; an ORU generated at
    13:55 is still queued in the engine; that result is then refused and never
    lands. Same reasoning that keeps acknowledge() off the watermark.
  * A merge refused as stale strands loops on a retired MRN -- the precise
    failure the merge exists to prevent, caused by the guard meant to prevent
    regressions.
  * Nothing is gained, because merge_patient selects by *current* MRN. Loops
    already carried to B by a newer A->B are no longer on A, so a late A->C
    finds nothing and is a no-op without any timestamp being compared. Late
    ordering corrects itself structurally.

So MSH-7 travels with a merge under _MERGE_MESSAGE_AT, for the audit, and takes
no part in _clinical_watermark.

Aliasing. A merge moves the loops that exist when it is applied, which is only
half the problem: an interface engine keeps emitting the prior MRN afterwards,
for a while, and those orders would open loops on a retired identifier that no
query on the surviving patient returns. The other half is store.mrn_aliases, and
identity is resolved through it exactly ONCE, by the listener at ingest, before
this module or the matcher sees anything. Every MRN reaching this file is
already the surviving one; the pre-resolution value travels alongside as
submitted_mrn so the log still shows what the message said.

The decisions behind that table, each justified where it is implemented:
aliases do not expire and are undone only by an explicit reversal
(store.record_alias, reverse_merge below); they compress on write so resolution
is one lookup (store._apply_alias); a merge that would make an identity cyclic
is refused outright rather than resolved by rule (errors.CircularMergeError);
and resolution happens at one call site rather than at each writer.

The flywheel, and why undoing a match is not undoing an acknowledgement.
Spec section 7 makes coordinator judgements the corpus a pack revision is
evaluated against, so four of the actions here also append a row to
store.labels: attach_orphan, undo_match, reverse_acknowledgement and
dismiss_orphan.

undo_match and reverse_acknowledgement look adjacent and are not. They differ in
subject, in target state, and -- decisively -- in what they are evidence of:

  * reverse_acknowledgement is about the HUMAN's claim. A coordinator confirmed
    a match and now withdraws that confirmation. The result stays on the loop;
    only the vouching is taken back, so ACKNOWLEDGED returns to RESULTED and the
    loop re-queues for someone to confirm it properly.
  * undo_match is about the MATCHER's claim. A coordinator says this result was
    never this loop's. The result does not stay: the loop returns to awaiting
    one, and the detached result becomes an orphan again so a human can put it
    where it belongs.

Neither is expressible as the other. reverse_acknowledgement cannot detach a
result and undo_match cannot withdraw a confirmation, and an undo_match from
ACKNOWLEDGED is refused precisely so that a coordinator undoing a match a human
had already vouched for records both facts rather than silently discarding the
first. That case -- a false match somebody acknowledged -- is the worst one
available, and it is the one that most deserves two entries in the history.

Spec section 4 rule 4 calls every acknowledgement reversal "a labeled false
positive", and section 7 says the same of every undone auto-match. Read as one
rule that would put clerical error into false-match rate, which section 7 makes
an absolute release veto -- so a coordinator who mis-clicks would block a pack
release no pack change could unblock. They are therefore recorded as two label
types with two outcomes (events.LabelOutcome), which satisfies both sentences:
every reversal is labeled, and only what the matcher actually got wrong counts
as a false match.

Audit. Seven methods here change clinical-facing state on a human's or an
administrator's say-so -- acknowledge, reverse_acknowledgement, dismiss_orphan,
attach_orphan, undo_match, merge_patient, reverse_merge -- and each is wrapped
in `audit.audited`, which
appends one row to the immutable audit database per *attempt*, refusals
included. Wrapped rather than called at the end of the happy path, because a
refused acknowledgement on a preliminary read is exactly the event a risk
officer asks about, and an audit that only records successes cannot answer them.

Nothing else here is audited. The message-driven transitions -- open_loop,
orphan, schedule, unschedule, cancel, record_result -- are already held verbatim and durably
in the raw archive, so copying them into a second exportable database would
double the PHI footprint for no added assurance.

The audit never blocks a transition and never receives an MRN, a reason, or any
other message-derived string; audit.py owns both decisions and the reasoning.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from .audit import (
    ENGINE_ACTOR,
    SYSTEM_ROLE,
    AuditAction,
    RefusalCode,
    audited,
)
from .clock import MAX_CLOCK_SKEW, is_future_dated
from .core import machine
from .core.machine import RejectionReason, TransitionRejected
from .core.states import DocumentationStatus, ReferralState
from .core.transitions import ActorRef, AssertionSource, Transition
from .errors import (
    MrnRetiredError,
    NoAppointmentError,
    ReferralLoopError,
    StaleMessageError,
)
from .events import LabelType, Loop, LoopEvent, LoopState
from .migration import canonical_state, to_referral
from .store import LoopStore

logger = logging.getLogger(__name__)

# The OBX-11 values this system acts on at all, as one constant rather than two copies.
# record_result and attach_orphan both validate against it -- attach_orphan first, on
# the status it reads off the orphan record, where it yields the distinct
# UNREADABLE_RESULT_STATUS refusal that the generic one would lose. Neither copy was
# dead; they could not disagree because they were the same literal, and that was the
# hazard: one predicate in two places diverges on the next edit.
_HANDLED_RESULT_STATUSES = frozenset({"P", "F", "C"})

# OBX-11 result status codes we act on. HL7 table 0085.
PRELIMINARY = "P"
FINAL = "F"
CORRECTED = "C"



# How the machine's refusal reasons land in the audit. The audit's vocabulary predates the
# machine and is what a risk officer asks about by name, so the mapping goes this way round
# rather than teaching audit.py a second set of codes for the same two facts.
_REFUSAL_FOR = {
    RejectionReason.NOT_A_LEGAL_TRANSITION: RefusalCode.WRONG_STATE,
    RejectionReason.PRELIMINARY_NOT_RECONCILABLE: RefusalCode.PRELIMINARY_NOT_ACKNOWLEDGEABLE,
    # Unreachable from acknowledge, which always asserts HUMAN, but mapped rather than
    # left to KeyError: a future caller routing a non-human reconciliation through here
    # should get an audited refusal, not a crash inside the except clause.
    RejectionReason.RECONCILE_REQUIRES_A_HUMAN: RefusalCode.WRONG_STATE,
}

# Only these event types carry a result. A merged_in event (Task 7) copies
# fields off another loop, and an orphaned event carries caller-supplied detail;
# if either could set the result status, a merge or an orphan would flip a
# preliminary to final with no result ever arriving. "reversed" is excluded
# deliberately: undoing an acknowledgement changes who vouched for the match, not
# what the radiologist read.
_RESULT_EVENTS = frozenset({"resulted", "reopened"})


# The only state a match may be undone from. Not ACKNOWLEDGED, deliberately: see
# the module note on undo_match versus reverse_acknowledgement.
# NOT superseded. Task 5 retired five from-state frozensets because each was a second
# opinion on state legality that core.machine already owned and could drift from; this
# one survived because it is not that. It enforces a rule the machine has no way to
# express -- see below -- so retiring it would lose the rule rather than deduplicate it,
# which is why it is live code and deliberately not labelled like the five that went.
#
# No count is offered of what else guards a from-state anywhere in this file. Several
# methods make an inline check, they answer to their own reasons, and a tally here would
# be stale the first time somebody added one without knowing to come and update it.
#
# undo_match resists expression as a Transition for a reason that is a finding rather
# than an obstacle: it detaches a result and mints a replacement orphan, so it is one
# operation over *two* aggregates, and machine.apply() takes one Referral and returns
# one. Its referral half would also need DOCUMENTED -> SENT, a backward edge
# deliberately absent from LEGAL_TRANSITIONS -- adding it to admit this one operation
# would weaken "a result cannot be un-ordered" for every other caller.
# Design spec 6.5 shaped, and therefore Plan 2c's.
_UNMATCHABLE_FROM = frozenset({LoopState.RESULTED})

# Detail key naming the orphan a result was attached from. Its presence is what
# distinguishes a matcher's false positive from a human's mistaken attachment
# when the result is later detached -- see undo_match.
_ATTACHED_FROM = "attached_from"

# Detail key carrying the tier a match fired at, written by the listener onto
# the resulting event. The single most useful feature on a false-match label: it
# names which rule misfired, which is what a pack revision has to act on.
_MATCH_TIER = "match_tier"

# Where an orphan's OBX-11 lives. The listener writes `result_status`;
# `obx11` is accepted as a second name because hand-built orphans in this
# codebase and in the plan use it, and an orphan whose status cannot be read is
# refused attachment rather than defaulted -- so a silent disagreement between
# the two names would refuse real work instead of merely reading nothing.
_ORPHAN_STATUS_KEYS = ("result_status", "obx11")


# The actor a message-driven transition is attributed to. A device rather than a
# person: the HL7 interface asserted this, and naming a coordinator would put a human
# behind a claim no human made. Spec 8.2 always carries a Device agent for the same
# reason.
_ENGINE_ACTOR_REF = ActorRef(kind="device", id="referral-loop")

# Detail key holding the clinical timestamp of the message that caused an event
# (MSH-7). Present only on message-driven events -- human actions must never
# advance the watermark, or an acknowledgement today would make tomorrow's
# correction look stale and safety rule 2 would silently stop working.
_MESSAGE_AT = "message_at"

# MSH-7 of an ADT^A40, recorded under its own key so the merge stays auditable
# without joining the clinical watermark. See merge_patient and the module note
# on why an identity correction is not a clinical observation.
_MERGE_MESSAGE_AT = "merge_message_at"

# Ranks an event for which no clinical time can be established at all -- one
# appended before any trusted stamp exists on the loop. Never compared against a
# real timestamp: the leading flag in _latest_result_event's key separates the
# two classes first, so this only orders such events against each other.
_NO_CLINICAL_TIME = datetime.min.replace(tzinfo=timezone.utc)

# Written by any event that supersedes an acknowledgement.
_CLEARED_ACK = {"ack_by": "", "ack_role": "", "ack_at": ""}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are treated as UTC rather than compared against aware
    ones, which raises TypeError. An HL7 MSH-7 frequently carries no offset, and
    a TypeError inside the ordering guard would send every such message down the
    AE path permanently."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class Registry:
    def __init__(self, store: LoopStore, pack_version: str = ""):
        self.store = store
        # Stamped on every label. A label whose pack version is unknown cannot
        # be attributed to the rules that produced the judgement, which is what
        # spec section 7 evaluates a revision against -- so a deployment passes
        # `pack.version` here, and an unset one records "unknown" rather than
        # inventing a value. Kept as a plain string rather than a RulePack:
        # nothing else in this module needs the pack, and holding one would
        # invite matching decisions to migrate into the state machine.
        self.pack_version = pack_version
        # Every rule here is check-then-append, and the check is worthless if
        # another thread appends between the two. Concretely: a correction and
        # an acknowledgement racing each other both pass their checks, the
        # acknowledgement lands second, and the loop is resolved on a
        # superseded read -- rule 2 defeated with no error raised anywhere.
        # socketserver hands each connection to the handler, and an interface
        # engine routinely holds several. Scope: one process. Two processes on
        # one database file are not serialized by this, and the store's own note
        # on _materialize says the same; that is a deployment constraint, not
        # something this module can express.
        self._lock = threading.RLock()
        # A counter, not metrics plumbing -- the same shape the listener's
        # counters have. A message dated beyond MAX_CLOCK_SKEW is applied
        # without advancing the watermark (see _stamp), and that decision has to
        # be countable: a stamp dropped silently is exactly how an unbounded
        # MSH-7 poisoning a loop forever went unnoticed in the first place.
        self.future_dated_message_count = 0

    def get(self, loop_id: str) -> Loop:
        return self.store.replay(loop_id)

    # No resolve_mrn here, deliberately. Identity is resolved exactly once, by
    # the listener at ingest, through LoopStore.resolve_mrn -- a delegate on this
    # class would invite a second call site, and two of those eventually
    # disagree about who a message is about.

    # ---------------------------------------------------------------- creation

    def open_loop(
        self,
        mrn: str,
        control_id: str,
        modality: str = "",
        placer_order_number: str = "",
        filler_order_number: str = "",
        service_code: str = "",
        ordering_provider: str = "",
        ordered_at: datetime | None = None,
        loop_id: str | None = None,
        message_at: datetime | None = None,
        submitted_mrn: str = "",
    ) -> str:
        """Open a loop. `mrn` must already be resolved (store.resolve_mrn).

        `submitted_mrn` is what the message carried before the listener resolved
        it, and defaults to `mrn` when nothing was resolved, so the field is
        always present -- "absent means unchanged" is exactly the implicit
        encoding an auditor cannot distinguish from "not written yet".
        """
        if not mrn:
            # A loop with no MRN is returned by no patient query and reviewed by
            # nobody. An unattributable result belongs in the orphan queue,
            # where it is explicitly visible as unattached.
            raise ReferralLoopError(
                "Refusing to open a loop with no MRN: it would be invisible to every "
                "patient-scoped query. Use orphan() for an unattributable result."
            )
        loop_id = loop_id or f"L-{uuid.uuid4().hex[:12]}"
        with self._lock:
            if self.store.events_for(loop_id):
                # A second "created" replays to OPEN, so a duplicate order
                # message with a fresh MSH-10 would reset a resulted loop and
                # drop its result out of sight.
                raise ReferralLoopError(
                    f"Loop {loop_id} already exists; a second 'created' event would reset its state"
                )
            # A guard, not a second resolution point: it never chooses an
            # identity, it only refuses one that has stopped being current.
            # Resolution happens at ingest, and a merge can commit between there
            # and here -- microseconds, but the loser is a loop stored on a
            # just-retired MRN, invisible to every query on the surviving
            # patient and to this merge's straggler scan, which has already run.
            # Raising turns that silent invisibility into a loud, retryable
            # error the listener answers by re-resolving and resending.
            if self.store.resolve_mrn(mrn) != mrn:
                # MrnRetiredError, not a bare ReferralLoopError: this refusal is
                # retryable and every other failure in this method is not, and
                # the listener has to answer AE here and AA elsewhere. Still a
                # ReferralLoopError, so existing callers catching that keep
                # working.
                #
                # The control id and not the MRN, for the reason merge_patient
                # states thirty lines further down and audit finding M4
                # records: the listener logs `%s` of this exception, a log
                # record is one of the artifacts spec test 14 greps, and an
                # identifier is an identifier whether it arrived on a page or
                # in a log line. The MSH-10 names the message and the message
                # is in the raw archive.
                raise MrnRetiredError(
                    f"The MRN carried by {control_id} was retired between ingest and this "
                    "write; re-resolve and retry. Storing the loop here would hide it from "
                    "the surviving patient."
                )
            self.store.append_event(
                LoopEvent(
                    loop_id=loop_id,
                    event_type="created",
                    occurred_at=_now(),
                    control_id=control_id,
                    detail=self._stamp(
                        {
                            # Already the surviving identifier: the listener
                            # resolves once at ingest, before this module sees
                            # anything. Resolving again here would be a second
                            # call site, and two of those eventually disagree.
                            "mrn": mrn,
                            # What the message actually said. An auditor asking
                            # why this loop sits on a patient the message never
                            # named needs the answer in the log, not in the
                            # listener's memory.
                            "submitted_mrn": submitted_mrn or mrn,
                            "modality": modality,
                            "placer_order_number": placer_order_number,
                            "filler_order_number": filler_order_number,
                            "service_code": service_code,
                            "ordering_provider": ordering_provider,
                            # Defaulted rather than left None: staleness (Task 9)
                            # measures from ordered_at, and a loop with no clock
                            # would never become stale -- it would age silently.
                            "ordered_at": (ordered_at or _now()).isoformat(),
                        },
                        message_at,
                        control_id,
                    ),
                )
            )
        return loop_id

    def orphan(
        self,
        control_id: str,
        mrn: str,
        detail: dict,
        message_at: datetime | None = None,
        submitted_mrn: str = "",
    ) -> str:
        """Create a loop-shaped record to hold a result nobody ordered.

        ORPHAN is not in _CLOSEABLE_FROM, so an orphan cannot be acknowledged
        away; it is retired by attachment to a real loop (Task 15).
        """
        loop_id = f"O-{uuid.uuid4().hex[:12]}"
        # mrn last: the explicit argument wins over a stray key in detail, so a
        # match key cannot silently re-attribute the record to another patient.
        # Already resolved by the listener, like open_loop's.
        self.store.append_event(
            LoopEvent(
                loop_id, "orphaned", _now(), control_id,
                self._stamp(
                    {**detail, "mrn": mrn, "submitted_mrn": submitted_mrn or mrn},
                    message_at,
                    control_id,
                ),
            )
        )
        return loop_id

    def merge_patient(
        self,
        prior_mrn: str,
        surviving_mrn: str,
        control_id: str,
        message_at: datetime | None = None,
    ) -> list[str]:
        """ADT^A40. Move every loop from the prior MRN to the surviving one.

        Spec section 4 rule 3, and the failure that breaks most homegrown
        trackers: a loop that does not follow the surviving identifier is
        returned by no query on the patient who still exists, so it vanishes
        from the worklist while remaining clinically open -- and the tool then
        reports all-clear on it. Every loop moves, in every state. An
        ACKNOWLEDGED or DISMISSED record stranded on a retired MRN corrupts the
        audit trail just as badly as an open one vanishing, and an ORPHAN left
        behind can never be attached, because the coordinator searching the
        surviving MRN does not see it.

        State never changes: "merged_in" is in store._NON_TRANSITIONAL, so
        replay carries the detail and skips the transition. A merge cannot
        resurrect a CANCELLED loop, retire an open one, or clear an
        acknowledgement.

        An unknown surviving MRN is not an error (failure matrix). There is no
        patient table here -- an MRN exists exactly insofar as loops carry it --
        so the surviving record is created by the carry itself, and logged.

        Two things happen, in this order: the alias is recorded, then the
        existing loops are carried. The alias is what makes *future* orders on
        the retired MRN land correctly; the carry is what fixes the ones already
        here. Neither alone is sufficient.

        Returns the loop ids moved. Empty is a legitimate outcome: an A40 for a
        patient with no loops, or the same A40 delivered twice.

        Not atomic across loops, and does not need to be. append_event owns one
        transaction per loop, so a store failure partway leaves some loops moved
        and some not. The engine gets AE and resends; because the alias is
        already durable and the resend rescans the whole chain, it picks up
        exactly the loops still behind and finishes the job. Resumable rather
        than all-or-nothing -- which is the right shape here, since a merge held
        open across thousands of loops in one transaction would block the
        interface instead.
        """
        # Message-driven, so the actor is the engine rather than a person, and
        # neither MRN reaches the audit: the identifiers are the one part of the
        # alias log that must not leave the building. What is recorded is that a
        # merge happened, when, and how many loops it moved -- the MSH-10 join
        # lives in mrn_alias_events, which the same auditor already has.
        with audited(
            AuditAction.PATIENT_MERGED, actor=ENGINE_ACTOR, role=SYSTEM_ROLE
        ) as scope:
            return self._merge_patient(prior_mrn, surviving_mrn, control_id, message_at, scope)

    def _merge_patient(self, prior_mrn, surviving_mrn, control_id, message_at, scope) -> list[str]:
        """The body of merge_patient, split out only so the audit wraps it.

        Same reason as _reverse_merge: the audited block must cover the circular-
        merge refusal too, and re-indenting the whole method to get that would
        make the diff unreadable.
        """
        if not prior_mrn or not surviving_mrn:
            # An empty surviving MRN would blank the identifier on every loop it
            # touched, which open_loop refuses for exactly the same reason: the
            # loop is then returned by no patient-scoped query. An empty prior
            # MRN would select every unattributed orphan and sweep them onto a
            # patient at random.
            # Which endpoint is missing, not what the other one holds. The
            # rule stated for the merge-into-itself warning below applies here
            # too and this refusal predated it (M4): `handle` catches it as a
            # plain ReferralLoopError and logs `%s`, so the surviving MRN went
            # into a log file every time an interface sent an A40 with an
            # empty MRG-1.
            raise ReferralLoopError(
                f"ADT^A40 {control_id}: a merge needs both a prior and a surviving MRN "
                f"(prior empty={not prior_mrn}, surviving empty={not surviving_mrn})"
            )

        if prior_mrn == surviving_mrn:
            # A no-op, not a failure: nothing moves and nothing is stranded. Not
            # raised, because a ReferralLoopError out of the listener is a
            # message the engine will represent forever, and this one will never
            # become acceptable. Logged because it is more likely to mean the
            # MRG-1 field map is wrong than that registration really merged a
            # patient into themselves.
            # The control id and not the MRN. A log record is one of the four
            # artifacts spec test 14 greps, and an identifier is an identifier
            # whether it arrived on a page or in a log line; the MSH-10 is
            # enough to find the message, and the message is in the archive.
            logger.warning(
                "ADT^A40 %s merges an MRN into itself; no loops moved", control_id
            )
            scope.loops_moved = 0
            return []

        moved: list[str] = []
        # Held across the whole merge, not per loop, and across the alias write
        # too. Otherwise an ORM arriving mid-merge opens a loop on the prior MRN
        # after the scan has passed it and before the alias exists to redirect
        # it -- stranded on a retired identifier at the one moment nobody is
        # looking for it. With both inside the lock there is no such window: an
        # order either lands before the merge and is carried by the scan, or
        # after it and is resolved by the alias.
        with self._lock:
            # BEFORE the loops move, deliberately. append_event owns one
            # transaction per loop, so the alias and the moves cannot share one;
            # of the two orderings only this one fails safe. Alias first: a
            # crash leaves the alias durable and some loops unmoved, so new
            # orders already resolve correctly and the resend finishes the moves.
            # Loops first: a crash leaves loops moved and no alias, which is
            # precisely the invisibility gap this table was added to close.
            #
            # Raises CircularMergeError if the claim contradicts one on file.
            # Nothing is written and nothing moves -- the refusal has to happen
            # before the first append, not partway through.
            applied = self.store.record_alias(prior_mrn, surviving_mrn, _now(), control_id)
            surviving = self.store.resolve_mrn(surviving_mrn)
            if applied is None:
                logger.info(
                    "ADT^A40 %s: the prior MRN is already retired into the surviving one; "
                    "no new alias recorded", control_id,
                )

            if not self.store.loops_for_mrn(surviving):
                logger.info(
                    "ADT^A40 %s: surviving MRN is unknown here; carrying loops onto it anyway",
                    control_id,
                )

            # Every identifier retired into this patient, not only the one the
            # message named. Two cases need that: compression, where A and B may
            # both now point at C, and a merge interrupted partway, whose loops
            # sit on an identifier the alias already resolves past. Scanning the
            # preimage is what makes the resend complete the job -- and it is one
            # indexed lookup, not a walk.
            stragglers = [
                loop
                for source in self.store.retired_into(surviving)
                for loop in self.store.loops_for_mrn(source)
            ]
            for loop in stragglers:
                self.store.append_event(
                    LoopEvent(
                        loop_id=loop.loop_id,
                        event_type="merged_in",
                        occurred_at=_now(),
                        control_id=control_id,
                        detail=self._stamp_merge(
                            {
                                # The RESOLVED survivor, not the identifier the
                                # message named. With compression those differ
                                # whenever an A40 arrives against an already
                                # retired MRN, and storing the message's version
                                # would put the loop on an MRN that resolves
                                # elsewhere -- reintroducing the split patient.
                                "mrn": surviving,
                                # Where this loop actually was, which is not
                                # always the prior MRN the message named: a
                                # straggler may be sitting on a third identifier
                                # compressed onto the same patient. The reversal
                                # reads this to know what to carry back.
                                "merged_from_mrn": loop.mrn,
                                "submitted_prior_mrn": prior_mrn,
                            },
                            message_at,
                            control_id,
                        ),
                    )
                )
                moved.append(loop.loop_id)

        logger.info("ADT^A40 %s: carried %d loop(s) to the surviving MRN", control_id, len(moved))
        scope.loops_moved = len(moved)
        return moved

    def reverse_merge(
        self, retired_mrn: str, actor: str, role: str, reason: str, control_id: str = ""
    ) -> list[str]:
        """Undo an ADT^A40 administratively. Returns the loop ids carried back.

        Aliases never expire, so without this a merge sent in error is
        permanent -- and "these two records are one patient" is exactly the kind
        of assertion registration sometimes gets wrong. It is the same shape as
        rule 4's acknowledgement reversal: an explicit human act, appended and
        never deleted, with actor, role and reason required, so the merge and its
        undoing both stay legible in the log.

        The loops go back too, and that is the part that matters clinically. An
        alias reversal alone would stop redirecting new orders while leaving the
        merged loops attributed to the surviving patient -- one patient's
        referrals sitting on another's chart, which is worse than the merge it
        was meant to undo. Only the loops this merge carried move back, found by
        the merged_from_mrn each one recorded; loops that arrived on the
        surviving MRN in their own right stay where they are.

        State is untouched, here as everywhere: the carry-back is another
        non-transitional merged_in.
        """
        with audited(
            AuditAction.MERGE_REVERSED, actor=actor, role=role, reason_required=True
        ) as scope:
            carried = self._reverse_merge(retired_mrn, actor, role, reason, control_id)
            scope.loops_moved = len(carried)
            return carried

    def _reverse_merge(
        self, retired_mrn: str, actor: str, role: str, reason: str, control_id: str
    ) -> list[str]:
        """The body of reverse_merge, split out only so the audit wraps it.

        Split rather than indented: the audited block has to cover the whole
        attempt including the refusals reverse_alias raises, and re-indenting
        forty lines would bury that in a diff nobody can read.
        """
        with self._lock:
            retired, surviving = self.store.reverse_alias(
                retired_mrn, actor, role, reason, control_id
            )

            carried: list[str] = []
            at = _now()
            for loop in self.store.loops_for_mrn(surviving):
                events = [e for e in self.store.events_for(loop.loop_id)
                          if e.event_type == "merged_in"]
                if not events or events[-1].detail.get("merged_from_mrn") != retired:
                    continue
                self.store.append_event(
                    LoopEvent(
                        loop_id=loop.loop_id,
                        event_type="merged_in",
                        occurred_at=at,
                        control_id=control_id,
                        detail={
                            "mrn": retired,
                            "merged_from_mrn": surviving,
                            "merge_reversed_by": actor,
                            "merge_reversed_role": role,
                            "merge_reversed_reason": reason,
                            "merge_reversed_at": at.isoformat(),
                        },
                    )
                )
                carried.append(loop.loop_id)

        logger.warning(
            "A patient merge was reversed by %s under control id %s; %d loop(s) returned "
            "to the previously retired identifier. Both identifiers are in mrn_alias_events.",
            actor, control_id, len(carried),
        )
        return carried

    # -------------------------------------------------------------- transitions

    def _documentation(self, loop_id: str) -> DocumentationStatus | None:
        """What condition this loop's documentation is in, folded from its own events.

        Reuses _latest_result_status, which reuses _latest_result_event. That ordering --
        newest by clinical time, an event with no clock of its own inheriting the newest
        clinical time established at its own point in the log, ties on append index -- took
        three review rounds during the security audit, and two successive bugs came from
        breaking the rule that a message time is never compared against an arrival time.
        It is not re-derived here. This method maps its answer into the domain vocabulary
        and does nothing else.

        An unreadable status folds to None rather than to PRELIMINARY. Both are refused by
        machine's allowlist, so the safety outcome is identical; None is the more honest of
        the two because the log did not assert a preliminary read, it asserted something
        this system could not read. Fabricating PRELIMINARY would put a clinical claim in
        the aggregate that no message ever made.
        """
        status = self._latest_result_status(loop_id)
        try:
            return DocumentationStatus(status) if status else None
        except ValueError:
            return None

    def _refuse_illegal_transition(
        self,
        loop: Loop,
        to_state: ReferralState,
        source: AssertionSource,
        actor: ActorRef,
        occurred_at: datetime,
    ) -> Transition:
        """Ask `core.machine` whether this move is legal, and raise if it is not.

        Returns the validated `Transition` so the caller hands it to
        `store.append_event(..., transition=...)`, which writes the provenance row on the
        same connection and the same commit as the loop_events append and the projection
        update. Task 5 moved the *decision* here and left the write behind, and that gap
        survived a green suite for four methods because routing the decision is observable
        and routing the write is not -- every test asserted on state, and state still came
        from replaying loop_events. Task 4b closed it.

        The `Referral` apply() returns is still discarded: loop_events drives replay until
        Plan 2c collapses the two logs.

        A loop whose state left the referral vocabulary under spec 6.5 -- ORPHAN,
        DISMISSED, ATTACHED -- and the reserved CLOSED both make `canonical_state` raise
        `ValueError`. That is converted here rather than allowed out, because listener.py
        catches `ReferralLoopError` one clause above a bare `except Exception` and answers
        the sending engine differently in each; an orphan refusing a schedule must keep
        answering what it answers today.

        `documentation` is whatever `to_referral` produces, which is `None`: the legacy
        row has no such column and the fold that populates it is Task 4's. That is safe
        for every `to_state` except `RECONCILED`, whose guard reads it -- so `acknowledge`
        cannot route through here until that fold exists. See the Task 5 note in the plan.
        """
        try:
            state = canonical_state(loop.state)
        except ValueError as exc:
            raise ReferralLoopError(
                f"Loop {loop.loop_id} is in state {loop.state.value}, which has no referral "
                f"lifecycle to move: {exc}"
            ) from exc
        if not isinstance(state, ReferralState):
            raise ReferralLoopError(
                f"Loop {loop.loop_id} is in state {loop.state.value}, which is an inbound "
                "artifact's state and not a referral's, so it has no transition to make "
                "(design spec section 6.5)"
            )

        # Non-empty: self.get() raises LoopNotFoundError before this is reached, and a
        # loop exists only by virtue of having events.
        events = self.store.events_for(loop.loop_id)
        referral = to_referral(loop, state_occurred_at=events[-1].occurred_at, seq=len(events))
        referral = replace(referral, documentation=self._documentation(loop.loop_id))
        transition = Transition(
            to_state=to_state,
            assertion_source=source,
            actor=actor,
            evidence=(),
            occurred_at=occurred_at,
            recorded_at=occurred_at,
            hold=None,
            rationale=None,
        )
        machine.apply(referral, transition)
        return transition

    def schedule(self, loop_id: str, control_id: str, message_at: datetime | None = None) -> None:
        with self._lock:
            loop = self.get(loop_id)
            transition = self._refuse_illegal_transition(
                loop,
                ReferralState.SCHEDULED,
                # An SIU is the receiving organisation telling us it booked the patient.
                AssertionSource.RECEIVING_ORG,
                _ENGINE_ACTOR_REF,
                message_at or _now(),
            )
            self._refuse_if_stale(loop_id, message_at, "schedule")
            self.store.append_event(
                LoopEvent(
                    loop_id, "scheduled", _now(), control_id,
                    self._stamp({}, message_at, control_id),
                ),
                transition=transition,
            )

    def unschedule(self, loop_id: str, control_id: str, message_at: datetime | None = None) -> None:
        """An SIU^S15: the appointment went away. The referral did not.

        Spec 6.1 glosses CANCELLED as "the referring side withdraws" -- a clinical
        judgement by a person here that this referral is dead. An S15 is a counterparty
        scheduler reporting that a booking no longer exists, which is close to the
        opposite: the patient still needs the visit, so somebody has to book it again.
        Collapsing the two onto CANCELLED meant the most benign path there is -- a patient
        rings the specialist's office and moves their CT -- drove a clinically open
        referral into a state that appears in neither open_loops() nor
        resulted_unacknowledged(), and that no later message can undo because CANCELLED is
        terminal in LEGAL_TRANSITIONS. On no queue, permanently, with every existing guard
        satisfied.

        So an S15 returns the referral to ACCEPTED: the receiving organisation still
        holds it and has not booked it, which is exactly what a cancelled appointment
        leaves behind. **Not back to where it was before it was booked** -- the chain
        said SENT then, and SCHEDULED -> SENT is deliberately absent from
        LEGAL_TRANSITIONS. Nor would it be right: an organisation that booked a patient
        has demonstrably accepted the referral, and a cancelled slot does not un-accept
        it.

        Two vocabularies meet in that sentence, so: ACCEPTED is the *transition chain*.
        The **projection** goes to `LoopState.OPEN`. Not SCHEDULED, which is in
        `_OPEN_STATES` too and would keep the referral on the worklist just as well --
        it is the state being *left*, and projecting there would re-assert the booking
        this message says has gone away. OPEN keeps the referral on the queue without
        claiming an appointment for it, which is the whole of what an S15 asserts.

        `migration.canonical_state` reads that OPEN back as SENT, not as the ACCEPTED
        the chain holds, because the legacy nine-state vocabulary has no member for
        ACCEPTED at all (`migration.WITHOUT_LEGACY_SOURCE`). So chain and projection
        disagree here, and spec 9.3 invariant 2 does not hold for an unscheduled
        referral -- pinned by
        test_an_unscheduled_referral_is_the_one_place_invariant_two_does_not_hold and
        resolved when Plan 2c collapses the two logs.

        That divergence is a disagreement between the two vocabularies about a state
        that genuinely exists. It is not the same thing as
        `reverse_acknowledgement` and `undo_match`, which also end with chain and
        projection apart but for an unrelated reason: both append a projection-moving
        event with no `transition=` at all, so the chain never records the move rather
        than recording it in a word the projection cannot spell. A reader of 2c needs
        the difference -- this one closes when ACCEPTED becomes expressible, those two
        close only when something writes their transitions.

        Either way the loop keeps ageing on the coordinator's queue, which is the
        definition of work outstanding. `cancel` keeps CANCELLED for the withdrawal it
        always meant.
        """
        with self._lock:
            loop = self.get(loop_id)

            # There has to have BEEN an appointment for one to be cancelled, and this is
            # the guard core.machine cannot make. LoopState.OPEN maps to
            # ReferralState.SENT, and SENT -> ACCEPTED is a legal edge for its own
            # unrelated and correct reason: a receiving organisation accepting a referral
            # it was sent. So an S15 naming a loop nobody ever booked would be applied
            # and would record an *acceptance* the receiving org never asserted, off a
            # message that said the opposite.
            #
            # This is NOT a regression to the from-state frozensets Task 5 retired. Those
            # were a second opinion on *state legality*, which core.machine owns, which is
            # exactly why they could drift out of agreement with it. This asks a different
            # question -- whether the evidence for the assertion exists at all -- and it
            # is the class of refusal listener._target_loop makes when it declines
            # ambiguous evidence, not the class the machine makes. `_UNMATCHABLE_FROM`
            # above is the existing precedent: a live guard, deliberately unretired and
            # annotated as such, for a rule the machine has no way to express.
            #
            # It answers first, so it also speaks for the three states that leave the
            # referral vocabulary under spec 6.5. That is not a loss of the 6.5 message:
            # an orphan has no appointment either, and both refusals reach the sending
            # engine the same way.
            #
            # `NoAppointmentError` rather than a bare `ReferralLoopError`, and it is a
            # subclass so nothing that catches the base changes. listener._apply_unschedule
            # has to tell three failures apart out of this one call -- no appointment, a
            # clinically older message, and the store falling over -- and only the first is
            # the ordinary rhythm of a scheduling feed rather than something an operator
            # should be woken for. Matching on the message text below would be the fragile
            # way to draw that line; see errors.NoAppointmentError.
            if loop.state is not LoopState.SCHEDULED:
                raise NoAppointmentError(
                    f"Loop {loop_id} is in state {loop.state.value}, so there is no appointment "
                    "to cancel. An SIU^S15 asserts only that a booking went away; a loop that "
                    "was never booked has none to lose, and treating one as an acceptance would "
                    "attribute to the receiving organisation a claim it never made."
                )

            transition = self._refuse_illegal_transition(
                loop,
                ReferralState.ACCEPTED,
                # The same counterparty scheduler that sends the S12 behind `schedule`.
                # Not HUMAN: no coordinator at this site clicked anything, and this is the
                # receiving organisation reporting on its own diary.
                AssertionSource.RECEIVING_ORG,
                _ENGINE_ACTOR_REF,
                message_at or _now(),
            )
            # `schedule`'s staleness posture, not `cancel`'s, and destructiveness is the
            # whole of the difference. `cancel` demands a readable clock because CANCELLED
            # is on no worklist, so a replayed S15 with a blank MSH-7 took a clinically
            # open loop off every queue at once -- the H2 exploit. Unscheduling hides
            # nothing: OPEN and SCHEDULED are both in the store's _OPEN_STATES, so the
            # loop stays exactly where the coordinator already sees it. Refusing it on an
            # unreadable clock would buy no protection and would leave the loop
            # advertising an appointment that no longer exists.
            self._refuse_if_stale(loop_id, message_at, "unschedule")
            self.store.append_event(
                LoopEvent(
                    loop_id, "unscheduled", _now(), control_id,
                    self._stamp({}, message_at, control_id),
                ),
                transition=transition,
            )

    def cancel(self, loop_id: str, control_id: str, message_at: datetime | None = None) -> None:
        with self._lock:
            loop = self.get(loop_id)
            # The withdrawal path, and nothing but. Spec 6.1 glosses CANCELLED as "the
            # referring side withdrew the referral" -- a clinical judgement by a person
            # that this patient no longer needs the study. The referral is over, so
            # CANCELLED being terminal and on no worklist is the correct answer rather
            # than the hazard it is everywhere else.
            #
            # SIU^S15 used to arrive here and no longer does. An S15 is a counterparty
            # scheduler reporting that a *booking* went away, which is close to the
            # opposite claim: the patient still needs the visit. It routes to
            # `unschedule`, which carries that argument in full.
            #
            # **This method has no production caller today, and that is deliberate.**
            # Nothing in v1 observes a referring clinician withdrawing a referral -- there
            # is no message for it and no UI action -- so wiring one is plan 2c's work.
            # It is kept rather than deleted because deleting it would leave the only
            # transition into CANCELLED unreachable and unproven, and 2c would rebuild it
            # from a blank page against the same spec line, without the state sweep and
            # the anti-replay guard below that already pin it. Coverage tooling reporting
            # this as unreferenced is reporting the schedule, not dead code.
            #
            # RECEIVING_ORG is left as it stands for the same reason it is not yet HUMAN:
            # changing it would be asserting who does the withdrawing before 2c decides
            # what drives it, and the source is what spec 8.2 answers "who said so" from.
            transition = self._refuse_illegal_transition(
                loop,
                ReferralState.CANCELLED,
                AssertionSource.RECEIVING_ORG,
                _ENGINE_ACTOR_REF,
                message_at or _now(),
            )
            # Destructive: CANCELLED appears in neither open_loops() nor
            # resulted_unacknowledged(), so a cancel applied on an unreadable
            # clock takes a clinically open referral off every queue at once.
            self._refuse_if_stale(loop_id, message_at, "cancel", require_message_time=True)
            self.store.append_event(
                LoopEvent(
                    loop_id, "cancelled", _now(), control_id,
                    self._stamp({}, message_at, control_id),
                ),
                transition=transition,
            )

    def record_result(
        self,
        loop_id: str,
        obx11: str,
        control_id: str,
        message_at: datetime | None = None,
        match_tier: int | None = None,
        attached_from: str = "",
    ) -> None:
        """Apply an arriving result. OBX-11 decides which transition is legal.

        `match_tier` and `attached_from` record HOW this result came to be this
        loop's, and both exist for undo_match. Without the tier, the most
        valuable label the system produces -- a human saying the matcher was
        wrong -- would not say which rule was wrong, and a pack revision has
        nothing to act on. Without `attached_from`, undoing a result a
        *coordinator* attached would be counted as a matcher false positive,
        putting human error into the metric that vetoes pack releases.

        Both are allowlisted, non-identifying values: an integer tier and a
        minted loop id. Omitted from the detail when unset rather than written
        as empty, so an event written before this existed and one written by the
        listener today are the same shape.
        """
        with self._lock:
            loop = self.get(loop_id)

            # The CANCELLED refusal that stood here is now the machine's: CANCELLED is
            # terminal in LEGAL_TRANSITIONS, so the edge does not exist. Its message
            # carried operational guidance the generic refusal does not -- "route to
            # orphan queue and flag" -- and no test pinned that text. Deliberately not
            # kept as a second check in front of the machine: it would agree today and
            # silently disagree the day the table changes, which is the two-enforcement-
            # points failure this task exists to remove. Under spec 6.5 a result arriving
            # for a cancelled referral is an InboundArtifact, and the orphan routing is
            # the ingest layer's decision to make on the refusal, not this method's to
            # embed in an error string.

            # The state guard, now asked of core.machine. It replaces both the CANCELLED
            # refusal above and the _NO_RESULT_FROM frozenset that Task 5 deleted:
            # CANCELLED is terminal in the table, and
            # ORPHAN/DISMISSED/ATTACHED leave the referral vocabulary under spec 6.5 and
            # are refused by _refuse_illegal_transition's own conversion. What that used
            # to say in prose -- that ORPHAN -> RESULTED -> ACKNOWLEDGED would retire a
            # result nobody ordered through the ordinary worklist -- is now a property of
            # the two aggregates rather than a list this method has to remember.
            #
            # The OBX-11 allowlist below deliberately does NOT move. It is the same
            # two-axis split that keeps _refuse_if_stale here: the machine answers "is
            # this state change legal", from the aggregate; whether a message's status is
            # one this system handles is a question about the *message*, answered before
            # there is a transition worth judging. Collapsing them would put message
            # parsing inside a pure function.
            #
            # HUMAN when a coordinator attached this, RECEIVING_ORG when it came off the
            # wire. `attached_from` is set only by attach_orphan, whose only non-test
            # caller is the coordinator worklist -- so it is exactly the signal spec 6.5
            # describes for an attachment being a human's assertion on the referral.
            transition = self._refuse_illegal_transition(
                loop,
                ReferralState.DOCUMENTED,
                AssertionSource.HUMAN if attached_from else AssertionSource.RECEIVING_ORG,
                _ENGINE_ACTOR_REF,
                message_at or _now(),
            )

            if obx11 not in _HANDLED_RESULT_STATUSES:
                raise ReferralLoopError(f"Unhandled OBX-11 status: {obx11!r}")

            # Destructive: a result supersedes the read a coordinator
            # acknowledged, and one applied out of clinical order re-arms an
            # acknowledgement on a read that a correction has already replaced.
            # `attached_from` is the exception -- a human's attachment, not a
            # message, carrying no MSH-7 by design (see _refuse_if_stale).
            # Safe *because* it is unreachable from the wire: attach_orphan is
            # its only caller, and attach_orphan's only non-test caller is the
            # coordinator worklist, which binds loopback and validates Host and
            # Origin. Nothing an interface engine can send sets this. If that
            # ever stops being true, this exemption has to be revisited, so the
            # reason is recorded here rather than left to be re-derived.
            self._refuse_if_stale(
                loop_id, message_at, f"result {obx11!r}",
                require_message_time=not attached_from,
            )

            provenance: dict = {"obx11": obx11}
            if match_tier is not None:
                provenance[_MATCH_TIER] = int(match_tier)
            if attached_from:
                provenance[_ATTACHED_FROM] = attached_from

            # Rule 2, generalised. A correction always reopens review; so does
            # any result landing on a loop somebody has already acknowledged,
            # because that acknowledgement was made against a read this message
            # supersedes.
            if obx11 == CORRECTED or loop.state is LoopState.ACKNOWLEDGED:
                detail = self._stamp({**provenance, **_CLEARED_ACK}, message_at, control_id)
                self.store.append_event(
                    LoopEvent(loop_id, "reopened", _now(), control_id, detail),
                    transition=transition,
                )
                return

            self.store.append_event(
                LoopEvent(
                    loop_id, "resulted", _now(), control_id,
                    self._stamp(provenance, message_at, control_id),
                ),
                transition=transition,
            )

    def acknowledge(self, loop_id: str, actor: str, role: str, control_id: str) -> None:
        """Acknowledge the match: this result belongs to this loop. Rule 1.

        Yields ACKNOWLEDGED, v1's terminal state -- never CLOSED. The claim is
        clerical: a coordinator confirming identifiers, which is what the §7
        matching metrics measure. It does not assert that anyone clinically
        competent read the finding, and the worklist must not say otherwise.

        `role` is recorded so that question stays answerable rather than assumed,
        and `ack_result_status` records what was actually looked at, so the audit
        answers "what did they see" as well as "who looked".

        No message_at: this is a human action, not a message, and it must not
        advance the clinical watermark. If it did, an acknowledgement made today
        would make a correction whose MSH-7 predates it look stale, and safety
        rule 2 would stop firing without any test noticing.
        """
        with audited(
            AuditAction.ACKNOWLEDGED, loop_id=loop_id, actor=actor, role=role
        ) as scope:
            if not actor or not role:
                raise ReferralLoopError(
                    "An acknowledgement needs a named actor and role; a resolution attributed "
                    "to nobody cannot answer who vouched for the match or on what authority"
                )

            with self._lock:
                loop = self.get(loop_id)
                # Both guards are now the machine's. The state check is
                # LEGAL_TRANSITIONS; spec rule 1 is the `documentation` guard, fed by the
                # fold in _refuse_illegal_transition -- which is why acknowledge could not
                # route until that fold existed.
                #
                # The refusal *codes* are what does not move. "TransitionRejected" cannot
                # tell a risk officer whether the loop was in the wrong state or the read
                # was preliminary, and the message that could is exactly what must not be
                # copied into an audit row. So RejectionReason is mapped back onto the
                # RefusalCode the audit has always recorded, rather than the audit
                # learning a second vocabulary for the same two facts.
                try:
                    transition = self._refuse_illegal_transition(
                        loop,
                        ReferralState.RECONCILED,
                        # A coordinator at this site, vouching for the match. The one
                        # assertion source machine.apply() will accept for RECONCILED --
                        # spec 6.3 -- and the reason acknowledge is a human-only path.
                        AssertionSource.HUMAN,
                        ActorRef(kind="practitioner", id=actor),
                        _now(),
                    )
                except TransitionRejected as exc:
                    scope.refusal = _REFUSAL_FOR[exc.reason]
                    raise

                status = self._latest_result_status(loop_id)

                at = _now()
                self.store.append_event(
                    LoopEvent(
                        loop_id, "acknowledged", at, control_id,
                        {
                            "ack_by": actor,
                            "ack_role": role,
                            "ack_at": at.isoformat(),
                            "ack_result_status": status,
                        },
                    ),
                    transition=transition,
                )

    def reverse_acknowledgement(
        self, loop_id: str, actor: str, role: str, reason: str, control_id: str = ""
    ) -> None:
        """Rule 4: a coordinator undoes their own acknowledgement.

        Rule 2 recovers the machine's error -- a result that later corrects.
        Nothing recovered the human's: a coordinator who acknowledged the wrong
        loop had no way back, so that loop stayed resolved while the real one
        stayed open and unwatched. This returns it to RESULTED and to the
        worklist.

        Appends, never mutates: the mistake and its correction both stay in the
        history, which is the whole reason the log is append-only. The reversing
        actor is recorded under its own keys so the acknowledgement it undoes
        remains legible in the event log rather than being overwritten.

        `reason` is required. Task 15: every reversal is a labeled false positive
        for the flywheel -- a human telling you the matcher was wrong on real site
        data, which is the most valuable label the system produces -- and an
        unexplained label teaches nothing.
        """
        with audited(
            AuditAction.ACKNOWLEDGEMENT_REVERSED, loop_id=loop_id, actor=actor, role=role,
            reason_required=True,
        ) as scope:
            if not actor or not role or not reason:
                raise ReferralLoopError(
                    "A reversal needs a named actor, role and reason: it is both an audit "
                    "record of undoing someone's resolution and a labeled false positive"
                )

            with self._lock:
                loop = self.get(loop_id)
                if loop.state is not LoopState.ACKNOWLEDGED:
                    scope.refusal = RefusalCode.WRONG_STATE
                    raise ReferralLoopError(
                        f"Cannot reverse an acknowledgement on a loop in state {loop.state}"
                    )

                at = _now()
                self.store.append_event(
                    LoopEvent(
                        loop_id, "reversed", at, control_id,
                        {
                            # Clearing these is what returns the loop to
                            # resulted_unacknowledged() and therefore to a human.
                            **_CLEARED_ACK,
                            "reversed_by": actor,
                            "reversed_role": role,
                            # Stays here and reaches no artifact: free text a
                            # human typed about a patient. The audit records
                            # only that one was given (see audit.py).
                            "reversed_reason": reason,
                            "reversed_at": at.isoformat(),
                        },
                    )
                )

                # Spec rule 4 calls a reversal a labeled false positive; spec
                # section 7 says the same of an undone auto-match. They are not
                # the same claim -- this one withdraws a human's confirmation and
                # leaves the result attached -- so it gets its own label type and
                # an outcome that is deliberately not FALSE_MATCH. See the module
                # note: folding it in would let a mis-click veto a pack release.
                self._label(
                    LabelType.ACKNOWLEDGEMENT_REVERSED,
                    loop_id=loop_id,
                    modality=loop.modality,
                    service_code=loop.service_code,
                    tier=self._latest_match_tier(loop_id),
                    actor_role=role,
                )

    def dismiss_orphan(
        self, loop_id: str, actor: str, role: str, reason: str, control_id: str = ""
    ) -> None:
        """Terminal state for an orphan that belongs to no loop here.

        Some results are misrouted from another facility, or arrive from a feed
        misconfiguration. Without a terminal state the orphan queue only grows,
        and a queue that only grows is one coordinators stop opening -- silently
        disabling the surface both the safety story and the flywheel depend on.

        Never automatic: nothing in this module calls it, and no message reaches
        it. A human decides an orphan is unattachable, and says why.
        """
        with audited(
            AuditAction.ORPHAN_DISMISSED, loop_id=loop_id, actor=actor, role=role,
            reason_required=True,
        ) as scope:
            if not actor or not role or not reason:
                raise ReferralLoopError(
                    "A dismissal needs a named actor, role and reason; it retires a result "
                    "permanently and 'someone dismissed it' is not an answer to why"
                )

            with self._lock:
                loop = self.get(loop_id)
                if loop.state is not LoopState.ORPHAN:
                    scope.refusal = RefusalCode.WRONG_STATE
                    raise ReferralLoopError(
                        f"Only an orphan can be dismissed; loop {loop_id} is in state {loop.state}"
                    )

                at = _now()
                self.store.append_event(
                    LoopEvent(
                        loop_id, "dismissed", at, control_id,
                        {
                            "dismissed_by": actor,
                            "dismissed_role": role,
                            "dismissed_reason": reason,
                            "dismissed_at": at.isoformat(),
                        },
                    )
                )

                # Spec section 5 makes dismissal rate a watched number: a rising
                # rate is a feed problem to investigate upstream, not a
                # coordinator working faster. That only holds if dismissals are
                # counted, which means a dismissal is a label too -- one saying
                # the result belongs to no loop at this site.
                self._label(
                    LabelType.ORPHAN_DISMISSED,
                    loop_id=loop_id,
                    modality=loop.modality,
                    service_code=loop.service_code,
                    tier=self._orphan_match_tier(loop_id),
                    actor_role=role,
                )

    # ----------------------------------------------------------- the flywheel

    def attach_orphan(
        self, orphan_id: str, target_loop_id: str, actor: str, role: str, control_id: str = ""
    ) -> None:
        """A coordinator says this unmatched result belongs to this loop.

        Spec section 5: orphans are workflow, not failure, and every attachment
        is a labeled example -- a human telling the matcher, on real site data,
        about a match it declined to make.

        **The result is applied through record_result, and that is the whole
        safety design of this method.** Manual attachment must not become a back
        door around safety rule 1: a coordinator attaching a preliminary read
        leaves the target in RESULTED and unacknowledgeable, exactly as if the
        ORU had matched on the wire. Writing the transition here instead would
        duplicate rule 1, rule 2, the CANCELLED refusal and the clinical
        ordering guard -- four safety rules, in a second place, drifting.

        Which target states are legal is therefore record_result's answer, not
        this method's, and it is the right one in every case: OPEN and SCHEDULED
        advance; RESULTED accepts a second result, which is how a final attaches
        to a loop that already holds the preliminary; ACKNOWLEDGED reopens under
        rule 2, because a result arriving on a settled loop supersedes the read
        that was settled; CANCELLED is refused by the failure matrix; ORPHAN and
        DISMISSED are refused because they are results, not expectations.

        The orphan's OBX-11 is read from its `orphaned` event directly.
        `_latest_result_status` correctly returns "" for an orphan -- an
        `orphaned` event is not in _RESULT_EVENTS -- so using it here would make
        every attached orphan look statusless, and every attachment would be
        refused. An unreadable status is refused rather than defaulted to final:
        defaulting would advance a loop off the awaiting-result queue on the
        strength of a status nobody could read, while refusing leaves the orphan
        exactly where a coordinator can see it.
        """
        with audited(
            AuditAction.ORPHAN_ATTACHED, loop_id=target_loop_id, actor=actor, role=role
        ) as scope:
            if not actor or not role:
                raise ReferralLoopError(
                    "An attachment needs a named actor and role: it asserts that this result "
                    "belongs to this loop, and an assertion attributed to nobody is not one"
                )
            with self._lock:
                # Both replays before anything is written. LoopNotFoundError from
                # either is the honest answer and must precede any state change.
                #
                # And before every refusal below, deliberately. Those refusals
                # compose their messages from the two ids, and worklist._refused
                # echoes the message into an HTTP response -- one of the four
                # artifacts spec test 14 greps. Replaying first means an id that
                # reaches a message is one this system minted; anything else has
                # already left through LoopNotFoundError, which echoes nothing.
                # Found by probing this task: the self-attachment guard used to
                # run first, so POSTing a sentinel as both ids returned it.
                orphan = self.get(orphan_id)
                target = self.get(target_loop_id)

                if orphan_id == target_loop_id:
                    # record_result would refuse this anyway -- core.machine has no
                    # edge out of the artifact states (spec 6.5) --
                    # but with a message about states that sends the reader
                    # looking for the wrong bug.
                    raise ReferralLoopError(
                        f"Cannot attach record {orphan_id} to itself; an orphan is attached "
                        "to the loop that ordered the study, and a record cannot have "
                        "ordered itself"
                    )

                if orphan.state is not LoopState.ORPHAN:
                    scope.refusal = RefusalCode.WRONG_STATE
                    raise ReferralLoopError(
                        f"Only an orphan can be attached; record {orphan_id} is in state "
                        f"{orphan.state}. Attaching the same orphan twice, or attaching a "
                        "record that is a real loop, would copy a result onto a loop no "
                        "coordinator has actually looked at."
                    )

                obx11 = self._orphan_result_status(orphan_id)
                if obx11 not in _HANDLED_RESULT_STATUSES:
                    scope.refusal = RefusalCode.UNREADABLE_RESULT_STATUS
                    raise ReferralLoopError(
                        f"Orphan {orphan_id} carries no readable OBX-11 (got {obx11!r}); "
                        "refusing to attach it. Advancing a loop on a status nobody could "
                        "read would let rule 1 be bypassed by a missing field."
                    )

                # First, because it is the step that can be refused. If the
                # target cannot take the result, the orphan must be untouched
                # and still on the queue -- the ordering that fails safe. The
                # reverse order would retire an orphan whose result went nowhere.
                self.record_result(
                    target_loop_id, obx11=obx11, control_id=control_id, attached_from=orphan_id
                )

                at = _now()
                self.store.append_event(
                    LoopEvent(
                        orphan_id, "attached", at, control_id,
                        {
                            "attached_to": target_loop_id,
                            "attached_by": actor,
                            "attached_role": role,
                            "attached_at": at.isoformat(),
                        },
                    )
                )

                # The orphan's own modality and service code, not the target's:
                # the label describes the RESULT the matcher failed to place,
                # and the target's values are the order's. Falls back to the
                # target only when the orphan carries none.
                self._label(
                    LabelType.ORPHAN_ATTACHED,
                    loop_id=target_loop_id,
                    modality=orphan.modality or target.modality,
                    service_code=orphan.service_code or target.service_code,
                    # The tier the matcher reached before declining. On a
                    # missed-match label that is the feature that matters: tier 5
                    # means nothing came close, tier 3 means the rule nearly
                    # fired and its window or tie-breaker is the thing to look at.
                    tier=self._orphan_match_tier(orphan_id),
                    actor_role=role,
                )

        if orphan.mrn != target.mrn:
            # No identifiers: a log record is one of the four artifacts spec
            # test 14 greps. That the two differed is the feed-quality signal --
            # tiers 3 and 4 both key on MRN, so an attachment across two
            # identifiers usually means an alias that was never recorded.
            logger.info(
                "Orphan %s was attached to loop %s across two different patient identifiers; "
                "check whether a merge was missed. The identifiers are in loop_events.",
                orphan_id, target_loop_id,
            )
        logger.info("Orphan %s attached to loop %s via a coordinator", orphan_id, target_loop_id)

    def undo_match(
        self, loop_id: str, actor: str, role: str, reason: str, control_id: str = ""
    ) -> str:
        """A coordinator says the matcher attached the wrong result. Returns the
        id of the orphan record that now holds the detached result.

        Spec section 7: the most valuable label the system produces, because a
        human is telling you the matcher was wrong on real site data rather than
        on a synthetic case. See the module note for why this is not
        reverse_acknowledgement and cannot be expressed as it.

        Two things have to happen and only one of them is obvious. The loop goes
        back to awaiting a result, which is the visible half. The *result* also
        has to go somewhere: it arrived, it is real, and it belongs to some loop
        even if not this one. Leaving it detached would put a result nobody is
        looking at back into a system whose entire purpose is not losing
        results -- it would exist only in the raw archive, which no coordinator
        reads and no queue shows. So the detached result becomes an orphan
        again, which is precisely the queue built for a result with no home, and
        the coordinator can then attach it where it belongs -- producing the
        second label.

        The new orphan carries the loop's MRN. That is the best available
        evidence rather than a guess: tiers 3 and 4 require loop.mrn == key.mrn,
        so a false match at those tiers is by construction between two orders
        for the same patient, and at tiers 1-2 the identifier the result carried
        is in the raw archive either way. It carries the loop's modality and
        service code for the same reason and with the same caveat, and
        deliberately carries no order number: an exact accession is the
        strongest identifier here and attributing the wrong one to a result
        would manufacture the next false match.

        Refused on an ACKNOWLEDGED loop, which is not an oversight. A human
        vouched for that match, and undoing it without an explicit reversal
        would discard their confirmation with no `reversed` event to show it.
        Reverse the acknowledgement first: both facts are then in the history
        and both are labeled, which is what that case -- a false match somebody
        signed off -- deserves.
        """
        with audited(
            AuditAction.MATCH_UNDONE, loop_id=loop_id, actor=actor, role=role,
            reason_required=True,
        ) as scope:
            if not actor or not role or not reason:
                raise ReferralLoopError(
                    "Undoing a match needs a named actor, role and reason: it is both an "
                    "audit record of detaching a result and a labeled false positive, and "
                    "an unexplained label teaches nothing"
                )

            with self._lock:
                loop = self.get(loop_id)
                if loop.state not in _UNMATCHABLE_FROM:
                    scope.refusal = RefusalCode.WRONG_STATE
                    raise ReferralLoopError(
                        f"Cannot undo a match on a loop in state {loop.state}; only a "
                        "RESULTED loop holds a match to undo. If it is ACKNOWLEDGED, "
                        "reverse the acknowledgement first so the withdrawal of that "
                        "confirmation is recorded too."
                    )

                event = self._latest_result_event(loop_id)
                detail = event.detail if event else {}
                attached_from = str(detail.get(_ATTACHED_FROM, ""))
                tier = detail.get(_MATCH_TIER)
                obx11 = str(detail.get("obx11", ""))

                # The replacement orphan FIRST, for the same reason the alias is
                # written before a merge moves loops: of the two orderings only
                # this one fails safe. A failure after this point leaves a
                # duplicate orphan and a loop still RESULTED -- visible and
                # correctable. The other order loses the result outright.
                orphan_id = self.orphan(
                    control_id=control_id,
                    mrn=loop.mrn,
                    detail={
                        "modality": loop.modality,
                        "service_code": loop.service_code,
                        "result_status": obx11,
                        _MATCH_TIER: tier,
                        # So the history reads as one story rather than as an
                        # unexplained orphan appearing minutes after an undo.
                        "detached_from": loop_id,
                    },
                )

                at = _now()
                self.store.append_event(
                    LoopEvent(
                        loop_id, "unmatched", at, control_id,
                        {
                            # Already clear on a RESULTED loop; written anyway so
                            # the event states what it leaves behind rather than
                            # relying on the state it was entered from.
                            **_CLEARED_ACK,
                            "unmatched_by": actor,
                            "unmatched_role": role,
                            # Stays here and reaches no artifact: free text a
                            # human typed about a patient. Above all it does not
                            # reach the label, which is the exportable one.
                            "unmatched_reason": reason,
                            "unmatched_at": at.isoformat(),
                            "detached_to": orphan_id,
                        },
                    )
                )

                # The distinction the release gate depends on. A result a
                # coordinator attached and then detached is a human's mistake,
                # not the matcher's, and counting it as a false match would let a
                # mis-click veto a pack release under section 7's absolute rule.
                self._label(
                    LabelType.ATTACHMENT_UNDONE if attached_from else LabelType.MATCH_UNDONE,
                    loop_id=loop_id,
                    modality=loop.modality,
                    service_code=loop.service_code,
                    # None for an undone attachment: no tier produced it.
                    tier=None if attached_from else tier,
                    actor_role=role,
                )

        logger.warning(
            "Match on loop %s undone by a coordinator; the result was detached to orphan %s "
            "and the loop is awaiting a result again.", loop_id, orphan_id,
        )
        return orphan_id

    # ------------------------------------------------------------------ helpers

    def _label(self, label_type: LabelType, **fields) -> None:
        """Append a label, best-effort. A label write never blocks the action.

        The same asymmetry audit.py argues for itself, and for the same reason:
        the clinical fact is already durable in loop_events, from which every
        label here is derivable, so a dropped label costs a training example and
        not a result. Failing closed would mean an unwritable labels table stops
        coordinators attaching orphans -- the queue then only grows, which is the
        failure spec section 5 added DISMISSED to prevent, caused this time by
        the flywheel meant to feed on it.

        Not silent: the exception *type* is logged at ERROR. Not str(exc), which
        for a SQLite error carries the database path.
        """
        try:
            self.store.record_label(label_type, pack_version=self.pack_version, **fields)
        except Exception as exc:  # noqa: BLE001 - deliberate, see the docstring
            logger.error(
                "Label %s dropped for loop %s (%s); the action itself succeeded and is in "
                "loop_events, from which the label can be rebuilt.",
                label_type.value, fields.get("loop_id", ""), type(exc).__name__,
            )

    def _orphan_event(self, orphan_id: str) -> LoopEvent | None:
        """The `orphaned` event that created this record, if it has one."""
        for event in self.store.events_for(orphan_id):
            if event.event_type == "orphaned":
                return event
        return None

    def _orphan_result_status(self, orphan_id: str) -> str:
        """The OBX-11 an orphan arrived with, read off its `orphaned` event.

        Deliberately not _latest_result_status: `orphaned` is absent from
        _RESULT_EVENTS -- correctly, since an orphan's detail is caller-supplied
        and must not be able to flip a real loop's result status -- so that
        method returns "" for every orphan and using it here would refuse every
        attachment.
        """
        event = self._orphan_event(orphan_id)
        if event is None:
            return ""
        for key in _ORPHAN_STATUS_KEYS:
            value = event.detail.get(key)
            if value:
                return str(value)
        return ""

    def _orphan_match_tier(self, orphan_id: str) -> int | None:
        event = self._orphan_event(orphan_id)
        return event.detail.get(_MATCH_TIER) if event else None

    def _latest_match_tier(self, loop_id: str) -> int | None:
        """The tier the result currently on this loop matched at, if recorded."""
        event = self._latest_result_event(loop_id)
        return event.detail.get(_MATCH_TIER) if event else None

    def _stamp(self, detail: dict, message_at: datetime | None, control_id: str) -> dict:
        """Record this message's MSH-7 on the event, unless the clock forbids it.

        A message dated beyond `MAX_CLOCK_SKEW` is applied but **not stamped**.
        `_clinical_watermark` is a max() over an append-only log, so a stamp
        taken from a clock running years fast can never afterwards be lowered,
        and every real message that follows -- the final report, the correction,
        the cancellation -- is refused as stale for the life of the loop.

        Dropping the stamp rather than refusing the message is the deliberate
        half of that. The watermark is a defence, not clinical content: declining
        to advance a defence on untrusted input costs ordering information for
        one message, where refusing the message costs the result itself, and a
        RIS running fast is endemic rather than exotic.

        The event is left with no stamp at all, and carries no mark saying so.
        `_latest_result_event` ranks an event that has no clinical time by its
        position in the append-only log, which is the same thing it must do for
        a message that genuinely carried no MSH-7 -- so the two need no telling
        apart, and a marker would be state nothing reads.

        Counted, not merely dropped. A silent drop is how this stayed invisible.
        """
        if message_at is None:
            return detail
        if is_future_dated(message_at, _now()):
            self.future_dated_message_count += 1
            logger.warning(
                "Message %r is dated beyond the clock-skew window (%s); applying it but not "
                "advancing the loop's clinical watermark (%d so far). Either a sender's clock "
                "is wrong or a message is forged; both need a human.",
                control_id, MAX_CLOCK_SKEW, self.future_dated_message_count,
            )
            return detail
        return {**detail, _MESSAGE_AT: _as_utc(message_at).isoformat()}

    def _stamp_merge(self, detail: dict, message_at: datetime | None, control_id: str) -> dict:
        """Record an A40's MSH-7 without letting it govern clinical ordering.

        Written even when the clock is beyond `MAX_CLOCK_SKEW`, unlike `_stamp`.
        `_MERGE_MESSAGE_AT` is read by nothing -- `_message_time` looks only at
        `_MESSAGE_AT`, `merged_in` is not a result event, and no state depends on
        it -- so a skewed A40 can regress nothing, and dropping the value here
        would lose an audit record to defend against nothing.

        It is still **counted**, on the same counter as every other skewed
        message. The merge's exemption is from the ordering guard, not from
        visibility: "a sender's clock is wrong or a message is forged" is exactly
        as true of an ADT^A40, and an identity merge is the highest-consequence
        message this subsystem accepts.
        """
        if message_at is None:
            return detail
        if is_future_dated(message_at, _now()):
            self.future_dated_message_count += 1
            logger.warning(
                "ADT^A40 %r is dated beyond the clock-skew window (%s); the merge is applied "
                "unchanged, as its timestamp governs no ordering (%d skewed message(s) so far). "
                "Either a sender's clock is wrong or a message is forged; both need a human.",
                control_id, MAX_CLOCK_SKEW, self.future_dated_message_count,
            )
        return {**detail, _MERGE_MESSAGE_AT: _as_utc(message_at).isoformat()}

    @staticmethod
    def _message_time(event: LoopEvent) -> datetime | None:
        raw = event.detail.get(_MESSAGE_AT)
        if not raw:
            return None
        try:
            return _as_utc(datetime.fromisoformat(raw))
        except (TypeError, ValueError):
            # An unparseable stamp must not be read as "older than everything".
            return None

    def _clinical_watermark(self, loop_id: str) -> datetime | None:
        """MSH-7 of the newest message already applied to this loop, if known."""
        times = [t for t in map(self._message_time, self.store.events_for(loop_id)) if t]
        return max(times) if times else None

    def _refuse_if_stale(
        self,
        loop_id: str,
        message_at: datetime | None,
        what: str,
        *,
        require_message_time: bool = False,
    ) -> None:
        """Refuse a message clinically older than one already accepted.

        Strictly older, never equal: MSH-7 is routinely minute-precision, so two
        messages in the same minute share a timestamp and rejecting on equality
        would discard real results.

        `message_at=None` means the caller could not read MSH-7 -- it is empty,
        it is not a timestamp, or it names a year no clock could produce
        (`clock.is_readable_clock`). `require_message_time` refuses that unknown
        once the loop carries a watermark, and the two destructive transitions
        set it, because the fail-open *was* the exploit: a blank MSH-7 turned off
        the only anti-replay control in the system, and a replayed message naming
        a scheduled loop then took it out of `open_loops()` and
        `resulted_unacknowledged()` alike -- clinically open, on no coordinator
        queue at all. The stated justification for failing open here ("Task 10's
        listener does not yet pass MSH-7") expired when the listener started
        passing it on every message-driven transition.

        The message that produced that incident was a replayed `SIU^S15`, and it
        no longer reaches a caller that sets this: an S15 routes to `unschedule`,
        which lands on `OPEN` and hides nothing, so there is nothing left for a
        replay of one to take away. The flag survives the message that motivated
        it because the property it guards is destructiveness, not message type --
        `cancel` still ends a referral outright, and a final result still arms
        acknowledgement -- and re-deriving that from the next destructive
        transition somebody adds is how a control gets left off one.

        Four callers deliberately do not set it:

          * `schedule`. `OPEN -> SCHEDULED` hides nothing -- both states are in
            the store's `_OPEN_STATES` and both are staleable -- so a replayed
            `SIU^S12` costs a coordinator nothing, where refusing it would buy no
            protection at the price of real refusals.
          * `unschedule`, for the mirror of that reason and argued in full there:
            `SCHEDULED -> OPEN` is inside the same `_OPEN_STATES`, so refusing an
            unreadable clock would buy nothing and would leave the loop
            advertising an appointment that no longer exists.
          * `attach_orphan`, through `record_result`'s `attached_from`. That is a
            coordinator's decision, not a replayed message, and it carries no
            MSH-7 for the same reason `acknowledge` carries none. Refusing it
            would close the orphan queue's only exit.
          * any loop carrying no watermark at all: there is no clinical ordering
            there to regress, and refusing would reject a whole site's traffic
            to defend nothing.
        """
        if message_at is None:
            if require_message_time and self._clinical_watermark(loop_id) is not None:
                raise StaleMessageError(
                    f"Refusing {what} for loop {loop_id}: the message carries no readable "
                    "MSH-7 and this loop already has clinical ordering to protect, so there "
                    "is no way to tell whether applying it would regress the loop. Route for "
                    "human review; the raw message is archived."
                )
            return
        mark = self._clinical_watermark(loop_id)
        if mark is None:
            return
        incoming = _as_utc(message_at)
        if incoming < mark:
            raise StaleMessageError(
                f"Refusing {what} for loop {loop_id}: message time {incoming.isoformat()} "
                f"predates the newest applied message {mark.isoformat()}. Applying it would "
                "regress the loop. Route for human review; the raw message is archived."
            )

    def _latest_result_event(self, loop_id: str) -> LoopEvent | None:
        """The newest result event this loop holds, or None.

        Newest by clinical time, falling back to arrival. _refuse_if_stale
        already keeps arrival order equal to clinical order for anything this
        registry appends, so this ordering only matters for a log written by
        something else -- a restore, a foreign writer, a future code path. That
        is exactly when getting it wrong would let a loop be acknowledged on a
        superseded read, so it is defended here rather than assumed away.

        **A message time is never compared against an arrival time.** That is
        the whole rule, and two successive bugs came from breaking it. An event
        with no usable clock -- a message whose MSH-7 was refused as skewed, a
        coordinator's attachment -- used to fall back to `occurred_at`, i.e.
        *now*, which beats every legitimately past MSH-7 and made such an event
        the loop's newest result permanently: a `P` blocking acknowledgement for
        good, an `F` outranking the correction that superseded it. Sorting those
        events unconditionally *below* stamped ones only moved the mixed
        comparison to the other side of the boundary and inverted the harm: a
        trusted older final then masked a genuinely newer correction from a RIS
        whose clock had jumped, and the audit recorded a coordinator vouching
        for the superseded read.

        So an event with no clock of its own inherits the newest clinical time
        established on the loop **at its own point in the log**, and ties break
        on append index. Both quantities being compared are then clinical times,
        and the tiebreak is position in an append-only log -- which is exactly
        what `_refuse_if_stale` already guarantees for anything this registry
        wrote. A distrusted result that lands after a trusted one therefore
        still wins, and a trusted correction that lands after a distrusted read
        still wins.

        The residual, stated rather than glossed: an event carrying no clock
        cannot be ranked *ahead* of a stamped event whose clinical time is later
        than anything established when it arrived, because nothing about it says
        it is newer. In particular, an unstamped event appearing before any
        trusted stamp on the loop has no clinical time at all and loses to every
        stamped event, whatever it claims. That is the direction that fails
        toward a human looking: the loop stays RESULTED and on the queue.

        `unmatched` is deliberately NOT a result event, even though it is what a
        detachment writes. Its timestamp is a human's clock and every other key
        here is a message's, and mixing the two would let a coordinator's undo
        outrank a later result whose MSH-7 is older than the moment they clicked
        -- which would refuse acknowledgement of a genuine final read. Nothing is
        lost: an undone loop is OPEN, and core.machine has no SENT -> RECONCILED edge.
        """
        best_key = None
        best: LoopEvent | None = None
        # The newest clinical time established anywhere on the loop so far, in
        # append order. Taken over every event, not only result events: a
        # `created` or `scheduled` carries an MSH-7 and dates what follows it.
        established: datetime | None = None
        for index, event in enumerate(self.store.events_for(loop_id)):
            stamped = self._message_time(event)
            if stamped is not None:
                established = stamped if established is None else max(established, stamped)
            if event.event_type not in _RESULT_EVENTS:
                continue
            clinical = stamped if stamped is not None else established
            key = (clinical is not None, clinical or _NO_CLINICAL_TIME, index)
            if best_key is None or key > best_key:
                best_key = key
                best = event
        return best

    def _latest_result_status(self, loop_id: str) -> str:
        """The OBX-11 of the newest result this loop holds, or "" if none."""
        event = self._latest_result_event(loop_id)
        return str(event.detail.get("obx11", "")) if event else ""
