"""Typed events and records for the referral loop pipeline.

Stdlib only. This module is the probe target of the import-closure test,
so it must not import anything outside the standard library.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Loop states (spec §4) ─────────────────────────────────────────────────────
# STALE is absent by design: it is derived at read time from age + the pack's
# per-modality threshold, never stored. See registry.effective_state.
OPEN = "OPEN"
SCHEDULED = "SCHEDULED"
RESULTED = "RESULTED"
CLOSED = "CLOSED"
CANCELLED = "CANCELLED"
ORPHAN = "ORPHAN"
STALE = "STALE"  # derived overlay only

STORED_STATES = frozenset({OPEN, SCHEDULED, RESULTED, CLOSED, CANCELLED, ORPHAN})

# ── Result statuses (HL7 OBX-11) ─────────────────────────────────────────────
PRELIMINARY = "P"
FINAL = "F"
CORRECTED = "C"


@dataclass(frozen=True)
class HL7Message:
    """A parsed message, restricted to allowlisted segments."""
    control_id: str                              # MSH-10
    message_type: str                              # e.g. "ORU^R01"
    raw: str
    segments: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def first(self, seg_id: str) -> tuple[str, ...] | None:
        for sid, fields in self.segments:
            if sid == seg_id:
                return fields
        return None

    def all(self, seg_id: str) -> list[tuple[str, ...]]:
        return [f for sid, f in self.segments if sid == seg_id]


@dataclass(frozen=True)
class LoopEvent:
    """Append-only. Replaying these in order reconstructs a loop (spec §10.5)."""
    loop_id: str
    event_type: str      # created|scheduled|resulted|closed|reopened|cancelled|merged|attached
    control_id: str      # MSH-10 of the causing message; "" for coordinator actions
    payload: str          # JSON
    occurred_at: str      # ISO 8601


@dataclass
class Loop:
    """Projection of a loop's event history."""
    loop_id: str
    mrn: str
    placer_order_number: str = ""
    filler_order_number: str = ""
    service_code: str = ""
    modality: str = ""
    ordering_provider: str = ""
    ordered_at: str = ""                  # ISO 8601
    state: str = OPEN
    last_result_status: str = ""          # OBX-11 of the most recent result
    merged_from: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MatchResult:
    """Outcome of resolving a result message against open loops (spec §5)."""
    loop_id: str | None
    tier: int            # 1..4 matched, 5 = orphan
    confidence: float
    reason: str
