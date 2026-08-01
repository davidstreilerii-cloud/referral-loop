"""Typed records. Every field here is one the allowlist permits."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LoopState(str, Enum):
    OPEN = "OPEN"
    SCHEDULED = "SCHEDULED"
    RESULTED = "RESULTED"

    # ACKNOWLEDGED is v1's terminal state and CLOSED is reserved for v2. They are
    # two different claims and the weaker one was wearing the stronger one's name
    # (spec section 4). A coordinator can reliably confirm that *this result
    # belongs to this order* -- identifier work. Whether a clinician competent to
    # act on an abnormal finding has read it is a different assertion, and nothing
    # in v1 observes it. Collapsing them reproduces the product's own failure mode
    # one layer up: every loop reported closed while no clinician saw a result.
    ACKNOWLEDGED = "ACKNOWLEDGED"

    # Reserved, never entered in v1. Defined rather than omitted so the name
    # cannot be quietly reused for the weaker claim; no event type maps to it and
    # LoopStore.append_event refuses any attempt to reach it. It ships in v2 once
    # a pilot site names who may clinically disposition a finding (spec q2).
    CLOSED = "CLOSED"

    CANCELLED = "CANCELLED"
    ORPHAN = "ORPHAN"

    # Terminal for orphans that belong to no loop here -- misrouted from another
    # facility, a feed misconfiguration. Without it the orphan queue only grows,
    # and a queue that only grows is one coordinators stop opening, which
    # silently disables both the safety surface and the flywheel (spec section 5).
    DISMISSED = "DISMISSED"

    # Terminal for an orphan a coordinator attached to a real loop (spec section
    # 5: "attaching an orphan is then a merge into the real loop"). It needs a
    # state of its own and cannot borrow one:
    #
    #   * DISMISSED means "belongs to no loop *here*" and its rate is a watched
    #     feed-health metric (spec sections 5 and 7). Retiring attached orphans
    #     through it would inflate the one number that is supposed to say the
    #     feed is misconfigured, so a busy attaching coordinator would read as a
    #     broken interface.
    #   * CANCELLED means an expectation was withdrawn. An orphan carries no
    #     expectation -- it is a result that already arrived -- and the failure
    #     matrix gives CANCELLED its own meaning for arriving results.
    #   * Leaving it in ORPHAN would put it back on the queue the coordinator
    #     just cleared, which is the queue-only-grows failure DISMISSED exists to
    #     prevent, arriving through the other door.
    #
    # Like DISMISSED it is terminal, is never entered automatically, and appears
    # in no worklist queue -- while the record itself stays in loop_events, so
    # what the coordinator did remains replayable.
    ATTACHED = "ATTACHED"
    # STALE is deliberately absent -- it is derived at read time. See staleness.py.
    # Storing it would destroy the underlying OPEN/SCHEDULED state, and since no
    # message causes the transition there would be no event to replay, making
    # "state reconstructible from loop_events alone" unsatisfiable.


class LabelType(str, Enum):
    """Which coordinator action produced a label. A closed set.

    Spec section 7's flywheel: every orphan a coordinator attaches is a labeled
    example, and every auto-match they undo is a labeled false positive -- more
    valuable than a synthetic case because it is a real interface quirk from a
    real site. Section 5 adds dismissals, whose rate is watched for feed drift.

    Four things a coordinator can tell the system, kept apart because they say
    different things about the matcher and the release gate reads them
    differently.
    """

    ORPHAN_ATTACHED = "orphan_attached"
    MATCH_UNDONE = "match_undone"
    ATTACHMENT_UNDONE = "attachment_undone"
    ACKNOWLEDGEMENT_REVERSED = "acknowledgement_reversed"
    ORPHAN_DISMISSED = "orphan_dismissed"


class LabelOutcome(str, Enum):
    """What a label says about the *matcher*, which is not the same question.

    The release gate in spec section 7 vetoes on false-match rate. It therefore
    needs one column it can count without knowing which button a coordinator
    pressed -- and, more importantly, without folding human clerical error into
    the one metric that is an absolute veto.
    """

    # The matcher declined and a human says it should not have. Recall / orphan
    # rate, never false-match rate.
    MISSED_MATCH = "missed_match"
    # The matcher attached a result to the wrong loop. THE false-match label.
    FALSE_MATCH = "false_match"
    # A human attached a result to the wrong loop and undid it. A human error,
    # deliberately not a false match: the matcher never made this claim, and
    # counting it would make a coordinator's slip veto a pack release.
    MISTAKEN_ATTACHMENT = "mistaken_attachment"
    # A coordinator withdrew their own confirmation. Says the acknowledgement
    # was premature or misplaced; says nothing on its own about the match.
    ACKNOWLEDGEMENT_WITHDRAWN = "acknowledgement_withdrawn"
    # The result belongs to no loop at this site. Feed health (section 5).
    NO_LOOP_HERE = "no_loop_here"


# Derived, never passed in. LoopStore.record_label computes the outcome from the
# label type rather than accepting it, so the two cannot drift apart and a caller
# cannot label a coordinator's slip as a false match.
LABEL_OUTCOME = {
    LabelType.ORPHAN_ATTACHED: LabelOutcome.MISSED_MATCH,
    LabelType.MATCH_UNDONE: LabelOutcome.FALSE_MATCH,
    LabelType.ATTACHMENT_UNDONE: LabelOutcome.MISTAKEN_ATTACHMENT,
    LabelType.ACKNOWLEDGEMENT_REVERSED: LabelOutcome.ACKNOWLEDGEMENT_WITHDRAWN,
    LabelType.ORPHAN_DISMISSED: LabelOutcome.NO_LOOP_HERE,
}


@dataclass(frozen=True)
class ParsedMessage:
    control_id: str                       # MSH-10
    message_type: str                     # "ORU^R01"
    is_known_type: bool
    segments: dict[str, list[list[str]]]  # allowlisted segments only
    flags_for_review: tuple[str, ...] = ()


@dataclass(frozen=True)
class Loop:
    loop_id: str
    mrn: str
    state: LoopState
    placer_order_number: str = ""
    filler_order_number: str = ""
    service_code: str = ""
    modality: str = ""
    ordering_provider: str = ""
    ordered_at: datetime | None = None
    ack_by: str = ""
    ack_role: str = ""
    ack_at: datetime | None = None


@dataclass(frozen=True)
class LoopEvent:
    loop_id: str
    event_type: str        # "created" | "scheduled" | "resulted" | "acknowledged" | ...
    occurred_at: datetime
    control_id: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    loop_id: str | None
    tier: int              # 1-4 matched, 5 = no match
    confidence: float
    reason: str
    # Set when an exact tier found candidates but the result named no patient,
    # so the match was declined rather than made (matcher._unattributable). A
    # field rather than something the listener re-derives from `reason` or from
    # the tier: it is the trigger for an operational counter and a warning, and
    # a counter keyed on a substring of a prose sentence stops counting the
    # first time the sentence is reworded.
    patient_unverified: bool = False
