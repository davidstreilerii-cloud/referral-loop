"""The Provenance projection, and the absence that carries the guarantee. Spec 8.2."""

import json
from datetime import datetime, timezone

import pytest

from referral_loop.core.models import PartyRef, PatientRef, Referral, ReferralId, Specialty
from referral_loop.core.states import ReferralState
from referral_loop.core.transitions import (
    ActorRef,
    AssertionSource,
    Evidence,
    EvidenceKind,
    Span,
    Transition,
)
from referral_loop.fhir.codesystems import ACTIVITY_CODES, ACTIVITY_URL
from referral_loop.fhir.provenance import to_provenance

_OCCURRED = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
_RECORDED = datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc)
_SENTINEL_MRN = "ZZSENTINELMRNFFF"


def _referral(**kw) -> Referral:
    base = dict(
        id=ReferralId("REF-1"), patient=PatientRef(mrn=_SENTINEL_MRN, aliases=()),
        sending_org=PartyRef(id="clinic-a", name="Clinic A"),
        receiving_org=PartyRef(id="example-lab", name="Example Lab"),
        referring_provider=None, specialty=Specialty("cardiology"), reason=None,
        service_request_id=None, state=ReferralState.DOCUMENTED, hold=None,
        state_occurred_at=_OCCURRED, seq=3,
    )
    base.update(kw)
    return Referral(**base)


def _event(*, source: AssertionSource, **kw) -> Transition:
    actor = {
        AssertionSource.HUMAN: ActorRef(kind="practitioner", id="coordinator-b"),
        AssertionSource.RECEIVING_ORG: ActorRef(kind="organization", id="example-lab"),
        AssertionSource.SYSTEM_INFERRED: ActorRef(kind="device", id="referral-loop"),
    }[source]
    base = dict(to_state=ReferralState.DOCUMENTED, actor=actor, evidence=(),
                occurred_at=_OCCURRED, recorded_at=_RECORDED, hold=None, rationale=None)
    base.update(kw)
    return Transition(assertion_source=source, **base)


def _types(provenance) -> list[str]:
    return [a.get("type", {}).get("coding", [{}])[0].get("code") for a in provenance["agent"]]


# ------------------------------------------------- the guarantee, on the output


def test_an_inferred_transition_has_no_human_verifier_agent():
    """Spec 8.2. The AI is never `verifier`, so the absence of a human verifier agent IS
    the machine-readable signal that a transition was inferred. This satisfies the
    requirement through conformant modelling rather than an invented extension -- which is
    why it must be tested as a property of the output, not of our intent.

    A consumer that has never heard of this system asks "is there an agent typed
    verifier". That question is answerable from Provenance alone, and this asserts the
    answer rather than asserting that we meant to give it.
    """
    p = to_provenance(_event(source=AssertionSource.SYSTEM_INFERRED), _referral())
    agents = p["agent"]
    assert len(agents) == 1
    assert agents[0]["who"]["reference"].startswith("Device/")
    assert not any(a.get("type", {}).get("coding", [{}])[0].get("code") == "verifier"
                   for a in agents)


@pytest.mark.parametrize("state", list(ReferralState))
def test_no_inferred_transition_into_any_state_carries_a_verifier(state):
    """The sweep. A guarantee that holds for DOCUMENTED and not for RECONCILED is not a
    guarantee, and RECONCILED is the one that matters most: machine.apply() refuses to
    reach it by inference, so a Provenance claiming a verifier there would describe a
    transition the domain layer will not produce."""
    p = to_provenance(_event(source=AssertionSource.SYSTEM_INFERRED, to_state=state),
                      _referral())
    assert len(p["agent"]) == 1
    assert "verifier" not in _types(p)


def test_a_human_transition_carries_a_practitioner_agent():
    p = to_provenance(_event(source=AssertionSource.HUMAN), _referral())
    assert len(p["agent"]) == 2
    assert _types(p) == ["author", "verifier"]
    assert p["agent"][1]["who"]["reference"] == "Practitioner/coordinator-b"


