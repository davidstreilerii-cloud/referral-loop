"""What a caller must say in order to move a referral, and who is saying it.

Design spec section 8.1 is authoritative for the field list and is not restated here.

The property this module exists to hold is a negative one: `assertion_source` has no
default. Not on the dataclass, not as a parameter default, not in a factory. The subsystem
accepts assertions from three sources that are not equivalent -- a coordinator clicking a
button, a receiving organisation's HL7 message, and this system's own matcher inferring a
link from a document that named no order -- and the difference between them is what
`core/machine.py` refuses a reconciliation on. A default would make that difference
implicit at every call site that did not think about it, which is every call site that
most needs to.

Nothing here reads a clock. `occurred_at` and `recorded_at` are supplied by the caller: a
`datetime.now()` default would make every transition's timing untestable and would quietly
paper over a missing `MSH-7` by substituting arrival time for clinical time, which is the
exact substitution the ordering guard exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .states import Hold, ReferralState


class AssertionSource(str, Enum):
    """Who says the state changed.

    Three, and they are ranked by nothing -- they are simply different claims. HUMAN is a
    person at this site taking responsibility. RECEIVING_ORG is a counterparty's assertion
    arriving over an interface: evidence, and frequently absent altogether, which is the
    reason this product exists. SYSTEM_INFERRED is this system's own conclusion from
    something that did not say so outright.
    """

    HUMAN = "human"
    RECEIVING_ORG = "receiving-org"
    SYSTEM_INFERRED = "system-inferred"


class EvidenceKind(str, Enum):
    """What a piece of cited evidence is. Spec section 8.1."""

    HL7_MESSAGE = "hl7-message"
    DOCUMENT = "document"
    FHIR_RESOURCE = "fhir-resource"
    USER_ACTION = "user-action"
    RULE = "rule"
    MATCH = "match"


# The two kinds that can be the product of an inference rather than an assertion, and so
# the only two a confidence means anything on. A score on a USER_ACTION or an HL7_MESSAGE
# would be a number attached to something that either happened or did not -- and the way a
# match score becomes an assertion is by being parked somewhere it is no longer labelled
# as a score.
_SCORED_KINDS = frozenset({EvidenceKind.MATCH, EvidenceKind.FHIR_RESOURCE})

# The three FHIR resource types spec 8.2 references from a Provenance agent, and nothing
# else. Closed rather than free text because the projection builds `{kind}/{id}` from it:
# a misspelling would produce a syntactically valid reference to a resource type that does
# not exist, which no downstream consumer would report as an error.
_ACTOR_KINDS = frozenset({"practitioner", "organization", "device"})


@dataclass(frozen=True)
class Span:
    """A half-open character range into the source document an inference read.

    Offsets, not text: a span carrying the quoted characters would put clinical narrative
    into the transition object, and from there into the event log and the Provenance
    projection.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"a span offset cannot be negative: start={self.start}")
        if self.end <= self.start:
            raise ValueError(
                f"a span must cover at least one character: start={self.start} end={self.end}"
            )


@dataclass(frozen=True)
class ActorRef:
    """Whoever or whatever asserted the transition, as spec 8.2 will reference it.

    `kind` is one of three because Provenance.agent.who references a Practitioner, an
    Organization or a Device and there is no fourth thing this system attributes a
    transition to. `id` is a local key -- a coordinator's username, an organisation's
    directory id, this system's own name -- and never a patient identifier.
    """

    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in _ACTOR_KINDS:
            raise ValueError(
                f"an actor kind must be one of {sorted(_ACTOR_KINDS)}, not {self.kind!r}"
            )
        if not self.id:
            raise ValueError("an actor must be named; an empty id references nobody")


@dataclass(frozen=True)
class Evidence:
    """One thing cited in support of a transition. Spec section 8.1.

    `ref` is a content hash, a resource reference or a rule id -- an identifier for
    something archived elsewhere, never the content itself.
    """

    kind: EvidenceKind
    ref: str
    spans: tuple[Span, ...] | None
    confidence: float | None

    def __post_init__(self) -> None:
        if not self.ref:
            raise ValueError(
                f"{self.kind.value} evidence must name what it is evidence of; ref is empty"
            )
        if self.spans is not None and not isinstance(self.spans, tuple):
            raise TypeError(
                f"spans must be a tuple, not {type(self.spans).__name__}: frozen stops the "
                "field being rebound, not the list behind it being appended to after the "
                "Provenance projection has already read it"
            )
        if self.confidence is None:
            return
        if self.kind not in _SCORED_KINDS:
            raise ValueError(
                f"a confidence is meaningless on {self.kind.value} evidence -- it either "
                "happened or it did not -- and permitting one is how a match score gets "
                "laundered into an assertion"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"a confidence outside 0.0..1.0 is not a confidence: {self.confidence!r}"
            )


@dataclass(frozen=True)
class HoldChange:
    """Apply a hold, or lift the one in place.

    Three cases have to be expressible and `Hold | None` on the Transition carries only
    two. Spec 8.2's activity vocabulary lists `hold` and `release` as separate activities,
    so both operations exist; the third and overwhelmingly common case is a transition that
    does not touch the hold at all, which is `Transition.hold is None` and must not be
    spelled the same way as lifting one -- a scheduling message would otherwise silently
    release a hold a coordinator placed.
    """

    hold: Hold | None


@dataclass(frozen=True)
class Transition:
    """The only thing that moves a referral's state. Spec section 8.1.

    `assertion_source` is required and has no default anywhere; see the module docstring.

    `occurred_at` is when the asserted event happened and `recorded_at` is when this store
    learned of it. They are separate because out-of-order arrival is detected on the first
    while the audit trail has to read correctly on the second, and collapsing them loses
    the `MSH-7` ordering guard entirely.
    """

    to_state: ReferralState
    assertion_source: AssertionSource
    actor: ActorRef
    evidence: tuple[Evidence, ...]
    occurred_at: datetime
    recorded_at: datetime
    hold: HoldChange | None
    rationale: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple):
            raise TypeError(
                f"evidence must be a tuple, not {type(self.evidence).__name__}; the same "
                "argument as Evidence.spans"
            )
