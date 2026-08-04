"""Spec tests 1 and 2. Safety, not correctness.

The first six tests are the plan's specified set. Everything after
SPEC TESTS END is an adversarial probe: each one exists because a plausible
sequence of real messages reaches an unsafe state without it.
"""
import itertools
import threading
from datetime import datetime, timedelta, timezone

import pytest

from referral_loop.core.states import ReferralState
from referral_loop.errors import (
    LoopNotFoundError,
    ReferralLoopError,
    ReservedStateError,
    StaleMessageError,
)
from referral_loop.events import LoopEvent, LoopState
from referral_loop.registry import Registry
from referral_loop.store import LoopStore

T0 = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


@pytest.fixture()
def store(tmp_path):
    return LoopStore(tmp_path / "loops.db")


@pytest.fixture()
def registry(store):
    return Registry(store)


def test_preliminary_advances_to_resulted(registry):
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="P", control_id="C2")
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_acknowledged_is_unreachable_from_a_preliminary_read(registry):
    """Spec test 1. A prelim that later corrects is the malpractice scenario;
    auto-resolving on it would make the tool the cause."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="P", control_id="C2")

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")

    assert registry.get(loop_id).state is LoopState.RESULTED


def test_final_result_permits_acknowledgement(registry):
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_corrected_result_reopens_an_acknowledged_loop(registry):
    """Spec test 2."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED

    registry.record_result(loop_id, obx11="C", control_id="C4")
    reopened = registry.get(loop_id)
    assert reopened.state is LoopState.RESULTED
    assert reopened.ack_at is None, "reopening must clear the prior acknowledgement"


def test_result_for_a_cancelled_loop_is_orphaned(registry):
    """Failure matrix: someone cancelled an order that then produced a result."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.cancel(loop_id, control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.record_result(loop_id, obx11="F", control_id="C3")


def test_acknowledgement_records_actor_and_role(registry):
    """Spec section 4: ACKNOWLEDGED is a clerical claim -- this result belongs to
    this loop. Recording the role keeps 'and on what authority' answerable rather
    than letting the worklist imply a clinician read the finding."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    loop = registry.get(loop_id)
    assert loop.ack_by == "coord1"
    assert loop.ack_role == "coordinator"


# ---------------------------------------------------------------- SPEC TESTS END
# Adversarial probes. Each is a route to ACKNOWLEDGED, or to a loop vanishing from
# every worklist query, that the six tests above do not close.


def test_a_preliminary_arriving_after_a_final_still_blocks_acknowledgement(registry):
    """Radiology resends a prelim after the final. The newest read we hold is
    preliminary, so ACKNOWLEDGED must be unreachable until a final or correction."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.record_result(loop_id, obx11="P", control_id="C3", message_at=T2)

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")


def test_a_preliminary_after_a_correction_blocks_acknowledgement(registry):
    """Correction reopens, then a prelim lands. Still not acknowledgeable."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.record_result(loop_id, obx11="C", control_id="C4", message_at=T2)
    registry.record_result(loop_id, obx11="P", control_id="C5", message_at=T2 + timedelta(minutes=5))

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C6")
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_acknowledgement_requires_an_explicit_final_or_corrected_status(registry, store):
    """Rule 1 as an allowlist, not a denylist. A resulted event carrying no
    OBX-11 -- a restored log, a foreign writer, a future code path -- must not
    resolve the loop just because its status is not literally 'P'."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    store.append_event(LoopEvent(loop_id, "resulted", T1, "C2", {}))
    assert registry.get(loop_id).state is LoopState.RESULTED

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")


def test_acknowledgement_is_unreachable_with_no_result_at_all(registry):
    """An order nobody resulted must never be acknowledgeable."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C2")
    registry.schedule(loop_id, control_id="C3")
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")


def test_an_orphan_is_not_acknowledgeable(registry):
    """Orphans hold results nobody ordered. Acknowledging one would retire a result
    that was never attached to a patient's loop."""
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={"obx11": "F"})
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(orphan_id, actor="coord1", role="coordinator", control_id="C2")


def test_an_orphan_cannot_be_resulted_out_of_the_orphan_queue(registry):
    """ORPHAN -> RESULTED -> ACKNOWLEDGED would retire a result nobody ordered through
    the ordinary worklist, and drop it from the orphan queue the flywheel counts
    without any coordinator attaching it."""
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={"obx11": "F"})
    with pytest.raises(ReferralLoopError):
        registry.record_result(orphan_id, obx11="F", control_id="C2")
    assert registry.get(orphan_id).state is LoopState.ORPHAN


def test_reacknowledging_an_acknowledged_loop_is_refused(registry):
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord2", role="coordinator", control_id="C4")


def test_acknowledging_a_cancelled_loop_is_refused(registry):
    """Cancel then close would retire a loop that never produced a result."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.cancel(loop_id, control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    assert registry.get(loop_id).state is LoopState.CANCELLED


def test_acknowledge_requires_a_named_actor_and_role(registry):
    """A resolution attributed to nobody cannot answer who vouched for it."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="", role="coordinator", control_id="C3")
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="", control_id="C4")
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_acknowledgement_records_which_result_was_acknowledged(registry, store):
    """'Were they clinically responsible' also needs 'what did they look at'."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")

    acks = [e for e in store.events_for(loop_id) if e.event_type == "acknowledged"]
    assert acks[-1].detail["ack_result_status"] == "F"
    assert acks[-1].control_id == "C3"


def test_reopening_clears_actor_and_role_not_only_the_timestamp(registry):
    """A reopened loop still showing ack_by reads as acknowledged on any screen
    that renders the name rather than the timestamp."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.record_result(loop_id, obx11="C", control_id="C4")

    loop = registry.get(loop_id)
    assert loop.ack_by == ""
    assert loop.ack_role == ""
    assert loop.ack_at is None


def test_a_reopened_loop_is_findable_by_the_coordinator_query(registry, store):
    """Task 5 note 8: reopened maps to RESULTED, which open_loops() does not
    select. A loop reopened by safety rule 2 that no query returns is a loop
    nobody reviews -- the exact failure this product exists to prevent."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.record_result(loop_id, obx11="C", control_id="C4")

    ids = {loop.loop_id for loop in store.resulted_unacknowledged()}
    assert loop_id in ids


def test_a_repeat_result_on_an_acknowledged_loop_does_not_hide_it(registry, store):
    """A final resent afterwards moves ACKNOWLEDGED -> RESULTED. If it left ack_at
    set, the loop would be in neither open_loops() nor resulted_unacknowledged()
    -- visible on no worklist at all."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.record_result(loop_id, obx11="F", control_id="C4", message_at=T2)

    loop = registry.get(loop_id)
    assert loop.state is LoopState.RESULTED
    assert loop.ack_at is None
    ids = {found.loop_id for found in store.resulted_unacknowledged()}
    assert loop_id in ids, "a result on an acknowledged loop must return it to a worklist"


