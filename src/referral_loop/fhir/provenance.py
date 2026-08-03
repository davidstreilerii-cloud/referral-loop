"""TransitionEvent -> FHIR Provenance. Design spec 8.2. A pure, total function.

**The AI is never `verifier`.** A transition this system inferred projects to exactly one
agent -- a Device, typed `author` -- and no human verifier agent. That absence is the
machine-readable signal that the transition was inferred, and it is what satisfies the
audit's H2 requirement through conformant modelling rather than an invented extension.

The distinction is worth stating because the alternative was easier: an extension saying
`inferred: true` would have been one line and would have been readable by nobody. A
consumer that has never heard of this system can ask "is there an agent whose type is
verifier" and get a correct answer, because that is a question about Provenance and not a
question about us.

Nothing here reads a clock or a store. Both timestamps come off the transition, for the
same reason `machine.apply()` takes them rather than reading them: a projection that
stamped `recorded` with the time it happened to run would produce a different resource
every time it replayed the same event.
"""

from __future__ import annotations

from typing import Any

from ..core.models import Referral
from ..core.states import ReferralState
from ..core.transitions import AssertionSource, Evidence, Transition
from .codesystems import ACTIVITY_URL

# Spec 8.2's activity vocabulary, keyed by the state the transition moves into. Total over
# ReferralState by construction, and tests/test_provenance.py asserts that rather than
# trusting it -- an unmapped state would raise inside a projection whose whole contract is
# that it is total.
_ACTIVITY_FOR_STATE: dict[ReferralState, str] = {
    ReferralState.DRAFT: "draft",
    ReferralState.SENT: "submit",
    ReferralState.RECEIVED: "receive",
    ReferralState.ACCEPTED: "accept",
    ReferralState.DECLINED: "decline",
    ReferralState.SCHEDULED: "schedule",
    ReferralState.SEEN: "see",
    ReferralState.DOCUMENTED: "document",
    ReferralState.RECONCILED: "reconcile",
    ReferralState.CANCELLED: "cancel",
    ReferralState.AGED_OUT: "age-out",
}

# The FHIR resource type each actor kind references. Total over ActorRef's closed set of
# three, which is why ActorRef validates `kind` at construction: a projection cannot be
# handed an actor it has no mapping for, so this dict needs no fallback and has none.
_RESOURCE_FOR_ACTOR_KIND: dict[str, str] = {
    "practitioner": "Practitioner",
    "organization": "Organization",
    "device": "Device",
}

# http://terminology.hl7.org/CodeSystem/provenance-participant-type. Standard codes, not
# ours: the whole point is that a consumer reads them without knowing us.
_PARTICIPANT_TYPE_URL = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"

# What the second agent's type is, per assertion source. SYSTEM_INFERRED is absent
# deliberately and that absence is the guarantee -- see the module docstring. HUMAN is
# `verifier` rather than `author` because the human is attesting to something the system
# already recorded; the Device below is the author of the record either way.
_SECOND_AGENT_TYPE: dict[AssertionSource, str] = {
    AssertionSource.HUMAN: "verifier",
    AssertionSource.RECEIVING_ORG: "informant",
}


def _coding(system: str, code: str) -> dict[str, Any]:
    return {"coding": [{"system": system, "code": code}]}


def _entity(evidence: Evidence) -> dict[str, Any]:
    """One Provenance.entity per Evidence, role `source`.

    `what.identifier.value` carries the reference -- a content hash, a resource reference
    or a rule id -- and never the content behind it. Evidence.ref is that by construction,
    which is what makes this safe rather than merely careful.
    """
    entity: dict[str, Any] = {
        "role": "source",
        "what": {
            "identifier": {"system": ACTIVITY_URL + "/evidence", "value": evidence.ref},
            "display": evidence.kind.value,
        },
    }
    return entity


def to_provenance(transition: Transition, referral: Referral) -> dict[str, Any]:
    """Project one accepted transition. Pure and total.

    `target` names the Task and, where the linkage is known, the ServiceRequest behind it.
    The patient is deliberately absent: Provenance.target is what the activity acted on,
    and a referral's identifier is enough to resolve the patient for anyone entitled to.
    """
    agents: list[dict[str, Any]] = [
        {
            # Always present, always first. This system recorded the transition whoever
            # asserted it, so the Device is the author of the record in every case.
            "type": _coding(_PARTICIPANT_TYPE_URL, "author"),
            "who": {"reference": "Device/referral-loop"},
        }
    ]
    second = _SECOND_AGENT_TYPE.get(transition.assertion_source)
    if second is not None:
        agents.append({
            "type": _coding(_PARTICIPANT_TYPE_URL, second),
            "who": {
                "reference": (
                    f"{_RESOURCE_FOR_ACTOR_KIND[transition.actor.kind]}/{transition.actor.id}"
                )
            },
        })

    targets: list[dict[str, Any]] = [{"reference": f"Task/{referral.id}"}]
    if referral.service_request_id:
        targets.append({"reference": f"ServiceRequest/{referral.service_request_id}"})

    return {
        "resourceType": "Provenance",
        "target": targets,
        "occurredDateTime": transition.occurred_at.isoformat(),
        "recorded": transition.recorded_at.isoformat(),
        "activity": _coding(ACTIVITY_URL, _ACTIVITY_FOR_STATE[transition.to_state]),
        "agent": agents,
        "entity": [_entity(item) for item in transition.evidence],
    }
