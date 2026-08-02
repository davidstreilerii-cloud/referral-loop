"""The two state vocabularies, and the properties that make them two rather than one."""

import pytest

from referral_loop.core.states import ArtifactState, Hold, ReferralState


def test_the_referral_lifecycle_has_eleven_states():
    assert len(ReferralState) == 11


def test_the_artifact_lifecycle_has_three():
    assert len(ArtifactState) == 3


def test_no_state_name_appears_in_both_vocabularies():
    """The split exists because an inbound artifact that matched nothing is not a referral
    in a funny state. A name in both would let a future reader treat them as one enum
    again, which is exactly the conflation this removes."""
    assert not ({s.name for s in ReferralState} & {s.name for s in ArtifactState})


@pytest.mark.parametrize("state", list(ReferralState))
def test_every_referral_state_is_its_own_value(state):
    assert state.value == state.name.lower().replace("_", "-")


def test_hold_is_not_a_state():
    """A referral held from ACCEPTED and one held from SCHEDULED are operationally
    different, and the aging thresholds escalate on that difference. Collapsing both into
    an ON_HOLD member would destroy it."""
    assert not hasattr(ReferralState, "ON_HOLD")
    assert not hasattr(ReferralState, "HOLD")


def test_a_hold_records_who_applied_it_and_why():
    h = Hold(reason="awaiting patient callback", actor="coordinator-b")
    assert h.reason and h.actor
    with pytest.raises(Exception):
        h.reason = "changed"  # frozen
