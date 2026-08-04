"""The only thing that moves a referral's state, and the one move it will never make.

The load-bearing test here is the sweep, and it asserts on the *reason* a transition was
refused rather than on the fact that something raised. Refusing `RECONCILED` from `SENT`
happens twice over -- once because only a human reconciles, once because the table has no
such edge -- and a sweep that accepted either would still pass with the auto-close guard
deleted. Asserting the reason is what makes deleting the guard turn every case red.
"""

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from referral_loop.core.machine import (
    LEGAL_TRANSITIONS,
    RejectionReason,
    TransitionRejected,
    apply,
)
from referral_loop.core.models import PartyRef, PatientRef, Referral, ReferralId, Specialty
from referral_loop.core.states import DocumentationStatus, Hold, ReferralState
from referral_loop.core.transitions import (
    ActorRef,
    AssertionSource,
    Evidence,
    EvidenceKind,
    HoldChange,
    Transition,
)
from referral_loop.errors import ReferralLoopError

_NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)
_LATER = datetime(2026, 8, 3, tzinfo=timezone.utc)

# The same shape test_spec_proofs.py uses: an identifier that cannot occur by chance, so a
# grep for it in an exception message is unambiguous.
_SENTINEL_MRN = "ZZSENTINELMRNEEE"


def _referral(state: ReferralState, *, hold: Hold | None = None, seq: int = 7,
              documentation: DocumentationStatus | None = None) -> Referral:
    return Referral(
        id=ReferralId("REF-1"),
        patient=PatientRef(mrn=_SENTINEL_MRN, aliases=()),
        sending_org=PartyRef(id="clinic-a", name="Clinic A"),
        receiving_org=PartyRef(id="example-lab", name="Example Lab"),
        referring_provider=None,
        specialty=Specialty("cardiology"),
        reason=None,
        service_request_id=None,
        state=state,
        hold=hold,
        state_occurred_at=_NOW,
        seq=seq,
        documentation=documentation,
    )


def _t(*, to_state: ReferralState, assertion_source: AssertionSource, **kw) -> Transition:
    """No default for `assertion_source`, here as everywhere. See tests/test_transitions.py."""
    base = dict(
        actor=ActorRef(kind="organization", id="example-lab"),
        evidence=(),
        occurred_at=_LATER,
        recorded_at=_LATER,
        hold=None,
        rationale=None,
    )
    base.update(kw)
    return Transition(to_state=to_state, assertion_source=assertion_source, **base)


# --------------------------------------------------------------- the guarantee


def test_the_system_cannot_reconcile_a_referral_on_its_own():
    """Spec section 6.3. This is the auto-close prohibition, moved from an unreachable
    enum member onto the transition, where it survives someone making the member
    reachable. An inferred completion that is wrong is a patient-safety event."""
    referral = _referral(state=ReferralState.DOCUMENTED)
    inferred = _t(to_state=ReferralState.RECONCILED,
                  assertion_source=AssertionSource.SYSTEM_INFERRED)
    with pytest.raises(TransitionRejected, match="reconcil"):
        apply(referral, inferred)


def test_a_human_may_reconcile_the_same_referral():
    referral = _referral(state=ReferralState.DOCUMENTED,
                         documentation=DocumentationStatus.FINAL)
    human = _t(to_state=ReferralState.RECONCILED, assertion_source=AssertionSource.HUMAN)
    assert apply(referral, human).state is ReferralState.RECONCILED


def test_the_receiving_organisation_cannot_reconcile_either():
    """RECEIVING_ORG is not a human on this side of the exchange. A specialist's office
    asserting 'done' is evidence, not a coordinator's confirmation -- and the whole
    referral product exists because that assertion frequently never arrives at all."""
    referral = _referral(state=ReferralState.DOCUMENTED)
    org = _t(to_state=ReferralState.RECONCILED,
             assertion_source=AssertionSource.RECEIVING_ORG)
    with pytest.raises(TransitionRejected):
        apply(referral, org)


@pytest.mark.parametrize("state", list(ReferralState))
@pytest.mark.parametrize("source", [AssertionSource.SYSTEM_INFERRED,
                                    AssertionSource.RECEIVING_ORG])
