"""Spec test 3: ADT^A40 moves every loop to the surviving MRN.

The four tests before SPEC TESTS END are the plan's specified set, with the
stale `CLOSED` corrected to `ACKNOWLEDGED` per the 2026-07-26 spec revision.
Everything after it is an adversarial probe: each one exists because a real
sequence of registration messages reaches a wrong state without it.
"""
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from healthcare_rag.referral_loop.errors import (
    CircularMergeError,
    ReferralLoopError,
    StoreUnavailableError,
)
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
    assert reg.store.alias_count() == 0, "a refused merge must not record an alias"


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
        _ingest_open(reg, "MRN_OLD", control_id="C3")
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
    # All three. The first two were carried by the merge's scan; the third
    # arrived after the merge committed and was redirected by the alias. The
    # lock covers the alias write as well as the scan, so there is no instant
    # at which an order can be neither carried nor redirected.
    assert len(reg.store.open_loops(mrn="MRN_NEW")) == 3
    assert reg.store.open_loops(mrn="MRN_OLD") == []


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


# ----------------------------------------------------- the alias table itself
# A merge fixes the loops that exist. The alias fixes the ones that arrive
# afterwards, and an engine keeps emitting the retired MRN for a while.
#
# Identity is resolved ONCE, at ingest. These tests call resolve_mrn where the
# listener will (Task 10) rather than assuming the registry does it, because a
# test that resolves somewhere the product does not proves nothing about the
# product.


def _ingest_open(reg, raw_mrn, control_id, **kwargs):
    """Open a loop the way Task 10's listener will: resolve, then store.

    The retry is the listener's too -- open_loop refuses an MRN retired between
    ingest and the write, and the answer to that is to re-resolve and resend.
    """
    try:
        return reg.open_loop(
            mrn=reg.store.resolve_mrn(raw_mrn), control_id=control_id,
            submitted_mrn=raw_mrn, **kwargs,
        )
    except ReferralLoopError:
        return reg.open_loop(
            mrn=reg.store.resolve_mrn(raw_mrn), control_id=control_id,
            submitted_mrn=raw_mrn, **kwargs,
        )


def test_an_order_on_a_retired_mrn_opens_on_the_surviving_patient(reg):
    """SPEC TEST 4. An ORM carrying the retired MRN, arriving AFTER the A40.

    Paired with spec test 3: that one carries the loops already here, this one
    catches the ones still coming. Either alone leaves a clinically open loop
    off the worklist, and which loops depends only on interface timing -- so a
    site would see it intermittently and never reproduce it.
    """
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    later = _ingest_open(reg, "MRN_OLD", control_id="C9", modality="CT")

    assert reg.get(later).mrn == "MRN_NEW"
    assert reg.store.open_loops(mrn="MRN_OLD") == []
    assert [loop.loop_id for loop in reg.store.open_loops(mrn="MRN_NEW")] == [later]


def test_an_order_before_the_merge_and_one_after_both_end_on_the_survivor(reg):
    """The two halves of the problem, named separately: the ORM before the A40
    is carried by merge_patient's scan, the one after is redirected by the
    alias."""
    before = _ingest_open(reg, "MRN_OLD", control_id="C1")
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    after = _ingest_open(reg, "MRN_OLD", control_id="C2")

    assert reg.get(before).mrn == "MRN_NEW", "the pre-merge order must be carried by the scan"
    assert reg.get(after).mrn == "MRN_NEW", "the post-merge order must be redirected by the alias"
    assert reg.store.open_loops(mrn="MRN_OLD") == []
    assert len(reg.store.open_loops(mrn="MRN_NEW")) == 2


def test_a_circular_merge_is_refused_and_changes_nothing(reg):
    """SPEC TEST 6. A -> B then B -> A.

    Resolving it by rule -- last writer wins, or stopping the walk where it
    started -- silently picks an arbitrary survivor and strands every loop on
    the losing side, which is this section's own failure mode chosen
    deliberately. So the merge is refused whole: table unmodified, no loop
    moved, a human told.
    """
    a = _ingest_open(reg, "MRN_A", control_id="C1")
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    before = reg.store.aliases()

    with pytest.raises(CircularMergeError):
        reg.merge_patient("MRN_B", "MRN_A", control_id="CM2")

    assert reg.store.aliases() == before, "the alias table must be unmodified"
    assert reg.get(a).mrn == "MRN_B", "no loop may move"
    assert len(reg.store.open_loops(mrn="MRN_B")) == 1
    assert reg.store.open_loops(mrn="MRN_A") == []
    # No identifier resolves to itself, i.e. the identity stayed acyclic.
    for retired, surviving in reg.store.aliases():
        assert retired != surviving
    assert reg.store.resolve_mrn("MRN_A") == "MRN_B"
    assert reg.store.resolve_mrn("MRN_B") == "MRN_B"


