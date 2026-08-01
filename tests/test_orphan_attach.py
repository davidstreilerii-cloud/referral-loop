"""Orphan attachment, match undo, and the labels they produce. Spec 5 and 7.

Three properties are load-bearing here, and each is asserted against the thing
that actually matters rather than against an intermediate:

  * **Safety rule 1 survives the manual path.** A coordinator attaching a
    preliminary read must leave the target RESULTED and unacknowledgeable.
    Attachment applies the result through `record_result`, so this is a property
    of not having a second implementation -- and the test asserts the refusal
    rather than the code path, because a future refactor could reintroduce one.
  * **A label may leave the building.** Sentinels are planted through the real
    listener and every coordinator action is driven; the export a site would
    contribute is then written to its own SQLite file and its *bytes* are
    grepped. The same grep is then run against a row written straight past the
    normalisers, so "clean" is a result rather than a scan that cannot fail.
  * **Undoing a match is not undoing an acknowledgement.** They produce
    different labels with different outcomes, and the release gate in spec
    section 7 counts one of them and not the other.
"""
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from referral_loop import audit
from referral_loop import store as store_module
from referral_loop.audit import AuditAction, referral_audit_entries
from referral_loop.errors import (
    LoopNotFoundError,
    ReferralLoopError,
    StoreUnavailableError,
)
from referral_loop.events import (
    LABEL_OUTCOME,
    LabelOutcome,
    LabelType,
    LoopState,
)
from referral_loop.registry import Registry
from referral_loop.store import LoopStore
from referral_loop.worklist import create_app
from tests._pack import PACK
from tests.test_worklist import (
    _E2E_ORDER,
    _E2E_RESULT,
    _E2E_SENTINELS,
    _E2E_UNMATCHED,
)

MRN = "MRN-ATTACH-1"
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

# Planted where nothing but a leak could carry them into a label.
ACTOR_SENTINEL = "ZZSENTINELACTOR"
REASON_SENTINEL = "ZZSENTINELREASON"
MRN_SENTINEL = "ZZSENTINELMRN0001"


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    store = LoopStore(tmp_path / "loops.db")
    registry = Registry(store, pack_version=PACK.version)
    app = create_app(store=store, registry=registry, pack=PACK)
    app.config["TESTING"] = True
    client = app.test_client()
    # The client stands in for the coordinator's browser, so it sends what one
    # sends on its own page's forms: an Origin matching the default Host. Without
    # it the form posts below are indistinguishable from cross-site ones, which
    # worklist.py refuses. See tests/test_worklist.py for the gate.
    client.environ_base["HTTP_ORIGIN"] = "http://localhost"
    return store, registry, client


@pytest.fixture()
def store(stack):
    return stack[0]


@pytest.fixture()
def registry(stack):
    return stack[1]


@pytest.fixture()
def http(stack):
    return stack[2]


def _orphan(registry, *, obx11="F", mrn=MRN, modality="CT", service_code="71260",
            tier=5, control_id="C-ORU", status_key="result_status"):
    """An orphan shaped the way the listener writes one: OBX-11 under
    `result_status`, plus the tier the matcher declined at."""
    detail = {"modality": modality, "service_code": service_code, "match_tier": tier}
    if obx11 is not None:
        detail[status_key] = obx11
    return registry.orphan(control_id=control_id, mrn=mrn, detail=detail)


def _target(registry, *, mrn=MRN, modality="CT", control_id="C-ORM", service_code="71260"):
    return registry.open_loop(
        mrn=mrn, modality=modality, service_code=service_code,
        control_id=control_id, ordered_at=NOW,
    )


def _matched(registry, *, tier=3, obx11="F", mrn=MRN):
    """A loop the matcher resolved a result onto, at a recorded tier."""
    loop_id = _target(registry, mrn=mrn)
    registry.record_result(loop_id, obx11=obx11, control_id="C-ORU", match_tier=tier)
    return loop_id


def _labels(store, label_type=None):
    rows = store.labels()
    if label_type is None:
        return rows
    return [r for r in rows if r["label_type"] == label_type.value]


# ------------------------------------------- attachment applies a real result

def test_attaching_an_orphan_advances_the_target_loop(store, registry):
    target = _target(registry)
    orphan_id = _orphan(registry)

    registry.attach_orphan(orphan_id, target, actor="coord1", role="coordinator")

    assert registry.get(target).state is LoopState.RESULTED
    assert registry.get(orphan_id).state is LoopState.ATTACHED


def test_the_attached_result_is_recorded_as_a_real_result_event(store, registry):
    """Not a bespoke transition. The event a coordinator's attachment produces on
    the target is the same `resulted` event an ORU produces, carrying the same
    OBX-11 -- which is what makes every rule keyed on it keep working."""
    target = _target(registry)
    registry.attach_orphan(_orphan(registry, obx11="F"), target, actor="a", role="r")

    events = store.events_for(target)
    assert [e.event_type for e in events] == ["created", "resulted"]
    assert events[-1].detail["obx11"] == "F"
    # And provenance, so a later undo can tell a human's attachment from a
    # matcher's attribution.
    assert events[-1].detail["attached_from"].startswith("O-")


def test_a_preliminary_orphan_cannot_be_attached_to_acknowledge_a_loop(store, registry):
    """THE assertion of this task. Safety rule 1 must survive the manual path:
    manual attachment must not become a back door around 'a preliminary read
    never reaches ACKNOWLEDGED'."""
    target = _target(registry)
    orphan_id = _orphan(registry, obx11="P")

    registry.attach_orphan(orphan_id, target, actor="coord1", role="coordinator")

    assert registry.get(target).state is LoopState.RESULTED
    with pytest.raises(ReferralLoopError, match="no final or corrected result"):
        registry.acknowledge(target, actor="coord1", role="coordinator", control_id="C-ACK")
    assert registry.get(target).state is LoopState.RESULTED


def test_the_preliminary_refusal_survives_a_second_attachment_of_a_final(store, registry):
    """And the loop becomes acknowledgeable once a final actually arrives, so the
    refusal above is rule 1 firing rather than attachment being inert."""
    target = _target(registry)
    registry.attach_orphan(_orphan(registry, obx11="P"), target, actor="a", role="r")
    registry.attach_orphan(
        _orphan(registry, obx11="F", control_id="C-ORU2"), target, actor="a", role="r"
    )

    registry.acknowledge(target, actor="a", role="r", control_id="C-ACK")
    assert registry.get(target).state is LoopState.ACKNOWLEDGED


