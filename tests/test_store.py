"""Spec tests 8 and 10: persist-before-ACK and state reconstruction."""
from datetime import datetime, timezone

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