@pytest.mark.parametrize("length", [2, 3, 4])
def test_a_circular_merge_is_refused_at_any_cycle_length(reg, length):
    """A cycle closed after two hops or four is the same contradiction."""
    chain = [f"M{i}" for i in range(length)]
    for prior, surviving in zip(chain, chain[1:]):
        reg.merge_patient(prior, surviving, control_id="CM")
    before = reg.store.aliases()

    with pytest.raises(CircularMergeError):
        reg.merge_patient(chain[-1], chain[0], control_id="CM_CYCLE")

    assert reg.store.aliases() == before
    assert all(retired != surviving for retired, surviving in reg.store.aliases())


def test_alias_chains_compress_to_a_single_lookup(reg):
    """SPEC TEST 10. A -> B then B -> C resolves A -> C, in ONE lookup.

    Asserted at write time, not by chasing at read: the stored row for A must
    already say C. Chasing would put unbounded work on every inbound message --
    the depth set by how often registration has merged this patient -- and turn
    a cycle into a hang instead of a refusal.
    """
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    reg.merge_patient("MRN_B", "MRN_C", control_id="CM2")

    # The projection itself, not the accessor: no walk could be hiding here.
    assert dict(reg.store.aliases()) == {"MRN_A": "MRN_C", "MRN_B": "MRN_C"}
    assert reg.store.resolve_mrn("MRN_A") == "MRN_C"

    reads: list[str] = []
    original = LoopStore._read

    def counted(self, sql, params=()):
        reads.append(sql)
        return original(self, sql, params)

    LoopStore._read = counted
    try:
        assert reg.store.resolve_mrn("MRN_A") == "MRN_C"
    finally:
        LoopStore._read = original
    assert len(reads) == 1, f"resolution took {len(reads)} lookups, not one: {reads}"


def test_compression_holds_across_many_merges(reg):
    """The property compression exists for: resolution cost is flat in the
    number of times a patient has been merged."""
    for i in range(25):
        reg.merge_patient(f"M{i}", f"M{i + 1}", control_id=f"CM{i}")

    assert reg.store.resolve_mrn("M0") == "M25"
    # Every row points straight at the survivor -- none at another retired one.
    survivors = {surviving for _retired, surviving in reg.store.aliases()}
    assert survivors == {"M25"}
    retired = {r for r, _ in reg.store.aliases()}
    assert survivors.isdisjoint(retired), "a row points at a retired identifier"


def test_a_merge_against_an_already_retired_identifier_does_not_split_the_patient(reg):
    """A40s arrive against whatever identifier the sending system still knows,
    so 'A merges into C' can arrive after A was already merged into B. Recording
    the raw pair would leave B's loops on B while new orders on A went to C --
    one patient split across two identifiers."""
    a = _ingest_open(reg, "MRN_A", control_id="C1")
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    b_loop = _ingest_open(reg, "MRN_B", control_id="C2")

    reg.merge_patient("MRN_A", "MRN_C", control_id="CM2")  # against the retired one

    assert reg.store.resolve_mrn("MRN_A") == "MRN_C"
    assert reg.store.resolve_mrn("MRN_B") == "MRN_C"
    assert reg.get(a).mrn == "MRN_C"
    assert reg.get(b_loop).mrn == "MRN_C"
    assert reg.store.open_loops(mrn="MRN_B") == []
    assert len(reg.store.open_loops(mrn="MRN_C")) == 2