def test_record_result_for_an_unknown_loop_raises_and_creates_nothing(registry, store):
    with pytest.raises(LoopNotFoundError):
        registry.record_result("L-does-not-exist", obx11="F", control_id="C1")
    assert store.all_loops() == []
    assert store.events_for("L-does-not-exist") == []


def test_schedule_and_cancel_for_an_unknown_loop_raise(registry, store):
    with pytest.raises(LoopNotFoundError):
        registry.schedule("L-nope", control_id="C1")
    with pytest.raises(LoopNotFoundError):
        registry.cancel("L-nope", control_id="C2")
    assert store.all_loops() == []


def test_open_loop_refuses_to_reuse_an_existing_loop_id(registry):
    """A second 'created' would reset a resulted loop to OPEN and drop its
    result from view."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.open_loop(mrn="MRN1", control_id="C3", loop_id=loop_id)
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_open_loop_requires_an_mrn(registry):
    """A loop with no MRN is matched by no patient query and reviewed by nobody.
    An unattributable result belongs in the orphan queue, not in a loop."""
    with pytest.raises(ReferralLoopError):
        registry.open_loop(mrn="", control_id="C1")


def test_cancel_of_an_acknowledged_loop_is_refused(registry):
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    with pytest.raises(ReferralLoopError):
        registry.cancel(loop_id, control_id="C4")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_cancel_of_a_resulted_loop_is_refused(registry):
    """CANCELLED is in neither open_loops() nor resulted_unacknowledged(), so
    cancelling a loop that already has a result erases the result from every
    worklist. An order that produced a result cannot be un-ordered."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="P", control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.cancel(loop_id, control_id="C3")
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_schedule_of_a_resulted_loop_is_refused(registry):
    """The plan's own worked example: a SIU landing after an ORU must not
    regress RESULTED back to SCHEDULED."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.schedule(loop_id, control_id="C3")
    assert registry.get(loop_id).state is LoopState.RESULTED


# --------------------------------------------------- late-arriving older message
# store.replay() orders by arrival, deliberately. Nothing beneath the registry
# guards clinical ordering, so the registry must refuse a message whose MSH-7 is
# older than the newest message already applied -- before appending it.


def test_a_stale_result_is_refused_and_not_applied(registry, store):
    """A final that predates an applied correction would otherwise re-arm
    an acknowledgement on a superseded read, defeating safety rule 2."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.record_result(loop_id, obx11="C", control_id="C4", message_at=T2)

    before = len(store.events_for(loop_id))
    with pytest.raises(StaleMessageError):
        registry.record_result(loop_id, obx11="F", control_id="C5", message_at=T1)

    assert len(store.events_for(loop_id)) == before, "a refused message must append nothing"
    assert registry.get(loop_id).state is LoopState.RESULTED
    assert registry.get(loop_id).ack_at is None, "the correction's reopen must still stand"
    assert registry._latest_result_status(loop_id) == "C", (
        "the superseded final must not become the read that governs closing"
    )


def test_a_stale_schedule_is_refused(registry, store):
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.schedule(loop_id, control_id="C2", message_at=T2)
    before = len(store.events_for(loop_id))
    with pytest.raises(StaleMessageError):
        registry.schedule(loop_id, control_id="C3", message_at=T1)
    assert len(store.events_for(loop_id)) == before


def test_a_stale_cancel_is_refused(registry):
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.schedule(loop_id, control_id="C2", message_at=T2)
    with pytest.raises(StaleMessageError):
        registry.cancel(loop_id, control_id="C3", message_at=T1)
    assert registry.get(loop_id).state is LoopState.SCHEDULED


def test_identical_message_timestamps_are_not_stale(registry):
    """MSH-7 is routinely minute-precision, so two messages in one minute carry
    the same timestamp. Rejecting on equality would drop real results."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T1)
    registry.record_result(loop_id, obx11="P", control_id="C2", message_at=T1)
    registry.record_result(loop_id, obx11="F", control_id="C3", message_at=T1)
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_an_acknowledgement_does_not_make_a_later_correction_look_stale(registry):
    """The watermark is over message time, not wall-clock. A human acking today
    must not render tomorrow's correction -- whose MSH-7 predates the ack --
    stale, because that would silently defeat safety rule 2."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")

    registry.record_result(loop_id, obx11="C", control_id="C4", message_at=T2)
    loop = registry.get(loop_id)
    assert loop.state is LoopState.RESULTED
    assert loop.ack_at is None


def test_a_naive_message_timestamp_is_compared_as_utc(registry):
    """A parser handing back a naive MSH-7 must not raise TypeError inside the
    guard -- that would take the message down the AE path forever."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0.replace(tzinfo=None))
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1.replace(tzinfo=None))
    with pytest.raises(StaleMessageError):
        registry.record_result(loop_id, obx11="P", control_id="C3", message_at=T0)
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_an_unstamped_message_applies_to_a_loop_that_carries_no_watermark(registry):
    """The fail-open survives exactly where there is nothing to regress. A loop
    nobody has stamped has no clinical ordering to protect, so refusing here
    would refuse a whole site's traffic to defend nothing."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    assert registry.get(loop_id).state is LoopState.RESULTED


# ------------------------------------------------------ one clock-skew policy
# Three findings, one bug: an attacker-controlled timestamp consumed with no
# upper bound. MSH-7 into the watermark (below), MSH-7 omitted to disable the
# guard (below), OBR-7 into `ordered_at` (test_listener). The bound is one
# constant, clock.MAX_CLOCK_SKEW, read at every ingest site.


def _beyond_skew() -> datetime:
    """Far enough ahead that no clock is this wrong, near enough that the parse
    still reads it -- the band where the registry, not the parser, must act."""
    return datetime.now(timezone.utc) + timedelta(days=30)


