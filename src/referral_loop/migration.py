"""The legacy nine-state vocabulary, mapped onto the canonical model.

**Temporary. Plan 2b deletes this module** when `Loop` and `LoopState` go away and the
canonical model is the only vocabulary in the repo. A temporary module carrying no expiry
note becomes permanent, and a second permanent state vocabulary is the thing this whole
plan exists to avoid -- so the note is here, at the top, rather than in a plan document
nobody reads while editing code.

Until then it is the bridge two callers need. Plan 2b's replay walks the event log through
`canonical_state`; an interoperability branch forking at `canonical-model-v1` uses
`to_referral` and `to_artifact` to get a canonical view of live data without waiting for
2b to land. Nothing in the running machine imports it, and `core/` must not: the import
closure test names this module as forbidden from the domain layer.

**What the map says.** Five legacy states carry over into `ReferralState`:

    OPEN -> SENT, SCHEDULED -> SCHEDULED, RESULTED -> DOCUMENTED,
    ACKNOWLEDGED -> RECONCILED, CANCELLED -> CANCELLED

Three leave the referral vocabulary entirely and land in `ArtifactState` -- `ORPHAN`,
`DISMISSED`, `ATTACHED`. That is design spec section 6.5's correction, expressed as code:
those three were never states a *referral* could be in, they are the life of an inbound
document that matched no referral. `CLOSED` raises; see `CLOSED_IS_UNREACHABLE`.

**What the map does not say, which is the point.** Six canonical referral states have no
legacy source at all -- `DRAFT`, `RECEIVED`, `ACCEPTED`, `DECLINED`, `SEEN`, `AGED_OUT`
(`WITHOUT_LEGACY_SOURCE` below). Not because the mapping is incomplete, but because the
existing machine cannot represent them: it has no notion of a referral before it is sent,
no receiving party to receive or accept or decline it, no record of the patient being
seen as distinct from a document arriving, and no terminal state for a referral that
simply went quiet. Those six are the concrete measure of what the richer model buys, and
they are the reason this is a migration rather than a rename.

The map is injective within each vocabulary: no two legacy states collapse onto one
canonical state. Plan 2b replays history through it, and a collapse there is history
that cannot be recovered afterwards.
"""

from __future__ import annotations

from datetime import datetime

from .core.models import (
    ArtifactId,
    ArtifactKind,
    InboundArtifact,
    PartyRef,
    PatientRef,
    Referral,
    ReferralId,
    Specialty,
)
from .core.states import ArtifactState, ReferralState
from .events import Loop, LoopState

# Written without regex metacharacters on purpose: the test matches on it with
# pytest.raises(match=...), which is a re.search, and a message that has to be escaped at
# every call site is a message that will eventually be matched loosely instead.
CLOSED_IS_UNREACHABLE = (
    "LoopState.CLOSED has no canonical equivalent. It is reserved for v2 and "
    "store.append_event refuses every event that would reach it, so no loop has ever been "
    "in it; a mapping would invent a meaning for a state that has never existed. Design "
    "spec section 6.3 replaces what it was reserved for with a rule on the transition into "
    "RECONCILED, which survives someone making this member reachable."
)

_TO_REFERRAL_STATE: dict[LoopState, ReferralState] = {
    LoopState.OPEN: ReferralState.SENT,
    LoopState.SCHEDULED: ReferralState.SCHEDULED,
    # RESULTED means a document arrived, not that the patient was seen. DOCUMENTED, not
    # SEEN: the legacy machine observes documents and infers nothing about the encounter,
    # and SEEN is precisely one of the distinctions it cannot make.
    LoopState.RESULTED: ReferralState.DOCUMENTED,
    # ACKNOWLEDGED is a coordinator confirming that this result belongs to this order --
    # identifier work. RECONCILED is the canonical name for exactly that claim, and the
    # events.py comment on ACKNOWLEDGED is careful to say it is not the stronger clinical
    # one. The two weak claims line up; mapping it to a state further down the path would
    # be the collapse that comment exists to prevent.
    LoopState.ACKNOWLEDGED: ReferralState.RECONCILED,
    LoopState.CANCELLED: ReferralState.CANCELLED,
}

# Design spec section 6.5. These three leave the referral vocabulary rather than being
# renamed within it, which is the whole content of that section.
_TO_ARTIFACT_STATE: dict[LoopState, ArtifactState] = {
    LoopState.ORPHAN: ArtifactState.UNMATCHED,
    LoopState.DISMISSED: ArtifactState.DISMISSED,
    LoopState.ATTACHED: ArtifactState.ATTACHED,
}

# The six with no legacy source, named as data so the claim in the docstring is checkable
# rather than merely written down. tests/test_migration.py derives the same set from
# _TO_REFERRAL_STATE and compares, so adding a state or wiring a legacy source to one of
# these fails a test instead of leaving the prose above quietly wrong.
WITHOUT_LEGACY_SOURCE: frozenset[ReferralState] = frozenset(
    {
        ReferralState.DRAFT,
        ReferralState.RECEIVED,
        ReferralState.ACCEPTED,
        ReferralState.DECLINED,
        ReferralState.SEEN,
        ReferralState.AGED_OUT,
    }
)

# The legacy machine tracks orders placed inside one facility, so a loop row names no
# sending organisation and no receiving one. Referral.sending_org is not optional -- a
# referral without a sender is not a referral -- so the absence is named here instead of
# being filled with a plausible-looking facility id. The empty id is deliberate: PartyRef.id
# is the local key and the thing anything downstream would match on, and a synthesised key
# like "legacy" would be matchable, would look real, and would eventually be joined against.
UNRECORDED_PARTY = PartyRef(id="", name="")