def test_reconciled_is_unreachable_from_every_state_without_a_human(state, source):
    """The state-space sweep, following the pattern of the existing
    test_registry_safety.py. A guarantee that holds from DOCUMENTED but not from
    SCHEDULED is not a guarantee.

    The reason is asserted, not just the exception: from every state but DOCUMENTED the
    table refuses this edge anyway, so a sweep that only checked `raises` would pass
    unchanged with the guard deleted and would be proving the table's behaviour.
    """
    referral = _referral(state=state)
    t = _t(to_state=ReferralState.RECONCILED, assertion_source=source)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, t)
    assert caught.value.reason is RejectionReason.RECONCILE_REQUIRES_A_HUMAN


def test_the_guard_is_checked_before_the_table_so_it_cannot_be_hidden_by_it():
    """The ordering is the reason the sweep above says anything. If legality were checked
    first, a system-asserted reconciliation from SENT would be refused as an illegal edge
    and the safety refusal would never be reached -- and the day someone adds the edge, the
    guard would be reached for the first time in production."""
    referral = _referral(state=ReferralState.SENT)
    t = _t(to_state=ReferralState.RECONCILED,
           assertion_source=AssertionSource.SYSTEM_INFERRED)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, t)
    assert caught.value.reason is RejectionReason.RECONCILE_REQUIRES_A_HUMAN
    assert ReferralState.RECONCILED not in LEGAL_TRANSITIONS[ReferralState.SENT], (
        "this test is only meaningful while the table has no SENT -> RECONCILED edge"
    )


def test_a_human_reconciliation_still_has_to_be_a_legal_move():
    """The guard is a floor, not a bypass. A coordinator cannot reconcile a referral that
    was never documented -- there is nothing for them to have looked at."""
    referral = _referral(state=ReferralState.SCHEDULED,
                         documentation=DocumentationStatus.FINAL)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=ReferralState.RECONCILED,
                           assertion_source=AssertionSource.HUMAN))
    assert caught.value.reason is RejectionReason.NOT_A_LEGAL_TRANSITION


# ------------------------------------------------------------------- the table


def test_every_state_is_a_key_so_terminality_is_written_down_not_inferred():
    """A missing key and an empty frozenset behave the same at the lookup and mean
    different things to a reader -- and to a KeyError."""
    assert set(LEGAL_TRANSITIONS) == set(ReferralState)


def test_exactly_three_states_are_terminal():
    """Spec 6.1's three exits. RECONCILED is deliberately not among them: 6.4's corrected
    document demotes it back to DOCUMENTED."""
    terminal = {s.name for s, targets in LEGAL_TRANSITIONS.items() if not targets}
    assert terminal == {"DECLINED", "CANCELLED", "AGED_OUT"}


def test_the_happy_path_of_section_six_one_walks_end_to_end():
    """DRAFT -> SENT -> RECEIVED -> ACCEPTED -> SCHEDULED -> SEEN -> DOCUMENTED -> RECONCILED,
    each step actually applied rather than read off the table."""
    path = [
        ReferralState.SENT, ReferralState.RECEIVED, ReferralState.ACCEPTED,
        ReferralState.SCHEDULED, ReferralState.SEEN, ReferralState.DOCUMENTED,
        ReferralState.RECONCILED,
    ]
    referral = _referral(state=ReferralState.DRAFT, seq=0,
                         documentation=DocumentationStatus.FINAL)
    for step in path:
        referral = apply(referral, _t(to_state=step, assertion_source=AssertionSource.HUMAN))
        assert referral.state is step
    assert referral.seq == len(path)


def test_aging_out_is_reachable_exactly_where_we_are_waiting_on_the_counterparty():
    """The rule behind the AGED_OUT column, asserted rather than left to the comment.

    Aging means counterparty silence. DRAFT is excluded because a draft has no counterparty
    to be silent -- an abandoned one exits via CANCELLED, which needs a human, because
    deciding a referral is dead is a clinical judgement and not a timeout.

    DOCUMENTED is excluded because there the wait is on us. A documented referral is one
    whose note came back and which nobody has reviewed; it is the population
    `store.resulted_unacknowledged()` selects, and that queue exists to stay non-empty
    until a person acts. Aging it out empties the queue that is the product. The store
    already holds this position -- `_NEVER_DELETABLE` lists RESULTED, the same population
    under the old vocabulary, as "a result nobody has acknowledged".
    """
    ages_out = {s.name for s, targets in LEGAL_TRANSITIONS.items()
                if ReferralState.AGED_OUT in targets}
    assert ages_out == {"SENT", "RECEIVED", "ACCEPTED", "SCHEDULED", "SEEN"}


