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
    # STALE is deliberately absent -- it is derived at read time. See staleness.py.
    # Storing it would destroy the underlying OPEN/SCHEDULED state, and since no
    # message causes the transition there would be no event to replay, making
    # "state reconstructible from loop_events alone" unsatisfiable.


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
