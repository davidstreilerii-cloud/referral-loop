"""The bridge from the legacy nine-state vocabulary to the canonical model.

These tests are the proof that the two vocabularies say the same thing where they
overlap, and the record of exactly where they do not. Plan 2b replays the event log
through this map, so a hole here becomes lost history there.
"""

from datetime import datetime, timezone

import pytest

from referral_loop import migration
from referral_loop.core.models import ArtifactKind, InboundArtifact, Referral
from referral_loop.core.states import ArtifactState, ReferralState
from referral_loop.events import Loop, LoopState
from referral_loop.migration import (
    CLOSED_IS_UNREACHABLE,
    WITHOUT_LEGACY_SOURCE,
    canonical_state,
    to_artifact,
    to_referral,
)

_AT = datetime(2026, 8, 2, 9, 30, tzinfo=timezone.utc)


def _loop(state: LoopState, **kw: object) -> Loop:
    fields: dict = {
        "loop_id": "L-0001",
        "mrn": "MRN1",
        "state": state,
        "placer_order_number": "PLC-9",
        "filler_order_number": "FIL-9",
        "service_code": "71260",
        "modality": "CT",
        "ordering_provider": "Dr Alvarez",
    }
    fields.update(kw)
    return Loop(**fields)


@pytest.mark.parametrize("legacy", [s for s in LoopState if s is not LoopState.CLOSED])
def test_every_legacy_state_maps_to_exactly_one_canonical_state(legacy):
    mapped = canonical_state(legacy)
    assert isinstance(mapped, (ReferralState, ArtifactState))


def test_the_three_orphan_states_map_to_the_artifact_vocabulary_not_the_referral_one():
    """This is the split, expressed as a test. If any of these three lands in
    ReferralState the conflation has come back."""
    assert canonical_state(LoopState.ORPHAN) is ArtifactState.UNMATCHED
    assert canonical_state(LoopState.DISMISSED) is ArtifactState.DISMISSED
    assert canonical_state(LoopState.ATTACHED) is ArtifactState.ATTACHED


def test_the_five_referral_states_map_to_the_referral_vocabulary():
    assert canonical_state(LoopState.OPEN) is ReferralState.SENT
    assert canonical_state(LoopState.SCHEDULED) is ReferralState.SCHEDULED
    assert canonical_state(LoopState.RESULTED) is ReferralState.DOCUMENTED
    assert canonical_state(LoopState.ACKNOWLEDGED) is ReferralState.RECONCILED
    assert canonical_state(LoopState.CANCELLED) is ReferralState.CANCELLED


def test_closed_has_no_mapping_and_says_why():
    """CLOSED is reserved and refused at store.append_event; nothing can produce a loop
    in it. Mapping it would invent a meaning for a state that has never existed."""
    with pytest.raises(ValueError, match=CLOSED_IS_UNREACHABLE):
        canonical_state(LoopState.CLOSED)


def test_the_mapping_is_injective_within_each_vocabulary():
    """Two legacy states collapsing onto one canonical state would make the migration
    lossy, and Plan 2b replays the event log through this map."""
    reachable = [s for s in LoopState if s is not LoopState.CLOSED]
    for vocabulary in (ReferralState, ArtifactState):
        targets = [
            canonical_state(s) for s in reachable if isinstance(canonical_state(s), vocabulary)
        ]
        assert targets, f"no legacy state maps into {vocabulary.__name__}"
        assert len(targets) == len(set(targets)), f"{vocabulary.__name__} mapping is lossy"


def test_the_canonical_states_with_no_legacy_source_are_exactly_the_six_named():
    """The six are the concrete measure of what the richer model buys, and the module
    names them in prose. Deriving the same set from the mapping and comparing keeps the
    prose from drifting: adding a twelfth state, or wiring a legacy source to one of the
    six, fails here rather than leaving a docstring quietly wrong."""
    reached = {
        canonical_state(s)
        for s in LoopState
        if s is not LoopState.CLOSED and isinstance(canonical_state(s), ReferralState)
    }
    assert WITHOUT_LEGACY_SOURCE == set(ReferralState) - reached
    assert WITHOUT_LEGACY_SOURCE == {
        ReferralState.DRAFT,
        ReferralState.RECEIVED,
        ReferralState.ACCEPTED,
        ReferralState.DECLINED,
        ReferralState.SEEN,
        ReferralState.AGED_OUT,
    }


def test_the_module_records_when_it_is_deleted():
    """A temporary module with no expiry note becomes permanent. The note is the only
    thing standing between this bridge and a second permanent state vocabulary."""
    doc = migration.__doc__ or ""
    assert "Plan 2b" in doc
    assert "delete" in doc.lower()


