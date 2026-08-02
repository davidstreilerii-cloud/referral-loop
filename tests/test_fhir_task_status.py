"""The projection to FHIR R4 Task.status, and the properties that justify the dual layer.

Task.status cannot say "accepted but never scheduled" as distinct from "scheduled but the
patient was never seen": SCHEDULED, SEEN and DOCUMENTED all collapse to `in-progress`, and
those three are precisely what the aging agent escalates on. If businessStatus did not carry
that distinction, and if a hold did not preserve the state it was held from, the eleven-state
model would have no justification over adopting Task.status as the vocabulary directly. These
tests are that justification, expressed as something that can fail.
"""

import pytest

from referral_loop.core.states import Hold, ReferralState
from referral_loop.fhir.codesystems import BUSINESS_STATUS
from referral_loop.fhir.task_status import R4_TASK_STATUS, project

_HOLD = Hold(reason="x", actor="y")


@pytest.mark.parametrize("state", list(ReferralState))
def test_the_projection_is_total(state):
    status, business = project(state, hold=None)
    assert status in R4_TASK_STATUS, f"{state} -> {status} is not an R4 Task.status code"
    assert business is None or isinstance(business, str)


@pytest.mark.parametrize("state", list(ReferralState))
def test_the_projection_is_total_under_hold_as_well(state):
    status, business = project(state, hold=_HOLD)
    assert status == "on-hold"
    assert business is not None, "a hold must not discard which state it was held from"


def test_the_three_states_that_collapse_are_distinguished_by_business_status():
    """SCHEDULED, SEEN and DOCUMENTED all project to in-progress. Those are exactly the
    three distinctions the aging agent escalates on, which is the concrete reason the
    model is dual-layer rather than just adopting Task.status as the vocabulary."""
    collapsing = [ReferralState.SCHEDULED, ReferralState.SEEN, ReferralState.DOCUMENTED]
    projected = [project(s, hold=None) for s in collapsing]
    assert {p[0] for p in projected} == {"in-progress"}
    assert len({p[1] for p in projected}) == 3, "the three must stay distinguishable"


def test_a_held_referral_can_be_told_apart_from_a_referral_held_from_elsewhere():
    a = project(ReferralState.ACCEPTED, hold=_HOLD)
    b = project(ReferralState.SCHEDULED, hold=_HOLD)
    assert a[0] == b[0] == "on-hold"
    assert a[1] != b[1]


def test_every_business_status_code_is_declared_in_our_codesystem():
    declared = {c["code"] for c in BUSINESS_STATUS["concept"]}
    for state in ReferralState:
        for hold in (None, _HOLD):
            _, business = project(state, hold=hold)
            if business is not None:
                assert business in declared, f"{business} is emitted but not declared"


# --- the mapping itself, per design spec section 7 -------------------------------------

# Retyped from the spec's table so that a change to the projection has to be a change to
# the spec as well. Not derived from the implementation in any way -- a table generated
# from the thing it checks proves only that the generator ran.
_SPEC_TABLE_7 = {
    ReferralState.DRAFT: ("draft", None),
    ReferralState.SENT: ("requested", None),
    ReferralState.RECEIVED: ("received", None),
    ReferralState.ACCEPTED: ("accepted", None),
    ReferralState.DECLINED: ("rejected", None),
    ReferralState.SCHEDULED: ("in-progress", "scheduled"),
    ReferralState.SEEN: ("in-progress", "seen"),
    ReferralState.DOCUMENTED: ("in-progress", "documented"),
    ReferralState.RECONCILED: ("completed", None),
    ReferralState.CANCELLED: ("cancelled", None),
    ReferralState.AGED_OUT: ("failed", "aged-out"),
}


@pytest.mark.parametrize("state,expected", sorted(_SPEC_TABLE_7.items(), key=lambda kv: kv[0].value))
def test_the_projection_matches_the_spec_table(state, expected):
    assert project(state, hold=None) == expected


def test_the_spec_table_covers_every_state():
    """Otherwise the parametrisation above shrinks silently when a state is added, and the
    only thing left checking the new state would be the totality test -- which accepts any
    R4 code at all."""
    assert set(_SPEC_TABLE_7) == set(ReferralState)


