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

Audit. Five methods here change clinical-facing state on a human's or an
administrator's say-so -- acknowledge, reverse_acknowledgement, dismiss_orphan,
merge_patient, reverse_merge -- and each is wrapped in `audit.audited`, which
appends one row to the immutable audit database per *attempt*, refusals
included. Wrapped rather than called at the end of the happy path, because a
refused acknowledgement on a preliminary read is exactly the event a risk
officer asks about, and an audit that only records successes cannot answer them.

Nothing else here is audited. The message-driven transitions -- open_loop,
orphan, schedule, cancel, record_result -- are already held verbatim and durably
in the raw archive, so copying them into a second exportable database would
double the PHI footprint for no added assurance.

The audit never blocks a transition and never receives an MRN, a reason, or any
other message-derived string; audit.py owns both decisions and the reasoning.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone

from .audit import (
    ENGINE_ACTOR,
    SYSTEM_ROLE,
    AuditAction,
    RefusalCode,
    audited,
)
from .errors import MrnRetiredError, ReferralLoopError, StaleMessageError
from .events import Loop, LoopEvent, LoopState
from .store import LoopStore

logger = logging.getLogger(__name__)

# OBX-11 result status codes we act on. HL7 table 0085.
PRELIMINARY = "P"
FINAL = "F"
CORRECTED = "C"

# States a loop may be acknowledged from.
_ACKNOWLEDGEABLE_FROM = frozenset({LoopState.RESULTED})

# OBX-11 values a coordinator may acknowledge. An allowlist: rule 1 must not be
# expressible as "anything that is not a preliminary", because that resolves the
# loop on every value we failed to anticipate.
_ACKNOWLEDGEABLE_STATUSES = frozenset({FINAL, CORRECTED})

# Only these event types carry a result. A merged_in event (Task 7) copies
# fields off another loop, and an orphaned event carries caller-supplied detail;
# if either could set the result status, a merge or an orphan would flip a
# preliminary to final with no result ever arriving. "reversed" is excluded
# deliberately: undoing an acknowledgement changes who vouched for the match, not
# what the radiologist read.
_RESULT_EVENTS = frozenset({"resulted", "reopened"})

# Terminal or otherwise result-proof states. A result recorded against any of
# these would leave the loop on no worklist, or retire it by a route no
# coordinator chose.
_NO_RESULT_FROM = frozenset({LoopState.ORPHAN, LoopState.DISMISSED})

# Scheduling and cancellation describe where an order sits in the workflow, so
# they are only meaningful while the loop is still waiting on a result.
# Cancelling a RESULTED loop would erase the result from every worklist query --
# CANCELLED appears in neither open_loops() nor resulted_unacknowledged().
_SCHEDULABLE_FROM = frozenset({LoopState.OPEN, LoopState.SCHEDULED})
_CANCELLABLE_FROM = frozenset({LoopState.OPEN, LoopState.SCHEDULED})

# Detail key holding the clinical timestamp of the message that caused an event
# (MSH-7). Present only on message-driven events -- human actions must never
# advance the watermark, or an acknowledgement today would make tomorrow's
# correction look stale and safety rule 2 would silently stop working.
_MESSAGE_AT = "message_at"

