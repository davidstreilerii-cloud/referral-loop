"""Spec tests 8 and 10: persist-before-ACK and state reconstruction."""
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from contextlib import closing
from datetime import datetime, timezone

import pytest

from healthcare_rag.referral_loop.errors import LoopNotFoundError, StoreUnavailableError
from healthcare_rag.referral_loop.events import LoopEvent, LoopState
from healthcare_rag.referral_loop.store import LoopStore

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
    with sqlite3.connect(tmp_path / "loops.db") as conn:
        try:
            conn.execute("DELETE FROM loop_events")
            deleted = True
        except sqlite3.DatabaseError:
            deleted = False
    assert deleted is False, "loop_events must reject DELETE"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM loop_events",
        "DELETE FROM loop_events WHERE loop_id = 'L1'",
        "UPDATE loop_events SET detail = '{\"ack_by\": \"forged\"}'",
        "UPDATE loop_events SET event_type = 'closed' WHERE loop_id = 'L1'",
        "UPDATE loop_events SET loop_id = 'L2' FROM loops WHERE loops.loop_id = 'L1'",
        "REPLACE INTO loop_events VALUES (1, 'L1', 'closed', 'x', 'C1', '{}')",
    ],
)
def test_loop_events_rejects_every_mutation_from_a_foreign_connection(tmp_path, sql):
    """UPDATE matters at least as much as DELETE: silently editing an event's
    detail to restore a cleared ack_by is the more plausible tamper."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    before = store.events_for("L1")

    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA recursive_triggers = ON")  # REPLACE's implicit delete
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
            conn.commit()

    assert store.events_for("L1") == before


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


_HARD_KILL_CHILD = """
import os, sys, time
sys.path.insert(0, {repo!r})
from datetime import datetime, timezone
from healthcare_rag.referral_loop.events import LoopEvent
from healthcare_rag.referral_loop.store import LoopStore

store = LoopStore({db!r})
store.record_raw("CTRL1", "MSH|must survive")
store.append_event(LoopEvent("L1", "created", datetime.now(timezone.utc), "CTRL1",
                             {{"mrn": "MRN1"}}))
with open({ready!r}, "w") as fh:      # both writes have RETURNED
    fh.write(str(os.getpid()))
time.sleep(120)
"""


@pytest.mark.skipif(
    os.name == "nt" and shutil.which("taskkill") is None,
    reason="no taskkill available to kill the child without cleanup",
)
def test_durability_survives_a_hard_kill(tmp_path):
    """The only behavioural proof of persist-before-ACK.

    test_replay_survives_process_restart uses `del store`, which is not process
    death -- LoopStore holds no connection between calls and everything has
    already committed, so it passes even with PRAGMA synchronous = OFF. This
    kills the interpreter outright: no atexit, no __del__, no conn.close().
    """
    db = tmp_path / "loops.db"
    ready = tmp_path / "ready"
    child = tmp_path / "child.py"
    child.write_text(_HARD_KILL_CHILD.format(
        repo=REPO_ROOT, db=str(db), ready=str(ready)))

    proc = subprocess.Popen([sys.executable, str(child)])
    try:
        deadline = time.time() + 60
        while not ready.exists() and time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"child exited early with {proc.returncode}")
            time.sleep(0.05)
        assert ready.exists(), "child never confirmed its writes returned"

        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=True)
        else:
            os.kill(proc.pid, __import__("signal").SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()

    reopened = LoopStore(db)
    assert reopened.raw_count() == 1, "raw message did not survive the kill"
    assert reopened.replay("L1").state is LoopState.OPEN
    # The projection committed with the event, so the loop is on the worklist.
    assert [loop.loop_id for loop in reopened.open_loops()] == ["L1"]


def test_projection_and_log_cannot_diverge(tmp_path):
    """A crash between the two writes must not leave an event in the
    authoritative log with no row on the coordinator's worklist."""
    store = LoopStore(tmp_path / "loops.db")

    def boom(self, loop_id, conn):
        raise RuntimeError("simulated failure between the two writes")

    original = LoopStore._materialize
    LoopStore._materialize = boom
    try:
        with pytest.raises(RuntimeError):
            store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    finally:
        LoopStore._materialize = original

    assert store.events_for("L1") == [], "event must roll back with its projection"
    assert store.open_loops() == []

    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    assert len(store.events_for("L1")) == 1
    assert [loop.loop_id for loop in store.open_loops()] == ["L1"]


def test_store_failures_are_typed_not_raw_sqlite_errors(tmp_path):
    """Task 10's listener answers AE on StoreUnavailableError. A raw
    OperationalError would sail past that handler and break persist-before-ACK,
    and disk-full / unmounted-volume / permission-denied all land here."""
    workdir = tmp_path / "volume"
    workdir.mkdir()
    store = LoopStore(workdir / "loops.db")
    store.record_raw("C0", "MSH|before")
    shutil.rmtree(workdir)  # the volume goes away underneath us

    with pytest.raises(StoreUnavailableError):
        store.record_raw("C1", "MSH|payload")
    with pytest.raises(StoreUnavailableError):
        store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    with pytest.raises(StoreUnavailableError):
        store.raw_count()
    with pytest.raises(StoreUnavailableError):
        store.open_loops()


def test_replay_of_an_unknown_loop_raises_a_referral_loop_error(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    with pytest.raises(LoopNotFoundError):
        store.replay("nope")


def test_a_reopened_loop_is_visible_to_someone(tmp_path):
    """Safety rule 2 clears the acknowledgement; that loop needs a human, and
    open_loops() does not select RESULTED."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent("L1", "resulted", NOW, "C2", {"obx11": "F"}))
    store.append_event(LoopEvent("L1", "closed", NOW, "C3",
                                 {"ack_by": "coord1", "ack_at": NOW.isoformat()}))
    assert store.resulted_unacknowledged() == []

    store.append_event(LoopEvent("L1", "reopened", NOW, "C4",
                                 {"ack_by": "", "ack_role": "", "ack_at": ""}))
    assert store.open_loops() == [], "reopened maps to RESULTED, not OPEN"
    assert [loop.loop_id for loop in store.resulted_unacknowledged()] == ["L1"]

    # A resulted loop nobody ever acknowledged counts too.
    store.append_event(LoopEvent("L2", "created", NOW, "C5", {"mrn": "MRN2"}))
    store.append_event(LoopEvent("L2", "resulted", NOW, "C6", {"obx11": "F"}))
    assert {loop.loop_id for loop in store.resulted_unacknowledged()} == {"L1", "L2"}
    assert {loop.loop_id for loop in store.resulted_unacknowledged(mrn="MRN2")} == {"L2"}


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