def test_loops_move_to_the_resolved_survivor_not_the_one_the_message_named(reg):
    """The A40's *surviving* MRN can itself already be retired.

    B is retired into C; then an A40 says A merges into B. The loops on A must
    land on C. Carrying them to the identifier the message named would park them
    on B -- an MRN that resolves elsewhere, so the loops are invisible to every
    query on the patient who actually survives, while resolution insists they
    have moved. Found by mutation: every other test in this file passes with the
    unresolved value, because in all of them the two happen to be equal.
    """
    reg.merge_patient("MRN_B", "MRN_C", control_id="CM1")
    a = _ingest_open(reg, "MRN_A", control_id="C1")

    reg.merge_patient("MRN_A", "MRN_B", control_id="CM2")  # target is already retired

    assert reg.get(a).mrn == "MRN_C", "the loop must land on the resolved survivor"
    assert reg.store.open_loops(mrn="MRN_B") == []
    assert [loop.loop_id for loop in reg.store.open_loops(mrn="MRN_C")] == [a]
    event = [e for e in reg.store.events_for(a) if e.event_type == "merged_in"][-1]
    assert event.detail["mrn"] == "MRN_C"
    assert event.detail["submitted_prior_mrn"] == "MRN_A"


def test_an_alias_target_that_later_becomes_a_prior_carries_everything_forward(reg):
    """B survives A, then B is itself retired in favour of C. A's loops must not
    be left behind by the second merge just because they arrived via the first.
    """
    a = _ingest_open(reg, "MRN_A", control_id="C1")
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    reg.merge_patient("MRN_B", "MRN_C", control_id="CM2")

    assert reg.get(a).mrn == "MRN_C"
    assert reg.get(_ingest_open(reg, "MRN_A", control_id="C9")).mrn == "MRN_C"
    assert reg.store.open_loops(mrn="MRN_A") == []
    assert reg.store.open_loops(mrn="MRN_B") == []


def test_the_event_log_records_what_the_message_said_and_what_was_stored(reg):
    """An auditor asking why this loop sits on a patient the message never named
    needs the answer in the log, not in the listener's memory."""
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    later = _ingest_open(reg, "MRN_OLD", control_id="C9")

    created = reg.store.events_for(later)[0]
    assert created.detail["mrn"] == "MRN_NEW"
    assert created.detail["submitted_mrn"] == "MRN_OLD"


def test_submitted_mrn_is_recorded_even_when_nothing_was_resolved(reg):
    """Always present, never 'absent means unchanged' -- an auditor cannot tell
    that apart from 'the field was not written yet'."""
    loop_id = _ingest_open(reg, "MRN_A", control_id="C1")
    detail = reg.store.events_for(loop_id)[0].detail
    assert detail["submitted_mrn"] == "MRN_A"
    assert detail["mrn"] == "MRN_A"


def test_an_orphan_carries_the_identifier_the_message_used(reg):
    """An orphan is retired by a coordinator attaching it to a real loop. Left
    on a retired MRN it is invisible to the coordinator searching the surviving
    patient, so it can never be attached and the result sits in the queue."""
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    raw = "MRN_OLD"

    orphan_id = reg.orphan(
        control_id="C9", mrn=reg.store.resolve_mrn(raw), detail={"obx11": "F"},
        submitted_mrn=raw,
    )

    assert reg.get(orphan_id).mrn == "MRN_NEW"
    assert reg.store.events_for(orphan_id)[0].detail["submitted_mrn"] == "MRN_OLD"


def test_an_unattributable_orphan_survives_resolution(reg):
    """resolve_mrn('') is '', so a result with no usable PID-3 still reaches the
    orphan queue instead of failing on the new ingest path -- and answers
    without touching the database, since resolution is now on every message."""
    assert reg.store.resolve_mrn("") == ""

    reads: list[str] = []
    original = LoopStore._read

    def counted(self, sql, params=()):
        reads.append(sql)
        return original(self, sql, params)

    LoopStore._read = counted
    try:
        assert reg.store.resolve_mrn("") == ""
    finally:
        LoopStore._read = original
    assert reads == [], "an unresolvable MRN should not cost a query"

    orphan_id = reg.orphan(control_id="C1", mrn="", detail={})
    assert reg.get(orphan_id).mrn == ""


