"""The only thing that moves a referral's state, and the one move it will never make.

`apply()` is pure: no clock, no store, no network, no logging. Everything time-shaped
arrives on the `Transition` the caller built, which is what makes an event log replayable
and a transition testable without a fixture. tests/test_machine.py asserts that on this
file's source, because the failure mode is an import added for one debug line.

Two enforcement points, checked in this order and no other:

1. **Spec 6.3, the auto-close guarantee.** A move into `RECONCILED` whose assertion source
   is anything but `HUMAN` is refused. `RECONCILED` means a coordinator at *this* site
   looked at the returned documentation and agreed the loop is closed; a receiving
   organisation asserting "done" over an interface is evidence toward that rather than the
   thing itself, and this system's own matcher inferring it from a document that named no
   order is further still. An inferred completion that is wrong is a patient-safety event:
   the loop leaves every worklist and nobody looks again.

   This replaces the existing design's "`CLOSED` is deliberately unreachable", which is
   enforced by refusing a reserved event type at the store. On the transition it survives
   someone making the state reachable.

2. **`LEGAL_TRANSITIONS`, the from-state table.**

3. **Spec rule 1, the preliminary prohibition.** A move into `RECONCILED` is permitted
   only when the referral's `documentation` is `FINAL` or `CORRECTED`. An allowlist, not
   a denylist: a referral documented by a read carrying no status at all -- a restored
   log, a foreign writer, a future code path -- must not reconcile merely because its
   status is not literally preliminary. `registry.py` enforces this today as
   `_ACKNOWLEDGEABLE_STATUSES` and makes the same allowlist argument in the same words.

The order is load-bearing rather than incidental. `RECONCILED` is legal only from
`DOCUMENTED`, so checking legality first would mean every other state refused a
system-asserted reconciliation as an illegal edge and the safety refusal was never
reached -- leaving it exercised for the first time in production on the day someone adds
an edge. Checked first, it is the reason the state-space sweep says anything at all.

The human guard stays above both: "the system may not reconcile" holds whatever the
documentation says, and whatever the table says.

Spec rule 1 sits *below* the table, and that ordering is the audit's. A loop in a state
that cannot reconcile at all is a bookkeeping refusal, and reporting it as the preliminary
prohibition would inflate the one refusal count a risk officer watches by name with every
wrong-state attempt. The table answers first; rule 1 answers only for the edge the table
allows, which is the only edge it has an opinion about.

## What is deliberately not here: the ordering axis

The clinical watermark and `_refuse_if_stale` stay at the ingest boundary in
`registry.py`. That is their correct home rather than a concession, and the distinction is
worth stating because it reads like an oversight to anyone who did not watch it get
decided:

* `_refuse_if_stale` asks **"should I apply this message at all?"** -- a question about a
  *message*, answered before there is a transition to judge, from the message's `MSH-7`
  and a fold over the loop's event log.
* `apply()` asks **"is this state change legal?"** -- a question about *state*, answered
  from the aggregate and the transition alone.

Two different questions with two different inputs. Collapsing them would put a clock and
an event-log fold inside a pure function in order to answer something that was never the
machine's question, and it would make every transition untestable without a store.

The same boundary owns the destructive/non-destructive distinction. `_refuse_if_stale`
fails open for `schedule` and demands a readable clock for `cancel` and `record_result`;
that is the ingest mapper deciding how much evidence a message must carry before it may
build a `Transition` at all. It is deliberately not a field on `Transition` -- a caller
that declared its own destructiveness could declare itself harmless.

`documentation` is on the other side of that line, which is why it is on the aggregate:
it is a fact *about the referral*, not about the message that carried it.
"""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import Mapping

from ..errors import ReferralLoopError
from .models import Referral, ReferralId
from .states import DocumentationStatus, ReferralState
from .transitions import AssertionSource, Transition

# The documentation a coordinator may reconcile on. An allowlist, and the distinction
# matters at exactly one value: `documentation is None` -- a referral documented by an
# event that carried no status -- must be refused, and a guard written as
# "not PRELIMINARY" would reconcile it. registry.py makes this argument in the same words
# about the same rule, and tests/test_registry_safety.py:125 pins it.
_RECONCILABLE_DOCUMENTATION = frozenset({
    DocumentationStatus.FINAL,
    DocumentationStatus.CORRECTED,
})


class RejectionReason(str, Enum):
    """Which rule refused, since the exception class alone does not say.

    The same argument `audit.RefusalCode` already makes for `registry.acknowledge`:
    refusing because the referral is in the wrong state is bookkeeping and refusing
    because only a human reconciles is spec 6.3, an auditor cannot tell those apart from
    "TransitionRejected", and the message that would tell them apart is exactly what must
    not be copied into an audit row.
    """

    RECONCILE_REQUIRES_A_HUMAN = "reconcile_requires_a_human"
    # The descendant of audit.RefusalCode.PRELIMINARY_NOT_ACKNOWLEDGEABLE, named to match
    # it so a Phase 2 audit row written from either enforcement point reads the same to
    # the risk officer who asks about this refusal by name.
    PRELIMINARY_NOT_RECONCILABLE = "preliminary_not_reconcilable"
    NOT_A_LEGAL_TRANSITION = "not_a_legal_transition"