def test_a_future_dated_message_does_not_advance_the_watermark(registry):
    """H1. `_clinical_watermark` is a max() over an append-only log: a stamp it
    should never have taken can never afterwards be lowered."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="P", control_id="C2", message_at=_beyond_skew())
    assert registry._clinical_watermark(loop_id) == T0


def test_a_future_dated_message_is_still_applied(registry):
    """Drop the stamp, not the message. A RIS running fast is endemic, and
    refusing its traffic would strand the results this system exists to keep."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=_beyond_skew())
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_a_dropped_stamp_is_counted(registry):
    """A silently dropped stamp is how this stayed invisible. A clock this wrong
    is an interface incident somebody has to be able to see."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=_beyond_skew())
    assert registry.future_dated_message_count == 1


def test_a_future_dated_result_cannot_deafen_a_loop_to_its_own_correction(registry):
    """H1's clinical sequence, end to end.

    A future-dated read lands, a coordinator acknowledges what looks like a
    normal final, and then the genuine amendment arrives. With the watermark
    poisoned the amendment is refused, the corrected finding exists only as a
    log line, and the worklist goes on reporting the loop handled.
    """
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.record_result(loop_id, obx11="F", control_id="C3", message_at=_beyond_skew())
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED

    registry.record_result(loop_id, obx11="C", control_id="C5", message_at=T2)

    loop = registry.get(loop_id)
    assert loop.state is LoopState.RESULTED, "the amendment must reopen the loop"
    assert not loop.ack_at, "safety rule 2 must still clear the acknowledgement"
    assert "C5" in [event.control_id for event in registry.store.events_for(loop_id)]


def test_an_unstamped_cancel_cannot_regress_a_watermarked_loop(registry):
    """H2. `CANCELLED` appears in neither open_loops() nor
    resulted_unacknowledged(), so an unstamped SIU^S15 naming a scheduled loop
    took a clinically open referral off every coordinator queue at once.

    Past tense on the S15, deliberately: it drives `unschedule` now and never arrives
    here. The guard stays on `cancel` because what it protects is destructiveness, not
    a message type, and `cancel` is still the transition that ends a referral outright
    -- so a caller plan 2c wires to it inherits the remediation rather than rediscovering
    the incident."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.schedule(loop_id, control_id="C2", message_at=T2)
    with pytest.raises(StaleMessageError):
        registry.cancel(loop_id, control_id="C3")
    assert registry.get(loop_id).state is LoopState.SCHEDULED


def test_an_unstamped_result_cannot_regress_a_watermarked_loop(registry):
    """The same hole under record_result: an unstamped final landing on top of
    an applied correction would re-arm acknowledgement on a superseded read."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="C", control_id="C2", message_at=T2)
    with pytest.raises(StaleMessageError):
        registry.record_result(loop_id, obx11="F", control_id="C3")
    assert registry._latest_result_status(loop_id) == "C"


def test_a_future_dated_preliminary_cannot_block_acknowledgement_forever(registry):
    """The drop-stamp band's own hole, one layer over from the watermark.

    `_latest_result_event` keyed an unstamped event on `occurred_at` -- arrival,
    i.e. now -- which beats every legitimately past MSH-7. So a `P` dated
    `now + 30d` became the loop's newest read permanently and `acknowledge`
    refused with "has no final or corrected result" *even after* a genuine
    correction arrived. The fallback's own justification was that arrival order
    equals clinical order for anything this registry appends; that stopped being
    true for exactly the events it deliberately refuses to stamp.
    """
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="P", control_id="C2", message_at=_beyond_skew())
    registry.record_result(loop_id, obx11="C", control_id="C3", message_at=T2)

    assert registry._latest_result_status(loop_id) == "C"
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_a_future_dated_final_does_not_outrank_a_genuine_correction(registry, store):
    """The same inversion, costing the audit its answer: the acknowledgement
    would record `ack_result_status` as the untrusted `F` rather than the `C` a
    coordinator was actually looking at."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=_beyond_skew())
    registry.record_result(loop_id, obx11="C", control_id="C3", message_at=T2)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")

    acked = [e for e in store.events_for(loop_id) if e.event_type == "acknowledged"]
    assert acked[0].detail["ack_result_status"] == "C"


def test_a_trusted_older_result_does_not_mask_a_distrusted_newer_one(registry, store):
    """The inversion flipped rather than removed.

    An unconditional `trusted` flag decided the ranking *before* the times were
    compared, so the mixed comparison was hidden rather than eliminated: a
    genuine correction from a RIS whose clock jumped -- endemic, by this
    subsystem's own account -- was masked by the very final it corrected. The
    transition fired and the loop stayed on the queue, but the audit recorded a
    coordinator as having vouched for the superseded read.
    """
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(
        loop_id, obx11="F", control_id="C2",
        message_at=datetime.now(timezone.utc) - timedelta(days=4),
    )
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")

    registry.record_result(loop_id, obx11="C", control_id="C4", message_at=_beyond_skew())

    assert registry.get(loop_id).state is LoopState.RESULTED
    assert registry._latest_result_status(loop_id) == "C"

    registry.acknowledge(loop_id, actor="coord2", role="coordinator", control_id="C5")
    acked = [e for e in store.events_for(loop_id) if e.event_type == "acknowledged"]
    assert acked[-1].detail["ack_result_status"] == "C", (
        "the audit must name the read the coordinator was actually shown"
    )


def test_a_distrusted_final_can_still_close_a_loop_left_preliminary(registry):
    """The second variant of the same flip, and a permanent block of the same
    class as the watermark poisoning this began with: a trusted `P` masked a
    distrusted `F`, so the loop could never be acknowledged at all."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(
        loop_id, obx11="P", control_id="C2",
        message_at=datetime.now(timezone.utc) - timedelta(days=4),
    )
    registry.record_result(loop_id, obx11="F", control_id="C3", message_at=_beyond_skew())

    assert registry._latest_result_status(loop_id) == "F"
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_a_future_dated_merge_is_counted_without_changing_the_merge(registry, store):
    """An ADT^A40 is exempt from the *ordering* guard, not from visibility.

    `merge_message_at` is read by nothing -- deliberately, and it stays that way
    -- so a skewed A40 can regress nothing. But "either a sender's clock is wrong
    or a message is forged, and both need a human" is exactly as true of the
    highest-value message type in the subsystem, and the counter never fired.
    """
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    carried = registry.merge_patient(
        prior_mrn="MRN1", surviving_mrn="MRN2", control_id="A40",
        message_at=_beyond_skew(),
    )

    assert carried == [loop_id], "the merge itself must be unchanged"
    assert registry.get(loop_id).mrn == "MRN2"
    assert registry.future_dated_message_count == 1


def test_a_message_dated_beyond_every_bound_takes_the_same_path_as_ordinary_skew(registry):
    """One future bound, one behaviour. An earlier draft bounded the *parse* at
    a year and *trust* at a day, so the year-9999 exploit that motivated the fix
    took the refuse-message path while the documented drop-stamp path -- and its
    counter -- never saw it."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    far = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=far)

    assert registry.get(loop_id).state is LoopState.RESULTED
    assert registry._clinical_watermark(loop_id) == T0
    assert registry.future_dated_message_count == 1


def test_an_unstamped_attachment_is_not_refused(registry):
    """A coordinator attaching an orphan is a human decision routed through
    record_result, not a replayed message, and it carries no MSH-7 for the same
    reason acknowledge() carries none. Refusing it for the absence would close
    the orphan queue's only exit -- which the first draft of this guard did, to
    six tests in test_eval and test_orphan_attach."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T2)
    registry.record_result(loop_id, obx11="C", control_id="C3", attached_from="O-1")
    assert registry._latest_result_status(loop_id) == "C"


def test_an_unstamped_schedule_keeps_the_fail_open(registry):
    """OPEN -> SCHEDULED hides nothing: both states are in `_OPEN_STATES` and
    both are staleable, so a replayed SIU^S12 costs a coordinator nothing.
    Refusing it would trade a real refusal for no protection at all."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T2)
    registry.schedule(loop_id, control_id="C2")
    assert registry.get(loop_id).state is LoopState.SCHEDULED