def test_the_orphan_obx11_is_read_from_the_orphaned_event_not_the_result_events(registry):
    """Task 6's trap, asserted rather than remembered. `_latest_result_status`
    correctly returns "" for an orphan, because `orphaned` is not a result event
    -- so an implementation using it would treat every attached orphan as
    statusless and refuse (or worse, default) every attachment."""
    orphan_id = _orphan(registry, obx11="P")

    assert registry._latest_result_status(orphan_id) == "", "the trap is still live"
    assert registry._orphan_result_status(orphan_id) == "P"


def test_an_orphan_written_with_the_obx11_key_is_still_readable(registry):
    """The listener writes `result_status`; hand-built orphans in this codebase
    write `obx11`. Both are accepted, because a disagreement between the two
    names would refuse real attachments rather than merely read nothing."""
    orphan_id = _orphan(registry, obx11="C", status_key="obx11")
    target = _target(registry)

    registry.attach_orphan(orphan_id, target, actor="a", role="r")
    assert registry.get(target).state is LoopState.RESULTED


def test_an_orphan_with_no_readable_status_is_refused_never_defaulted(store, registry):
    """Defaulting an unreadable OBX-11 to final would advance a loop off the
    awaiting-result queue on a status nobody could read. Refusing leaves the
    orphan exactly where a coordinator can see it."""
    target = _target(registry)
    for bad in (None, "", "X"):
        orphan_id = _orphan(registry, obx11=bad)
        with pytest.raises(ReferralLoopError, match="no readable OBX-11"):
            registry.attach_orphan(orphan_id, target, actor="a", role="r")
        assert registry.get(orphan_id).state is LoopState.ORPHAN
        assert registry.get(target).state is LoopState.OPEN
    assert _labels(store) == [], "a refused attachment teaches nothing"


# --------------------------------------------------- which targets are legal

@pytest.mark.parametrize("state", ["OPEN", "SCHEDULED", "RESULTED", "ACKNOWLEDGED"])
def test_attachment_is_legal_onto_a_loop_still_able_to_take_a_result(store, registry, state):
    """OPEN and SCHEDULED advance. RESULTED accepts a second result -- which is
    how a final attaches to a loop already holding the preliminary. ACKNOWLEDGED
    reopens under safety rule 2, because a result arriving on a settled loop
    supersedes the read that was settled."""
    target = _target(registry)
    if state == "SCHEDULED":
        registry.schedule(target, control_id="C-SIU")
    if state in ("RESULTED", "ACKNOWLEDGED"):
        registry.record_result(target, obx11="F", control_id="C-ORU0")
    if state == "ACKNOWLEDGED":
        registry.acknowledge(target, actor="a", role="r", control_id="C-ACK")

    registry.attach_orphan(_orphan(registry), target, actor="a", role="r")

    assert registry.get(target).state is LoopState.RESULTED
    if state == "ACKNOWLEDGED":
        assert registry.get(target).ack_at is None, "rule 2: the acknowledgement is cleared"


def test_attachment_onto_a_cancelled_loop_is_refused(store, registry):
    """Failure matrix: a result for a CANCELLED loop is an orphan plus a flag,
    and a coordinator asserting otherwise does not change that -- the order was
    withdrawn, so a result arriving against it needs a human, not a transition."""
    target = _target(registry)
    registry.cancel(target, control_id="C-CANCEL")
    orphan_id = _orphan(registry)

    with pytest.raises(ReferralLoopError, match="CANCELLED"):
        registry.attach_orphan(orphan_id, target, actor="a", role="r")
    assert registry.get(orphan_id).state is LoopState.ORPHAN, "the orphan is untouched"


@pytest.mark.parametrize("terminal", ["ORPHAN", "DISMISSED", "ATTACHED"])
def test_attachment_onto_a_record_that_is_itself_a_result_is_refused(store, registry, terminal):
    """An orphan, a dismissed orphan and an already-attached orphan are results,
    not expectations. Chaining onto one would build a queue of records nobody
    ordered pointing at each other, with no real loop at the end."""
    victim = _orphan(registry, control_id="C-VICTIM")
    if terminal == "DISMISSED":
        registry.dismiss_orphan(victim, actor="a", role="r", reason="misrouted")
    if terminal == "ATTACHED":
        registry.attach_orphan(victim, _target(registry), actor="a", role="r")

    with pytest.raises(ReferralLoopError):
        registry.attach_orphan(_orphan(registry, control_id="C-2"), victim, actor="a", role="r")
    assert registry.get(victim).state is LoopState[terminal]


def test_the_same_orphan_cannot_be_attached_twice(store, registry):
    first, second = _target(registry), _target(registry, control_id="C-ORM2")
    orphan_id = _orphan(registry)
    registry.attach_orphan(orphan_id, first, actor="a", role="r")

    with pytest.raises(ReferralLoopError, match="Only an orphan can be attached"):
        registry.attach_orphan(orphan_id, second, actor="a", role="r")

    assert registry.get(second).state is LoopState.OPEN
    assert len(_labels(store, LabelType.ORPHAN_ATTACHED)) == 1


def test_an_orphan_cannot_be_attached_to_itself(store, registry):
    orphan_id = _orphan(registry)
    with pytest.raises(ReferralLoopError, match="to itself"):
        registry.attach_orphan(orphan_id, orphan_id, actor="a", role="r")
    assert registry.get(orphan_id).state is LoopState.ORPHAN


def test_a_record_that_is_not_an_orphan_cannot_be_attached(store, registry):
    source, target = _target(registry), _target(registry, control_id="C-ORM2")
    with pytest.raises(ReferralLoopError, match="Only an orphan can be attached"):
        registry.attach_orphan(source, target, actor="a", role="r")
    assert registry.get(target).state is LoopState.OPEN


def test_attachment_needs_a_named_actor_and_role(store, registry):
    target, orphan_id = _target(registry), _orphan(registry)
    for actor, role in (("", "r"), ("a", "")):
        with pytest.raises(ReferralLoopError, match="named actor and role"):
            registry.attach_orphan(orphan_id, target, actor=actor, role=role)
    assert registry.get(orphan_id).state is LoopState.ORPHAN


def test_a_missing_loop_on_either_side_raises_loop_not_found(store, registry):
    target, orphan_id = _target(registry), _orphan(registry)
    with pytest.raises(LoopNotFoundError):
        registry.attach_orphan("O-000000000000", target, actor="a", role="r")
    with pytest.raises(LoopNotFoundError):
        registry.attach_orphan(orphan_id, "L-000000000000", actor="a", role="r")
    assert registry.get(orphan_id).state is LoopState.ORPHAN
    assert registry.get(target).state is LoopState.OPEN