class TransitionRejected(ReferralLoopError):
    """A transition the machine will not apply.

    A `ReferralLoopError` so it lands in the clause `listener.py` already uses to answer
    the sending engine, rather than in the bare `except Exception` immediately below it.

    The message names the referral and the attempted move and nothing else. It must never
    carry a patient identifier: a confirmed finding in this codebase is that MRNs reach
    application logs through exception messages, and every caller of this logs the string.
    """

    def __init__(
        self,
        referral_id: ReferralId,
        from_state: ReferralState,
        to_state: ReferralState,
        reason: RejectionReason,
        detail: str,
    ) -> None:
        super().__init__(
            f"Referral {referral_id}: {from_state.name} -> {to_state.name} refused "
            f"({reason.value}); {detail}"
        )
        self.referral_id = referral_id
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason


# The lifecycle of design spec 6.1, written out rather than generated from the happy path,
# because the edges that are not on the happy path are the ones a reader has to be able to
# check. Every state is a key, including the terminal ones, so terminality is written down
# rather than inferred from a missing key -- and so the lookup cannot raise KeyError.
#
# Three properties this table carries beyond 6.1's straight line:
#
# * **Forward skips are legal.** An acknowledgement frequently never arrives, and an SIU
#   or a result is routinely the first thing a receiving organisation sends. The machine
#   registry.py runs today already permits this (`_SCHEDULABLE_FROM` contains OPEN, and
#   `record_result` refuses only the artifact states and CANCELLED), so a table demanding
#   each step would refuse work that succeeds now -- and Task 5 has to route those methods
#   through here without changing what they accept.
# * **Two self-edges.** SCHEDULED -> SCHEDULED is a reschedule and DOCUMENTED ->
#   DOCUMENTED is an updated or corrected document; both are legal in the current machine.
# * **RECONCILED is not terminal.** Spec 6.4: a corrected document demotes it back to
#   DOCUMENTED, which is the existing `reopened` event.
#
# AGED_OUT is reachable exactly from the states where we are waiting on the counterparty:
# SENT, RECEIVED, ACCEPTED, SCHEDULED, SEEN. Nowhere else, and the two exclusions are the
# rule rather than omissions.
#
# DRAFT is excluded because a draft has no counterparty to be silent; an abandoned one
# exits through CANCELLED, which needs a human, because deciding a referral is dead is a
# clinical judgement rather than a timeout.
#
# DOCUMENTED is excluded because there we are waiting on *ourselves*. A DOCUMENTED
# referral is one whose consult note came back and which no coordinator has reviewed --
# not an unclosed loop but an unreviewed one -- and it is the population
# `store.resulted_unacknowledged()` selects, a queue that exists precisely to stay
# non-empty until a person acts. Aging it out empties the queue that is the product.
# The existing store already takes this position: `_NEVER_DELETABLE` lists RESULTED, the
# same population under the old vocabulary, as "a result nobody has acknowledged".
# That AGED_OUT projects to `Task.status = failed` and is therefore loud rather than
# silent does not rescue it: a loud wrong status still removes the referral from the list
# a coordinator works.
#
# One backwards move, and the encounter is what bounds it. SCHEDULED -> ACCEPTED is an
# SIU^S15: the counterparty's scheduler saying a booking went away, which is not the
# referring side withdrawing the referral -- the patient still needs the visit and somebody
# has to book it again. Driving that to CANCELLED, as the legacy machine did, took a
# clinically open referral off every worklist permanently on the most benign path there is.
# See registry.unschedule.
#
# It is admitted because nothing clinical has happened yet. SCHEDULED -> SCHEDULED is
# *already* legal as a reschedule, and an S15 followed by an S12 is that same reschedule
# expressed in two messages rather than one -- so admitting the collapsed form while
# refusing the intermediate one would be incoherent.
#
# Backwards moves are otherwise absent, and the line is the encounter. A referral does not
# return to SCHEDULED or to ACCEPTED once SEEN: the patient attended, so there is no
# appointment left to cancel. CANCELLED after the encounter happened is likewise not a
# withdrawal -- registry.cancel already refuses a RESULTED loop for that reason, and
# CANCELLED appears on no worklist.
LEGAL_TRANSITIONS: Mapping[ReferralState, frozenset[ReferralState]] = {
    # Not yet anybody else's problem. AGED_OUT is absent: 6.1 defines it as terminal by
    # timeout with no counterparty signal, and a draft has no counterparty to be silent.
    ReferralState.DRAFT: frozenset({
        ReferralState.SENT,
        ReferralState.CANCELLED,
    }),
    ReferralState.SENT: frozenset({
        ReferralState.RECEIVED,
        ReferralState.ACCEPTED,
        ReferralState.DECLINED,
        ReferralState.SCHEDULED,
        ReferralState.SEEN,
        ReferralState.DOCUMENTED,
        ReferralState.CANCELLED,
        ReferralState.AGED_OUT,
    }),
    ReferralState.RECEIVED: frozenset({
        ReferralState.ACCEPTED,
        ReferralState.DECLINED,
        ReferralState.SCHEDULED,
        ReferralState.SEEN,
        ReferralState.DOCUMENTED,
        ReferralState.CANCELLED,
        ReferralState.AGED_OUT,
    }),
    # DECLINED is still reachable: an organisation that accepted a referral and later
    # refuses it is common, and modelling that as a cancellation would attribute the
    # refusal to the referring side.
    ReferralState.ACCEPTED: frozenset({
        ReferralState.DECLINED,
        ReferralState.SCHEDULED,
        ReferralState.SEEN,
        ReferralState.DOCUMENTED,
        ReferralState.CANCELLED,
        ReferralState.AGED_OUT,
    }),
    ReferralState.SCHEDULED: frozenset({
        # The S15 edge. See the note above the table: an appointment that went away is
        # not a referral that was withdrawn.
        ReferralState.ACCEPTED,
        ReferralState.SCHEDULED,
        ReferralState.SEEN,
        ReferralState.DOCUMENTED,
        ReferralState.CANCELLED,
        ReferralState.AGED_OUT,
    }),
    # The encounter happened. Withdrawing it is not a thing, and neither is the receiving
    # organisation declining it after the fact. AGED_OUT stays reachable because seen but
    # never documented is precisely the loop this product exists to escalate.
    ReferralState.SEEN: frozenset({
        ReferralState.DOCUMENTED,
        ReferralState.AGED_OUT,
    }),
    # No AGED_OUT. See the note above the table: from here the wait is on us.
    ReferralState.DOCUMENTED: frozenset({
        ReferralState.DOCUMENTED,
        ReferralState.RECONCILED,
    }),
    # Spec 6.4 and nothing else.
    ReferralState.RECONCILED: frozenset({
        ReferralState.DOCUMENTED,
    }),
    ReferralState.DECLINED: frozenset(),
    ReferralState.CANCELLED: frozenset(),
    ReferralState.AGED_OUT: frozenset(),
}