def test_an_unreviewed_document_cannot_be_aged_out_from_under_a_coordinator():
    """The case above, exercised rather than read off the table."""
    referral = _referral(state=ReferralState.DOCUMENTED)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=ReferralState.AGED_OUT,
                           assertion_source=AssertionSource.SYSTEM_INFERRED))
    assert caught.value.reason is RejectionReason.NOT_A_LEGAL_TRANSITION


def test_a_corrected_document_demotes_a_reconciled_referral():
    """Spec 6.4, the existing corrected-result behaviour (OBX-11 = C) carried over."""
    referral = _referral(state=ReferralState.RECONCILED)
    corrected = _t(to_state=ReferralState.DOCUMENTED,
                   assertion_source=AssertionSource.RECEIVING_ORG)
    assert apply(referral, corrected).state is ReferralState.DOCUMENTED


_LEGAL_PAIRS = [(f, t) for f, targets in LEGAL_TRANSITIONS.items() for t in sorted(targets)]
_ILLEGAL_PAIRS = [
    (f, t) for f in ReferralState for t in ReferralState
    if t not in LEGAL_TRANSITIONS[f]
]


@pytest.mark.parametrize(("from_state", "to_state"), _LEGAL_PAIRS)
def test_every_legal_transition_is_reachable(from_state, to_state):
    """A table entry no call can exercise is documentation, not a rule.

    Held at FINAL documentation so this sweep isolates the legality axis: the spec rule 1
    guard is a separate question with its own tests below, and a fixture that tripped it
    would make this sweep silently stop testing the table.
    """
    referral = _referral(state=from_state, documentation=DocumentationStatus.FINAL)
    moved = apply(referral, _t(to_state=to_state, assertion_source=AssertionSource.HUMAN))
    assert moved.state is to_state


@pytest.mark.parametrize(("from_state", "to_state"), _ILLEGAL_PAIRS)
def test_every_illegal_transition_is_refused(from_state, to_state):
    """FINAL documentation for the same reason as the sweep above."""
    referral = _referral(state=from_state, documentation=DocumentationStatus.FINAL)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=to_state, assertion_source=AssertionSource.HUMAN))
    assert caught.value.reason is RejectionReason.NOT_A_LEGAL_TRANSITION
    assert caught.value.from_state is from_state
    assert caught.value.to_state is to_state


def test_the_table_covers_every_pair_exactly_once():
    """Guards the two parametrisations above against each other: if the illegal list were
    built from a stale copy of the table, both sweeps could pass while a pair went
    untested."""
    assert len(_LEGAL_PAIRS) + len(_ILLEGAL_PAIRS) == len(ReferralState) ** 2
    assert not set(_LEGAL_PAIRS) & set(_ILLEGAL_PAIRS)


def test_a_scheduled_referral_may_be_rescheduled():
    """SCHEDULED -> SCHEDULED is legal in the machine registry.py runs today
    (_SCHEDULABLE_FROM contains SCHEDULED), and Task 5 has to route `schedule` through
    this table without changing what it accepts."""
    referral = _referral(state=ReferralState.SCHEDULED)
    assert apply(referral, _t(to_state=ReferralState.SCHEDULED,
                              assertion_source=AssertionSource.RECEIVING_ORG)).state is (
        ReferralState.SCHEDULED)


def test_a_scheduled_referral_may_be_unscheduled_back_to_accepted():
    """An SIU^S15 cancels an appointment, not a referral: the patient still needs to be
    seen and the booking has to be made again. The referral therefore goes back to the
    state it was in before it was booked rather than to CANCELLED, which is terminal and
    on no worklist.

    Not a backwards move of the kind the table's note refuses. Nothing clinical has
    happened yet -- SCHEDULED -> SCHEDULED is already legal as a reschedule, and an S15
    followed by an S12 is that same reschedule sent as two messages.
    """
    referral = _referral(state=ReferralState.SCHEDULED)
    assert apply(referral, _t(to_state=ReferralState.ACCEPTED,
                              assertion_source=AssertionSource.RECEIVING_ORG)).state is (
        ReferralState.ACCEPTED)


