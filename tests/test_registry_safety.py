"""Spec tests 1 and 2. Safety, not correctness.

The first six tests are the plan's specified set. Everything after
SPEC TESTS END is an adversarial probe: each one exists because a plausible
sequence of real messages reaches an unsafe state without it.
"""
import itertools
import threading
from datetime import datetime, timedelta, timezone

import pytest

from healthcare_rag.referral_loop.errors import (
    LoopNotFoundError,
    ReferralLoopError,
    ReservedStateError,
    StaleMessageError,
)
from healthcare_rag.referral_loop.events import LoopEvent, LoopState
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore

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


def test_an_unknown_message_time_neither_blocks_nor_is_blocked(registry):
    """Task 10 does not yet pass MSH-7. message_at=None must keep the loop
    working rather than refusing every message."""
    loop_id = registry.open_loop(mrn="MRN1", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2", message_at=T2)
    registry.record_result(loop_id, obx11="C", control_id="C3")
    assert registry.get(loop_id).state is LoopState.RESULTED


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
    from healthcare_rag.referral_loop.store import _EVENT_STATE

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