# ------------------------------------------------- the queue, and the record

def test_an_attached_orphan_leaves_the_queue_but_stays_in_the_record(store, registry, http):
    """A coordinator must not be shown the same orphan again -- an orphan queue
    that never shrinks is one they stop opening -- while the record itself has to
    survive, because the whole log is the audit."""
    target = _target(registry)
    orphan_id = _orphan(registry)
    assert [loop.loop_id for loop in store.loops_in_states([LoopState.ORPHAN])] == [orphan_id]

    registry.attach_orphan(orphan_id, target, actor="a", role="r")

    assert store.loops_in_states([LoopState.ORPHAN]) == []
    queues = http.get("/worklist/?format=json").get_json()["queues"]
    assert queues["orphans"] == []
    assert [row["loop_id"] for row in queues["awaiting_acknowledgement"]] == [target]

    events = [e.event_type for e in store.events_for(orphan_id)]
    assert events == ["orphaned", "attached"]
    assert store.replay(orphan_id).state is LoopState.ATTACHED


def test_an_attached_orphan_is_not_a_match_candidate_or_a_dismissal_candidate(store, registry):
    """It is terminal in both directions: a redelivered result must not re-match
    it, and dismissing it would inflate the dismissal rate spec section 5 watches
    for feed drift with work a coordinator actually completed."""
    from referral_loop.matcher import MATCHABLE_STATES

    orphan_id = _orphan(registry)
    registry.attach_orphan(orphan_id, _target(registry), actor="a", role="r")

    assert LoopState.ATTACHED not in MATCHABLE_STATES
    assert store.loops_in_states(MATCHABLE_STATES) != []
    assert orphan_id not in [loop.loop_id for loop in store.loops_in_states(MATCHABLE_STATES)]
    with pytest.raises(ReferralLoopError, match="Only an orphan can be dismissed"):
        registry.dismiss_orphan(orphan_id, actor="a", role="r", reason="x")
    assert _labels(store, LabelType.ORPHAN_DISMISSED) == []


def test_a_result_cannot_be_recorded_against_an_attached_record(registry):
    orphan_id = _orphan(registry)
    registry.attach_orphan(orphan_id, _target(registry), actor="a", role="r")
    with pytest.raises(ReferralLoopError, match="ATTACHED"):
        registry.record_result(orphan_id, obx11="F", control_id="C-LATE")


def test_state_is_reconstructible_from_the_event_log_after_attach_and_undo(tmp_path):
    """Spec 10.5, over the two new event types. A fresh store over the same file
    replays both to the same states, with nothing but loop_events consulted."""
    db = tmp_path / "loops.db"
    reg = Registry(LoopStore(db))
    target = _target(reg)
    orphan_id = _orphan(reg)
    reg.attach_orphan(orphan_id, target, actor="a", role="r")
    detached = reg.undo_match(target, actor="a", role="r", reason="wrong loop")

    reopened = LoopStore(db)
    assert reopened.replay(orphan_id).state is LoopState.ATTACHED
    assert reopened.replay(target).state is LoopState.OPEN
    assert reopened.replay(detached).state is LoopState.ORPHAN


# ------------------------------------------------------------- undoing a match

def test_undoing_a_match_returns_the_loop_to_the_awaiting_result_queue(store, registry, http):
    loop_id = _matched(registry)
    assert [r["loop_id"] for r in http.get("/worklist/?format=json").get_json()
            ["queues"]["awaiting_acknowledgement"]] == [loop_id]

    registry.undo_match(loop_id, actor="a", role="r", reason="belongs to another order")

    assert registry.get(loop_id).state is LoopState.OPEN
    queues = http.get("/worklist/?format=json").get_json()["queues"]
    assert [r["loop_id"] for r in queues["awaiting_result"]] == [loop_id]
    assert queues["awaiting_acknowledgement"] == []


def test_undoing_a_match_returns_the_result_to_the_orphan_queue(store, registry):
    """A false match is still a real result. Detaching it without re-queueing it
    would leave it in the raw archive alone -- which no coordinator reads and no
    queue shows -- in a system whose purpose is not losing results."""
    loop_id = _matched(registry, tier=4)

    detached = registry.undo_match(loop_id, actor="a", role="r", reason="wrong patient's order")

    orphan = registry.get(detached)
    assert orphan.state is LoopState.ORPHAN
    assert [loop.loop_id for loop in store.loops_in_states([LoopState.ORPHAN])] == [detached]
    detail = store.events_for(detached)[0].detail
    assert detail["detached_from"] == loop_id
    assert detail["result_status"] == "F"
    assert detail["match_tier"] == 4
    # The strongest identifiers are deliberately absent: attributing the wrong
    # accession to a result would manufacture the next false match.
    assert "placer_order_number" not in detail and "filler_order_number" not in detail


def test_the_detached_result_can_then_be_attached_to_the_right_loop(store, registry):
    """The flywheel closing: a false match, undone, re-homed by a human -- two
    labels out of one interface quirk."""
    wrong = _matched(registry)
    right = _target(registry, control_id="C-ORM2")

    detached = registry.undo_match(wrong, actor="a", role="r", reason="wrong order")
    registry.attach_orphan(detached, right, actor="a", role="r")

    assert registry.get(wrong).state is LoopState.OPEN
    assert registry.get(right).state is LoopState.RESULTED
    assert [r["outcome"] for r in store.labels()] == [
        LabelOutcome.FALSE_MATCH.value, LabelOutcome.MISSED_MATCH.value
    ]


@pytest.mark.parametrize("state", ["OPEN", "SCHEDULED", "CANCELLED", "ACKNOWLEDGED", "ORPHAN"])
def test_a_match_can_only_be_undone_from_resulted(store, registry, state):
    """Including ACKNOWLEDGED, which is the interesting one: a human vouched for
    that match, and undoing it without an explicit reversal would discard their
    confirmation with no `reversed` event to show it happened."""
    if state == "ORPHAN":
        loop_id = _orphan(registry)
    else:
        loop_id = _target(registry)
        if state == "SCHEDULED":
            registry.schedule(loop_id, control_id="C-SIU")
        if state == "CANCELLED":
            registry.cancel(loop_id, control_id="C-CANCEL")
        if state == "ACKNOWLEDGED":
            registry.record_result(loop_id, obx11="F", control_id="C-ORU")
            registry.acknowledge(loop_id, actor="a", role="r", control_id="C-ACK")

    with pytest.raises(ReferralLoopError, match="Cannot undo a match"):
        registry.undo_match(loop_id, actor="a", role="r", reason="w")

    assert registry.get(loop_id).state is LoopState[state]
    assert _labels(store) == [], "a refused undo is not a label"
    assert store.loops_in_states([LoopState.ORPHAN]) == ([] if state != "ORPHAN"
                                                         else [registry.get(loop_id)])


