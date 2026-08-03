from dataclasses import replace
from datetime import datetime, timezone

import pytest

from referral_loop.core.models import ArtifactKind, InboundArtifact, PartyRef, PatientRef, Referral
from referral_loop.core.states import (
    ArtifactState,
    DocumentationStatus,
    Hold,
    ReferralState,
)


def _ref() -> Referral:
    return Referral(
        id="R-0001",
        patient=PatientRef(mrn="MRN1"),
        sending_org=PartyRef(id="example-ris", name="Example RIS"),
        receiving_org=None,
        referring_provider=None,
        specialty="cardiology",
        reason=None,
        service_request_id=None,
        state=ReferralState.SENT,
        hold=None,
        state_occurred_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        seq=1,
    )


def test_a_referral_is_immutable():
    with pytest.raises(Exception):
        _ref().state = ReferralState.RECONCILED


def test_a_referral_may_be_held_without_losing_the_state_it_was_held_from():
    held = _ref().with_hold(Hold(reason="patient unreachable", actor="coordinator-b"))
    assert held.hold is not None
    assert held.state is ReferralState.SENT, "the underlying state must survive a hold"


def test_releasing_a_hold_returns_the_referral_to_the_state_it_never_left():
    """.released() is the inverse of .with_hold(), and neither is a setter: Plan 2b
    replays the event log through these, so an in-place mutation would let a replayed
    prefix of the log disagree with the same prefix replayed twice."""
    original = _ref()
    released = original.with_hold(Hold(reason="x", actor="y")).released()
    assert released.hold is None
    assert released == original, "release must restore the aggregate exactly"
    assert released is not original


def test_an_artifact_may_have_no_patient_because_that_is_the_whole_problem():
    """An inbound consult note from another EHR frequently carries no identifier this site
    can resolve. A model that requires one cannot represent the case the reconciliation
    engine exists to handle."""
    a = InboundArtifact(
        id="A-0001",
        received_from=PartyRef(id="example-lab", name="Example Lab"),
        patient=None,
        content_hash="a" * 64,
        kind=ArtifactKind.RESULT,
        state=ArtifactState.UNMATCHED,
        received_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        observed_at=None,
    )
    assert a.patient is None


def test_an_artifact_is_immutable_too():
    a = InboundArtifact(
        id="A-0001",
        received_from=PartyRef(id="example-lab", name="Example Lab"),
        patient=None,
        content_hash="a" * 64,
        kind=ArtifactKind.DOCUMENT,
        state=ArtifactState.UNMATCHED,
        received_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        observed_at=None,
    )
    with pytest.raises(Exception):
        a.state = ArtifactState.ATTACHED


def test_the_two_aggregates_do_not_share_an_identifier_space():
    """A referral id and an artifact id must never be interchangeable, or an attach could
    name the wrong thing and typecheck."""
    assert Referral.__annotations__["id"] is not InboundArtifact.__annotations__["id"]


def test_the_two_identifier_types_are_types_and_not_aliases_for_str():
    """The annotation check above compares two names; this compares the things they name.
    `ReferralId = str` would satisfy the former and defeat the entire point, since every
    artifact id would then be a valid referral id to a type checker."""
    from referral_loop.core.models import ArtifactId, ReferralId

    assert ReferralId is not ArtifactId
    assert ReferralId is not str
    assert ArtifactId is not str


def test_an_artifact_is_one_of_exactly_three_kinds():
    assert {k.name for k in ArtifactKind} == {"RESULT", "DOCUMENT", "SCHEDULE_NOTICE"}


def test_a_patient_may_carry_aliases_without_the_model_resolving_them():
    """Spec section 5: PatientRef is 'local identity + known aliases'. Resolving an alias is
    the matcher's job and it needs a store to do it; the model only has to be able to
    carry what has already been resolved, or the domain layer would need a database
    connection to be constructed."""
    p = PatientRef(mrn="MRN1", aliases=("example-lab:NS-77",))
    assert p.aliases == ("example-lab:NS-77",)
    assert PatientRef(mrn="MRN1").aliases == ()


_IMPURE = ("datetime.now", "datetime.utcnow", "time.time", "sqlite3", "socket", "requests", "open(")


def _code_without_prose(source: str) -> str:
    """`source` with comments and docstrings removed.

    Scanning the raw file instead would make the assertion below unfalsifiable in the
    wrong direction: core/models.py's own docstrings explain *why* it must not call
    datetime.now(), so a substring search over the text fails on the explanation rather
    than on the behaviour. ast.unparse drops comments; docstrings are stripped here.
    """
    import ast

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_no_aggregate_method_reads_a_clock_a_database_or_a_network():
    """Design spec section 4's layering, as a property rather than a docstring. A method
    that stamped datetime.now() would make two replays of the same event log produce
    different aggregates; that value belongs on a Transition, which records when the
    event happened rather than when the code ran."""
    import inspect

    from referral_loop.core import models

    code = _code_without_prose(inspect.getsource(models))
    for forbidden in _IMPURE:
        assert forbidden not in code, f"core/models.py reached for {forbidden}"


def test_the_clock_check_would_notice_a_clock():
    """A negative assertion over source text is worth exactly what its ability to fail is
    worth, and this one runs over a rewritten AST. So put a clock read through the same
    rewrite and confirm it survives -- otherwise the test above passes because the
    stripper ate the evidence."""
    impure = 'from datetime import datetime\n\n\ndef stamp():\n    """when."""\n    return datetime.now()\n'
    assert "datetime.now" in _code_without_prose(impure)
    # ...and that a mention in prose alone does not trip it, which is the case that
    # produced this helper in the first place.
    assert "datetime.now" not in _code_without_prose('"""never calls datetime.now()."""\nx = 1\n')


def test_a_referral_carries_the_condition_of_its_documentation():
    """The fact the machine needs in order to refuse reconciling a preliminary read,
    living on the aggregate rather than being fetched from the event log. apply() stays
    pure only because this is here."""
    assert _ref().documentation is None
    documented = replace(_ref(), state=ReferralState.DOCUMENTED,
                         documentation=DocumentationStatus.FINAL)
    assert documented.documentation is DocumentationStatus.FINAL


def test_documentation_defaults_so_the_interop_fork_point_still_builds():
    """core/models.py is the tagged point the interop branch builds on, and migration.py
    constructs a Referral without this field. A defaulted field appended to the end of a
    frozen dataclass is backward compatible; anything else would break a build on another
    branch without warning, which the plan's coordination note forbids."""
    import inspect

    signature = inspect.signature(Referral)
    parameters = list(signature.parameters)
    assert parameters[-1] == "documentation", (
        "documentation must be last, or adding it reorders an existing positional field"
    )
    assert signature.parameters["documentation"].default is None
    # Constructed with no mention of the field at all -- the migration.py call shape.
    assert _ref().documentation is None