def canonical_state(legacy: LoopState) -> ReferralState | ArtifactState:
    """Map one legacy state onto the canonical vocabulary it actually belongs to.

    Total over the eight reachable members. `CLOSED` raises `ValueError`; it is the only
    member with no image, and the reason is in `CLOSED_IS_UNREACHABLE`.

    The return type is a union on purpose. A caller has to decide which aggregate it is
    holding before it can do anything with the answer, and that decision is the section 6.5
    split arriving at every call site rather than being made once and forgotten.
    """
    if legacy is LoopState.CLOSED:
        raise ValueError(CLOSED_IS_UNREACHABLE)

    referral = _TO_REFERRAL_STATE.get(legacy)
    if referral is not None:
        return referral

    artifact = _TO_ARTIFACT_STATE.get(legacy)
    if artifact is not None:
        return artifact

    # Unreachable while LoopState has nine members, and the parametrised test is what
    # proves it. Named here so that a tenth member added without a mapping fails at the
    # migration with its own name in the message, rather than as a KeyError or -- worse --
    # a None that flows on and becomes a referral in no state at all.
    raise ValueError(f"legacy state {legacy!r} has no canonical mapping; migration.py is incomplete")


def to_referral(loop: Loop, *, state_occurred_at: datetime, seq: int) -> Referral:
    """Build the canonical `Referral` a legacy loop row stands for.

    `state_occurred_at` and `seq` are arguments rather than fields read off the row
    because the row does not carry them: a `Loop` records `ordered_at` and `ack_at` and
    nothing about when it entered `SCHEDULED` or `RESULTED`. Both values live in
    `loop_events`, which is where Plan 2b's replay reads them from anyway. Defaulting
    `state_occurred_at` to `ordered_at` would assert that a schedule happened at order
    time, and defaulting it to the clock would be a timestamp no message ever asserted --
    the same fabrication `InboundArtifact.observed_at` is nullable to avoid.

    Raises `ValueError` for a loop whose state maps into the artifact vocabulary. Silently
    producing a `Referral` in `ORPHAN` would reintroduce the conflation section 6.5
    removes, one layer further down where it is harder to see.
    """
    state = canonical_state(loop.state)
    if not isinstance(state, ReferralState):
        raise ValueError(
            f"loop {loop.loop_id} is in {loop.state.value}, which is an artifact state, not a "
            "referral state. Use to_artifact. Design spec section 6.5: an inbound document "
            "that matched no referral is not a referral in a funny state."
        )

    return Referral(
        id=ReferralId(loop.loop_id),
        patient=PatientRef(mrn=loop.mrn),
        sending_org=UNRECORDED_PARTY,
        receiving_org=None,
        # Display text only, so it goes in `name` and `id` stays empty. The row stores a
        # provider string and no key for it; synthesising one from the text would make two
        # spellings of one clinician into two parties that something later joins on.
        referring_provider=PartyRef(id="", name=loop.ordering_provider) if loop.ordering_provider else None,
        # The row has a service_code and a modality, and neither is a specialty. Writing
        # "CT" here would put a modality code in the field the FHIR projection and the
        # site's routing both read as a specialty, and nothing downstream could tell it
        # from a real one. Empty is the honest answer; Plan 2b's ingest work is where a
        # specialty starts being captured.
        specialty=Specialty(""),
        reason=None,
        service_request_id=loop.placer_order_number or loop.filler_order_number or None,
        state=state,
        # No hold: the legacy machine has none. Staleness is not one -- it is derived at
        # read time from the underlying state (staleness.py), nobody applied it, and a Hold
        # requires an actor and a reason that would both have to be made up.
        hold=None,
        state_occurred_at=state_occurred_at,
        seq=seq,
    )


def to_artifact(loop: Loop, *, received_at: datetime, content_hash: str) -> InboundArtifact:
    """Build the canonical `InboundArtifact` a legacy orphan row stands for.

    `received_at` and `content_hash` are arguments for the same reason `to_referral` takes
    its two: the loop row holds neither. The digest is computed and archived by the
    listener against the raw payload, and the arrival time is on the `orphaned` event.

    Raises `ValueError` for a loop whose state maps into the referral vocabulary.
    """
    state = canonical_state(loop.state)
    if not isinstance(state, ArtifactState):
        raise ValueError(
            f"loop {loop.loop_id} is in {loop.state.value}, which is a referral state, not an "
            "artifact state. Use to_referral."
        )

    return InboundArtifact(
        id=ArtifactId(loop.loop_id),
        # Not the sending facility: the loop row does not record the authenticated peer,
        # and the facility a message asserts for itself is exactly the value spec 11.3 says
        # not to trust. Naming the absence beats copying the untrustworthy one.
        received_from=UNRECORDED_PARTY,
        # An orphan row can carry an empty mrn -- the matcher declines rather than attaches
        # when a result names no patient it can resolve. A PatientRef with an empty mrn
        # would be an identifier resolving to nobody, which is the case InboundArtifact
        # made `patient` optional for.
        patient=PatientRef(mrn=loop.mrn) if loop.mrn else None,
        content_hash=content_hash,
        # Every legacy orphan is a result. registry.orphan is reached from the result path
        # in the listener and from undoing a result match in the registry, and from nowhere
        # else -- so this is a fact about the machine being migrated, not a default that
        # happens to be common. Documents and schedule notices become orphans only once
        # Plan 2b's MDM and SIU ingest work lands, by which time this module is gone.
        kind=ArtifactKind.RESULT,
        state=state,
        received_at=received_at,
        # The clinical time the artifact claims for itself. The legacy row keeps the
        # observation time in the event detail rather than on the loop, and defaulting it
        # to received_at would fabricate a clinical timestamp nothing downstream could tell
        # from an asserted one.
        observed_at=None,
    )