def test_undoing_a_match_needs_an_actor_role_and_reason(store, registry):
    loop_id = _matched(registry)
    for actor, role, reason in (("", "r", "w"), ("a", "", "w"), ("a", "r", "")):
        with pytest.raises(ReferralLoopError, match="named actor, role and reason"):
            registry.undo_match(loop_id, actor=actor, role=role, reason=reason)
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_the_acknowledged_case_is_recoverable_in_two_recorded_steps(store, registry):
    """The refusal above is not a dead end. Reversing first records the human's
    withdrawal, undoing then records the matcher's error -- and a false match
    somebody signed off is exactly the case that deserves both entries."""
    loop_id = _matched(registry, tier=1)
    registry.acknowledge(loop_id, actor="a", role="r", control_id="C-ACK")

    registry.reverse_acknowledgement(loop_id, actor="a", role="r", reason="not mine")
    registry.undo_match(loop_id, actor="a", role="r", reason="matcher had the wrong order")

    assert registry.get(loop_id).state is LoopState.OPEN
    assert [r["label_type"] for r in store.labels()] == [
        LabelType.ACKNOWLEDGEMENT_REVERSED.value, LabelType.MATCH_UNDONE.value
    ]
    assert [e.event_type for e in store.events_for(loop_id)] == [
        "created", "resulted", "acknowledged", "reversed", "unmatched"
    ]


def test_undoing_a_match_on_an_attached_result_is_not_a_matcher_false_positive(store, registry):
    """The distinction the release gate depends on. A result a coordinator
    attached and then detached is a human's mistake; counting it as a false match
    would let a mis-click veto a pack release under section 7's absolute rule."""
    target = _target(registry)
    registry.attach_orphan(_orphan(registry), target, actor="a", role="r")

    registry.undo_match(target, actor="a", role="r", reason="I attached the wrong one")

    undo = _labels(store)[-1]
    assert undo["label_type"] == LabelType.ATTACHMENT_UNDONE.value
    assert undo["outcome"] == LabelOutcome.MISTAKEN_ATTACHMENT.value
    assert undo["tier"] is None, "no tier produced it; a human did"
    assert [r for r in store.labels() if r["outcome"] == LabelOutcome.FALSE_MATCH.value] == []


def test_undoing_a_match_never_auto_matched_records_no_tier_but_still_labels(store, registry):
    """A result recorded with no tier -- a replay, a hand-built loop, a listener
    older than this task. The label is still a false match, because the matcher
    is still what put the result there; only the feature naming which rule
    misfired is missing, and it is recorded as missing rather than invented."""
    loop_id = _target(registry)
    registry.record_result(loop_id, obx11="F", control_id="C-ORU")  # no match_tier

    registry.undo_match(loop_id, actor="a", role="r", reason="wrong order")

    label = _labels(store)[-1]
    assert label["label_type"] == LabelType.MATCH_UNDONE.value
    assert label["outcome"] == LabelOutcome.FALSE_MATCH.value
    assert label["tier"] is None


# ------------------------------------------------------------------- labels

def test_an_attachment_records_a_missed_match_label(store, registry):
    target = _target(registry)
    registry.attach_orphan(
        _orphan(registry, modality="CT", service_code="71260", tier=3),
        target, actor="coord1", role="referral_coordinator",
    )

    rows = store.labels()
    assert len(rows) == 1
    assert rows[0]["label_type"] == LabelType.ORPHAN_ATTACHED.value
    assert rows[0]["outcome"] == LabelOutcome.MISSED_MATCH.value
    assert rows[0]["loop_id"] == target
    assert rows[0]["modality"] == "CT"
    assert rows[0]["service_code"] == "71260"
    assert rows[0]["tier"] == 3, "the tier the matcher declined at is the feature that matters"
    assert rows[0]["actor_role"] == "referral_coordinator"
    assert rows[0]["pack_version"] == PACK.version


def test_an_undone_auto_match_records_a_false_match_label_carrying_its_tier(store, registry):
    loop_id = _matched(registry, tier=4)

    registry.undo_match(loop_id, actor="a", role="referral_coordinator", reason="wrong order")

    rows = store.labels()
    assert len(rows) == 1
    assert rows[0]["label_type"] == LabelType.MATCH_UNDONE.value
    assert rows[0]["outcome"] == LabelOutcome.FALSE_MATCH.value
    assert rows[0]["tier"] == 4, "which rule misfired is the whole point of the label"
    assert rows[0]["loop_id"] == loop_id


def test_the_tier_on_a_false_match_label_comes_from_the_matcher_not_the_caller(tmp_path):
    """End to end through the listener, and the reason it has to be.

    Every other test here hands `record_result` a tier, which proves only that
    the store round-trips an integer -- the test supplies its own answer. Caught
    by mutation: deleting `match_tier=outcome.tier` from the listener left every
    assertion in this file green while the most valuable label the system
    produces silently stopped saying which rule misfired.
    """
    from referral_loop.listener import MessageHandler

    store = LoopStore(tmp_path / "loops.db")
    reg = Registry(store, pack_version=PACK.version)
    handler = MessageHandler(store=store, registry=reg, pack=PACK)
    for message in (_E2E_ORDER, _E2E_RESULT):
        assert "|AA|" in handler.handle(message)
    loop_id = next(loop.loop_id for loop in store.all_loops() if loop.state is LoopState.RESULTED)

    detached = reg.undo_match(loop_id, actor="a", role="r", reason="wrong order")

    label = store.labels()[0]
    assert label["label_type"] == LabelType.MATCH_UNDONE.value
    # _E2E_RESULT carries OBR-2 = PLACER1, the placer number on _E2E_ORDER, so
    # the matcher fires at tier 1. The label has to say so without being told.
    assert label["tier"] == 1
    assert store.events_for(detached)[0].detail["match_tier"] == 1


