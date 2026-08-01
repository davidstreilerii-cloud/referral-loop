"""Derived per-modality staleness.

STALE is never stored (events.py, LoopState) -- it is computed here at read
time from state, ordered_at, and the pack's per-modality threshold. Every test
in this module that exercises is_stale/staleness_ratio runs with the site's
acceptance already granted (see `_accepted` below); test_thresholds_must_be_*
is the one place that deliberately withholds it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from referral_loop.errors import ThresholdsNotAcceptedError
from referral_loop.events import Loop, LoopState
from referral_loop.staleness import (
    age,
    is_stale,
    require_thresholds_accepted,
    staleness_ratio,
)
from tests._pack import PACK

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _accepted(monkeypatch):
    """Every test here runs post-acceptance except the gate test itself."""
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")


def _loop(state=LoopState.OPEN, modality="CT", hours_ago=1, ordered_at="_default"):
    if ordered_at == "_default":
        ordered_at = NOW - timedelta(hours=hours_ago)
    return Loop(
        loop_id="L1", mrn="MRN1", state=state, modality=modality,
        ordered_at=ordered_at,
    )


# ---------------------------------------------------------------- is_stale


def test_stat_ct_is_stale_at_five_hours():
    assert is_stale(_loop(modality="CT", hours_ago=5), NOW, PACK) is True


def test_stat_ct_is_not_stale_at_three_hours():
    assert is_stale(_loop(modality="CT", hours_ago=3), NOW, PACK) is False


def test_threshold_is_per_modality():
    """A CT at 100h is stale; the same age on the default threshold is not."""
    assert is_stale(_loop(modality="CT", hours_ago=100), NOW, PACK) is True
    assert is_stale(_loop(modality="MG", hours_ago=100), NOW, PACK) is False


def test_modality_absent_from_pack_falls_back_to_default():
    """MG is not a key in PACK.staleness_hours; it must use `_default` (336h)."""
    assert "MG" not in PACK.staleness_hours
    assert is_stale(_loop(modality="MG", hours_ago=335), NOW, PACK) is False
    assert is_stale(_loop(modality="MG", hours_ago=337), NOW, PACK) is True


def test_acknowledged_loops_are_never_stale():
    """v1's actual terminal state (spec section 4) -- not CLOSED, which is
    reserved for v2 and unreachable."""
    assert is_stale(_loop(state=LoopState.ACKNOWLEDGED, hours_ago=9999), NOW, PACK) is False


def test_cancelled_loops_are_never_stale():
    assert is_stale(_loop(state=LoopState.CANCELLED, hours_ago=9999), NOW, PACK) is False


def test_dismissed_loops_are_never_stale():
    assert is_stale(_loop(state=LoopState.DISMISSED, hours_ago=9999), NOW, PACK) is False


def test_resulted_loops_are_not_stale():
    """The result arrived. It awaits acknowledgement, a different queue."""
    assert is_stale(_loop(state=LoopState.RESULTED, hours_ago=9999), NOW, PACK) is False


def test_orphan_loops_are_not_stale():
    """An orphan already has a result; nothing further is being awaited. Its
    problem is identification, not turnaround time."""
    assert is_stale(_loop(state=LoopState.ORPHAN, hours_ago=9999), NOW, PACK) is False


def test_future_dated_observation_is_clamped_not_rejected():
    """Failure matrix: accept, clamp for staleness math, flag. Clock skew is endemic."""
    future = _loop(hours_ago=-48)
    assert is_stale(future, NOW, PACK) is False


def test_ordered_at_none_is_always_stale_not_hidden():
    """An OPEN/SCHEDULED loop with no known order time is a data-quality gap,
    not evidence of timeliness. Silently reporting False would bury it at the
    bottom of every worklist forever -- the one place it must never land."""
    orphaned_order = _loop(state=LoopState.OPEN, ordered_at=None)
    assert is_stale(orphaned_order, NOW, PACK) is True


def test_is_stale_at_exactly_the_threshold_is_false():
    """staleness_ratio documents 1.0 as "exactly at threshold"; is_stale means
    "waited longer than allowed", so the boundary itself is not yet stale --
    guards against an off-by-one (>= instead of >)."""
    at_threshold = _loop(modality="CT", hours_ago=4)  # PACK CT threshold is 4h
    assert is_stale(at_threshold, NOW, PACK) is False


def test_scheduled_state_is_staleable():
    assert is_stale(_loop(state=LoopState.SCHEDULED, modality="CT", hours_ago=5), NOW, PACK) is True


# ---------------------------------------------------------------- age


def test_age_is_zero_for_future_dated_order():
    assert age(_loop(hours_ago=-48), NOW) == timedelta(0)


def test_age_matches_elapsed_time_for_past_order():
    assert age(_loop(hours_ago=5), NOW) == timedelta(hours=5)


def test_age_is_zero_when_ordered_at_is_none():
    assert age(_loop(ordered_at=None), NOW) == timedelta(0)


def test_age_never_raises_on_naive_ordered_at_against_aware_now():
    """A naive ordered_at (e.g. round-tripped through a store that doesn't
    preserve tzinfo) against a tz-aware `now` must not raise TypeError -- that
    would fail the whole worklist render rather than mis-rank one loop."""
    naive_loop = _loop(ordered_at=datetime(2026, 7, 25, 7, 0))  # naive, no tzinfo
    assert age(naive_loop, NOW) == timedelta(hours=5)


def test_is_stale_never_raises_on_naive_ordered_at_against_aware_now():
    naive_loop = _loop(modality="CT", ordered_at=datetime(2026, 7, 25, 6, 0))
    assert is_stale(naive_loop, NOW, PACK) is True


# ---------------------------------------------------------------- staleness_ratio


def test_staleness_ratio_is_one_at_exactly_the_threshold():
    at_threshold = _loop(modality="CT", hours_ago=4)  # PACK CT threshold is 4h
    assert staleness_ratio(at_threshold, NOW, PACK) == pytest.approx(1.0)


def test_staleness_ratio_below_one_when_not_yet_stale():
    assert staleness_ratio(_loop(modality="CT", hours_ago=2), NOW, PACK) == pytest.approx(0.5)


def test_staleness_ratio_above_one_when_stale():
    assert staleness_ratio(_loop(modality="CT", hours_ago=8), NOW, PACK) == pytest.approx(2.0)


def test_staleness_ratio_is_zero_for_terminal_loop():
    """Terminal/non-staleable loops sort out of the way at 0.0, not some
    residual age-derived number that could still rank them mid-worklist."""
    ack = _loop(state=LoopState.ACKNOWLEDGED, hours_ago=99999)
    assert staleness_ratio(ack, NOW, PACK) == 0.0


def test_staleness_ratio_is_zero_for_resulted_and_orphan():
    assert staleness_ratio(_loop(state=LoopState.RESULTED, hours_ago=99999), NOW, PACK) == 0.0
    assert staleness_ratio(_loop(state=LoopState.ORPHAN, hours_ago=99999), NOW, PACK) == 0.0


def test_staleness_ratio_is_infinite_for_missing_ordered_at():
    """Sorts to the very top of a worklist ordered on this ratio, matching
    is_stale's "always stale" rather than a 0.0 it did nothing to earn."""
    assert staleness_ratio(_loop(ordered_at=None), NOW, PACK) == float("inf")


