"""The two state vocabularies, and the properties that make them two rather than one."""

import pytest

from referral_loop.core.states import (
    ArtifactState,
    DocumentationStatus,
    Hold,
    ReferralState,
)


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


def test_documentation_is_a_condition_and_not_a_state():
    """The gap the nine-state design papered over with OBX-11.

    A referral documented by a preliminary read and one documented by a final read are
    clinically different -- one is closeable and one is emphatically not -- and DOCUMENTED
    on its own cannot tell them apart. Modelling it as two states would double the
    lifecycle for one distinction that matters at exactly one edge, so it is an attribute
    carried alongside the state, for the same reason Hold is (spec 6.2).
    """
    assert {s.name for s in DocumentationStatus} == {"PRELIMINARY", "FINAL", "CORRECTED"}
    assert not hasattr(ReferralState, "PRELIMINARILY_DOCUMENTED")


def test_the_documentation_vocabulary_is_the_hl7_table_the_registry_already_acts_on():
    """HL7 table 0085 P/F/C, which registry.py names PRELIMINARY/FINAL/CORRECTED. The
    values match so Phase 2's fold over resulted events maps without a translation table
    that could disagree with itself."""
    assert [s.value for s in DocumentationStatus] == ["P", "F", "C"]
