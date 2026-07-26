"""Typed records. Every field here is one the allowlist permits."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LoopState(str, Enum):
    OPEN = "OPEN"
    SCHEDULED = "SCHEDULED"
    RESULTED = "RESULTED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    ORPHAN = "ORPHAN"
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
    event_type: str        # "created" | "scheduled" | "resulted" | "closed" | ...
    occurred_at: datetime
    control_id: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    loop_id: str | None
    tier: int              # 1-4 matched, 5 = no match
    confidence: float
    reason: str