@pytest.mark.parametrize("state", [ReferralState.SEEN, ReferralState.DOCUMENTED,
                                   ReferralState.RECONCILED])
def test_unscheduling_stops_at_the_encounter(state):
    """The boundary the new SCHEDULED -> ACCEPTED edge must not move. Once the patient has
    been seen there is no appointment left to cancel, and a late S15 for the slot they
    already attended must not return the referral to awaiting a booking."""
    referral = _referral(state=state, documentation=DocumentationStatus.FINAL)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=ReferralState.ACCEPTED,
                           assertion_source=AssertionSource.RECEIVING_ORG))
    assert caught.value.reason is RejectionReason.NOT_A_LEGAL_TRANSITION


def test_a_sent_referral_may_be_scheduled_without_a_receipt_ever_arriving():
    """Also required by Task 5: _SCHEDULABLE_FROM contains OPEN, which is SENT here. An
    SIU is routinely the first thing a receiving org sends, and a table that demanded a
    RECEIVED step first would refuse the common case."""
    referral = _referral(state=ReferralState.SENT)
    assert apply(referral, _t(to_state=ReferralState.SCHEDULED,
                              assertion_source=AssertionSource.RECEIVING_ORG)).state is (
        ReferralState.SCHEDULED)


def test_a_cancelled_referral_accepts_nothing_further():
    """Carried over from registry.record_result, which refuses a result on a CANCELLED
    loop and routes it to the orphan queue instead. Under the two-aggregate split that
    late arrival is an InboundArtifact, not a resurrection."""
    referral = _referral(state=ReferralState.CANCELLED)
    for state in ReferralState:
        with pytest.raises(TransitionRejected):
            apply(referral, _t(to_state=state, assertion_source=AssertionSource.HUMAN))


# ---------------------------------------------------------------------- purity


def test_apply_returns_a_new_referral_and_does_not_mutate_the_input():
    """Plan 2b replays the event log through this type. An in-place mutation would make a
    replayed prefix of the log disagree with the same prefix replayed twice."""
    referral = _referral(state=ReferralState.SCHEDULED, seq=7)
    moved = apply(referral, _t(to_state=ReferralState.SEEN,
                               assertion_source=AssertionSource.RECEIVING_ORG))
    assert moved is not referral
    assert referral.state is ReferralState.SCHEDULED
    assert referral.seq == 7
    assert referral.state_occurred_at == _NOW


def test_seq_increments_by_exactly_one():
    referral = _referral(state=ReferralState.SCHEDULED, seq=41)
    assert apply(referral, _t(to_state=ReferralState.SEEN,
                              assertion_source=AssertionSource.HUMAN)).seq == 42


def test_a_refused_transition_moves_nothing_at_all():
    referral = _referral(state=ReferralState.SEEN, seq=3)
    with pytest.raises(TransitionRejected):
        apply(referral, _t(to_state=ReferralState.SCHEDULED,
                           assertion_source=AssertionSource.HUMAN))
    assert referral.state is ReferralState.SEEN
    assert referral.seq == 3


def test_the_new_state_time_comes_off_the_transition_and_not_a_clock():
    """`state_occurred_at` is when the state's event happened. Reading it from the clock
    here would stamp arrival time onto a message that carried its own MSH-7, and nothing
    downstream could tell the two apart."""
    referral = _referral(state=ReferralState.SCHEDULED)
    backdated = datetime(2026, 7, 1, tzinfo=timezone.utc)
    moved = apply(referral, _t(to_state=ReferralState.SEEN,
                               assertion_source=AssertionSource.RECEIVING_ORG,
                               occurred_at=backdated, recorded_at=_LATER))
    assert moved.state_occurred_at == backdated


def test_applying_the_same_inputs_twice_gives_equal_results():
    """The behavioural half of purity: no clock, no counter, no accumulated state."""
    referral = _referral(state=ReferralState.SCHEDULED)
    t = _t(to_state=ReferralState.SEEN, assertion_source=AssertionSource.HUMAN)
    assert apply(referral, t) == apply(referral, t)