def test_a_referral_loop_becomes_a_referral_carrying_what_the_row_actually_held():
    r = to_referral(_loop(LoopState.SCHEDULED), state_occurred_at=_AT, seq=4)
    assert isinstance(r, Referral)
    assert r.id == "L-0001"
    assert r.patient.mrn == "MRN1"
    assert r.state is ReferralState.SCHEDULED
    assert r.state_occurred_at == _AT
    assert r.seq == 4
    assert r.service_request_id == "PLC-9", "the placer number is the referral's order linkage"
    assert r.referring_provider is not None
    assert r.referring_provider.name == "Dr Alvarez"


def test_a_referral_from_a_legacy_loop_invents_no_party_and_no_specialty():
    """The nine-state machine tracks orders placed inside one facility: there is no
    second party on the row and nothing that is a specialty. Filling those in with the
    modality, or with a plausible-looking facility id, would put values downstream
    consumers cannot tell from real ones into two fields they key on."""
    r = to_referral(_loop(LoopState.OPEN), state_occurred_at=_AT, seq=1)
    assert r.sending_org is migration.UNRECORDED_PARTY
    assert r.receiving_org is None
    assert r.specialty == ""
    assert "CT" not in (r.specialty, r.reason)
    assert r.reason is None
    assert r.hold is None, "the legacy machine has no hold; staleness is derived, not applied"


def test_a_provider_that_the_row_never_named_stays_absent():
    r = to_referral(_loop(LoopState.OPEN, ordering_provider=""), state_occurred_at=_AT, seq=1)
    assert r.referring_provider is None


def test_the_referring_provider_carries_no_local_key_because_the_row_has_none():
    """PartyRef.id is the local key and is what a match would key on. The legacy row
    stores display text only, so a key synthesised from that text would be matchable and
    wrong -- two spellings of one clinician becoming two parties."""
    r = to_referral(_loop(LoopState.OPEN), state_occurred_at=_AT, seq=1)
    assert r.referring_provider is not None
    assert r.referring_provider.id == ""


def test_an_orphan_becomes_an_artifact_not_a_referral():
    a = to_artifact(_loop(LoopState.ORPHAN, loop_id="O-0001"), received_at=_AT, content_hash="a" * 64)
    assert isinstance(a, InboundArtifact)
    assert a.id == "O-0001"
    assert a.state is ArtifactState.UNMATCHED
    assert a.received_at == _AT
    assert a.content_hash == "a" * 64
    assert a.observed_at is None


def test_every_legacy_orphan_is_a_result_because_that_is_the_only_thing_that_makes_one():
    """registry.orphan is reached from the result path and from undoing a result match,
    and from nowhere else. So RESULT is a fact about the legacy machine here, not a
    default that happens to be common."""
    a = to_artifact(_loop(LoopState.DISMISSED), received_at=_AT, content_hash="b" * 64)
    assert a.kind is ArtifactKind.RESULT


def test_an_artifact_that_named_no_patient_has_no_patient():
    """The matcher leaves a result unattributed when it names no patient it can resolve,
    and the row then carries an empty mrn. A PatientRef with an empty mrn would be a
    patient identifier that resolves to nobody, which every consumer downstream would
    have to special-case."""
    a = to_artifact(_loop(LoopState.ORPHAN, mrn=""), received_at=_AT, content_hash="c" * 64)
    assert a.patient is None


def test_to_referral_refuses_a_loop_whose_state_is_an_artifact_state():
    """The two aggregates are the point of the split. A conversion that silently
    produced a Referral in ORPHAN would put the conflation back one layer down."""
    with pytest.raises(ValueError, match="artifact"):
        to_referral(_loop(LoopState.ORPHAN), state_occurred_at=_AT, seq=1)


def test_to_artifact_refuses_a_loop_whose_state_is_a_referral_state():
    with pytest.raises(ValueError, match="referral"):
        to_artifact(_loop(LoopState.SCHEDULED), received_at=_AT, content_hash="d" * 64)


@pytest.mark.parametrize("convert", [to_referral, to_artifact])
def test_neither_conversion_can_be_asked_for_a_closed_loop(convert):
    with pytest.raises(ValueError, match=CLOSED_IS_UNREACHABLE):
        if convert is to_referral:
            convert(_loop(LoopState.CLOSED), state_occurred_at=_AT, seq=1)
        else:
            convert(_loop(LoopState.CLOSED), received_at=_AT, content_hash="e" * 64)