# ------------------------------------------------- _latest_result_status probes


def test_a_merged_in_event_carrying_obx11_does_not_change_result_status(registry, store):
    """Task 7 merges carry fields from another loop. If a merged_in detail could
    set the result status, a merge would flip a final to preliminary or the
    reverse without any result ever arriving."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    # Stamped NEWER than the real result, so only the event-type filter can stop
    # it winning -- not an accident of timestamp ordering.
    store.append_event(
        LoopEvent(loop_id, "merged_in", T2, "C3", {"obx11": "P", "message_at": T2.isoformat()})
    )

    assert registry._latest_result_status(loop_id) == "F"
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_an_orphan_detail_carrying_obx11_does_not_count_as_a_result(registry):
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={"obx11": "F"})
    assert registry._latest_result_status(orphan_id) == ""


def test_latest_result_status_follows_clinical_time_not_arrival(registry, store):
    """Defence in depth behind the staleness guard: if a stale result ever
    reaches the log -- a restored backup, a foreign writer, a future code path
    -- the newest read by MSH-7 must still be the one that governs closing."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="P", control_id="C2", message_at=T2)
    # Appended straight to the store, bypassing the registry's guard entirely.
    store.append_event(
        LoopEvent(loop_id, "resulted", T1, "C3", {"obx11": "F", "message_at": T1.isoformat()})
    )

    assert registry._latest_result_status(loop_id) == "P"
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C4")


def test_a_correction_cannot_interleave_with_an_acknowledgement(registry, monkeypatch):
    """Every rule here is check-then-append. If a correction lands between the
    two halves of an acknowledgement, the loop closes on a read the correction
    already superseded -- safety rule 2 defeated with no error raised anywhere.
    An interface engine holds several connections, so this is not theoretical.
    """
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")

    finished = threading.Event()

    def correct():
        registry.record_result(loop_id, obx11="C", control_id="C4")
        finished.set()

    thread = threading.Thread(target=correct, daemon=True)
    original = Registry._latest_result_status
    seen: list[str] = []

    def hooked(self, lid):
        if not seen:  # mid-acknowledge, between the check and the append
            seen.append(lid)
            thread.start()
            assert not finished.wait(0.5), (
                "a correction landed inside an acknowledgement's check-then-append window"
            )
        return original(self, lid)

    monkeypatch.setattr(Registry, "_latest_result_status", hooked)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    monkeypatch.undo()

    thread.join(timeout=5)
    assert finished.is_set(), "the correction must still be applied, only serialized"
    assert registry.get(loop_id).state is LoopState.RESULTED
    assert registry.get(loop_id).ack_at is None


# ------------------------------------- spec test 5: CLOSED is unreachable in v1
# ACKNOWLEDGED is a coordinator's clerical claim that this result belongs to this
# loop. CLOSED asserts a clinically responsible actor dispositioned the finding,
# which nothing in v1 observes. The states must not be confusable, so CLOSED is
# reserved and asserted unreachable rather than merely unused.


def test_no_event_type_maps_to_closed(registry, store):
    """The whole v1 guarantee in one assertion: state comes only from replaying
    the event-type map, so a state absent from its values cannot be reached."""
    from referral_loop.store import _EVENT_STATE

    assert LoopState.CLOSED not in _EVENT_STATE.values()


def test_the_closed_event_type_is_refused_at_append(registry, store):
    """Failure matrix: an attempted transition to CLOSED is refused and logged.
    Typed, not asserted -- an assertion disappears under python -O."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")

    with pytest.raises(ReservedStateError):
        store.append_event(
            LoopEvent(loop_id, "closed", T2, "C3", {"ack_by": "coord1"})
        )
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_a_log_containing_a_closed_event_refuses_to_replay(tmp_path):
    """A restored or foreign-written log. Silently mapping it to ACKNOWLEDGED
    would read a v2 claim back as v1's weaker one."""
    import sqlite3

    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.append_event(LoopEvent("L1", "created", T0, "C1", {"mrn": "MRN1"}))
    with sqlite3.connect(db) as conn:  # bypasses append_event entirely
        conn.execute(
            "INSERT INTO loop_events (loop_id, event_type, occurred_at, control_id, detail) "
            "VALUES ('L1', 'closed', ?, 'C2', '{}')",
            (T1.isoformat(),),
        )

    with pytest.raises(ReservedStateError):
        LoopStore(db).replay("L1")


_OPS = [
    ("schedule", lambda reg, lid: reg.schedule(lid, control_id="X")),
    ("cancel", lambda reg, lid: reg.cancel(lid, control_id="X")),
    ("result:P", lambda reg, lid: reg.record_result(lid, obx11="P", control_id="X")),
    ("result:F", lambda reg, lid: reg.record_result(lid, obx11="F", control_id="X")),
    ("result:C", lambda reg, lid: reg.record_result(lid, obx11="C", control_id="X")),
    ("ack", lambda reg, lid: reg.acknowledge(lid, actor="a", role="r", control_id="X")),
    ("reverse", lambda reg, lid: reg.reverse_acknowledgement(
        lid, actor="a", role="r", reason="w")),
    ("dismiss", lambda reg, lid: reg.dismiss_orphan(lid, actor="a", role="r", reason="w")),
    # Task 15's two coordinator actions. Omitting them would silently narrow
    # what this sweep claims: "no coordinator action reaches CLOSED" is only
    # worth asserting over every coordinator action there is.
    ("undo_match", lambda reg, lid: reg.undo_match(lid, actor="a", role="r", reason="w")),
    # Attaching a record to itself. The self-attachment is refused, which is the
    # point -- the sweep is over what a caller can *invoke*, not over what
    # succeeds, and a refusal that left state half-applied is exactly the kind of
    # path a hand-written test does not think to try.
    ("attach_self", lambda reg, lid: reg.attach_orphan(lid, lid, actor="a", role="r")),
]


