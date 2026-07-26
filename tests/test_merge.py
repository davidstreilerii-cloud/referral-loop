"""Spec test 3: ADT^A40 moves every loop to the surviving MRN.

The four tests before SPEC TESTS END are the plan's specified set, with the
stale `CLOSED` corrected to `ACKNOWLEDGED` per the 2026-07-26 spec revision.
Everything after it is an adversarial probe: each one exists because a real
sequence of registration messages reaches a wrong state without it.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from healthcare_rag.referral_loop.errors import ReferralLoopError, StoreUnavailableError
from healthcare_rag.referral_loop.events import LoopState
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore

T0 = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def _registry(tmp_path):
    return Registry(LoopStore(tmp_path / "loops.db"))


@pytest.fixture()
def reg(tmp_path):
    return _registry(tmp_path)


def test_merge_moves_every_open_loop_to_surviving_mrn(tmp_path):
    reg = _registry(tmp_path)
    a = reg.open_loop(mrn="MRN_OLD", modality="CT", control_id="C1")
    b = reg.open_loop(mrn="MRN_OLD", modality="MG", control_id="C2")
    reg.schedule(b, control_id="C3")

    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEW", control_id="C4")

    assert reg.get(a).mrn == "MRN_NEW"
    assert reg.get(b).mrn == "MRN_NEW"
    assert reg.get(b).state is LoopState.SCHEDULED, "merge must not alter loop state"


def test_no_loop_is_orphaned_by_a_merge(tmp_path):
    """The assertion that matters: nothing may be left behind on the prior MRN."""
    reg = _registry(tmp_path)
    for i in range(5):
        reg.open_loop(mrn="MRN_OLD", modality="CT", control_id=f"C{i}")

    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEW", control_id="CM")

    assert reg.store.open_loops(mrn="MRN_OLD") == []
    assert len(reg.store.open_loops(mrn="MRN_NEW")) == 5


def test_merge_to_unknown_surviving_mrn_still_carries_loops(tmp_path):
    """Failure matrix: create the surviving record, carry loops, log."""
    reg = _registry(tmp_path)
    a = reg.open_loop(mrn="MRN_OLD", modality="CT", control_id="C1")
    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEVER_SEEN", control_id="C2")
    assert reg.get(a).mrn == "MRN_NEVER_SEEN"


def test_acknowledged_loops_also_follow_the_surviving_identifier(tmp_path):
    """A resolved loop left on a retired MRN corrupts the audit trail just as
    badly as an open one vanishing from the worklist."""
    reg = _registry(tmp_path)
    a = reg.open_loop(mrn="MRN_OLD", modality="CT", control_id="C1")
    reg.record_result(a, obx11="F", control_id="C2")
    reg.acknowledge(a, actor="coord1", role="coordinator", control_id="C3")

    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEW", control_id="C4")

    merged = reg.get(a)
    assert merged.mrn == "MRN_NEW"
    assert merged.state is LoopState.ACKNOWLEDGED


# ------------------------------------------------------------- SPEC TESTS END


# ------------------------------------------------- the merge is a real carry,
# not a projection-only edit


def test_the_surviving_mrn_survives_a_replay_from_events_alone(reg):
    """The loops table is a projection. If the merge only reached it, a restore
    that rebuilds from loop_events would silently put every loop back on the
    retired MRN -- and the worklist would look correct until the day it matters.
    """
    a = reg.open_loop(mrn="MRN_OLD", modality="CT", control_id="C1")
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="C2")

    assert reg.store.replay(a).mrn == "MRN_NEW"
    reg.store.rebuild_projection()
    assert [loop.loop_id for loop in reg.store.open_loops(mrn="MRN_NEW")] == [a]


def test_a_merge_carries_the_clinical_fields_untouched(reg):
    """Only the identifier moves. A merge that dropped the accession would break
    every subsequent match for that patient."""
    a = reg.open_loop(
        mrn="MRN_OLD", modality="CT", control_id="C1",
        placer_order_number="P1", filler_order_number="F1",
        service_code="71260", ordering_provider="DR_WHO", ordered_at=T0,
    )
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="C2")

    loop = reg.get(a)
    assert (loop.placer_order_number, loop.filler_order_number) == ("P1", "F1")
    assert (loop.service_code, loop.modality, loop.ordering_provider) == (
        "71260", "CT", "DR_WHO",
    )
    assert loop.ordered_at == T0


def test_a_merge_records_where_the_loop_came_from(reg):
    """The audit question after a merge is 'which record was this before'.
    Without merged_from_mrn the prior identifier is unrecoverable."""
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1")
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM", message_at=T1)

    event = [e for e in reg.store.events_for(a) if e.event_type == "merged_in"][-1]
    assert event.detail["merged_from_mrn"] == "MRN_OLD"
    assert event.detail["mrn"] == "MRN_NEW"
    assert event.control_id == "CM"


def test_merge_returns_the_loops_it_moved(reg):
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1")
    b = reg.open_loop(mrn="MRN_OLD", control_id="C2")
    reg.open_loop(mrn="MRN_OTHER", control_id="C3")

    assert sorted(reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")) == sorted([a, b])


def test_a_merge_does_not_touch_another_patients_loops(reg):
    other = reg.open_loop(mrn="MRN_OTHER", control_id="C1")
    reg.open_loop(mrn="MRN_OLD", control_id="C2")

    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    assert reg.get(other).mrn == "MRN_OTHER"


# --------------------------------------------------- state is never changed


_SEEDS = {
    LoopState.OPEN: lambda r: r.open_loop(mrn="MRN_OLD", control_id="S1"),
    LoopState.SCHEDULED: lambda r: _seed(r, [("schedule", {})]),
    LoopState.RESULTED: lambda r: _seed(r, [("result", {"obx11": "P"})]),
    LoopState.ACKNOWLEDGED: lambda r: _seed(
        r, [("result", {"obx11": "F"}), ("ack", {})]
    ),
    LoopState.CANCELLED: lambda r: _seed(r, [("cancel", {})]),
    LoopState.ORPHAN: lambda r: r.orphan(control_id="S1", mrn="MRN_OLD", detail={}),
    LoopState.DISMISSED: lambda r: _dismissed(r),
}


def _seed(reg, steps):
    loop_id = reg.open_loop(mrn="MRN_OLD", control_id="S1")
    for step, kwargs in steps:
        if step == "schedule":
            reg.schedule(loop_id, control_id="S2")
        elif step == "cancel":
            reg.cancel(loop_id, control_id="S2")
        elif step == "result":
            reg.record_result(loop_id, control_id="S2", **kwargs)
        elif step == "ack":
            reg.acknowledge(loop_id, actor="a", role="coordinator", control_id="S3")
    return loop_id


def _dismissed(reg):
    orphan_id = reg.orphan(control_id="S1", mrn="MRN_OLD", detail={})
    reg.dismiss_orphan(orphan_id, actor="a", role="coordinator", reason="misrouted")
    return orphan_id


@pytest.mark.parametrize("state", list(_SEEDS))
def test_a_merge_moves_a_loop_in_any_state_and_changes_none_of_them(reg, state):
    """'Every loop in any state' includes the ones it is tempting to skip.

    An ORPHAN holds a result nobody ordered; left on a retired MRN it can never
    be attached, because the coordinator searching the surviving patient does
    not see it. A DISMISSED or CANCELLED record left behind is an audit hole.
    And a merge must never be a transition: merged_in is non-transitional, so
    it can neither resurrect a terminal loop nor retire a live one.
    """
    loop_id = _SEEDS[state](reg)
    assert reg.get(loop_id).state is state, "seed did not reach the intended state"

    moved = reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    assert moved == [loop_id]
    assert reg.get(loop_id).mrn == "MRN_NEW"
    assert reg.get(loop_id).state is state, "a merge must not be a transition"


def test_a_merge_does_not_clear_or_forge_an_acknowledgement(reg):
    """merged_in carries only the identifier. If it could write ack fields, a
    merge would either resolve an open loop or reopen a resolved one."""
    a = _SEEDS[LoopState.ACKNOWLEDGED](reg)
    before = reg.get(a)

    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    after = reg.get(a)
    assert (after.ack_by, after.ack_role, after.ack_at) == (
        before.ack_by, before.ack_role, before.ack_at,
    )
    assert reg.store.resulted_unacknowledged(mrn="MRN_NEW") == []


def test_a_merged_resulted_loop_is_still_on_the_coordinator_worklist(reg):
    """The worklist queries are MRN-scoped. A merge that moved the loop out of
    resulted_unacknowledged() would hide exactly the population needing a human.
    """
    a = _seed(reg, [("result", {"obx11": "F"})])
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    assert reg.store.resulted_unacknowledged(mrn="MRN_OLD") == []
    assert [loop.loop_id for loop in reg.store.resulted_unacknowledged(mrn="MRN_NEW")] == [a]


# ------------------------------------------------------- chained and circular


def test_a_chained_merge_leaves_the_loops_on_the_last_surviving_mrn(reg):
    """A -> B then B -> C. Registration systems merge repeatedly; the loops must
    end on C, not be stranded on B by the first merge having already run."""
    a = reg.open_loop(mrn="MRN_A", control_id="C1")

    assert reg.merge_patient("MRN_A", "MRN_B", control_id="CM1") == [a]
    assert reg.merge_patient("MRN_B", "MRN_C", control_id="CM2") == [a]

    assert reg.get(a).mrn == "MRN_C"
    assert reg.store.open_loops(mrn="MRN_A") == []
    assert reg.store.open_loops(mrn="MRN_B") == []
    assert len(reg.store.open_loops(mrn="MRN_C")) == 1


def test_a_circular_merge_ends_on_the_last_surviving_mrn_and_strands_nothing(reg):
    """A -> B then B -> A. Two contradicting merges; last writer wins is the
    only available answer, and the requirement is that the loop is on exactly
    one MRN and visible there."""
    a = reg.open_loop(mrn="MRN_A", control_id="C1")
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    # Asserted mid-cycle: the end state is indistinguishable from a merge that
    # never moved anything, so without this the test passes on a broken merge.
    assert reg.get(a).mrn == "MRN_B"
    reg.merge_patient("MRN_B", "MRN_A", control_id="CM2")

    assert reg.get(a).mrn == "MRN_A"
    assert reg.store.open_loops(mrn="MRN_B") == []
    assert len(reg.store.open_loops(mrn="MRN_A")) == 1


def test_merging_an_mrn_into_itself_is_a_no_op_not_a_failure(reg, caplog):
    """Refusing would make the engine represent a message that can never become
    acceptable. Nothing moves and nothing is stranded, so it is a no-op -- but a
    logged one, because it more likely means MRG-1 was mapped wrong."""
    a = reg.open_loop(mrn="MRN_A", control_id="C1")
    before = len(reg.store.events_for(a))

    with caplog.at_level(logging.WARNING):
        assert reg.merge_patient("MRN_A", "MRN_A", control_id="CM") == []

    assert len(reg.store.events_for(a)) == before
    assert reg.get(a).mrn == "MRN_A"
    assert "itself" in caplog.text


def test_a_merge_of_an_mrn_with_no_loops_is_a_no_op(reg):
    reg.open_loop(mrn="MRN_OTHER", control_id="C1")
    assert reg.merge_patient("MRN_GHOST", "MRN_NEW", control_id="CM") == []


@pytest.mark.parametrize(
    ("prior", "surviving"), [("", "MRN_NEW"), ("MRN_OLD", ""), ("", "")]
)
def test_a_merge_missing_either_identifier_is_refused(reg, prior, surviving):
    """An empty surviving MRN blanks the identifier on every loop it touches --
    the same invisibility open_loop refuses. An empty prior MRN would select
    every orphan recorded without one and sweep them onto a patient.
    """
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1")
    unattributed = reg.orphan(control_id="C2", mrn="", detail={})

    with pytest.raises(ReferralLoopError):
        reg.merge_patient(prior, surviving, control_id="CM")

    assert reg.get(a).mrn == "MRN_OLD"
    assert reg.get(unattributed).mrn == ""


# ----------------------------------------------------------------- idempotency


def test_the_same_merge_delivered_twice_appends_nothing_the_second_time(reg):
    """record_raw dedupes on MSH-10, but merge_patient is callable directly and
    an engine that resends after a timeout is ordinary. Selection is by current
    MRN, so the second delivery finds nothing -- idempotent structurally rather
    than by a dedup check that could be bypassed."""
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1")

    assert reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM") == [a]
    assert reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM") == []

    merges = [e for e in reg.store.events_for(a) if e.event_type == "merged_in"]
    assert len(merges) == 1
    assert reg.get(a).mrn == "MRN_NEW"


# ------------------------------------------------------- MSH-7 and the watermark


def test_a_clinically_stale_merge_is_still_applied(reg):
    """An A40 is an administrative correction about identity, not a clinical
    observation. Refusing it as stale strands loops on a retired MRN -- the very
    failure the merge exists to prevent -- and it can regress nothing, because a
    merge is non-transitional and selects by current MRN."""
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1", message_at=T1)
    reg.record_result(a, obx11="F", control_id="C2", message_at=T2)

    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM", message_at=T0)

    assert reg.get(a).mrn == "MRN_NEW"
    assert reg.get(a).state is LoopState.RESULTED


def test_a_merge_does_not_make_a_later_arriving_older_result_look_stale(reg):
    """Registration merges at 14:00; an ORU generated at 13:55 is still queued
    in the engine. If the merge advanced the watermark that result is refused
    and never lands. Same reasoning that keeps acknowledge() off the watermark.
    """
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1", message_at=T0)
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM", message_at=T2)

    reg.record_result(a, obx11="F", control_id="C2", message_at=T1)

    assert reg.get(a).state is LoopState.RESULTED
    assert reg._clinical_watermark(a) == T1


def test_the_merge_message_time_is_still_recorded_for_the_audit(reg):
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1")
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM", message_at=T2)

    event = [e for e in reg.store.events_for(a) if e.event_type == "merged_in"][-1]
    assert event.detail["merge_message_at"] == T2.isoformat()
    assert "message_at" not in event.detail


def test_a_merge_does_not_change_which_result_governs_acknowledgement(reg):
    """merged_in is not a result event. If it were, a merge would decide what a
    coordinator is allowed to acknowledge."""
    a = reg.open_loop(mrn="MRN_OLD", control_id="C1", message_at=T0)
    reg.record_result(a, obx11="P", control_id="C2", message_at=T1)

    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM", message_at=T2)

    assert reg._latest_result_status(a) == "P"
    with pytest.raises(ReferralLoopError):
        reg.acknowledge(a, actor="a", role="coordinator", control_id="C3")


# ---------------------------------------------------------------- concurrency


def test_a_loop_opened_mid_merge_cannot_be_stranded_on_the_prior_mrn(reg, monkeypatch):
    """An ORM arriving while the merge is scanning must not open a loop on the
    prior MRN after the scan has passed it. The merge holds the registry lock
    across every append, so the ORM is serialized either side of it -- and
    either side is safe, but inside is not.
    """
    reg.open_loop(mrn="MRN_OLD", control_id="C1")
    reg.open_loop(mrn="MRN_OLD", control_id="C2")

    opened = threading.Event()

    def open_another():
        reg.open_loop(mrn="MRN_OLD", control_id="C3")
        opened.set()

    thread = threading.Thread(target=open_another, daemon=True)
    original = LoopStore.append_event
    started = []

    def hooked(self, event):
        if not started:  # mid-merge, after the scan and between two appends
            started.append(event.loop_id)
            thread.start()
            assert not opened.wait(0.5), (
                "a loop was opened on the prior MRN inside the merge's scan-then-append window"
            )
        return original(self, event)

    monkeypatch.setattr(LoopStore, "append_event", hooked)
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    monkeypatch.undo()

    thread.join(timeout=5)
    assert opened.is_set(), "the ORM must still be applied, only serialized"
    assert len(reg.store.open_loops(mrn="MRN_NEW")) == 2
    # The third loop is genuinely later than the merge, and v1 has no alias
    # table: it lands on the prior MRN. Asserted so the gap is visible in the
    # suite rather than discovered at a site.
    assert len(reg.store.open_loops(mrn="MRN_OLD")) == 1


def test_a_second_delivery_of_the_same_merge_cannot_interleave_with_the_first(reg, monkeypatch):
    """Two engine connections replaying the same A40.

    Idempotency here is structural -- the second merge finds nothing on the
    prior MRN -- but only if the first one's scan and appends are atomic. Racing
    two threads on a barrier does NOT prove that: run against a lockless merge
    it passed 5 times out of 5, because the first thread finishes before the
    second is scheduled. The interleaving has to be forced.
    """
    for i in range(3):
        reg.open_loop(mrn="MRN_OLD", control_id=f"C{i}")

    done = threading.Event()
    second: list[list[str]] = []

    def run_second():
        second.append(reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM2"))
        done.set()

    thread = threading.Thread(target=run_second, daemon=True)
    original = LoopStore.append_event
    started: list[int] = []

    def hooked(self, event):
        if not started:  # inside the first merge, after its scan, before any append
            started.append(1)
            thread.start()
            assert not done.wait(0.5), (
                "a second delivery of the same A40 ran inside the first merge's "
                "scan-then-append window and re-moved loops it had already claimed"
            )
        return original(self, event)

    monkeypatch.setattr(LoopStore, "append_event", hooked)
    first = reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM1")
    monkeypatch.undo()

    thread.join(timeout=5)
    assert done.is_set(), "the second delivery must still run, only serialized"
    assert len(first) == 3
    assert second == [[]], "the second delivery must find nothing left to move"
    assert reg.store.open_loops(mrn="MRN_OLD") == []
    for loop in reg.store.open_loops(mrn="MRN_NEW"):
        merges = [e for e in reg.store.events_for(loop.loop_id) if e.event_type == "merged_in"]
        assert len(merges) == 1, "a loop was merged twice"


def test_a_merge_that_dies_partway_is_finished_by_the_resend(reg, monkeypatch):
    """A store failure mid-merge strands the loops it had not reached yet.

    append_event owns one transaction per loop, so the merge is not atomic
    across loops. That is survivable only because selection is by current MRN:
    the engine gets AE, resends the A40, and the resend picks up exactly the
    loops still on the prior identifier. If it were selected any other way --
    from the message, from a cached list -- the half-merged patient would stay
    half-merged and the remainder would be invisible on the surviving MRN.
    """
    ids = [reg.open_loop(mrn="MRN_OLD", control_id=f"C{i}") for i in range(4)]
    original = LoopStore.append_event
    calls: list[int] = []

    def failing(self, event):
        calls.append(1)
        if len(calls) == 3:
            raise StoreUnavailableError("disk full")
        return original(self, event)

    monkeypatch.setattr(LoopStore, "append_event", failing)
    with pytest.raises(StoreUnavailableError):
        reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    monkeypatch.undo()

    stranded = reg.store.open_loops(mrn="MRN_OLD")
    assert len(stranded) == 2, "the failure must leave the unreached loops where they were"

    # The resend. No bookkeeping, no cursor: the prior MRN is the cursor.
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    assert reg.store.open_loops(mrn="MRN_OLD") == []
    assert sorted(loop.loop_id for loop in reg.store.open_loops(mrn="MRN_NEW")) == sorted(ids)
    for loop_id in ids:
        merges = [e for e in reg.store.events_for(loop_id) if e.event_type == "merged_in"]
        assert len(merges) == 1, "the resend must not double-merge a loop it already moved"


# ---------------------------------------------------------------------- scale


def test_a_merge_does_not_scan_the_whole_event_log(reg):
    """merge_patient runs under the registry lock, so its cost blocks every
    other message on the interface. all_loops() replays every loop in the file
    to find the handful on one MRN; measured at 10,000 loops that is 340ms
    against 1.3ms for the indexed lookup. Asserted structurally rather than by a
    wall-clock threshold, which would be flaky on shared CI.
    """
    for i in range(50):
        reg.open_loop(mrn=f"MRN_OTHER_{i}", control_id=f"C{i}")
    a = reg.open_loop(mrn="MRN_OLD", control_id="CX")

    replayed: list[str] = []
    original = LoopStore._replay_on

    def hooked(self, conn, loop_id):
        replayed.append(loop_id)
        return original(self, conn, loop_id)

    LoopStore._replay_on = hooked
    try:
        assert reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM") == [a]
    finally:
        LoopStore._replay_on = original

    scanned = [lid for lid in replayed if lid != a]
    assert not scanned, f"the merge replayed {len(scanned)} unrelated loops"


def test_a_merge_of_many_loops_completes_in_reasonable_time(reg):
    """Guards the lookup, not the throughput: a regression to all_loops() here
    is O(loops in file) per merge and shows up as a wall-clock cliff."""
    for i in range(300):
        reg.open_loop(mrn="MRN_OLD" if i % 3 == 0 else f"MRN_{i}", control_id=f"C{i}")

    start = time.perf_counter()
    moved = reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    elapsed = time.perf_counter() - start

    assert len(moved) == 100
    assert elapsed < 10, f"merging 100 loops took {elapsed:.1f}s"