def test_a_reversal_is_labeled_but_is_not_a_false_match(store, registry):
    """Spec rule 4 calls a reversal a labeled false positive and section 7 says
    the same of an undone auto-match. Read as one rule, a coordinator's mis-click
    would put clerical error into the metric that is an absolute release veto."""
    loop_id = _matched(registry, tier=2)
    registry.acknowledge(loop_id, actor="a", role="r", control_id="C-ACK")

    registry.reverse_acknowledgement(loop_id, actor="a", role="coordinator", reason="not mine")

    rows = store.labels()
    assert len(rows) == 1
    assert rows[0]["label_type"] == LabelType.ACKNOWLEDGEMENT_REVERSED.value
    assert rows[0]["outcome"] == LabelOutcome.ACKNOWLEDGEMENT_WITHDRAWN.value
    assert rows[0]["outcome"] != LabelOutcome.FALSE_MATCH.value
    assert rows[0]["tier"] == 2
    assert registry.get(loop_id).state is LoopState.RESULTED, "the result stays attached"


def test_a_dismissal_is_a_label_too(store, registry):
    """Spec section 5: a rising dismissal rate is a feed problem to investigate
    upstream, which only holds if dismissals are counted."""
    orphan_id = _orphan(registry, tier=5, modality="MR")

    registry.dismiss_orphan(orphan_id, actor="a", role="coordinator", reason="another facility")

    rows = store.labels()
    assert len(rows) == 1
    assert rows[0]["label_type"] == LabelType.ORPHAN_DISMISSED.value
    assert rows[0]["outcome"] == LabelOutcome.NO_LOOP_HERE.value
    assert rows[0]["modality"] == "MR"
    assert rows[0]["tier"] == 5


def test_every_label_type_has_exactly_one_outcome_and_all_are_reachable(store, registry):
    """The mapping is not decoration: the release gate counts `outcome`, so a
    type with no outcome would be a coordinator judgement that silently never
    reaches the gate."""
    assert set(LABEL_OUTCOME) == set(LabelType)

    target = _target(registry)
    registry.attach_orphan(_orphan(registry), target, actor="a", role="r")           # attached
    registry.undo_match(target, actor="a", role="r", reason="w")                     # attachment
    matched = _matched(registry, mrn="MRN-2")
    registry.acknowledge(matched, actor="a", role="r", control_id="C-ACK")
    registry.reverse_acknowledgement(matched, actor="a", role="r", reason="w")       # reversed
    registry.undo_match(matched, actor="a", role="r", reason="w")                    # match
    registry.dismiss_orphan(_orphan(registry, control_id="C-D"), actor="a", role="r",
                            reason="w")                                              # dismissed

    assert {r["label_type"] for r in store.labels()} == {t.value for t in LabelType}


def test_a_label_survives_a_merge_of_the_patient_it_concerns(store, registry):
    """It has to, and it does so by not referring to the patient at all: an
    ADT^A40 rewrites which MRN a loop hangs on, and the label is keyed on a loop
    id that a merge never changes."""
    target = _target(registry, mrn="MRN-OLD")
    registry.attach_orphan(_orphan(registry, mrn="MRN-OLD"), target, actor="a", role="r")
    before = store.labels()

    moved = registry.merge_patient("MRN-OLD", "MRN-NEW", control_id="A40-1")

    assert target in moved
    assert store.labels() == before
    assert registry.get(target).mrn == "MRN-NEW"
    assert store.labels()[0]["loop_id"] == target, "still joinable to the loop it describes"


def test_the_labels_table_is_append_only(store, registry, tmp_path):
    """These rows feed a gate that vetoes a pack release on a false-match
    regression. A deletable label is a gate you pass by deleting the evidence."""
    registry.undo_match(_matched(registry), actor="a", role="r", reason="w")

    conn = sqlite3.connect(store.db_path)  # a foreign connection, no authorizer
    conn.execute("PRAGMA recursive_triggers = ON")
    try:
        for sql in ("DELETE FROM labels", "UPDATE labels SET outcome = 'missed_match'"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
    finally:
        conn.close()
    assert len(store.labels()) == 1


def test_a_label_write_failure_never_blocks_the_coordinator(store, registry, monkeypatch, caplog):
    """The same asymmetry audit.py argues. A dropped label costs a training
    example; failing closed would stop coordinators attaching orphans, and the
    queue then only grows -- the failure DISMISSED was added to prevent, caused
    by the flywheel meant to feed on it."""
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(
        store, "record_label",
        lambda *a, **k: (_ for _ in ()).throw(StoreUnavailableError("disk full at /srv/phi.db")),
    )
    target, orphan_id = _target(registry), _orphan(registry)

    registry.attach_orphan(orphan_id, target, actor="a", role="r")

    assert registry.get(target).state is LoopState.RESULTED, "the action outranks the label"
    assert registry.get(orphan_id).state is LoopState.ATTACHED
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "StoreUnavailableError" in logged
    assert "/srv/phi.db" not in logged, "the type, never str(exc), which carries the db path"


# ------------------------------------------------------- what a label may hold

def test_record_label_has_no_parameter_that_accepts_an_identifier(store):
    """The control is the absence of an argument, not a filter. A caller holding
    a patient name has nowhere to put it -- the same control audit.py uses, and
    stronger than sanitising a dict a caller composed."""
    import inspect

    params = set(inspect.signature(store.record_label).parameters)
    assert params == {"label_type", "loop_id", "modality", "service_code", "tier",
                      "actor_role", "pack_version"}
    for forbidden in ("mrn", "actor", "reason", "detail", "control_id", "accession",
                      "placer_order_number", "filler_order_number", "note"):
        assert forbidden not in params


def test_a_loop_id_this_system_never_minted_is_not_echoed_into_a_label(store):
    """A coordinator's browser can POST to /worklist/<anything>/attach, so the
    id reaching a label is caller-controlled on the refusal paths."""
    store.record_label(LabelType.ORPHAN_DISMISSED, loop_id=f"{MRN_SENTINEL}")
    store.record_label(LabelType.ORPHAN_DISMISSED, loop_id="L-000000000000")

    assert [r["loop_id"] for r in store.labels()] == ["unminted", "L-000000000000"]


def test_a_value_that_does_not_look_like_a_code_is_dropped(store):
    """modality and service_code are the two message-derived values a label may
    hold, and they are coded values: short, from a coded charset. A name, an
    address or a note is none of those."""
    store.record_label(
        LabelType.ORPHAN_ATTACHED, loop_id="L-000000000000",
        modality=f"SMITH^JANE {MRN_SENTINEL} 1980-01-01, 555 ELM ST",
        service_code="x" * 64,
    )
    row = store.labels()[0]
    assert row["modality"] == "" and row["service_code"] == ""


def test_a_role_is_bounded_and_single_lined(store):
    store.record_label(
        LabelType.ORPHAN_ATTACHED, loop_id="L-000000000000",
        actor_role="referral\n coordinator " + "x" * 200,
    )
    role = store.labels()[0]["actor_role"]
    assert len(role) == 64 and "\n" not in role and role.startswith("referral coordinator ")


@pytest.mark.parametrize("tier,expected", [(3, 3), (0, None), (6, None), ("3", None),
                                           (None, None), (True, None)])
def test_a_tier_outside_the_five_the_matcher_has_is_recorded_as_unknown(store, tier, expected):
    store.record_label(LabelType.ORPHAN_ATTACHED, loop_id="L-000000000000", tier=tier)
    assert store.labels()[0]["tier"] == expected


def test_a_pack_version_that_did_not_come_from_a_verified_pack_is_unknown(store):
    store.record_label(LabelType.ORPHAN_ATTACHED, loop_id="L-000000000000",
                       pack_version=f"1.0.0 {MRN_SENTINEL}\n{'x' * 300}")
    assert store.labels()[0]["pack_version"] == "unknown"


def test_an_unknown_label_type_is_refused_rather_than_stored(store):
    """A label the eval harness cannot interpret is worse than no label: it
    would be counted as *something*."""
    with pytest.raises(ReferralLoopError, match="unknown type"):
        store.record_label("false_positive", loop_id="L-000000000000")
    assert store.labels() == []


def test_the_label_loop_ref_rule_still_agrees_with_the_audit_one(store):
    """Duplicated deliberately -- a change to one must not silently widen the
    other -- so the duplication is checked rather than trusted."""
    assert store_module._LABEL_LOOP_REF_RE.pattern == audit._LOOP_REF_RE.pattern
    assert store_module._LABEL_PACK_VERSION_RE.pattern == audit._PACK_VERSION_RE.pattern
    assert store_module._UNMINTED_LOOP_REF == audit.UNMINTED
    assert store_module._UNKNOWN_PACK_VERSION == audit.UNKNOWN_VERSION


def test_a_label_carries_a_date_and_not_a_timestamp(store):
    """An exact time on an exportable row pins a label to the hour a study
    resulted. Ordering is label_id's job; the bucket that matters is the pack."""
    store.record_label(LabelType.ORPHAN_ATTACHED, loop_id="L-000000000000")
    row = store.labels()[0]
    assert "created_at" not in row
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["created_date"])


