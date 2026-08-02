"""The total projection from the canonical referral state onto FHIR R4 Task.

Design spec section 7 is the authoritative table; this module implements it and nothing
else. Two properties are load-bearing and both are pinned in
tests/test_fhir_task_status.py:

* SCHEDULED, SEEN and DOCUMENTED all collapse to `in-progress`, and those three are exactly
  what the aging agent escalates on. `businessStatus` is what keeps them apart, and it is
  the concrete reason the model is dual-layer rather than adopting Task.status as the
  vocabulary outright.
* A hold sets Task.status to `on-hold`, which discards the state entirely. So the
  businessStatus under a hold is the state the referral was held *from*: "accepted but
  never scheduled" and "scheduled but never seen" release into different aging thresholds,
  and a hold that had forgotten which it was would land in the wrong one.

Total by construction over ReferralState, and the totality is proved by parametrising the
tests over the enum rather than by asserting it here.
"""

from __future__ import annotations

from ..core.states import Hold, ReferralState

# The published R4 value set for Task.status (http://hl7.org/fhir/task-status, twelve
# codes), reproduced as data. THIS IS NOT OURS TO EDIT: the binding on Task.status is
# `required`, so a code that is not in this list is a code no conformant server will
# accept. Adding one here to make a mapping typecheck would move the failure from this
# repo's test suite to a receiving system at run time.
R4_TASK_STATUS = frozenset(
    {
        "draft",
        "requested",
        "received",
        "accepted",
        "rejected",
        "ready",
        "cancelled",
        "in-progress",
        "on-hold",
        "failed",
        "completed",
        "entered-in-error",
    }
)

# Design spec section 7. businessStatus is None wherever Task.status already carries the
# whole state, and populated wherever it does not.
_PROJECTION: dict[ReferralState, tuple[str, str | None]] = {
    ReferralState.DRAFT: ("draft", None),
    ReferralState.SENT: ("requested", None),
    ReferralState.RECEIVED: ("received", None),
    ReferralState.ACCEPTED: ("accepted", None),
    ReferralState.SCHEDULED: ("in-progress", "scheduled"),
    ReferralState.SEEN: ("in-progress", "seen"),
    ReferralState.DOCUMENTED: ("in-progress", "documented"),
    ReferralState.RECONCILED: ("completed", None),
    ReferralState.DECLINED: ("rejected", None),
    ReferralState.CANCELLED: ("cancelled", None),
    # AGED_OUT -> failed is a judgment call, flagged in spec section 7 as revisitable and
    # repeated here because that is where someone will read it. `failed` is terminal and
    # honest for reporting: the referral did not reach an outcome. The alternative reading
    # leaves aged-out referrals `in-progress` forever, which makes every in-progress count
    # a lie. It is filed as a decision to revisit if a customer objects -- `failed` may read
    # as blame where the truth is only silence, and the businessStatus code is what says
    # which of the two happened.
    ReferralState.AGED_OUT: ("failed", "aged-out"),
}


def project(state: ReferralState, *, hold: Hold | None) -> tuple[str, str | None]:
    """Return `(Task.status, Task.businessStatus | None)` for a referral state.

    Total: every ReferralState maps, held or not. The raise at the bottom is unreachable
    for any state in the enum -- the parametrised tests are what prove that, and they fail
    the moment a state is added without a mapping. It is there so that the failure is a
    named error at the projection site rather than a KeyError or a None that flows
    downstream and becomes an invalid resource somewhere else.
    """
    mapped = _PROJECTION.get(state)
    if mapped is None:
        raise AssertionError(f"unmapped referral state: {state!r} (spec section 7 table is incomplete)")

    if hold is not None:
        # Task.status has one on-hold code and no room for what was suspended, so the
        # state moves into businessStatus wholesale. Note this is looked up above first:
        # an unmapped state must fail here too, not only on the unheld path.
        #
        # A hold on a terminal state is meaningless, but refusing it is a state-machine
        # invariant (Plan 2b) and not the projection's job. This stays total either way.
        return "on-hold", state.value

    return mapped
