"""The loop state machine.

A loop is an expectation that a result returns. Two transitions here are
clinical safety decisions rather than engineering choices, and both are
enforced structurally rather than by convention:

  1. A preliminary read (OBX-11 = P) may reach RESULTED but never CLOSED.
  2. A corrected read (OBX-11 = C) returns a CLOSED loop to RESULTED.

Rule 1 is written as an allowlist, not as "not P". Only an explicitly final or
corrected read opens the door to CLOSED, so an absent, unrecognised or
future-dialect OBX-11 fails closed instead of closing the loop by default.

Rule 2 is generalised: *any* result arriving on a CLOSED loop reopens it and
clears the acknowledgement. A resent final that only moved CLOSED -> RESULTED
while leaving ack_at set would sit in neither open_loops() nor
resulted_unacknowledged() -- a loop on no worklist at all, which is the failure
this product exists to prevent.

Ordering. store.replay() orders events by arrival (event_id), because every
event was validated here at the moment it was applied, so arrival order is the
authoritative accepted sequence. That makes rejecting a clinically-older message
this module's job and nothing else's: see _refuse_if_stale.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from .errors import ReferralLoopError, StaleMessageError
from .events import Loop, LoopEvent, LoopState
from .store import LoopStore

# OBX-11 result status codes we act on. HL7 table 0085.
PRELIMINARY = "P"
FINAL = "F"
CORRECTED = "C"

# States a loop may be closed from.
_CLOSEABLE_FROM = frozenset({LoopState.RESULTED})

# OBX-11 values that may open the door to CLOSED. An allowlist: rule 1 must not
# be expressible as "anything that is not a preliminary", because that closes on
# every value we failed to anticipate.
_CLOSEABLE_STATUSES = frozenset({FINAL, CORRECTED})

# Only these event types carry a result. A merged_in event (Task 7) copies
# fields off another loop, and an orphaned event carries caller-supplied detail;
# if either could set the result status, a merge or an orphan would flip a
# preliminary to final with no result ever arriving.
_RESULT_EVENTS = frozenset({"resulted", "reopened"})

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
        # acknowledgement lands second, and the loop is CLOSED on a superseded
        # read -- safety rule 2 defeated with no error raised anywhere.
        # socketserver hands each connection to the handler, and an interface
        # engine routinely holds several. Scope: one process. Two processes on
        # one database file are not serialized by this, and the store's own note
        # on _materialize says the same; that is a deployment constraint, not
        # something this module can express.
        self._lock = threading.RLock()

    def get(self, loop_id: str) -> Loop:
        return self.store.replay(loop_id)

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
    ) -> str:
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
            self.store.append_event(
                LoopEvent(
                    loop_id=loop_id,
                    event_type="created",
                    occurred_at=_now(),
                    control_id=control_id,
                    detail=self._stamp(
                        {
                            "mrn": mrn,
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

    def orphan(self, control_id: str, mrn: str, detail: dict, message_at: datetime | None = None) -> str:
        """Create a loop-shaped record to hold a result nobody ordered.

        ORPHAN is not in _CLOSEABLE_FROM, so an orphan cannot be acknowledged
        away; it is retired by attachment to a real loop (Task 15).
        """
        loop_id = f"O-{uuid.uuid4().hex[:12]}"
        # mrn last: the explicit argument wins over a stray key in detail, so a
        # match key cannot silently re-attribute the record to another patient.
        self.store.append_event(
            LoopEvent(
                loop_id, "orphaned", _now(), control_id,
                self._stamp({**detail, "mrn": mrn}, message_at),
            )
        )
        return loop_id

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

            if loop.state is LoopState.ORPHAN:
                # Otherwise ORPHAN -> RESULTED -> CLOSED retires a result nobody
                # ordered through the ordinary worklist, and it leaves the orphan
                # queue the gap flywheel counts without any coordinator ever
                # attaching it. An orphan is retired by attach_orphan (Task 15).
                raise ReferralLoopError(
                    f"Loop {loop_id} is an orphan; results are not recorded against orphans. "
                    "Attach it to a real loop instead."
                )

            if obx11 not in (PRELIMINARY, FINAL, CORRECTED):
                raise ReferralLoopError(f"Unhandled OBX-11 status: {obx11!r}")

            self._refuse_if_stale(loop_id, message_at, f"result {obx11!r}")

            # Rule 2, generalised. A correction always reopens review; so does
            # any result landing on a loop somebody has already closed, because
            # the acknowledgement was made against a read this message
            # supersedes.
            if obx11 == CORRECTED or loop.state is LoopState.CLOSED:
                detail = self._stamp({"obx11": obx11, **_CLEARED_ACK}, message_at)
                self.store.append_event(LoopEvent(loop_id, "reopened", _now(), control_id, detail))
                return

            self.store.append_event(
                LoopEvent(
                    loop_id, "resulted", _now(), control_id, self._stamp({"obx11": obx11}, message_at)
                )
            )

    def acknowledge(self, loop_id: str, actor: str, role: str, control_id: str) -> None:
        """Close the loop. Refuses on a preliminary read -- spec section 4 rule 1.

        `role` is recorded because if the acknowledging party is not clinically
        responsible then CLOSED means 'someone looked at it', and the worklist
        must not claim more than that. See open question 2. The status actually
        acknowledged is recorded alongside, so the audit answers "what did they
        look at" as well as "who looked".

        No message_at: this is a human action, not a message, and it must not
        advance the clinical watermark. If it did, an acknowledgement made today
        would make a correction whose MSH-7 predates it look stale, and safety
        rule 2 would stop firing without any test noticing.
        """
        if not actor or not role:
            raise ReferralLoopError(
                "An acknowledgement needs a named actor and role; CLOSED attributed to "
                "nobody cannot answer who closed the loop or whether they were responsible"
            )

        with self._lock:
            loop = self.get(loop_id)
            if loop.state not in _CLOSEABLE_FROM:
                raise ReferralLoopError(f"Cannot close a loop in state {loop.state}")

            status = self._latest_result_status(loop_id)
            if status not in _CLOSEABLE_STATUSES:
                raise ReferralLoopError(
                    f"Loop {loop_id} has no final or corrected result (latest OBX-11 {status!r}); "
                    "CLOSED is unreachable"
                )

            at = _now()
            self.store.append_event(
                LoopEvent(
                    loop_id, "closed", at, control_id,
                    {
                        "ack_by": actor,
                        "ack_role": role,
                        "ack_at": at.isoformat(),
                        "ack_result_status": status,
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
        is exactly when getting it wrong would let CLOSED be reached on a
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