@pytest.mark.parametrize("seed_orphan", [False, True])
def test_closed_is_unreachable_by_any_sequence_of_coordinator_actions(registry, seed_orphan):
    """Spec test 5. Every sequence of every public mutating call, to depth 3,
    from both a real loop and an orphan -- 1110 sequences each. No message, no
    coordinator action and no replay path reaches CLOSED.

    A sweep rather than a hand-picked path: CLOSED being unreachable is a claim
    about paths nobody thought of, which is exactly what a hand-written test
    cannot cover.
    """
    checked = 0
    for length in (1, 2, 3):
        for combo in itertools.product(_OPS, repeat=length):
            if seed_orphan:
                loop_id = registry.orphan(control_id="C0", mrn="MRN1", detail={"obx11": "F"})
            else:
                loop_id = registry.open_loop(mrn="MRN1", control_id="C0", message_at=T0)
            for _name, call in combo:
                try:
                    call(registry, loop_id)
                except ReferralLoopError:
                    pass  # refused transitions are the point; keep going
                state = registry.get(loop_id).state
                assert state is not LoopState.CLOSED, f"reached CLOSED via {combo}"
            checked += 1
    ops = len(_OPS)
    assert checked == ops + ops**2 + ops**3 == 1110


# --------------------------------- spec test 6: acknowledgement is reversible


def test_a_reversal_returns_an_acknowledged_loop_to_resulted(registry):
    """Spec test 6. Rule 2 recovered the machine's error; nothing recovered the
    human's. A coordinator who acknowledged the wrong loop left it resolved while
    the real one stayed open."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED

    registry.reverse_acknowledgement(
        loop_id, actor="coord2", role="coordinator", reason="matched the wrong order"
    )
    loop = registry.get(loop_id)
    assert loop.state is LoopState.RESULTED
    assert loop.ack_by == ""
    assert loop.ack_at is None


def test_a_reversal_records_the_actor_and_mutates_no_prior_event(registry, store):
    """Spec test 6, second half. The mistake and its correction both stay in the
    history -- that is what the append-only log is for."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    before = store.events_for(loop_id)

    registry.reverse_acknowledgement(
        loop_id, actor="coord2", role="supervisor", reason="matched the wrong order"
    )
    after = store.events_for(loop_id)

    assert after[: len(before)] == before, "no prior event may be mutated"
    assert len(after) == len(before) + 1
    assert after[-1].event_type == "reversed"
    assert after[-1].detail["reversed_by"] == "coord2"
    assert after[-1].detail["reversed_role"] == "supervisor"
    assert after[-1].detail["reversed_reason"] == "matched the wrong order"
    # The acknowledgement it undoes stays legible rather than being overwritten.
    ack = [e for e in after if e.event_type == "acknowledged"][-1]
    assert ack.detail["ack_by"] == "coord1"


def test_a_reversed_loop_is_back_on_the_coordinator_worklist(registry, store):
    """A reversal that did not re-queue the loop would be worse than none: the
    coordinator believes they undid it and nothing shows up."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    assert store.resulted_unacknowledged() == []

    registry.reverse_acknowledgement(
        loop_id, actor="coord1", role="coordinator", reason="wrong loop"
    )
    assert [loop.loop_id for loop in store.resulted_unacknowledged()] == [loop_id]


def test_a_reversal_needs_an_actor_role_and_reason(registry):
    """Every reversal is a labeled false positive for the flywheel, and an
    unexplained label teaches nothing."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")

    for kwargs in (
        {"actor": "", "role": "coordinator", "reason": "w"},
        {"actor": "a", "role": "", "reason": "w"},
        {"actor": "a", "role": "coordinator", "reason": ""},
    ):
        with pytest.raises(ReferralLoopError):
            registry.reverse_acknowledgement(loop_id, **kwargs)
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_reversing_a_loop_that_was_never_acknowledged_is_refused(registry):
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.reverse_acknowledgement(loop_id, actor="a", role="r", reason="w")
    with pytest.raises(ReferralLoopError):  # and not twice
        registry.reverse_acknowledgement(loop_id, actor="a", role="r", reason="w")


def test_a_reversal_does_not_count_as_a_result(registry):
    """reversed is not in _RESULT_EVENTS: undoing an acknowledgement changes who
    vouched for the match, not what the radiologist read. If it counted, the
    re-acknowledgement after a reversal would see an empty status and refuse."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.reverse_acknowledgement(loop_id, actor="coord1", role="coordinator", reason="oops")

    assert registry._latest_result_status(loop_id) == "F"
    registry.acknowledge(loop_id, actor="coord2", role="coordinator", control_id="C4")
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_a_correction_after_a_reversal_still_reopens(registry):
    """Rules 2 and 4 must compose: a reversal leaves RESULTED, and a correction
    arriving afterwards must still be handled as a correction."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.reverse_acknowledgement(loop_id, actor="coord1", role="coordinator", reason="oops")
    registry.record_result(loop_id, obx11="C", control_id="C4", message_at=T2)

    assert registry.get(loop_id).state is LoopState.RESULTED
    assert registry._latest_result_status(loop_id) == "C"


def test_a_reversal_does_not_advance_the_clinical_watermark(registry):
    """Same property as acknowledge: a human action must not make a correction
    whose MSH-7 predates it look stale."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1", message_at=T0)
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T1)
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    registry.reverse_acknowledgement(loop_id, actor="coord1", role="coordinator", reason="oops")

    registry.record_result(loop_id, obx11="C", control_id="C4", message_at=T2)
    assert registry.get(loop_id).state is LoopState.RESULTED


# ------------------------------------------------- DISMISSED: terminal orphans


def test_dismissing_an_orphan_is_terminal(registry):
    """Without a terminal state the orphan queue only grows, and a queue that
    only grows is one coordinators stop opening."""
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={"obx11": "F"})
    registry.dismiss_orphan(
        orphan_id, actor="coord1", role="coordinator", reason="misrouted from another facility"
    )
    assert registry.get(orphan_id).state is LoopState.DISMISSED

    for call in (
        lambda: registry.record_result(orphan_id, obx11="F", control_id="C2"),
        lambda: registry.acknowledge(orphan_id, actor="a", role="r", control_id="C3"),
        lambda: registry.schedule(orphan_id, control_id="C4"),
        lambda: registry.cancel(orphan_id, control_id="C5"),
        lambda: registry.dismiss_orphan(orphan_id, actor="a", role="r", reason="again"),
    ):
        with pytest.raises(ReferralLoopError):
            call()
    assert registry.get(orphan_id).state is LoopState.DISMISSED


def test_only_an_orphan_can_be_dismissed(registry):
    """Dismissal retires a result permanently. A real loop is acknowledged or
    cancelled, never dismissed."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    with pytest.raises(ReferralLoopError):
        registry.dismiss_orphan(loop_id, actor="a", role="r", reason="w")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.dismiss_orphan(loop_id, actor="a", role="r", reason="w")
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_a_dismissal_needs_an_actor_role_and_reason(registry):
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={})
    for kwargs in (
        {"actor": "", "role": "r", "reason": "w"},
        {"actor": "a", "role": "", "reason": "w"},
        {"actor": "a", "role": "r", "reason": ""},
    ):
        with pytest.raises(ReferralLoopError):
            registry.dismiss_orphan(orphan_id, **kwargs)
    assert registry.get(orphan_id).state is LoopState.ORPHAN