def test_staleness_ratio_handles_zero_threshold_without_dividing_by_zero(monkeypatch):
    from dataclasses import replace

    zero_threshold_pack = replace(PACK, staleness_hours={**PACK.staleness_hours, "CT": 0})
    assert staleness_ratio(_loop(modality="CT", hours_ago=1), NOW, zero_threshold_pack) == float("inf")
    # An order placed at this exact instant (zero age, zero threshold) has not
    # yet had a chance to be late.
    assert staleness_ratio(_loop(modality="CT", hours_ago=0), NOW, zero_threshold_pack) == 0.0


def test_staleness_ratio_handles_negative_threshold_without_flipping_sort_order():
    from dataclasses import replace

    negative_pack = replace(PACK, staleness_hours={**PACK.staleness_hours, "CT": -5})
    assert staleness_ratio(_loop(modality="CT", hours_ago=1), NOW, negative_pack) == float("inf")


def test_worklist_sort_puts_most_overdue_and_unknown_first():
    """Sanity check on the actual use case: sorting a mixed worklist on
    staleness_ratio descending should put the missing-ordered_at loop and the
    most-overdue loop ahead of a barely-stale one, and terminal loops last."""
    barely_stale = _loop(modality="CT", hours_ago=5)      # ratio 1.25
    very_stale = _loop(modality="CT", hours_ago=40)        # ratio 10.0
    terminal = _loop(state=LoopState.ACKNOWLEDGED, hours_ago=99999)
    unknown_order_time = _loop(ordered_at=None)
    loops = [barely_stale, very_stale, terminal, unknown_order_time]

    ranked = sorted(loops, key=lambda loop: staleness_ratio(loop, NOW, PACK), reverse=True)
    assert ranked == [unknown_order_time, very_stale, barely_stale, terminal]


# ---------------------------------------------------------------- require_thresholds_accepted


def test_thresholds_must_be_explicitly_accepted(monkeypatch):
    """Open question 3: shipping a default implies a clinical standard."""
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    with pytest.raises(ThresholdsNotAcceptedError):
        require_thresholds_accepted()

    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    require_thresholds_accepted()


def test_is_stale_itself_refuses_without_acceptance(monkeypatch):
    """The gate is enforced at the point of use, not merely available for a
    caller to remember: is_stale must refuse on its own, without any other
    module having to call require_thresholds_accepted first."""
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    with pytest.raises(ThresholdsNotAcceptedError):
        is_stale(_loop(modality="CT", hours_ago=5), NOW, PACK)


def test_staleness_ratio_itself_refuses_without_acceptance(monkeypatch):
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    with pytest.raises(ThresholdsNotAcceptedError):
        staleness_ratio(_loop(modality="CT", hours_ago=5), NOW, PACK)
