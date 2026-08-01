"""Spec tests 8 and 10: persist-before-ACK and state reconstruction."""
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone

import pytest

from referral_loop.errors import LoopNotFoundError, StoreUnavailableError
from referral_loop.events import LoopEvent, LoopState
from referral_loop.store import LoopStore

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The package moved under src/, so the repo root is no longer the directory that
# holds it. A child pointed at the repo root would import whatever copy happens to
# be installed instead of the tree under test.
SRC_ROOT = os.path.join(REPO_ROOT, "src")


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
        "UPDATE loop_events SET event_type = 'acknowledged' WHERE loop_id = 'L1'",
        "UPDATE loop_events SET loop_id = 'L2' FROM loops WHERE loops.loop_id = 'L1'",
        "REPLACE INTO loop_events VALUES (1, 'L1', 'acknowledged', 'x', 'C1', '{}')",
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
sys.path.insert(0, {src!r})
from datetime import datetime, timezone
from referral_loop.events import LoopEvent
from referral_loop.store import LoopStore

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
        src=SRC_ROOT, db=str(db), ready=str(ready)))

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
    store.append_event(LoopEvent("L1", "acknowledged", NOW, "C3",
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
    store.append_event(LoopEvent("L1", "acknowledged", NOW, "C2",
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


# ------------------------------------------------- applied_messages (Task 10)


def test_loops_in_states_returns_only_the_requested_states(tmp_path):
    """Found by mutation: making this ignore its filter passed the whole suite,
    because the matcher re-checks state and the extra candidates changed no
    outcome. That makes the filter unobservable through the listener but not
    unimportant -- it is a public store method whose contract the next caller
    will rely on, and an untested contract is one that drifts.
    """
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L-open", "created", NOW, "C1", {"mrn": "M1"}))
    store.append_event(LoopEvent("L-done", "created", NOW, "C2", {"mrn": "M1"}))
    store.append_event(LoopEvent("L-done", "cancelled", NOW, "C3", {}))

    ids = {loop.loop_id for loop in store.loops_in_states([LoopState.OPEN])}
    assert ids == {"L-open"}, "a CANCELLED loop is not OPEN"

    both = store.loops_in_states([LoopState.OPEN, LoopState.CANCELLED])
    assert {loop.loop_id for loop in both} == {"L-open", "L-done"}
    assert store.loops_in_states([]) == []


def _candidate_store(tmp_path) -> LoopStore:
    """Two patients, each with an open loop, plus one order number shared."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L-a", "created", NOW, "C1", {
        "mrn": "M_A", "placer_order_number": "P_A", "filler_order_number": "F_A"}))
    store.append_event(LoopEvent("L-b", "created", NOW, "C2", {
        "mrn": "M_B", "placer_order_number": "P_SHARED", "filler_order_number": "F_B"}))
    store.append_event(LoopEvent("L-c", "created", NOW, "C3", {"mrn": "M_C"}))
    return store


def test_loops_in_states_narrows_to_one_patient(tmp_path):
    """Ingest must be able to ask for one patient's loops rather than every
    patient's. Unscoped, an arriving result is compared against the whole
    table, and the only thing standing between it and another patient's loop is
    a field the sender chose."""
    store = _candidate_store(tmp_path)
    found = store.loops_in_states([LoopState.OPEN], mrn="M_A")
    assert {loop.loop_id for loop in found} == {"L-a"}


def test_loops_in_states_also_admits_the_order_numbers_named(tmp_path):
    """The other patient's loop is still reachable *by order number*, which is
    what keeps a cross-feed numbering collision detectable: the matcher can only
    report a collision it was shown."""
    store = _candidate_store(tmp_path)
    found = store.loops_in_states([LoopState.OPEN], mrn="M_A", order_numbers=("P_SHARED",))
    assert {loop.loop_id for loop in found} == {"L-a", "L-b"}


def test_loops_in_states_ignores_an_empty_order_number(tmp_path):
    """`""` is not an order number, and a loop carrying no placer must not be
    admitted by a result carrying no placer -- the `"" == ""` equivalence class
    that would hand back most of the table under the guise of a narrowed query.
    """
    store = _candidate_store(tmp_path)
    found = store.loops_in_states([LoopState.OPEN], mrn="M_A", order_numbers=("", ""))
    assert {loop.loop_id for loop in found} == {"L-a"}


def test_loops_in_states_is_unnarrowed_when_no_filter_is_given(tmp_path):
    """The worklist asks for every loop in a state and must keep getting it."""
    store = _candidate_store(tmp_path)
    found = store.loops_in_states([LoopState.OPEN])
    assert {loop.loop_id for loop in found} == {"L-a", "L-b", "L-c"}


def test_a_content_key_can_only_be_claimed_once(tmp_path):
    """The unique index, tested at the store rather than through the listener.

    Found by mutation: dropping UNIQUE passed the whole suite, because the
    listener serializes every message behind one lock and the check-then-insert
    never actually races in-process. That makes the constraint look redundant
    and it is not -- the lock covers one process, and the index is the only
    thing standing between two processes on one database file and a second
    `resulted` transition for the same result.
    """
    store = LoopStore(tmp_path / "loops.db")
    assert store.record_applied("CTRL_A", "sha-of-the-result") is True
    assert store.record_applied("CTRL_B", "sha-of-the-result") is False, (
        "a second control id must not be able to claim content already applied"
    )
    assert store.content_key_owner("sha-of-the-result") == "CTRL_A"
    assert store.applied_count() == 1


def test_a_control_id_can_only_be_applied_once(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    assert store.record_applied("CTRL_A", "key-1") is True
    assert store.record_applied("CTRL_A", "key-2") is False
    assert store.control_id_applied("CTRL_A") is True
    assert store.control_id_applied("CTRL_NEVER_SEEN") is False


def test_messages_with_no_content_key_do_not_collide(tmp_path):
    """content_key is NULL for unknown types and for content duplicates. A
    non-partial UNIQUE index on an engine treating NULLs as equal would let the
    first such message block every later one."""
    store = LoopStore(tmp_path / "loops.db")
    assert store.record_applied("CTRL_A", None) is True
    assert store.record_applied("CTRL_B", None) is True
    assert store.applied_count() == 2


def test_applied_messages_is_append_only(tmp_path):
    """Same guarantee as the archive: a deleted row is a message applied twice,
    and an updated one is a content key reassigned to a message that never
    carried it."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.record_applied("CTRL_A", "key-1")
    with closing(sqlite3.connect(db)) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM applied_messages")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE applied_messages SET content_key = 'other'")


def test_record_applied_refuses_an_empty_control_id(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    with pytest.raises(StoreUnavailableError):
        store.record_applied("", "key-1")


def test_raw_payloads_returns_messages_in_receipt_order(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    for control_id, payload in (("C1", "first"), ("C2", "second"), ("C3", "third")):
        store.record_raw(control_id, payload)
        time.sleep(0.002)
    assert store.raw_payloads() == ["first", "second", "third"]