def test_a_dismissal_records_who_and_why(registry, store):
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={})
    registry.dismiss_orphan(
        orphan_id, actor="coord1", role="coordinator", reason="feed misconfiguration"
    )
    event = store.events_for(orphan_id)[-1]
    assert event.event_type == "dismissed"
    assert event.detail["dismissed_by"] == "coord1"
    assert event.detail["dismissed_reason"] == "feed misconfiguration"


def test_orphan_detail_cannot_override_the_explicit_mrn(registry):
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={"mrn": "MRN-WRONG"})
    assert registry.get(orphan_id).mrn == "MRN1"
    assert registry.get(orphan_id).state is LoopState.ORPHAN


# ------------------------------------ Plan 2b Task 5: what routing must not change


def _loop_in(registry, state: LoopState) -> str:
    """A loop in `state`, built the way real traffic reaches it.

    Every LoopState except CLOSED, which is unreachable by construction (spec test 5) and
    so cannot be swept over here -- the tests above prove it stays that way.
    """
    if state is LoopState.ORPHAN:
        return registry.orphan(control_id="S-ORU", mrn="MRN9", detail={"result_status": "F"})
    if state is LoopState.DISMISSED:
        orphan_id = registry.orphan(control_id="S-ORU", mrn="MRN9", detail={"result_status": "F"})
        registry.dismiss_orphan(orphan_id, actor="a", role="r", reason="misrouted")
        return orphan_id
    if state is LoopState.ATTACHED:
        orphan_id = registry.orphan(control_id="S-ORU", mrn="MRN9", detail={"result_status": "F"})
        target = registry.open_loop(mrn="MRN9", modality="CT", control_id="S-ORM")
        registry.attach_orphan(orphan_id, target, actor="a", role="r")
        return orphan_id

    loop_id = registry.open_loop(mrn="MRN9", modality="CT", control_id="S-ORM")
    if state is LoopState.OPEN:
        return loop_id
    if state is LoopState.SCHEDULED:
        registry.schedule(loop_id, control_id="S-SIU")
        return loop_id
    if state is LoopState.CANCELLED:
        registry.cancel(loop_id, control_id="S-CAN", message_at=T1)
        return loop_id
    registry.record_result(loop_id, obx11="F", control_id="S-ORU")
    if state is LoopState.RESULTED:
        return loop_id
    if state is LoopState.ACKNOWLEDGED:
        registry.acknowledge(loop_id, actor="a", role="r", control_id="S-ACK")
        return loop_id
    raise AssertionError(f"no construction for {state}")


_SCHEDULE_ACCEPTS = frozenset({LoopState.OPEN, LoopState.SCHEDULED})


@pytest.mark.parametrize("state", [s for s in LoopState if s is not LoopState.CLOSED])
def test_schedule_accepts_exactly_two_states_and_refuses_the_rest(registry, state):
    """The `schedule` guard as it behaves today, pinned before Plan 2b routes it through
    machine.apply(). Task 5 preserves what it accepts and what it refuses; it may not
    preserve anything it was never asked about, so the sweep asks about all of them.

    The refusal type is asserted, not merely that something raised. Three of these states
    leave the referral vocabulary entirely under spec 6.5, and `migration.to_referral`
    raises ValueError for them -- which would satisfy a bare `raises(Exception)` while
    changing what listener.py answers on the wire, because it catches ReferralLoopError
    one clause above a bare Exception.
    """
    loop_id = _loop_in(registry, state)
    assert registry.get(loop_id).state is state

    if state in _SCHEDULE_ACCEPTS:
        registry.schedule(loop_id, control_id="S-NEW")
        assert registry.get(loop_id).state is LoopState.SCHEDULED
    else:
        with pytest.raises(ReferralLoopError):
            registry.schedule(loop_id, control_id="S-NEW")
        assert registry.get(loop_id).state is state, "a refused schedule moved the loop"


_CANCEL_ACCEPTS = frozenset({LoopState.OPEN, LoopState.SCHEDULED})


@pytest.mark.parametrize("state", [s for s in LoopState if s is not LoopState.CLOSED])
def test_cancel_accepts_exactly_two_states_and_refuses_the_rest(registry, state):
    """`cancel` as it behaves today, pinned before Task 5 routes it.

    Every call carries an explicit `message_at` so this sweep tests the *state* axis
    alone. Cancel is one of the two transitions that demand a readable clock
    (`require_message_time=True`), and a sweep that let the ordering guard do the refusing
    would go green while proving nothing about which states cancel accepts -- the same
    trap the RECONCILED sweep fell into, one layer out.

    The refusal type is asserted for the reason schedule's sweep gives: three of these
    states leave the referral vocabulary under spec 6.5, and a ValueError escaping in
    place of a ReferralLoopError changes what listener.py answers the sending engine.
    """
    loop_id = _loop_in(registry, state)
    assert registry.get(loop_id).state is state

    if state in _CANCEL_ACCEPTS:
        registry.cancel(loop_id, control_id="C-NEW", message_at=T2)
        assert registry.get(loop_id).state is LoopState.CANCELLED
    else:
        with pytest.raises(ReferralLoopError):
            registry.cancel(loop_id, control_id="C-NEW", message_at=T2)
        assert registry.get(loop_id).state is state, "a refused cancel moved the loop"


def test_cancel_still_demands_a_readable_clock_once_a_loop_has_a_watermark(registry):
    """The ordering axis, kept separate from the sweep above and pinned in its own right.

    This is the H2 remediation: a blank MSH-7 used to turn off the only anti-replay
    control in the system, and a replayed SIU^S15 then cancelled a scheduled loop out of
    open_loops() and resulted_unacknowledged() alike -- clinically open, on no coordinator
    queue at all. Routing the state guard through the machine must not disturb it, so it
    is asserted here before the routing rather than assumed after.

    The S15 has since been routed away to `unschedule`, which sets no such flag because
    `SCHEDULED -> OPEN` hides nothing. That does not retire this test. The flag guards
    destructiveness rather than a message type, `cancel` still ends a referral outright
    and still lands it on no worklist, and the transition has no production caller until
    plan 2c wires the withdrawal path -- which is exactly the condition under which a
    control gets quietly dropped and then has to be rediscovered by the incident that
    motivated it the first time.
    """
    loop_id = registry.open_loop(mrn="MRN9", modality="CT", control_id="W-ORM")
    registry.schedule(loop_id, control_id="W-SIU", message_at=T1)
    with pytest.raises(StaleMessageError):
        registry.cancel(loop_id, control_id="W-CAN", message_at=None)
    assert registry.get(loop_id).state is LoopState.SCHEDULED


_RESULT_REFUSING_STATES = frozenset({
    LoopState.CANCELLED, LoopState.ORPHAN, LoopState.DISMISSED, LoopState.ATTACHED,
})
_HANDLED_OBX11 = ("P", "F", "C")
# Absent, empty, unrecognised, and a wrong-case variant of a handled one. The allowlist
# must refuse all four: `obx11 not in (P, F, C)` is the guard, and a denylist here would
# admit every value nobody anticipated.
_UNHANDLED_OBX11 = ("", " ", "X", "f")


