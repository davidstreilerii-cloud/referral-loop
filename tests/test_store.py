"""Spec tests 8 and 10: persist-before-ACK and state reconstruction."""
import sqlite3
from datetime import datetime, timezone

import pytest

from healthcare_rag.referral_loop.errors import StoreUnavailableError
from healthcare_rag.referral_loop.events import LoopEvent, LoopState
from healthcare_rag.referral_loop.store import LoopStore

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def test_raw_message_persists_before_any_parse(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    store.record_raw("CTRL1", "MSH|raw payload")
    assert store.raw_count() == 1


def test_duplicate_control_id_is_a_noop(tmp_path):
    """Failure matrix: duplicate MSH-10 is a no-op, logged, never a second transition."""
    store = LoopStore(tmp_path / "loops.db")
    assert store.record_raw("CTRL1", "MSH|first") is True
    assert store.record_raw("CTRL1", "MSH|second") is False
    assert store.raw_count() == 1


def test_state_is_reconstructible_from_events_alone(tmp_path):
    """Spec test 10: the property an auditor will ask you to demonstrate."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1", "modality": "CT"}))
    store.append_event(LoopEvent("L1", "scheduled", NOW, "C2", {}))
    store.append_event(LoopEvent("L1", "resulted", NOW, "C3", {"obx11": "F"}))

    loop = store.replay("L1")
    assert loop.state is LoopState.RESULTED
    assert loop.mrn == "MRN1"
    assert loop.modality == "CT"


def test_events_are_append_only(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    import sqlite3
    with sqlite3.connect(tmp_path / "loops.db") as conn:
        try:
            conn.execute("DELETE FROM loop_events")
            deleted = True
        except sqlite3.DatabaseError:
            deleted = False
    assert deleted is False, "loop_events must reject DELETE"


def test_replay_survives_process_restart(tmp_path):
    """Spec test 8: kill between durable write and parse; replay reconstructs state."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.record_raw("CTRL1", "MSH|payload")
    store.append_event(LoopEvent("L1", "created", NOW, "CTRL1", {"mrn": "MRN1"}))
    del store  # simulate process death before parse completed

    reopened = LoopStore(db)
    assert reopened.raw_count() == 1
    assert reopened.replay("L1").state is LoopState.OPEN


def test_an_event_can_clear_a_field(tmp_path):
    """Safety rule 2 depends on this: a corrected result must clear the prior
    acknowledgement, and the event log is the only way state changes."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent("L1", "closed", NOW, "C2",
                                 {"ack_by": "coord1", "ack_role": "coordinator",
                                  "ack_at": NOW.isoformat()}))
    assert store.replay("L1").ack_by == "coord1"

    store.append_event(LoopEvent("L1", "reopened", NOW, "C3",
                                 {"ack_by": "", "ack_role": "", "ack_at": ""}))
    reopened = store.replay("L1")
    assert reopened.ack_by == ""
    assert reopened.ack_at is None


def test_unknown_event_type_is_refused_at_append(tmp_path):
    """A typo must not leave the loop quietly in its prior state -- and must be
    caught before the insert, since an append-only log cannot be corrected."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))

    with pytest.raises(StoreUnavailableError, match="resluted"):
        store.append_event(LoopEvent("L1", "resluted", NOW, "C2", {"obx11": "F"}))

    assert len(store.events_for("L1")) == 1, "the bad event must not be in the log"
    assert store.replay("L1").state is LoopState.OPEN


def test_replay_rejects_an_unknown_event_type_written_out_of_band(tmp_path):
    """Defence in depth for a restored or foreign-written log: replay must
    refuse rather than derive a state the events do not support."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO loop_events (loop_id, event_type, occurred_at, control_id, detail) "
            "VALUES ('L1', 'resluted', ?, 'C2', '{}')", (NOW.isoformat(),)
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StoreUnavailableError, match="resluted"):
        store.replay("L1")


def test_merged_in_carries_fields_without_changing_state(tmp_path):
    """The one deliberate non-transitional event type still works."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent("L1", "scheduled", NOW, "C2", {}))
    store.append_event(LoopEvent("L1", "merged_in", NOW, "C3", {"mrn": "MRN2"}))

    loop = store.replay("L1")
    assert loop.state is LoopState.SCHEDULED
    assert loop.mrn == "MRN2"


def test_durability_pragmas_are_pinned_not_defaulted(tmp_path):
    """persist-before-ACK holds only while these hold. Fail loudly if flipped."""
    store = LoopStore(tmp_path / "loops.db")
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2, "must be FULL"
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    finally:
        conn.close()


def test_replace_into_cannot_overwrite_the_raw_archive(tmp_path):
    """REPLACE's implicit delete must fire the append-only trigger."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.record_raw("C1", "ORIGINAL EVIDENCE")

    conn = store._connect()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("REPLACE INTO raw_messages VALUES ('C1', 'TAMPERED', 'now')")
    finally:
        conn.close()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT payload FROM raw_messages").fetchone()[0] == "ORIGINAL EVIDENCE"
    finally:
        conn.close()


def test_empty_control_id_is_refused(tmp_path):
    """SQLite allows repeated NULLs in a TEXT primary key, so dedup would fail
    silently. Unkeyable means the listener must answer AE, not AA."""
    store = LoopStore(tmp_path / "loops.db")
    for bad in (None, ""):
        with pytest.raises(StoreUnavailableError):
            store.record_raw(bad, "MSH|payload")
    assert store.raw_count() == 0


def test_received_at_is_timezone_aware_utc(tmp_path):
    """Naive local timestamps in an evidence archive are DST-ambiguous."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.record_raw("C1", "MSH|payload")

    conn = sqlite3.connect(db)
    try:
        received_at = conn.execute("SELECT received_at FROM raw_messages").fetchone()[0]
    finally:
        conn.close()
    assert datetime.fromisoformat(received_at).tzinfo is not None


def test_projection_is_rebuildable_from_the_event_log(tmp_path):
    """A restore that replays events must not leave the worklist empty."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent("L2", "scheduled", NOW, "C2", {"mrn": "MRN2"}))
    assert len(store.open_loops()) == 2

    conn = sqlite3.connect(db)  # loops is a projection, so it is deletable
    try:
        conn.execute("DELETE FROM loops")
        conn.commit()
    finally:
        conn.close()
    assert store.open_loops() == []

    assert store.rebuild_projection() == 2
    assert {loop.loop_id for loop in store.open_loops()} == {"L1", "L2"}