_MACHINE_SRC = (
    Path(__file__).resolve().parents[1] / "src" / "referral_loop" / "core" / "machine.py"
)

# Everything machine.py is allowed to import. The closure test in test_import_closure.py
# polices the package boundary; this polices the much narrower claim in the docstring --
# that apply() reads no clock and writes no log. `datetime` is absent deliberately: the
# module annotates no datetime and has no business constructing one.
_PERMITTED_IMPORTS = {
    "__future__", "dataclasses", "enum", "typing",
    "referral_loop.errors", "models", "states", "transitions", "errors",
}


def test_the_machine_imports_no_clock_no_logger_and_no_io():
    """Asserted on the source rather than by monkeypatching a clock, because the failure
    mode is an import added for one debug line and never removed -- which no behavioural
    test would notice until the day it mattered."""
    tree = ast.parse(_MACHINE_SRC.read_text(encoding="utf-8"), filename=str(_MACHINE_SRC))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= _PERMITTED_IMPORTS, (
        f"machine.py imported something outside the pure set: {sorted(imported - _PERMITTED_IMPORTS)}"
    )


def test_nothing_in_the_machine_calls_a_clock_or_prints():
    """The import check above is blind to `states.datetime.now()` reached through an
    already-permitted module, and to a bare print left behind."""
    tree = ast.parse(_MACHINE_SRC.read_text(encoding="utf-8"), filename=str(_MACHINE_SRC))
    forbidden = {"now", "utcnow", "today", "time", "monotonic", "print", "open", "random"}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else "")
        if name in forbidden:
            found.append(f"{name}() at line {node.lineno}")
    assert found == [], f"machine.py is not pure: {found}"


# ------------------------------------------------------------------ the refusal


def test_a_refusal_names_the_referral_and_the_attempted_move():
    referral = _referral(state=ReferralState.SEEN)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=ReferralState.SCHEDULED,
                           assertion_source=AssertionSource.HUMAN))
    message = str(caught.value)
    assert "REF-1" in message
    assert "SEEN" in message and "SCHEDULED" in message


@pytest.mark.parametrize(("from_state", "to_state"), _ILLEGAL_PAIRS)
def test_no_refusal_message_carries_the_patient_identifier(from_state, to_state):
    """A live finding in this codebase is that MRNs reach application logs through
    exception messages. Swept over every refusal rather than spot-checked, because the one
    that leaks is the branch nobody wrote a test for.

    str, args and repr are all checked: a caller that logs `%r` of the exception gets a
    different string from one that logs `%s`, and only one of them is usually tested.
    """
    referral = _referral(state=from_state)
    for source in AssertionSource:
        with pytest.raises(TransitionRejected) as caught:
            apply(referral, _t(to_state=to_state, assertion_source=source))
        exc = caught.value
        for rendering in (str(exc), repr(exc), str(exc.args)):
            assert _SENTINEL_MRN not in rendering, f"the MRN reached {rendering!r}"


def test_a_rejection_is_a_referral_loop_error_so_the_listener_still_answers_correctly():
    """listener.py catches ReferralLoopError at line 689 and answers the engine from it,
    with a bare `except Exception` immediately below. A rejection that escaped the first
    clause would be answered as an unhandled failure rather than a refusal."""
    referral = _referral(state=ReferralState.SEEN)
    with pytest.raises(ReferralLoopError):
        apply(referral, _t(to_state=ReferralState.SCHEDULED,
                           assertion_source=AssertionSource.HUMAN))


def test_the_two_refusals_are_told_apart_by_a_code_and_not_by_their_message():
    """The same argument audit.RefusalCode already makes: refusing because the state is
    wrong is bookkeeping and refusing because only a human reconciles is spec 6.3, and
    'TransitionRejected' says neither. The message that would say which is the one thing
    that must not be copied into an audit row."""
    assert {r.name for r in RejectionReason} == {
        "RECONCILE_REQUIRES_A_HUMAN", "PRELIMINARY_NOT_RECONCILABLE",
        "NOT_A_LEGAL_TRANSITION"}


# ------------------------------------------------------------------------ hold