# Where each accepted (state, OBX-11) pair actually lands, generated by running the
# current implementation rather than by reasoning about what it ought to do -- the point
# is to pin what *is*, and a hand-written table would encode what I believe instead.
#
# THIS IS A SNAPSHOT OF BEHAVIOUR TO PRESERVE ACROSS A REFACTOR. IT IS NOT A
# SPECIFICATION OF CORRECT BEHAVIOUR, AND AT LEAST ONE ROW IS KNOWN TO BE WRONG.
#
# Two things it records that this project has already flagged as defects:
#   * ACKNOWLEDGED + 'P' -> RESULTED. Spec 6.4 describes only a *corrected* document
#     demoting a reconciled referral; observed behaviour demotes on a preliminary too.
#     Broader than the spec says, and worth deciding rather than inheriting.
#   * The attached_from staleness exemption, which spec 6.5 deletes outright when
#     InboundArtifact lands in Plan 2c.
#
# So do not read this table as the design. When one of those is fixed, this table is
# meant to change, and a row changing is not by itself a regression.
_RESULT_LANDS_ON = {
    (LoopState.OPEN, 'P'): LoopState.RESULTED,
    (LoopState.OPEN, 'F'): LoopState.RESULTED,
    (LoopState.OPEN, 'C'): LoopState.RESULTED,
    (LoopState.SCHEDULED, 'P'): LoopState.RESULTED,
    (LoopState.SCHEDULED, 'F'): LoopState.RESULTED,
    (LoopState.SCHEDULED, 'C'): LoopState.RESULTED,
    (LoopState.RESULTED, 'P'): LoopState.RESULTED,
    (LoopState.RESULTED, 'F'): LoopState.RESULTED,
    (LoopState.RESULTED, 'C'): LoopState.RESULTED,
    (LoopState.ACKNOWLEDGED, 'P'): LoopState.RESULTED,
    (LoopState.ACKNOWLEDGED, 'F'): LoopState.RESULTED,
    (LoopState.ACKNOWLEDGED, 'C'): LoopState.RESULTED,
}


@pytest.mark.parametrize("obx11", _HANDLED_OBX11 + _UNHANDLED_OBX11)
@pytest.mark.parametrize("state", [s for s in LoopState if s is not LoopState.CLOSED])
def test_record_result_accepts_only_handled_statuses_on_non_terminal_loops(
    registry, state, obx11
):
    """`record_result`'s guards as they behave today, pinned before Task 5 routes it.

    Two axes crossed deliberately: which states may receive a result, and which OBX-11
    values are handled at all. Routing moves only the first into core.machine -- the
    status allowlist is a question about a message's content, not about state legality,
    and it stays here for the same reason _refuse_if_stale does.

    Note what this does *not* cover, because it is a different layer: the weakest-OBX-11
    rule and the fail-safe-to-preliminary behaviour live in listener._result_status
    (listener.py:1139), which reads every OBX segment before ever calling this method.
    `record_result` takes a single already-resolved status. Those guarantees are pinned by
    test_listener.py:753 and :764, and routing cannot reach them.
    """
    loop_id = _loop_in(registry, state)
    accepted = state not in _RESULT_REFUSING_STATES and obx11 in _HANDLED_OBX11

    if accepted:
        registry.record_result(loop_id, obx11=obx11, control_id="R-NEW", message_at=T2)
        assert registry.get(loop_id).state is _RESULT_LANDS_ON[(state, obx11)]
    else:
        with pytest.raises(ReferralLoopError):
            registry.record_result(loop_id, obx11=obx11, control_id="R-NEW", message_at=T2)
        assert registry.get(loop_id).state is state, "a refused result moved the loop"


def test_an_unhandled_status_is_refused_before_the_state_is_even_consulted(registry):
    """The allowlist is not reachable only from the states that accept results. A loop in
    a perfectly resultable state must still refuse a status nobody anticipated, or the
    guard is a property of the state rather than of the message."""
    loop_id = _loop_in(registry, LoopState.OPEN)
    with pytest.raises(ReferralLoopError, match="Unhandled OBX-11"):
        registry.record_result(loop_id, obx11="Z", control_id="R-NEW")
    assert registry.get(loop_id).state is LoopState.OPEN


_ACK_ACCEPTS_FROM = frozenset({LoopState.RESULTED})


@pytest.mark.parametrize("obx11", ("P", "F", "C"))
@pytest.mark.parametrize("state", [s for s in LoopState if s is not LoopState.CLOSED])
def test_acknowledge_accepts_only_a_resulted_loop_on_a_final_or_corrected_read(
    registry, state, obx11
):
    """Both of acknowledge's guards, crossed, before Task 5 routes either.

    _ACKNOWLEDGEABLE_FROM is a state question and moves to core.machine.
    _ACKNOWLEDGEABLE_STATUSES is the preliminary prohibition -- spec rule 1, the
    malpractice scenario -- and becomes the machine's `documentation` guard once the fold
    populates it. Both must keep refusing exactly what they refuse today.

    The status is established by landing a result of that OBX-11 on the loop wherever a
    result can land, so the pair being tested is (state the loop is in, read it holds).
    """
    loop_id = _loop_in(registry, state)
    if state not in _RESULT_REFUSING_STATES:
        registry.record_result(loop_id, obx11=obx11, control_id="A-ORU", message_at=T2)
    before = registry.get(loop_id).state

    accepted = before in _ACK_ACCEPTS_FROM and obx11 in ("F", "C")
    if accepted:
        registry.acknowledge(loop_id, actor="a", role="r", control_id="A-ACK")
        assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED
    else:
        with pytest.raises(ReferralLoopError):
            registry.acknowledge(loop_id, actor="a", role="r", control_id="A-ACK")
        assert registry.get(loop_id).state is before, "a refused acknowledgement moved the loop"


def test_a_loop_with_no_result_at_all_is_not_acknowledgeable(registry):
    """The allowlist's boundary case, kept separate from the sweep because no OBX-11
    parameter can express 'no result ever arrived'. A denylist on PRELIMINARY would
    acknowledge this one."""
    loop_id = _loop_in(registry, LoopState.OPEN)
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="a", role="r", control_id="A-ACK")
    assert registry.get(loop_id).state is LoopState.OPEN


# ------------------------------------------- an S15 un-schedules, it does not cancel