def test_aged_out_projects_to_a_terminal_status_and_that_is_the_revisitable_call():
    """Spec 7 flags this one as a judgment call. `failed` is terminal and honest for
    reporting; the alternative leaves aged-out referrals in-progress forever. Pinned here
    so that revisiting it is a deliberate edit rather than a drift."""
    assert project(ReferralState.AGED_OUT, hold=None) == ("failed", "aged-out")


# --- the value set is the published one, not ours --------------------------------------


def test_r4_task_status_is_the_published_r4_value_set():
    """Twelve codes, from http://hl7.org/fhir/task-status. Pinned as a literal because the
    totality test asserts membership in it: a code quietly added here would make
    `project()` free to emit something no FHIR server will accept, and the totality test
    would still pass."""
    assert R4_TASK_STATUS == frozenset(
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


def test_the_membership_check_in_the_totality_test_can_fail():
    """`status in R4_TASK_STATUS` proves nothing if R4_TASK_STATUS admits anything."""
    assert "scheduled" not in R4_TASK_STATUS, "an internal state name is not an R4 code"
    assert "" not in R4_TASK_STATUS


def test_two_r4_codes_are_unreachable_from_this_model_and_which_ones():
    """`ready` has no referral meaning here -- nothing in the lifecycle is "the work can
    start now" separately from ACCEPTED -- and `entered-in-error` is a retraction, which is
    a Provenance concern (Plan 2b) rather than a state. Recorded so that the gap is a
    decision on the record instead of an omission nobody noticed."""
    reachable = {project(s, hold=h)[0] for s in ReferralState for h in (None, _HOLD)}
    assert R4_TASK_STATUS - reachable == {"ready", "entered-in-error"}


# --- the CodeSystem, and whether the check against it is worth anything ------------------


def test_the_codesystem_declares_exactly_what_the_projection_can_emit():
    """test_every_business_status_code_is_declared_in_our_codesystem passes trivially if
    project() never emits a business status, and it would pass unfalsifiably if the concept
    list were generated from ReferralState. This pins both ends: every emitted code is
    declared *and* every declared code is emitted, so a dead concept in a published
    CodeSystem is as much a failure as an undeclared emission."""
    emitted = {
        b
        for s in ReferralState
        for h in (None, _HOLD)
        if (b := project(s, hold=h)[1]) is not None
    }
    assert emitted, "nothing is emitted, so the declaration check above is vacuous"
    declared = {c["code"] for c in BUSINESS_STATUS["concept"]}
    assert declared == emitted


def test_the_declaration_check_would_reject_an_undeclared_code():
    """The other half of the same worry: `declared` has to be a set that can say no."""
    declared = {c["code"] for c in BUSINESS_STATUS["concept"]}
    assert "escalated" not in declared
    assert "in-progress" not in declared, "R4 status codes are a different vocabulary"


def test_a_business_status_is_emitted_for_every_state_under_hold_and_they_are_all_distinct():
    """The load-bearing property of the whole dual layer: on-hold discards the state in
    Task.status, so if the eleven holds did not produce eleven distinct business statuses
    the aging thresholds could not tell a hold from ACCEPTED from a hold from SCHEDULED."""
    held = [project(s, hold=_HOLD)[1] for s in ReferralState]
    assert len(set(held)) == len(list(ReferralState)) == 11


def test_the_codesystem_is_a_resource_with_a_stable_canonical_and_a_version():
    """The canonical url is the identity every stored Coding refers back to. Renaming it
    after publication silently invalidates them all, so it is pinned as a literal: changing
    it should require editing a test that says why."""
    assert BUSINESS_STATUS["resourceType"] == "CodeSystem"
    assert BUSINESS_STATUS["url"] == "https://referral-loop.health/fhir/CodeSystem/referral-business-status"
    assert BUSINESS_STATUS["version"] == "1.0.0"
    assert BUSINESS_STATUS["content"] == "complete"


def test_every_concept_carries_a_definition():
    """A published code with no definition is a code the receiving site has to guess at,
    and guessing is what businessStatus exists to remove."""
    for concept in BUSINESS_STATUS["concept"]:
        assert concept["display"].strip()
        assert concept["definition"].strip()