def test_a_corrupt_projection_holding_a_self_pointer_blocks_further_merges(reg):
    """The post-condition inside _apply_alias, which the reasoning above it says
    can never fire.

    It can, for a projection this code did not write: a restore, or a foreign
    connection -- mrn_aliases is deliberately not append-only, because
    compression rewrites it. A self-pointer there means resolution has a fixed
    point that is also retired, so refusing further merges and demanding a
    rebuild is the only safe answer. Without the check the corruption is
    silently built upon.
    """
    with sqlite3.connect(reg.store.db_path) as conn:
        conn.execute(
            "INSERT INTO mrn_aliases (retired_mrn, surviving_mrn, established_at, "
            "established_by) VALUES ('MRN_SELF', 'MRN_SELF', ?, 'FOREIGN')",
            (T0.isoformat(),),
        )

    with pytest.raises(CircularMergeError):
        reg.merge_patient("MRN_P", "MRN_Q", control_id="CM")

    assert reg.store.resolve_mrn("MRN_P") == "MRN_P", "the refused merge wrote nothing"
    assert reg.store.alias_events() == []
    # And the remedy: rebuilding from the event log drops what nobody recorded.
    reg.store.rebuild_alias_projection()
    assert reg.store.aliases() == []
    reg.merge_patient("MRN_P", "MRN_Q", control_id="CM")
    assert reg.store.resolve_mrn("MRN_P") == "MRN_Q"


def test_an_mrn_retired_between_ingest_and_the_write_is_refused_not_stranded(reg):
    """Resolution happens at ingest, so a merge can commit before the loop is
    written. The loser would be a loop on a just-retired MRN -- invisible to the
    surviving patient and to that merge's straggler scan, which has already run.
    open_loop refuses instead, and the listener re-resolves and resends.
    """
    reg.open_loop(mrn="MRN_OLD", control_id="C0")
    resolved_at_ingest = reg.store.resolve_mrn("MRN_OLD")
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")  # lands in the window

    with pytest.raises(ReferralLoopError):
        reg.open_loop(mrn=resolved_at_ingest, control_id="C1", submitted_mrn="MRN_OLD")

    loop_id = _ingest_open(reg, "MRN_OLD", control_id="C1")  # the retry
    assert reg.get(loop_id).mrn == "MRN_NEW"
    assert reg.store.open_loops(mrn="MRN_OLD") == []


def test_a_self_alias_cannot_be_recorded(reg):
    with pytest.raises(CircularMergeError):
        reg.store.record_alias("MRN_A", "MRN_A", T0, "CM")


def test_an_alias_with_an_empty_mrn_is_refused(reg):
    with pytest.raises(StoreUnavailableError):
        reg.store.record_alias("", "MRN_B", T0, "CM")
    with pytest.raises(StoreUnavailableError):
        reg.store.record_alias("MRN_A", "", T0, "CM")


def test_a_merge_into_itself_records_no_alias(reg):
    """The self-merge no-op must not leave a self-edge for resolution to trip
    over."""
    reg.open_loop(mrn="MRN_A", control_id="C1")
    assert reg.merge_patient("MRN_A", "MRN_A", control_id="CM") == []
    assert reg.store.alias_count() == 0
    assert reg.store.resolve_mrn("MRN_A") == "MRN_A"


def test_a_repeated_merge_records_only_one_alias(reg):
    """The same A40 twice is an engine resending after a timeout, not a
    contradiction -- so it is a no-op, not a refusal."""
    reg.open_loop(mrn="MRN_OLD", control_id="C1")
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    assert reg.store.alias_count() == 1
    assert len([e for e in reg.store.alias_events()]) == 1