def test_a_cancelled_appointment_leaves_the_referral_on_the_worklist(registry, store):
    """The defect this method exists for, as a regression test.

    A patient rings the specialist's office and moves their CT. The office sends an
    SIU^S15 for the old slot. Routed to `cancel`, that drove the loop to CANCELLED, which
    is in neither `open_loops()` nor `resulted_unacknowledged()` and is terminal in
    LEGAL_TRANSITIONS -- so the referral was clinically open, still needed the scan, and
    was on no coordinator queue at all, permanently. Every existing guard passed: the
    right loop was resolved, the clock was readable, and SCHEDULED -> CANCELLED is a legal
    edge. Nothing refused it, because nothing was asked the right question.

    The appointment went away; the referral did not. So the loop keeps ageing on the same
    queue it was on before it was ever booked.
    """
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="U-ORM", message_at=T0)
    registry.schedule(loop_id, control_id="U-S12", message_at=T1)

    registry.unschedule(loop_id, control_id="U-S15", message_at=T2)

    assert registry.get(loop_id).state is LoopState.OPEN
    assert [loop.loop_id for loop in store.open_loops("MRN1")] == [loop_id]


def test_an_unschedule_is_attributed_to_the_counterparty_that_sent_it(registry, store):
    """The provenance half, which the projection cannot show.

    The scheduler at the receiving organisation is reporting on its own diary -- the same
    party that sent the S12 behind `schedule`. HUMAN would put a coordinator at this site
    behind a claim nobody here made, and this is the log spec 8.2 answers "who said so"
    from, so a wrong source there is not recoverable from the loop's state.
    """
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="U-ORM", message_at=T0)
    registry.schedule(loop_id, control_id="U-S12", message_at=T1)
    registry.unschedule(loop_id, control_id="U-S15", message_at=T2)

    assert store.fold_transitions(loop_id) is ReferralState.ACCEPTED
    rows = store._read(
        "SELECT to_state, assertion_source FROM transition_events "
        "WHERE referral_id = ? ORDER BY seq", (loop_id,))
    assert [tuple(row) for row in rows] == [
        ("scheduled", "receiving-org"), ("accepted", "receiving-org")
    ]


def test_an_s15_for_a_loop_that_was_never_scheduled_is_refused(registry, store):
    """There is no appointment to cancel, and the machine will not catch this one.

    `LoopState.OPEN` maps to `ReferralState.SENT`, and SENT -> ACCEPTED is a legal edge
    for its own unrelated and correct reasons -- a receiving organisation accepting a
    referral it was sent. So an S15 naming a loop nobody ever booked would sail through
    `machine.apply()` and record an *acceptance* the receiving org never asserted, off a
    message that said the opposite. The refusal has to be `unschedule`'s own.

    Asserted on the state AND on the log: the loop is already OPEN, so a check that only
    looked at the projection would pass while an `unscheduled` event and a
    provenance row for a transition into ACCEPTED sat in the history.
    """
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="U-ORM", message_at=T0)
    before = [event.event_type for event in store.events_for(loop_id)]

    with pytest.raises(ReferralLoopError, match="no appointment"):
        registry.unschedule(loop_id, control_id="U-S15", message_at=T2)

    assert registry.get(loop_id).state is LoopState.OPEN
    assert [event.event_type for event in store.events_for(loop_id)] == before


def test_an_s15_then_an_s12_is_a_reschedule_expressed_in_two_messages(registry):
    """The collapsed form -- an S12 for the new slot with no S15 first -- is already legal
    as SCHEDULED -> SCHEDULED. This is the same reschedule sent as two messages, which is
    what a scheduler that cancels before it rebooks emits, and refusing the intermediate
    state while admitting the collapsed one would be incoherent."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="U-ORM", message_at=T0)
    registry.schedule(loop_id, control_id="U-S12", message_at=T0)
    registry.unschedule(loop_id, control_id="U-S15", message_at=T1)
    registry.schedule(loop_id, control_id="U-S12B", message_at=T2)

    assert registry.get(loop_id).state is LoopState.SCHEDULED


_UNSCHEDULE_ACCEPTS = frozenset({LoopState.SCHEDULED})


@pytest.mark.parametrize("state", [s for s in LoopState if s is not LoopState.CLOSED])
def test_unschedule_accepts_only_a_scheduled_loop_and_refuses_the_rest(registry, state):
    """The state axis alone, swept the way `schedule` and `cancel` are swept above.

    Two different mechanisms do the refusing here and both are meant to. RESULTED,
    ACKNOWLEDGED and CANCELLED have no edge into ACCEPTED in LEGAL_TRANSITIONS, which is
    the machine's question; OPEN has one and is refused by `unschedule`'s own
    no-appointment check, which is an evidence question. The sweep asserts the outcome
    they share -- nothing moves -- and the OPEN case has its own test above that pins
    which of the two answered.

    The refusal type is asserted for the reason schedule's sweep gives: three of these
    states leave the referral vocabulary under spec 6.5, and a ValueError escaping in
    place of a ReferralLoopError changes what listener.py answers the sending engine.
    """
    loop_id = _loop_in(registry, state)
    assert registry.get(loop_id).state is state

    if state in _UNSCHEDULE_ACCEPTS:
        registry.unschedule(loop_id, control_id="U-NEW", message_at=T2)
        assert registry.get(loop_id).state is LoopState.OPEN
    else:
        with pytest.raises(ReferralLoopError):
            registry.unschedule(loop_id, control_id="U-NEW", message_at=T2)
        assert registry.get(loop_id).state is state, "a refused unschedule moved the loop"


def test_an_unstamped_unschedule_is_applied_rather_than_refused(registry, store):
    """The staleness posture, and it is `schedule`'s rather than `cancel`'s.

    `cancel` demands a readable MSH-7 because it is destructive: a blank one turned off
    the only anti-replay control in the system and a replayed S15 took a scheduled loop
    off every queue. Unscheduling hides nothing -- OPEN and SCHEDULED are both in the
    store's `_OPEN_STATES`, so the loop stays exactly where a coordinator already sees it
    -- so refusing an unreadable clock would buy no protection at the price of leaving the
    loop advertising an appointment that no longer exists.
    """
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="U-ORM", message_at=T0)
    registry.schedule(loop_id, control_id="U-S12", message_at=T1)

    registry.unschedule(loop_id, control_id="U-S15", message_at=None)

    assert registry.get(loop_id).state is LoopState.OPEN
    assert [loop.loop_id for loop in store.open_loops("MRN1")] == [loop_id]


def test_a_clinically_older_unschedule_is_still_refused(registry):
    """Failing open on an *absent* clock is not failing open on a *readable and older*
    one. An S15 for a slot that a later S12 already replaced must not un-book the booking
    that superseded it, and that ordering question is the watermark's, not the machine's."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="U-ORM", message_at=T0)
    registry.schedule(loop_id, control_id="U-S12", message_at=T2)

    with pytest.raises(StaleMessageError):
        registry.unschedule(loop_id, control_id="U-S15", message_at=T1)

    assert registry.get(loop_id).state is LoopState.SCHEDULED