def apply(referral: Referral, transition: Transition) -> Referral:
    """Move a referral, or refuse to. Pure -- see the module docstring for what that costs.

    Returns a new `Referral`; the input is untouched, because Plan 2b replays the event log
    through this type and an in-place mutation would make a replayed prefix of the log
    disagree with the same prefix replayed twice.

    `state_occurred_at` comes off the transition rather than a clock, so a message carrying
    its own `MSH-7` is not restamped with its arrival time. `hold` moves only when the
    transition says so: `Transition.hold is None` means the hold is not this transition's
    business, which is a different thing from `HoldChange(hold=None)` lifting one.
    """
    if (
        transition.to_state is ReferralState.RECONCILED
        and transition.assertion_source is not AssertionSource.HUMAN
    ):
        raise TransitionRejected(
            referral.id,
            referral.state,
            transition.to_state,
            RejectionReason.RECONCILE_REQUIRES_A_HUMAN,
            f"only a human at this site reconciles a referral; {transition.assertion_source.value} "
            "is evidence toward that, not the coordinator's confirmation of it (spec 6.3)",
        )

    if transition.to_state not in LEGAL_TRANSITIONS[referral.state]:
        raise TransitionRejected(
            referral.id,
            referral.state,
            transition.to_state,
            RejectionReason.NOT_A_LEGAL_TRANSITION,
            "no such edge in the lifecycle of spec 6.1",
        )

    if (
        transition.to_state is ReferralState.RECONCILED
        and referral.documentation not in _RECONCILABLE_DOCUMENTATION
    ):
        raise TransitionRejected(
            referral.id,
            referral.state,
            transition.to_state,
            RejectionReason.PRELIMINARY_NOT_RECONCILABLE,
            "a referral is reconcilable only on documentation that is final or corrected; "
            f"this one's is {referral.documentation.value if referral.documentation else 'absent'} "
            "(spec rule 1)",
        )

    hold = referral.hold if transition.hold is None else transition.hold.hold
    return replace(
        referral,
        state=transition.to_state,
        hold=hold,
        state_occurred_at=transition.occurred_at,
        seq=referral.seq + 1,
    )