def test_the_alias_event_log_is_append_only_from_a_foreign_connection(tmp_path):
    """An alias silently edited is a patient's loops silently redirected. Same
    protection as loop_events, by triggers that travel with the file rather than
    an authorizer bound to our own connections."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.record_alias("MRN_A", "MRN_B", T0, "CM")

    with sqlite3.connect(db) as conn:
        for sql in (
            "DELETE FROM mrn_alias_events",
            "UPDATE mrn_alias_events SET surviving_mrn = 'MRN_EVIL'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)

    assert LoopStore(db).resolve_mrn("MRN_A") == "MRN_B"


def test_the_alias_projection_is_rebuildable_from_its_event_log(reg):
    """Spec 10.5. mrn_aliases is compressed and therefore derived; a restore
    that replays the events must reproduce it exactly, or every restart resumes
    stranding orders on retired MRNs."""
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    reg.merge_patient("MRN_B", "MRN_C", control_id="CM2")
    expected = reg.store.aliases()

    with sqlite3.connect(reg.store.db_path) as conn:
        conn.execute("DELETE FROM mrn_aliases")  # a projection may be dropped
    assert reg.store.aliases() == []

    assert reg.store.rebuild_alias_projection() == 2
    assert reg.store.aliases() == expected
    assert reg.store.resolve_mrn("MRN_A") == "MRN_C"


def test_the_alias_survives_a_process_restart(tmp_path):
    db = tmp_path / "loops.db"
    Registry(LoopStore(db)).merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    reopened = Registry(LoopStore(db))
    assert reopened.store.resolve_mrn("MRN_OLD") == "MRN_NEW"
    assert reopened.get(_ingest_open(reopened, "MRN_OLD", control_id="C1")).mrn == "MRN_NEW"


def test_the_alias_is_recorded_before_the_loops_move(reg, monkeypatch):
    """Ordering is the whole safety argument, since the two cannot share one
    transaction. Alias first: a crash leaves new orders resolving correctly and
    the resend finishes the moves. Loops first: a crash leaves loops moved and
    no alias -- the invisibility gap this table exists to close."""
    reg.open_loop(mrn="MRN_OLD", control_id="C1")
    order: list[str] = []
    original_alias = LoopStore.record_alias
    original_append = LoopStore.append_event

    def alias(self, *args, **kwargs):
        order.append("alias")
        return original_alias(self, *args, **kwargs)

    def append(self, event):
        order.append(f"event:{event.event_type}")
        return original_append(self, event)

    monkeypatch.setattr(LoopStore, "record_alias", alias)
    monkeypatch.setattr(LoopStore, "append_event", append)
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")

    assert order == ["alias", "event:merged_in"]


def test_a_merge_that_dies_before_moving_loops_still_redirects_new_orders(reg, monkeypatch):
    """The half-completed merge from the other side. The alias is durable, so
    orders arriving during the outage land correctly even though the existing
    loops have not moved yet -- and the resend then collects them."""
    stranded = reg.open_loop(mrn="MRN_OLD", control_id="C1")
    monkeypatch.setattr(
        LoopStore, "append_event",
        lambda self, event: (_ for _ in ()).throw(StoreUnavailableError("disk full")),
    )
    with pytest.raises(StoreUnavailableError):
        reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    monkeypatch.undo()

    assert reg.get(stranded).mrn == "MRN_OLD", "the loop did not move"
    assert reg.store.resolve_mrn("MRN_OLD") == "MRN_NEW", "but the alias is already durable"
    during_outage = _ingest_open(reg, "MRN_OLD", control_id="C2")
    assert reg.get(during_outage).mrn == "MRN_NEW"

    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")  # the resend
    assert reg.store.open_loops(mrn="MRN_OLD") == []
    assert len(reg.store.open_loops(mrn="MRN_NEW")) == 2


def test_a_circular_merge_refusal_leaves_no_partial_write(reg, monkeypatch):
    """Refused whole means whole: not one loop moved, not one event appended."""
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    _ingest_open(reg, "MRN_A", control_id="C1")
    events_before = len(reg.store.alias_events())
    appended: list[str] = []
    original = LoopStore.append_event
    monkeypatch.setattr(
        LoopStore, "append_event",
        lambda self, event: (appended.append(event.event_type), original(self, event))[1],
    )

    with pytest.raises(CircularMergeError):
        reg.merge_patient("MRN_B", "MRN_A", control_id="CM2")

    assert appended == [], "a refused merge appended a loop event"
    assert len(reg.store.alias_events()) == events_before, "a refused merge wrote an alias event"


# ------------------------------------------------- administrative reversal
# Aliases never expire, so this is the only way back from an A40 sent in error.


def test_reversing_a_merge_stops_redirecting_new_orders(reg):
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM")
    assert reg.store.resolve_mrn("MRN_A") == "MRN_B"

    reg.reverse_merge("MRN_A", actor="reg1", role="registrar", reason="wrong patient")

    assert reg.store.resolve_mrn("MRN_A") == "MRN_A"
    assert reg.get(_ingest_open(reg, "MRN_A", control_id="C9")).mrn == "MRN_A"


def test_reversing_a_merge_carries_the_merged_loops_back(reg):
    """The clinically important half. An alias reversal alone would stop
    redirecting new orders while leaving the merged loops on the surviving
    patient -- one patient's referrals on another's chart, worse than the merge
    it undoes."""
    a = _ingest_open(reg, "MRN_A", control_id="C1")
    native = _ingest_open(reg, "MRN_B", control_id="C2")
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM")
    assert reg.get(a).mrn == "MRN_B"

    carried = reg.reverse_merge("MRN_A", actor="reg1", role="registrar", reason="wrong patient")

    assert carried == [a]
    assert reg.get(a).mrn == "MRN_A", "the merged loop must go back"
    assert reg.get(native).mrn == "MRN_B", "a loop that was always B's must stay"
    assert [loop.loop_id for loop in reg.store.open_loops(mrn="MRN_A")] == [a]
    assert [loop.loop_id for loop in reg.store.open_loops(mrn="MRN_B")] == [native]


def test_a_reversal_does_not_change_loop_state(reg):
    a = _seed(reg, [("result", {"obx11": "F"}), ("ack", {})])
    reg.merge_patient("MRN_OLD", "MRN_NEW", control_id="CM")
    reg.reverse_merge("MRN_OLD", actor="reg1", role="registrar", reason="wrong patient")

    assert reg.get(a).state is LoopState.ACKNOWLEDGED
    assert reg.get(a).mrn == "MRN_OLD"


def test_a_reversal_is_appended_never_deleted(reg):
    """Same posture as rule 4's acknowledgement reversal: the merge and its
    undoing both stay in the log, so an auditor sees a human decided."""
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM")
    reg.reverse_merge("MRN_A", actor="reg1", role="registrar", reason="typo in registration")

    kinds = [row["event_type"] for row in reg.store.alias_events()]
    assert kinds == ["established", "reversed"], "the establishment must survive its reversal"
    reversal = reg.store.alias_events()[-1]
    assert "typo in registration" in reversal["detail"]
    assert "reg1" in reversal["detail"]


def test_a_reversal_needs_an_actor_role_and_reason(reg):
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM")
    for actor, role, reason in [("", "r", "w"), ("a", "", "w"), ("a", "r", "")]:
        with pytest.raises(ReferralLoopError):
            reg.reverse_merge("MRN_A", actor=actor, role=role, reason=reason)
    assert reg.store.resolve_mrn("MRN_A") == "MRN_B", "a refused reversal must change nothing"


def test_reversing_a_merge_that_never_happened_is_refused(reg):
    with pytest.raises(ReferralLoopError):
        reg.reverse_merge("MRN_A", actor="reg1", role="registrar", reason="none")


def test_a_reversal_uncompresses_the_chain_it_was_part_of(reg):
    """Compression is lossy, so the projection is rebuilt from the log rather
    than edited. A -> B -> C reversed at A must leave B -> C standing."""
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    reg.merge_patient("MRN_B", "MRN_C", control_id="CM2")
    assert dict(reg.store.aliases()) == {"MRN_A": "MRN_C", "MRN_B": "MRN_C"}

    reg.reverse_merge("MRN_A", actor="reg1", role="registrar", reason="wrong patient")

    assert dict(reg.store.aliases()) == {"MRN_B": "MRN_C"}
    assert reg.store.resolve_mrn("MRN_A") == "MRN_A"
    assert reg.store.resolve_mrn("MRN_B") == "MRN_C"


def test_a_merge_can_be_re_established_after_a_reversal(reg):
    """A reversal is not a permanent ban: registration may merge them again,
    correctly, later."""
    a = _ingest_open(reg, "MRN_A", control_id="C1")
    reg.merge_patient("MRN_A", "MRN_B", control_id="CM1")
    reg.reverse_merge("MRN_A", actor="reg1", role="registrar", reason="wrong patient")

    reg.merge_patient("MRN_A", "MRN_B", control_id="CM2")

    assert reg.store.resolve_mrn("MRN_A") == "MRN_B"
    assert reg.get(a).mrn == "MRN_B"


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
