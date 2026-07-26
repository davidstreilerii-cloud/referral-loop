"""Staleness is derived, never stored.

Writing STALE into the state column would destroy the underlying state -- a
stale loop is still OPEN or SCHEDULED, and must return to plain OPEN the moment
a result arrives, without a second transition to undo. Worse, no message
causes the STALE transition; it is entered by the passage of time alone, so
there would be no event to replay and "state reconstructible from loop_events
alone" (spec section 10.5) would be unsatisfiable. So it is computed at read
time from `ordered_at`, `state`, and the pack's per-modality threshold, and
used as the worklist's primary sort key (see staleness_ratio).

Thresholds ship as defaults but the site must accept them explicitly (spec
open question 3). Shipping a number silently would imply a clinical standard
that is the hospital's call, not ours. That acceptance gate is enforced here,
at the two functions that actually label or rank a loop as stale -- not left
for some future caller to remember -- so the guarantee holds regardless of who
calls in. `age()` does not gate: it reports a raw duration and asserts nothing
about lateness, so it carries no clinical claim to guard.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .errors import ThresholdsNotAcceptedError
from .events import Loop, LoopState
from .pack import RulePack

# Only OPEN and SCHEDULED loops are still waiting on a result in the sense
# staleness means -- an order placed (or scheduled) and nothing back yet.
#
# RESULTED already has a result; it is waiting on a clinician's acknowledgement,
# a different queue answering a different question ("did someone see this")
# than "is the order overdue". Folding it into staleness would compete for the
# same worklist slot with a genuinely unresulted order and bury the more urgent
# one.
#
# ACKNOWLEDGED, CANCELLED, and DISMISSED are terminal: nothing is being waited
# on, so "stale" has no referent. CLOSED is reserved for v2 and unreachable in
# v1 (events.py, LoopStore.append_event refuses it) -- excluded on the same
# terminal reasoning, even though no loop can ever hold it today.
#
# ORPHAN is a result that arrived with no matching order. It already has data;
# there is nothing further being awaited, so "overdue for a result" does not
# apply. An orphan's problem is identification (it needs a human to match it
# or dismiss it), not turnaround time -- a different queue, the same way
# RESULTED is.
_STALEABLE_STATES = frozenset({LoopState.OPEN, LoopState.SCHEDULED})


def require_thresholds_accepted() -> None:
    """Refuse to compute staleness until the site has accepted the thresholds.

    Per-modality staleness thresholds ship as defaults in the signed pack, but
    a threshold implies a clinical standard -- how long is too long to wait for
    a stat CT versus a screening mammogram -- and that call belongs to the
    hospital, not to us. Called by is_stale and staleness_ratio themselves
    (not merely documented as a precondition) so the gate cannot be bypassed by
    a caller that forgets to check it first.
    """
    if os.environ.get("REFERRAL_THRESHOLDS_ACCEPTED", "0") != "1":
        raise ThresholdsNotAcceptedError(
            "Per-modality staleness thresholds are shipped defaults, not a clinical "
            "standard. Set REFERRAL_THRESHOLDS_ACCEPTED=1 after the site has "
            "reviewed rules/pack.json staleness_hours."
        )


def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are treated as UTC so age arithmetic never raises.

    Mixing an aware `now` with a naive stored `ordered_at` raises TypeError on
    subtraction. A TypeError here means the whole worklist fails to render
    instead of one loop being ranked wrong, which is a far worse failure --
    same reasoning, and the same fix, as matcher._as_utc. Assuming UTC is
    consistent with how the store and registry write timestamps (store.py
    round-trips via datetime.isoformat/fromisoformat; nothing in this
    subsystem writes a non-UTC naive timestamp on purpose).
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def age(loop: Loop, now: datetime) -> timedelta:
    """Age of the expectation. Future-dated orders clamp to zero, never negative.

    Clock skew between a RIS and an interface engine is endemic; the failure
    matrix says accept the message, clamp for staleness math, and flag
    elsewhere (parsing/matching), not here. A loop with no known order time
    (`ordered_at is None`) also reports zero age -- this is a raw-duration
    primitive with no basis for inventing a timestamp, and it makes no clinical
    claim by itself. See is_stale for why a missing ordered_at is nonetheless
    never treated as "not stale".
    """
    if loop.ordered_at is None:
        return timedelta(0)
    delta = _as_utc(now) - _as_utc(loop.ordered_at)
    return delta if delta > timedelta(0) else timedelta(0)


def is_stale(loop: Loop, now: datetime, pack: RulePack) -> bool:
    """True only for a loop that can still be waiting on a result.

    A loop with `ordered_at is None` is always stale, never "not stale". An
    OPEN or SCHEDULED loop with no known order time is a data-quality gap, not
    evidence of timeliness -- silently reporting False would rank it at the
    quiet bottom of every worklist forever, hidden exactly where it most needs
    a human to notice it. Treating "unknown" as "already overdue" fails toward
    visibility instead of toward silence, consistent with how this subsystem
    treats every other unknown (an unmatched result becomes a visible ORPHAN,
    never a silently dropped message).
    """
    require_thresholds_accepted()
    if loop.state not in _STALEABLE_STATES:
        return False
    if loop.ordered_at is None:
        return True
    threshold = timedelta(hours=pack.staleness_threshold_hours(loop.modality))
    return age(loop, now) > threshold


def staleness_ratio(loop: Loop, now: datetime, pack: RulePack) -> float:
    """How far past threshold, for worklist sorting. 1.0 is exactly at threshold.

    A modality absent from the pack's staleness_hours falls back to
    `_default` (RulePack.staleness_threshold_hours; load_pack enforces that
    `_default` always exists). Terminal/RESULTED/ORPHAN loops sort as 0.0 --
    they are not on this worklist at all, and 0.0 keeps them out of the way of
    a sort ascending or descending. A missing `ordered_at` sorts as +inf,
    matching is_stale's "always stale": it belongs at the very top of a
    worklist sorted on this ratio, not buried by a 0.0 it did nothing to earn.
    """
    require_thresholds_accepted()
    if loop.state not in _STALEABLE_STATES:
        return 0.0
    if loop.ordered_at is None:
        return float("inf")
    threshold_hours = pack.staleness_threshold_hours(loop.modality)
    age_hours = age(loop, now).total_seconds() / 3600
    if threshold_hours <= 0:
        # A non-positive threshold means "stale the instant it isn't brand
        # new". Dividing by zero (or a negative number, which would flip the
        # sort order) is not meaningful here; report the same maximally-stale
        # signal a missing ordered_at gets, so a worklist sorted on this ratio
        # still surfaces it first rather than burying it under a stray 0.0.
        return float("inf") if age_hours > 0 else 0.0
    return age_hours / threshold_hours