def test_a_receiving_org_transition_carries_an_informant_not_a_verifier():
    """A counterparty asserting something over an interface is an informant. Typing it
    `verifier` would say a human at this site confirmed it, which is the claim the whole
    RECONCILED guarantee exists to withhold."""
    p = to_provenance(_event(source=AssertionSource.RECEIVING_ORG), _referral())
    assert _types(p) == ["author", "informant"]
    assert "verifier" not in _types(p)
    assert p["agent"][1]["who"]["reference"] == "Organization/example-lab"


def test_the_device_agent_is_always_present_and_always_first():
    """This system recorded the transition whoever asserted it, so it authors the record
    in every case."""
    for source in AssertionSource:
        p = to_provenance(_event(source=source), _referral())
        assert p["agent"][0]["who"]["reference"] == "Device/referral-loop"
        assert _types(p)[0] == "author"


# ---------------------------------------------------------------------- totality


@pytest.mark.parametrize("state", list(ReferralState))
def test_the_activity_mapping_is_total_over_every_referral_state(state):
    """Same totality argument that makes the Task.status projection trustworthy: a
    projection with a hole raises on the state nobody tested."""
    p = to_provenance(_event(source=AssertionSource.HUMAN, to_state=state), _referral())
    code = p["activity"]["coding"][0]["code"]
    assert code in ACTIVITY_CODES
    assert p["activity"]["coding"][0]["system"] == ACTIVITY_URL


@pytest.mark.parametrize(("kind", "expected"), [("practitioner", "Practitioner"),
                                                ("organization", "Organization"),
                                                ("device", "Device")])
def test_the_actor_mapping_is_total_over_the_closed_set_of_actor_kinds(kind, expected):
    """ActorRef validates `kind` against exactly these three, which is what lets the
    projection have no fallback branch. If that set widens, this fails rather than
    emitting a reference to a resource type that does not exist."""
    p = to_provenance(
        _event(source=AssertionSource.HUMAN, actor=ActorRef(kind=kind, id="x")), _referral())
    assert p["agent"][1]["who"]["reference"] == f"{expected}/x"


# ------------------------------------------------------ targets, times, entities


def test_every_evidence_becomes_an_entity_with_role_source():
    p = to_provenance(_event(source=AssertionSource.SYSTEM_INFERRED, evidence=(
        Evidence(kind=EvidenceKind.MATCH, ref="sha256:aa", spans=(Span(start=1, end=4),),
                 confidence=0.9),
        Evidence(kind=EvidenceKind.HL7_MESSAGE, ref="MSG-1", spans=None, confidence=None),
    )), _referral())
    assert [e["role"] for e in p["entity"]] == ["source", "source"]
    assert [e["what"]["identifier"]["value"] for e in p["entity"]] == ["sha256:aa", "MSG-1"]


def test_a_transition_citing_nothing_emits_no_entities():
    assert to_provenance(_event(source=AssertionSource.HUMAN), _referral())["entity"] == []


def test_the_service_request_is_targeted_only_when_the_linkage_is_known():
    plain = to_provenance(_event(source=AssertionSource.HUMAN), _referral())
    assert [t["reference"] for t in plain["target"]] == ["Task/REF-1"]
    linked = to_provenance(_event(source=AssertionSource.HUMAN),
                           _referral(service_request_id="SR-9"))
    assert [t["reference"] for t in linked["target"]] == ["Task/REF-1", "ServiceRequest/SR-9"]


def test_occurred_and_recorded_stay_distinct():
    """Collapsing them would lose the out-of-order arrival the whole MSH-7 guard exists to
    detect, in the artifact an auditor actually reads."""
    p = to_provenance(_event(source=AssertionSource.RECEIVING_ORG), _referral())
    assert p["occurredDateTime"] == _OCCURRED.isoformat()
    assert p["recorded"] == _RECORDED.isoformat()
    assert p["occurredDateTime"] != p["recorded"]


def test_no_patient_identifier_reaches_the_projected_resource():
    """The referral id resolves the patient for anyone entitled to resolve it. Putting the
    MRN in a Provenance would put it in every downstream copy of the audit trail."""
    for source in AssertionSource:
        for state in ReferralState:
            p = to_provenance(_event(source=source, to_state=state), _referral())
            assert _SENTINEL_MRN not in json.dumps(p)