def test_a_transition_that_says_nothing_about_the_hold_leaves_it_alone():
    """The common case, and the one that must not be spelled the same way as lifting a
    hold: an SIU arriving on a referral a coordinator suspended would otherwise silently
    release it."""
    held = Hold(reason="awaiting patient callback", actor="coordinator-b")
    referral = _referral(state=ReferralState.SCHEDULED, hold=held)
    moved = apply(referral, _t(to_state=ReferralState.SEEN,
                               assertion_source=AssertionSource.RECEIVING_ORG))
    assert moved.hold == held


def test_an_inbound_schedule_notice_does_not_release_a_hold_a_coordinator_placed():
    """The failure HoldChange exists to prevent, written out as the case it actually is.

    A coordinator holds a referral for "patient unreachable". An SIU then arrives from the
    receiving organisation and moves the referral to SCHEDULED. That message says nothing
    about the hold and must not touch it -- but with `Hold | None` on the Transition there
    is no way to spell "leave it alone" that is distinct from "lift it", so the ingest path
    would have to pass None and would silently release a suspension a person applied.

    The hold is compared whole, not merely for presence: a release-and-reapply that lost
    the reason or the actor would put the referral back on the wrong aging threshold and
    attribute the suspension to nobody.
    """
    held = Hold(reason="patient unreachable", actor="coordinator-b")
    referral = _referral(state=ReferralState.SENT, hold=held)
    siu = _t(to_state=ReferralState.SCHEDULED,
             assertion_source=AssertionSource.RECEIVING_ORG,
             hold=None)
    moved = apply(referral, siu)
    assert moved.state is ReferralState.SCHEDULED
    assert moved.hold is held
    assert moved.hold.reason == "patient unreachable"
    assert moved.hold.actor == "coordinator-b"


def test_a_transition_may_apply_a_hold_while_the_state_moves():
    referral = _referral(state=ReferralState.SCHEDULED)
    held = Hold(reason="awaiting patient callback", actor="coordinator-b")
    moved = apply(referral, _t(to_state=ReferralState.SEEN,
                               assertion_source=AssertionSource.HUMAN,
                               hold=HoldChange(hold=held)))
    assert moved.hold == held
    assert moved.state is ReferralState.SEEN


def test_a_transition_may_lift_one():
    referral = _referral(state=ReferralState.SCHEDULED,
                         hold=Hold(reason="awaiting callback", actor="coordinator-b"))
    moved = apply(referral, _t(to_state=ReferralState.SEEN,
                               assertion_source=AssertionSource.HUMAN,
                               hold=HoldChange(hold=None)))
    assert moved.hold is None


def test_a_refused_transition_does_not_apply_its_hold_either():
    """The hold change rides on the transition, so a rejection that had already written it
    would leave a referral suspended by a move that never happened."""
    referral = _referral(state=ReferralState.SEEN)
    with pytest.raises(TransitionRejected):
        apply(referral, _t(to_state=ReferralState.SCHEDULED,
                           assertion_source=AssertionSource.HUMAN,
                           hold=HoldChange(hold=Hold(reason="x", actor="y"))))
    assert referral.hold is None


# -------------------------------------------------------------------- evidence


def test_the_machine_does_not_care_what_evidence_says_only_who_asserted_it():
    """A 0.99 match is still an inference. Spec 6.3 keys the refusal on the assertion
    source alone, and a confidence floor here would be a threshold at which the system
    reconciles on its own -- which is the thing being refused."""
    referral = _referral(state=ReferralState.DOCUMENTED)
    certain = _t(
        to_state=ReferralState.RECONCILED,
        assertion_source=AssertionSource.SYSTEM_INFERRED,
        evidence=(Evidence(kind=EvidenceKind.MATCH, ref="sha256:aa", spans=(),
                           confidence=1.0),),
    )
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, certain)
    assert caught.value.reason is RejectionReason.RECONCILE_REQUIRES_A_HUMAN


# ------------------------------------------------- reconciling a preliminary read