# --------------------------------------------------- PHI: the export on disk

def _export_bytes(store, path) -> bytes:
    """Write the labels a site would contribute to their own SQLite file.

    The bytes rather than the dicts: an implementation that built clean rows and
    then wrote something else would pass every assertion made against its own
    return values. It has to be a separate file because `labels` shares
    loops.db with `raw_messages`, which holds every message verbatim -- grepping
    that file would report a leak for every sentinel in the archive, which is
    exactly the proof that cannot fail.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE labels (label_id INTEGER, label_type TEXT, outcome TEXT, "
            "loop_id TEXT, modality TEXT, service_code TEXT, tier INTEGER, "
            "actor_role TEXT, pack_version TEXT, created_date TEXT)"
        )
        for row in store.labels():
            conn.execute(
                "INSERT INTO labels VALUES (?,?,?,?,?,?,?,?,?,?)",
                tuple(row[k] for k in (
                    "label_id", "label_type", "outcome", "loop_id", "modality",
                    "service_code", "tier", "actor_role", "pack_version", "created_date",
                )),
            )
        conn.commit()
    finally:
        conn.close()
    return path.read_bytes()


def _leaks(blob, sentinels) -> list[str]:
    haystack = blob if isinstance(blob, bytes) else blob.encode()
    return sorted(k for k, v in sentinels.items() if v.encode() in haystack)


def _drive_every_labelling_action(tmp_path, sentinels):
    """Real HL7 in, every label out. Sentinels in PID/NK1/GT1/NTE arrive through
    the listener; the actor name and the free-text reason are sentinels too,
    because those are the channels a coordinator opens by typing."""
    from referral_loop.listener import MessageHandler

    store = LoopStore(tmp_path / "loops.db")
    reg = Registry(store, pack_version=PACK.version)
    handler = MessageHandler(store=store, registry=reg, pack=PACK)
    for message in (_E2E_ORDER, _E2E_RESULT, _E2E_UNMATCHED):
        assert "|AA|" in handler.handle(message)

    matched = [loop for loop in store.all_loops() if loop.state is LoopState.RESULTED]
    orphans = [loop for loop in store.all_loops() if loop.state is LoopState.ORPHAN]
    assert matched and orphans, "the fixtures did not exercise both paths"

    reason = f"belongs to {sentinels['PID_NAME']}, kin {sentinels['NK1_NAME']}, {REASON_SENTINEL}"
    actor = f"{ACTOR_SENTINEL} {sentinels['GT1_NAME']}"

    # Every label type, in an order that reaches all five: the matcher's own
    # attribution undone, an orphan attached in its place, that acknowledged and
    # reversed, the attachment then undone, and the detached result dismissed.
    loop_id = matched[0].loop_id
    reg.undo_match(loop_id, actor=actor, role="coordinator", reason=reason)
    reg.attach_orphan(orphans[0].loop_id, loop_id, actor=actor, role="coordinator")
    reg.acknowledge(loop_id, actor=actor, role="coordinator", control_id="C-ACK")
    reg.reverse_acknowledgement(loop_id, actor=actor, role="coordinator", reason=reason)
    detached = reg.undo_match(loop_id, actor=actor, role="coordinator", reason=reason)
    reg.dismiss_orphan(detached, actor=actor, role="coordinator", reason=reason)

    # And a hostile id straight off a URL, through the HTTP surface.
    app = create_app(store=store, registry=reg, pack=PACK)
    app.config["TESTING"] = True
    client = app.test_client()
    responses = [
        client.post(f"/worklist/{sentinels['PID_MRN']}/attach",
                    json={"target_loop_id": sentinels["PID_NAME"], "actor": actor,
                          "role": "coordinator"}),
        client.post(f"/worklist/{sentinels['PID_MRN']}/undo_match",
                    json={"actor": actor, "role": "coordinator", "reason": reason}),
    ]
    return store, [r.data.decode() for r in responses]


def test_no_sentinel_reaches_the_label_export_on_disk(tmp_path, caplog):
    """The labels table is the artifact designed to leave the building, so it
    gets the tightest rule in the system: nothing identifying, nothing free-text,
    nothing an actor typed except their role."""
    caplog.set_level(logging.DEBUG)
    sentinels = {**_E2E_SENTINELS, "ACTOR": ACTOR_SENTINEL, "REASON": REASON_SENTINEL}
    store, responses = _drive_every_labelling_action(tmp_path, sentinels)

    # Every label type this task can produce; the acknowledgement is not one,
    # because it asserts that nothing was wrong.
    assert len(store.labels()) == 5, "the drive did not actually produce labels"
    assert {r["label_type"] for r in store.labels()} == {t.value for t in LabelType}
    assert _leaks(_export_bytes(store, tmp_path / "contribution.db"), sentinels) == []
    assert _leaks(json.dumps(store.labels()), sentinels) == []
    # The other artifacts this task adds: two HTTP responses and the log records.
    for body in responses:
        assert _leaks(body, sentinels) == []
    assert _leaks("\n".join(r.getMessage() for r in caplog.records), sentinels) == []


def test_the_label_export_grep_can_actually_detect_a_leak(tmp_path, caplog):
    """A PHI proof that cannot fail is the failure mode this task exists to
    prevent. The same scan that just cleared the export is run again after a row
    carrying a sentinel is written straight past the normalisers -- which is
    exactly what a contributor inserting into `labels` directly would do."""
    caplog.set_level(logging.DEBUG)
    sentinels = {**_E2E_SENTINELS, "ACTOR": ACTOR_SENTINEL, "REASON": REASON_SENTINEL}
    store, _ = _drive_every_labelling_action(tmp_path, sentinels)
    assert _leaks(_export_bytes(store, tmp_path / "clean.db"), sentinels) == [], "not clean first"

    conn = sqlite3.connect(store.db_path)  # straight past record_label
    try:
        conn.execute(
            "INSERT INTO labels (label_type, outcome, loop_id, modality, service_code, "
            "actor_role, pack_version, created_date) VALUES (?,?,?,?,?,?,?,?)",
            ("orphan_attached", "missed_match", "L-000000000000",
             sentinels["PID_NAME"], "71260", f"coordinator for {ACTOR_SENTINEL}", "test",
             "2026-07-25"),
        )
        conn.commit()
    finally:
        conn.close()

    assert _leaks(_export_bytes(store, tmp_path / "dirty.db"), sentinels) == ["ACTOR", "PID_NAME"]
    assert _leaks(json.dumps(store.labels()), sentinels) == ["ACTOR", "PID_NAME"]


def test_the_reason_a_coordinator_typed_stays_in_the_event_log_and_reaches_no_label(
    store, registry
):
    """The single most useful thing an auditor could read, and free text a human
    typed about a patient. It stays where the audit needs it."""
    loop_id = _matched(registry)
    reason = f"this is {REASON_SENTINEL}, it belongs to another patient"

    registry.undo_match(loop_id, actor=ACTOR_SENTINEL, role="coordinator", reason=reason)

    assert REASON_SENTINEL in store.events_for(loop_id)[-1].detail["unmatched_reason"]
    blob = json.dumps(store.labels())
    assert REASON_SENTINEL not in blob and ACTOR_SENTINEL not in blob
    assert store.labels()[0]["actor_role"] == "coordinator"


# ------------------------------------------------------------------- the audit

def _audit_rows(action):
    return [r for r in referral_audit_entries()
            if json.loads(r["detail"])["action"] == action.value]


def test_an_attachment_and_an_undo_each_append_one_audit_row(store, registry):
    target, orphan_id = _target(registry), _orphan(registry)

    registry.attach_orphan(orphan_id, target, actor="Ada Coordinator", role="coordinator")
    registry.undo_match(target, actor="Ada Coordinator", role="coordinator", reason="wrong")

    attached = _audit_rows(AuditAction.ORPHAN_ATTACHED)
    undone = _audit_rows(AuditAction.MATCH_UNDONE)
    assert len(attached) == 1 and len(undone) == 1
    assert attached[0]["outcome"] == "success"
    assert attached[0]["resource_id"] == target, "the loop whose state changed"
    assert attached[0]["actor"] == "Ada Coordinator"
    assert json.loads(undone[0]["detail"])["reason_recorded"] is True
    assert "wrong" not in json.dumps(undone[0]), "the reason itself never reaches the audit"


def test_a_refused_attachment_is_audited_with_a_named_refusal(store, registry):
    """A refused action is half of what the audit exists to record."""
    target = _target(registry)
    registry.cancel(target, control_id="C-CANCEL")
    with pytest.raises(ReferralLoopError):
        registry.attach_orphan(_orphan(registry), target, actor="a", role="r")
    with pytest.raises(ReferralLoopError):
        registry.attach_orphan(_orphan(registry, obx11="X", control_id="C-2"),
                               _target(registry, control_id="C-ORM2"), actor="a", role="r")

    rows = _audit_rows(AuditAction.ORPHAN_ATTACHED)
    assert [r["outcome"] for r in rows] == ["denied", "denied"]
    assert json.loads(rows[1]["detail"])["refusal"] == "unreadable_result_status"


# ------------------------------------------------------------- the HTTP surface

def test_attach_endpoint_requires_actor_role_and_target(http, registry):
    target, orphan_id = _target(registry), _orphan(registry)

    for body in ({"target_loop_id": target}, {"target_loop_id": target, "actor": "c1"},
                 {"actor": "c1", "role": "r"}, {"target_loop_id": target, "actor": "  ",
                                                "role": "r"}):
        assert http.post(f"/worklist/{orphan_id}/attach", json=body).status_code == 400

    good = http.post(f"/worklist/{orphan_id}/attach",
                     json={"target_loop_id": target, "actor": "c1", "role": "coordinator"})
    assert good.status_code == 200
    assert good.get_json()["state"] == "RESULTED"
    assert registry.get(orphan_id).state is LoopState.ATTACHED


def test_undo_match_endpoint_requires_actor_role_and_reason(http, registry):
    loop_id = _matched(registry)
    for body in ({"actor": "c1"}, {"actor": "c1", "role": "r"}, {"role": "r", "reason": "w"}):
        assert http.post(f"/worklist/{loop_id}/undo_match", json=body).status_code == 400

    good = http.post(f"/worklist/{loop_id}/undo_match",
                     json={"actor": "c1", "role": "r", "reason": "wrong order"})
    assert good.status_code == 200
    assert good.get_json()["state"] == "OPEN"
    assert good.get_json()["detached_to"].startswith("O-")


def test_a_refused_attachment_is_a_409_and_a_missing_loop_is_a_404(http, registry):
    """The distinction the route pattern exists for: a missing loop is 'no such
    thing', not 'refused', and the 409 path would call registry.get on it."""
    target = _target(registry)
    registry.cancel(target, control_id="C-CANCEL")
    orphan_id = _orphan(registry)

    refused = http.post(f"/worklist/{orphan_id}/attach",
                        json={"target_loop_id": target, "actor": "c", "role": "r"})
    assert refused.status_code == 409

    for body in ({"target_loop_id": "L-000000000000", "actor": "c", "role": "r"},):
        missing = http.post(f"/worklist/{orphan_id}/attach", json=body)
        assert missing.status_code == 404
    assert http.post("/worklist/L-000000000000/undo_match",
                     json={"actor": "c", "role": "r", "reason": "w"}).status_code == 404
    assert http.post(f"/worklist/{orphan_id}/undo_match",
                     json={"actor": "c", "role": "r", "reason": "w"}).status_code == 409