# MSH-7 of an ADT^A40, recorded under its own key so the merge stays auditable
# without joining the clinical watermark. See merge_patient and the module note
# on why an identity correction is not a clinical observation.
_MERGE_MESSAGE_AT = "merge_message_at"

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
    def __init__(self, store: LoopStore):
        self.store = store
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
                raise MrnRetiredError(
                    f"MRN {mrn} was retired between ingest and this write; re-resolve and "
                    "retry. Storing the loop here would hide it from the surviving patient."
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
                    {**detail, "mrn": mrn, "submitted_mrn": submitted_mrn or mrn}, message_at
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
            raise ReferralLoopError(
                "A merge needs both a prior and a surviving MRN; "
                f"got prior={prior_mrn!r} surviving={surviving_mrn!r}"
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

    def schedule(self, loop_id: str, control_id: str, message_at: datetime | None = None) -> None:
        with self._lock:
            loop = self.get(loop_id)
            if loop.state not in _SCHEDULABLE_FROM:
                raise ReferralLoopError(f"Cannot schedule a loop in state {loop.state}")
            self._refuse_if_stale(loop_id, message_at, "schedule")
            self.store.append_event(
                LoopEvent(loop_id, "scheduled", _now(), control_id, self._stamp({}, message_at))
            )

    def cancel(self, loop_id: str, control_id: str, message_at: datetime | None = None) -> None:
        with self._lock:
            loop = self.get(loop_id)
            if loop.state not in _CANCELLABLE_FROM:
                raise ReferralLoopError(
                    f"Cannot cancel a loop in state {loop.state}: an order that already produced "
                    "a result cannot be un-ordered, and CANCELLED appears on no worklist"
                )
            self._refuse_if_stale(loop_id, message_at, "cancel")
            self.store.append_event(
                LoopEvent(loop_id, "cancelled", _now(), control_id, self._stamp({}, message_at))
            )

    def record_result(
        self, loop_id: str, obx11: str, control_id: str, message_at: datetime | None = None
    ) -> None:
        """Apply an arriving result. OBX-11 decides which transition is legal."""
        with self._lock:
            loop = self.get(loop_id)

            if loop.state is LoopState.CANCELLED:
                raise ReferralLoopError(
                    f"Result arrived for CANCELLED loop {loop_id}; route to orphan queue and flag"
                )

            if loop.state in _NO_RESULT_FROM:
                # ORPHAN -> RESULTED -> ACKNOWLEDGED would retire a result nobody
                # ordered through the ordinary worklist, leaving the orphan queue
                # the gap flywheel counts without any coordinator attaching it.
                # An orphan is retired by attach_orphan (Task 15) or dismissed
                # explicitly. DISMISSED is terminal and stays terminal.
                raise ReferralLoopError(
                    f"Loop {loop_id} is in state {loop.state}; results are not recorded "
                    "against orphaned or dismissed records. Attach it to a real loop instead."
                )

            if obx11 not in (PRELIMINARY, FINAL, CORRECTED):
                raise ReferralLoopError(f"Unhandled OBX-11 status: {obx11!r}")

            self._refuse_if_stale(loop_id, message_at, f"result {obx11!r}")

            # Rule 2, generalised. A correction always reopens review; so does
            # any result landing on a loop somebody has already acknowledged,
            # because that acknowledgement was made against a read this message
            # supersedes.
            if obx11 == CORRECTED or loop.state is LoopState.ACKNOWLEDGED:
                detail = self._stamp({"obx11": obx11, **_CLEARED_ACK}, message_at)
                self.store.append_event(LoopEvent(loop_id, "reopened", _now(), control_id, detail))
                return

            self.store.append_event(
                LoopEvent(
                    loop_id, "resulted", _now(), control_id, self._stamp({"obx11": obx11}, message_at)
                )
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
                if loop.state not in _ACKNOWLEDGEABLE_FROM:
                    scope.refusal = RefusalCode.WRONG_STATE
                    raise ReferralLoopError(f"Cannot acknowledge a loop in state {loop.state}")

                status = self._latest_result_status(loop_id)
                if status not in _ACKNOWLEDGEABLE_STATUSES:
                    # Spec rule 1, and the one refusal a risk officer will ask
                    # about by name. "ReferralLoopError" cannot distinguish it
                    # from the state check above, and the message that could is
                    # exactly what must not be copied into the audit.
                    scope.refusal = RefusalCode.PRELIMINARY_NOT_ACKNOWLEDGEABLE
                    raise ReferralLoopError(
                        f"Loop {loop_id} has no final or corrected result "
                        f"(latest OBX-11 {status!r}); ACKNOWLEDGED is unreachable"
                    )

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
                    )
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

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _stamp(detail: dict, message_at: datetime | None) -> dict:
        if message_at is None:
            return detail
        return {**detail, _MESSAGE_AT: _as_utc(message_at).isoformat()}

    @staticmethod
    def _stamp_merge(detail: dict, message_at: datetime | None) -> dict:
        """Record an A40's MSH-7 without letting it govern clinical ordering."""
        if message_at is None:
            return detail
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

    def _refuse_if_stale(self, loop_id: str, message_at: datetime | None, what: str) -> None:
        """Refuse a message clinically older than one already accepted.

        Strictly older, never equal: MSH-7 is routinely minute-precision, so two
        messages in the same minute share a timestamp and rejecting on equality
        would discard real results.

        message_at=None means the caller could not determine MSH-7. Such a
        message is neither blocked nor blocking -- it is not compared, and it
        does not move the watermark. Task 10's listener does not yet pass MSH-7,
        so until it does this guard is inert for live traffic; that is a
        deliberate fail-open on an unknown rather than refusing all traffic.
        """
        if message_at is None:
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

    def _latest_result_status(self, loop_id: str) -> str:
        """The OBX-11 of the newest result this loop holds, or "" if none.

        Newest by clinical time, falling back to arrival. _refuse_if_stale
        already keeps arrival order equal to clinical order for anything this
        registry appends, so this ordering only matters for a log written by
        something else -- a restore, a foreign writer, a future code path. That
        is exactly when getting it wrong would let a loop be acknowledged on a
        superseded read, so it is defended here rather than assumed away.
        """
        best_key = None
        status = ""
        for index, event in enumerate(self.store.events_for(loop_id)):
            if event.event_type not in _RESULT_EVENTS:
                continue
            key = (self._message_time(event) or _as_utc(event.occurred_at), index)
            if best_key is None or key > best_key:
                best_key = key
                status = str(event.detail.get("obx11", ""))
        return status
