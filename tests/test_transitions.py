"""The object that makes 'who asserted this' unforgettable rather than merely required."""

from datetime import datetime, timezone

import pytest

from referral_loop.core.states import Hold, ReferralState
from referral_loop.core.transitions import (
    ActorRef,
    AssertionSource,
    Evidence,
    EvidenceKind,
    HoldChange,
    Span,
    Transition,
)

_NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _t(*, assertion_source: AssertionSource, **kw):
    """Build a Transition for a test.

    `assertion_source` is keyword-only with no default, and the base dict below does not
    carry one. That is deliberate and is the same rule the dataclass holds: a default here
    would be copied into the next helper and the one after that, and in six months the
    guarantee would be "whatever the first caller happened to pass".
    """
    base = dict(
        to_state=ReferralState.DOCUMENTED,
        actor=ActorRef(kind="organization", id="example-lab"),
        evidence=(),
        occurred_at=_NOW,
        recorded_at=_NOW,
        hold=None,
        rationale=None,
    )
    base.update(kw)
    return Transition(assertion_source=assertion_source, **base)


def test_assertion_source_has_no_default():
    """The whole design rests on there being no way to move state without saying who said
    so. A default -- any default -- turns that from a guarantee into a convention, and the
    convention would be 'whatever the first caller happened to pass'."""
    with pytest.raises(TypeError):
        Transition(  # type: ignore[call-arg]
            to_state=ReferralState.SENT,
            actor=ActorRef(kind="device", id="referral-loop"),
            evidence=(),
            occurred_at=_NOW,
            recorded_at=_NOW,
            hold=None,
            rationale=None,
        )