def test_no_refusal_body_echoes_an_id_this_system_never_minted(http):
    """Found by probing this task. The 409 body carries the registry's message,
    which is composed from the two loop ids -- so a refusal that could fire
    before either id was validated would reflect whatever was POSTed. Every
    refusal path is therefore driven with a sentinel in every position."""
    bodies = [
        http.post(f"/worklist/{MRN_SENTINEL}/attach",
                  json={"target_loop_id": MRN_SENTINEL, "actor": "a", "role": "r"}),
        http.post(f"/worklist/{MRN_SENTINEL}/attach",
                  json={"target_loop_id": "L-000000000000", "actor": "a", "role": "r"}),
        http.post("/worklist/L-000000000000/attach",
                  json={"target_loop_id": MRN_SENTINEL, "actor": "a", "role": "r"}),
        http.post(f"/worklist/{MRN_SENTINEL}/undo_match",
                  json={"actor": "a", "role": "r", "reason": REASON_SENTINEL}),
        http.post("/worklist/attach", data={
            "orphan_id": MRN_SENTINEL, "target_loop_id": MRN_SENTINEL,
            "actor": "a", "role": "r"}),
    ]
    for response in bodies:
        assert response.status_code == 404, response.data
        assert MRN_SENTINEL not in response.data.decode()
        assert REASON_SENTINEL not in response.data.decode()

    # And the same scan against a deliberately contaminated copy, so "clean" is
    # a result rather than a check that cannot fail.
    assert MRN_SENTINEL in (bodies[0].data.decode() + MRN_SENTINEL)


