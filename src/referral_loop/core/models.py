"""The two aggregates: a referral, and an inbound artifact that matched no referral.

Field lists are design spec sections 5 and 5.1; they are authoritative and not restated
here. Both are frozen, and no method on either reads a clock, a database or a network --
see the class docstrings for why that is load-bearing rather than tidy.

Deliberately not FHIR-shaped (INTEROP_SPEC A1). The projection to Task/ServiceRequest and
DocumentReference lives in fhir/, one layer out, so that the model can carry distinctions
FHIR's status vocabulary cannot express.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import NewType

from .states import ArtifactState, Hold, ReferralState

# Two identifier spaces, not one with two names. An attach names a referral *and* an
# artifact, and if the ids were interchangeable an attach that swapped its two arguments
# would still typecheck -- which is precisely the class of error the split of the two
# aggregates (spec 6.5) exists to make impossible rather than merely unlikely.
ReferralId = NewType("ReferralId", str)
ArtifactId = NewType("ArtifactId", str)

# Spec section 5 names this type but does not enumerate it, and it must not become an
# enum here: the specialty vocabulary is whatever the site's HL7 feed emits, so a closed
# set in the domain layer would need a code change to onboard a site. Validation against a
# site's configured list is the ingest layer's job, where the configuration is reachable.
Specialty = NewType("Specialty", str)


class ArtifactKind(str, Enum):
    """What an unmatched inbound document is, which decides how it can be dispositioned.

    Three because that is what the ingest surface can produce: ORU results, MDM/consult
    documents, and SIU schedule notices. Not a free string -- retention and the
    DocumentReference projection both key on it, and a typo'd kind would silently create
    a fourth retention class nobody had written a policy for.
    """

    RESULT = "result"
    DOCUMENT = "document"
    SCHEDULE_NOTICE = "schedule-notice"


@dataclass(frozen=True)
class PartyRef:
    """An organisation or a person, as this site knows it.

    `id` is the local key. `name` is display text and is never matched on: for an inbound
    artifact this comes off the wire from an authenticated peer, and a facility name is
    self-asserted (spec 11.3) even when the connection is not.
    """

    id: str
    name: str


@dataclass(frozen=True)
class PatientRef:
    """Local identity plus whatever aliases have already been resolved.

    `aliases` is what has been established elsewhere, not a lookup: resolving an alias
    needs the store, and a model that needed a database connection to be constructed
    could not be built inside a CDS Hooks request or a replay. A tuple rather than a
    list so the frozen dataclass is actually immutable through its fields.
    """

    mrn: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Referral:
    """A referral and the state it is currently in. Spec section 5.

    Frozen, and every mutator returns a new instance. Plan 2b replays the event log
    through this type, so an in-place mutation would make a replayed prefix of the log
    disagree with the same prefix replayed twice.

    `state_occurred_at` is when the current state's *event* happened, not when this object
    was built. Nothing here calls datetime.now(): a value the code reads from the clock at
    construction time is unreplayable, and it belongs on the Transition that carries it
    (Plan 2b, spec 8.1).
    """

    id: ReferralId
    patient: PatientRef
    sending_org: PartyRef
    receiving_org: PartyRef | None
    referring_provider: PartyRef | None
    specialty: Specialty
    reason: str | None
    service_request_id: str | None
    state: ReferralState
    hold: Hold | None
    state_occurred_at: datetime
    seq: int

    def with_hold(self, hold: Hold) -> Referral:
        """Suspend, keeping the state. Spec 6.2.

        The state is untouched on purpose: "accepted but never scheduled" and "scheduled
        but never seen" escalate on different aging thresholds, and a held referral that
        had forgotten which of those it was would land in the wrong one when released.
        """
        return replace(self, hold=hold)

    def released(self) -> Referral:
        """Lift the hold. The exact inverse of with_hold(), because the state never moved."""
        return replace(self, hold=None)


@dataclass(frozen=True)
class InboundArtifact:
    """An inbound document, and whether anyone has decided what it belongs to. Spec 5.1.

    A referral never enters ArtifactState and an artifact never enters the eleven; that
    separation is the whole content of spec 6.5, and it is what removes the
    `attached_from` carve-out from the ordering guard and ATTACHED from the matcher's
    exact-tier set.

    `received_from` is the authenticated peer, never the facility the message asserts for
    itself -- the two disagree exactly when it matters (spec 11.3).
    """

    id: ArtifactId
    received_from: PartyRef
    patient: PatientRef | None
    content_hash: str
    kind: ArtifactKind
    state: ArtifactState
    received_at: datetime
    # The clinical time the artifact claims for itself, which is not when it arrived and is
    # frequently absent. Kept as a separate nullable field rather than defaulted to
    # received_at, because defaulting would fabricate a clinical timestamp that no message
    # ever asserted and nothing downstream could tell apart from a real one.
    observed_at: datetime | None