def test_no_construction_site_in_the_domain_layer_supplies_a_default_assertion_source():
    """The test above proves the dataclass has no default. This proves nothing else grew
    one: a factory, a classmethod or a helper defaulting the field would satisfy that test
    while restoring exactly the convention it exists to prevent.

    Read as source over the whole of core/ rather than as behaviour, because a default that
    no test happens to exercise is still a default, and because the module that grows one
    next is the one nobody thought to list here.
    """
    import ast
    from pathlib import Path

    core = Path(__file__).resolve().parents[1] / "src" / "referral_loop" / "core"
    offenders = []
    for path in sorted(core.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                defaulted = list(args.args[len(args.args) - len(args.defaults):])
                defaulted += [
                    a for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
                ]
                if any(a.arg == "assertion_source" for a in defaulted):
                    offenders.append(f"{path.name}:{node.lineno} {node.name}()")
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                if (
                    isinstance(target, ast.Name)
                    and target.id == "assertion_source"
                    and node.value is not None
                ):
                    offenders.append(f"{path.name}:{node.lineno} field default")
    assert offenders == [], f"assertion_source acquired a default in {offenders}"


def test_there_are_exactly_three_assertion_sources():
    assert {s.name for s in AssertionSource} == {"HUMAN", "RECEIVING_ORG", "SYSTEM_INFERRED"}


def test_a_transition_is_immutable():
    with pytest.raises(Exception):
        _t(assertion_source=AssertionSource.RECEIVING_ORG).assertion_source = AssertionSource.HUMAN


def test_occurred_at_and_recorded_at_are_separate():
    """Out-of-order arrival is detected on occurred_at; recorded_at stays monotonic per
    store, so the audit trail reads correctly when reality arrives backwards. Collapsing
    them loses the MSH-7 ordering guard."""
    t = _t(
        assertion_source=AssertionSource.RECEIVING_ORG,
        occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        recorded_at=_NOW,
    )
    assert t.occurred_at < t.recorded_at


def test_only_an_inferred_transition_may_carry_a_confidence():
    """A confidence on a human assertion is meaningless -- the human either asserted it or
    did not. Allowing it invites a caller to launder a match score into an assertion."""
    inferred = Evidence(kind=EvidenceKind.MATCH, ref="sha256:aa", spans=(), confidence=0.93)
    assert inferred.confidence == 0.93
    with pytest.raises(ValueError):
        Evidence(kind=EvidenceKind.USER_ACTION, ref="worklist", spans=(), confidence=0.93)


@pytest.mark.parametrize(
    "kind", [EvidenceKind.HL7_MESSAGE, EvidenceKind.DOCUMENT, EvidenceKind.USER_ACTION,
             EvidenceKind.RULE],
)
def test_no_asserted_evidence_kind_accepts_a_confidence(kind):
    """The sweep behind the test above. A guarantee that holds for USER_ACTION but not for
    RULE is not a guarantee -- and RULE is the one a caller would reach for first when
    looking for somewhere to park a score."""
    with pytest.raises(ValueError):
        Evidence(kind=kind, ref="r", spans=(), confidence=0.5)


@pytest.mark.parametrize("kind", [EvidenceKind.MATCH, EvidenceKind.FHIR_RESOURCE])
def test_the_two_inferred_kinds_do_accept_one(kind):
    assert Evidence(kind=kind, ref="r", spans=(), confidence=0.5).confidence == 0.5


@pytest.mark.parametrize("bad", [-0.01, 1.01, 42.0])
def test_a_confidence_outside_zero_to_one_is_not_a_confidence(bad):
    """Unbounded, the field accepts a raw similarity score or a count of matched fields,
    and the Provenance projection would publish it as though it were a probability."""
    with pytest.raises(ValueError):
        Evidence(kind=EvidenceKind.MATCH, ref="r", spans=(), confidence=bad)


def test_evidence_without_a_confidence_is_the_ordinary_case():
    assert Evidence(kind=EvidenceKind.HL7_MESSAGE, ref="sha256:bb", spans=None,
                    confidence=None).confidence is None


def test_spans_must_be_a_tuple_so_the_frozen_dataclass_is_immutable_through_its_fields():
    """Same argument as PatientRef.aliases: frozen stops rebinding the field, not mutating
    what it points at, and a list of spans could be appended to after the Provenance
    projection had already read it."""
    with pytest.raises(TypeError):
        Evidence(kind=EvidenceKind.MATCH, ref="r", spans=[Span(start=1, end=2)],  # type: ignore[arg-type]
                 confidence=None)


def test_evidence_must_name_what_it_is_evidence_of():
    """An empty ref projects to a Provenance entity referencing nothing, which reads as
    'we cited something' while citing nothing."""
    with pytest.raises(ValueError):
        Evidence(kind=EvidenceKind.DOCUMENT, ref="", spans=None, confidence=None)


def test_the_transitions_evidence_is_a_tuple_too():
    with pytest.raises(TypeError):
        _t(assertion_source=AssertionSource.HUMAN, evidence=[])


def test_a_span_points_into_a_source_document():
    s = Span(start=10, end=42)
    assert s.end > s.start
    with pytest.raises(ValueError):
        Span(start=42, end=10)


def test_a_span_covers_at_least_one_character():
    with pytest.raises(ValueError):
        Span(start=10, end=10)


def test_a_span_offset_is_not_negative():
    with pytest.raises(ValueError):
        Span(start=-1, end=4)


def test_an_actor_is_one_of_the_three_things_provenance_can_reference():
    """Spec 8.2 maps the three assertion sources onto Practitioner, Organization and
    Device, and nothing else. A free string here would let a typo -- 'organisation' --
    reach the projection, where it would either raise or silently produce a reference to a
    resource type that does not exist."""
    assert ActorRef(kind="practitioner", id="coordinator-b").kind == "practitioner"
    with pytest.raises(ValueError):
        ActorRef(kind="organisation", id="example-lab")


def test_an_actor_must_be_named():
    """An empty id projects to `Practitioner/`, which is a reference to nothing wearing the
    shape of a reference to someone."""
    with pytest.raises(ValueError):
        ActorRef(kind="device", id="")


def test_a_hold_change_distinguishes_applying_from_lifting_from_not_touching():
    """Three cases, and `Hold | None` on the Transition can only carry two. Spec 8.2's
    activity vocabulary lists `hold` and `release` as separate activities, so both have to
    be expressible -- and the overwhelmingly common case, a transition that does not touch
    the hold at all, must not be spelled the same way as lifting one."""
    applied = HoldChange(hold=Hold(reason="awaiting callback", actor="coordinator-b"))
    lifted = HoldChange(hold=None)
    assert applied.hold is not None
    assert lifted.hold is None
    assert _t(assertion_source=AssertionSource.HUMAN).hold is None