def test_a_self_attachment_of_a_real_orphan_is_still_refused_as_a_409(http, registry):
    """Moving the guard behind the two replays must not make it unreachable."""
    orphan_id = _orphan(registry)
    response = http.post(f"/worklist/{orphan_id}/attach",
                         json={"target_loop_id": orphan_id, "actor": "a", "role": "r"})
    assert response.status_code == 409
    assert "to itself" in response.get_json()["error"]


def test_the_page_offers_both_actions_and_still_claims_nothing_clinical(http, registry):
    from tests.test_worklist import FORBIDDEN_WORDS

    _orphan(registry)
    _matched(registry, mrn="MRN-2")
    body = http.get("/worklist/").data.decode()

    assert 'action="/worklist/attach"' in body
    assert 'action="/worklist/undo_match"' in body
    assert all(word not in body.lower() for word in FORBIDDEN_WORDS)


def test_the_page_forms_actually_work(http, registry):
    """The standalone forms carry the id in the body rather than the URL, so they
    are a second entry point and are exercised as one."""
    target, orphan_id = _target(registry), _orphan(registry)

    attached = http.post("/worklist/attach", data={
        "orphan_id": orphan_id, "target_loop_id": target, "actor": "c", "role": "r"})
    assert attached.status_code == 200

    undone = http.post("/worklist/undo_match", data={
        "loop_id": target, "actor": "c", "role": "r", "reason": "wrong order"})
    assert undone.status_code == 200
    assert registry.get(target).state is LoopState.OPEN


# --------------------------------------------------------------- concurrency

def test_two_coordinators_attaching_one_orphan_to_two_loops_produce_one_attachment(stack):
    """Check-then-append is worthless if another thread appends between the two.
    Two coordinators working the same queue is not hypothetical -- it is what a
    shared worklist is for -- and the loser must be refused, not silently
    duplicated onto a second loop."""
    store, registry, _ = stack
    orphan_id = _orphan(registry)
    targets = [_target(registry, control_id="C-A"), _target(registry, control_id="C-B")]
    barrier = threading.Barrier(len(targets))
    results: list = []

    def attach(target):
        barrier.wait()
        try:
            registry.attach_orphan(orphan_id, target, actor="a", role="r")
            results.append(("ok", target))
        except ReferralLoopError as exc:
            results.append(("refused", type(exc).__name__))

    threads = [threading.Thread(target=attach, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(r[0] for r in results) == ["ok", "refused"]
    winner = next(t for status, t in results if status == "ok")
    assert registry.get(orphan_id).state is LoopState.ATTACHED
    assert registry.get(winner).state is LoopState.RESULTED
    loser = next(t for t in targets if t != winner)
    assert registry.get(loser).state is LoopState.OPEN, "the loser took no result"
    assert len(_labels(store, LabelType.ORPHAN_ATTACHED)) == 1


def test_no_event_type_added_by_this_task_maps_to_closed():
    """Spec test 9, over the two new event types specifically."""
    from referral_loop.store import _EVENT_STATE

    assert _EVENT_STATE["attached"] is LoopState.ATTACHED
    assert _EVENT_STATE["unmatched"] is LoopState.OPEN
    assert LoopState.CLOSED not in _EVENT_STATE.values()


def test_an_undone_loop_ages_from_its_original_order_date(store, registry, http):
    """Staleness measures from ordered_at, and the undo must not reset that clock
    -- a loop whose false match is undone has been waiting since it was ordered,
    not since the coordinator noticed."""
    loop_id = registry.open_loop(
        mrn=MRN, modality="CT", control_id="C1",
        ordered_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    registry.record_result(loop_id, obx11="F", control_id="C2", match_tier=3)
    registry.undo_match(loop_id, actor="a", role="r", reason="wrong order")

    row = next(r for r in http.get("/worklist/?format=json").get_json()
               ["queues"]["awaiting_result"] if r["loop_id"] == loop_id)
    assert row["age_basis"] == "ordered"
    assert row["age_hours"] > 700
    assert row["is_stale"] is True