def test_a_preliminary_document_cannot_be_reconciled():
    """Spec rule 1, and the reason `documentation` is on the aggregate at all.

    A preliminary that later corrects is the malpractice scenario: reconciling on it takes
    the referral off `resulted_unacknowledged()` before the read that supersedes it has
    arrived. registry.py enforced this as `_ACKNOWLEDGEABLE_STATUSES` until Plan 2b Task 5, by
    fetching the latest OBX-11 from the event log; it is now decided on the aggregate
    alone, which is what keeps apply() pure and state legality to one enforcement point.
    """
    referral = _referral(state=ReferralState.DOCUMENTED,
                         documentation=DocumentationStatus.PRELIMINARY)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=ReferralState.RECONCILED,
                           assertion_source=AssertionSource.HUMAN))
    assert caught.value.reason is RejectionReason.PRELIMINARY_NOT_RECONCILABLE


@pytest.mark.parametrize("status", [DocumentationStatus.FINAL, DocumentationStatus.CORRECTED])
def test_a_final_or_corrected_document_may_be_reconciled_by_a_human(status):
    referral = _referral(state=ReferralState.DOCUMENTED, documentation=status)
    moved = apply(referral, _t(to_state=ReferralState.RECONCILED,
                               assertion_source=AssertionSource.HUMAN))
    assert moved.state is ReferralState.RECONCILED


def test_a_referral_with_no_documentation_at_all_cannot_be_reconciled():
    """The allowlist, not the denylist -- and this is the case that tells them apart.

    registry.py already refuses this and says why at tests/test_registry_safety.py:125:
    "Rule 1 as an allowlist, not a denylist. A resulted event carrying no OBX-11 -- a
    restored log, a foreign writer, a future code path -- must not resolve the loop just
    because its status is not literally 'P'." A guard written as `is PRELIMINARY` would
    reconcile on None and regress
    test_acknowledgement_is_unreachable_with_no_result_at_all.
    """
    referral = _referral(state=ReferralState.DOCUMENTED, documentation=None)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=ReferralState.RECONCILED,
                           assertion_source=AssertionSource.HUMAN))
    assert caught.value.reason is RejectionReason.PRELIMINARY_NOT_RECONCILABLE


@pytest.mark.parametrize("status", [None, DocumentationStatus.PRELIMINARY])
def test_the_reconcilable_set_is_an_allowlist_swept_over_everything_outside_it(status):
    referral = _referral(state=ReferralState.DOCUMENTED, documentation=status)
    for source in AssertionSource:
        with pytest.raises(TransitionRejected):
            apply(referral, _t(to_state=ReferralState.RECONCILED, assertion_source=source))


def test_the_human_guarantee_outranks_the_documentation_guard():
    """Both refuse a system-asserted reconciliation of a preliminary. The reported reason
    is the human one, because 'the system may not reconcile' holds whatever the
    documentation says, while the documentation guard would stop applying the moment a
    final arrived."""
    referral = _referral(state=ReferralState.DOCUMENTED,
                         documentation=DocumentationStatus.PRELIMINARY)
    with pytest.raises(TransitionRejected) as caught:
        apply(referral, _t(to_state=ReferralState.RECONCILED,
                           assertion_source=AssertionSource.SYSTEM_INFERRED))
    assert caught.value.reason is RejectionReason.RECONCILE_REQUIRES_A_HUMAN


def test_the_documentation_guard_binds_only_the_edge_into_reconciled():
    """A preliminary read is a perfectly ordinary referral in every other respect. A guard
    that refused all movement would strand it: the correction that supersedes it arrives as
    DOCUMENTED -> DOCUMENTED, and refusing that would make the preliminary permanent."""
    referral = _referral(state=ReferralState.DOCUMENTED,
                         documentation=DocumentationStatus.PRELIMINARY)
    for target in (ReferralState.DOCUMENTED,):
        assert apply(referral, _t(to_state=target,
                                  assertion_source=AssertionSource.RECEIVING_ORG)).state is target


def test_apply_never_writes_the_documentation_field():
    """The store owns it. It is a fold over the resulted/reopened chain ranked by clinical
    time, which registry._latest_result_event already computes and which the security audit
    fixed a real ordering inversion in; the machine reads that fold and never re-derives
    it. So apply() passes it through untouched and there is exactly one writer."""
    referral = _referral(state=ReferralState.DOCUMENTED,
                         documentation=DocumentationStatus.PRELIMINARY)
    moved = apply(referral, _t(to_state=ReferralState.DOCUMENTED,
                               assertion_source=AssertionSource.RECEIVING_ORG))
    assert moved.documentation is DocumentationStatus.PRELIMINARY
