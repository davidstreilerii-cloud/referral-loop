"""Spec section 6: retention is configured, not assumed.

The assertion that matters in this file is that an open loop cannot be deleted
by age through any route that can be constructed, including a `loops`
projection that has been rewritten to lie about a loop's state. Everything else
here is bookkeeping around that one property.
"""
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from referral_loop import audit, retention
from referral_loop import store as store_module
from referral_loop.errors import ReferralLoopError, StoreUnavailableError
from referral_loop.events import LabelType, LoopEvent, LoopState
from referral_loop.registry import Registry
from referral_loop.retention import (
    RAW_DAYS_ENV,
    RESOLVED_DAYS_ENV,
    RetentionPolicy,
    purge,
)
from referral_loop.store import LoopStore

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
LONG_AGO = NOW - timedelta(days=400)
YESTERDAY = NOW - timedelta(days=1)

GOOD_ENV = {RAW_DAYS_ENV: "30", RESOLVED_DAYS_ENV: "365"}
POLICY = RetentionPolicy(raw_days=30, resolved_days=365)

# The four states spec section 4 makes terminal in v1, and the event type that
# enters each. ORPHAN is deliberately absent -- it is a result awaiting a human.
TERMINAL = {
    LoopState.ACKNOWLEDGED: "acknowledged",
    LoopState.CANCELLED: "cancelled",
    LoopState.DISMISSED: "dismissed",
    LoopState.ATTACHED: "attached",
}
NON_TERMINAL = {
    LoopState.OPEN: "created",
    LoopState.SCHEDULED: "scheduled",
    LoopState.RESULTED: "resulted",
    LoopState.ORPHAN: "orphaned",
}


# ------------------------------------------------------------------- helpers


def fresh(tmp_path) -> LoopStore:
    return LoopStore(tmp_path / "loops.db")


def aged_loop(store: LoopStore, loop_id: str, final_event: str, when: datetime,
              detail: dict | None = None) -> str:
    """A loop created and then driven to `final_event`, every event at `when`.

    Written through append_event with an explicit occurred_at rather than
    through a backdating helper: there is no UPDATE path over loop_events and
    this file is not going to add one.
    """
    store.append_event(LoopEvent(loop_id, "created", when, f"{loop_id}-C1", {"mrn": "MRN1"}))
    if final_event != "created":
        store.append_event(LoopEvent(loop_id, final_event, when, f"{loop_id}-C2", detail or {}))
    return loop_id


def insert_raw(db: Path, control_id: str, when: datetime, payload: str = "MSH|x") -> None:
    """Insert an already-aged archive row. INSERT is not what the triggers block."""
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO raw_messages (control_id, payload, received_at) VALUES (?, ?, ?)",
            (control_id, payload, when.isoformat()),
        )
        conn.commit()


def loop_ids(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {r[0] for r in conn.execute("SELECT loop_id FROM loops")}


def event_loop_ids(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {r[0] for r in conn.execute("SELECT DISTINCT loop_id FROM loop_events")}


def raw_ids(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {r[0] for r in conn.execute("SELECT control_id FROM raw_messages")}


def triggers(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}


# ----------------------------------------------------------------- the policy


def test_policy_must_be_configured_never_defaulted():
    """An unset retention period is a decision the hospital has not made."""
    with pytest.raises(ReferralLoopError):
        RetentionPolicy.from_env({})


@pytest.mark.parametrize("present", [RAW_DAYS_ENV, RESOLVED_DAYS_ENV])
def test_one_period_configured_and_not_the_other_is_refused(present):
    """Half a policy is not a policy. Purging raw on a stated period while
    guessing at loops -- or the reverse -- is the guess the refusal exists to
    prevent, made about whichever half the site forgot."""
    with pytest.raises(ReferralLoopError) as exc:
        RetentionPolicy.from_env({present: "30"})
    assert RAW_DAYS_ENV in str(exc.value) and RESOLVED_DAYS_ENV in str(exc.value)


def test_policy_reads_both_periods_from_env():
    policy = RetentionPolicy.from_env(GOOD_ENV)
    assert policy.raw_days == 30
    assert policy.resolved_days == 365


def test_the_refusal_names_both_variables_a_site_must_set():
    with pytest.raises(ReferralLoopError) as exc:
        RetentionPolicy.from_env({})
    assert RAW_DAYS_ENV in str(exc.value)
    assert RESOLVED_DAYS_ENV in str(exc.value)


def test_a_blank_value_is_not_a_configured_period():
    with pytest.raises(ReferralLoopError):
        RetentionPolicy.from_env({RAW_DAYS_ENV: "  ", RESOLVED_DAYS_ENV: "365"})


@pytest.mark.parametrize("bad", ["0", "-1", "-365", "thirty", "30.5", "", "1e3", "0x1f", "3 0"])
def test_an_unusable_period_is_refused_rather_than_coerced(bad):
    """Zero is refused with the rest, and deliberately. It means "keep nothing",
    which destroys the replay archive section 7 evaluates a pack against and
    deletes a loop in the same second it resolves -- and it is indistinguishable
    from a placeholder somebody typed intending to fill it in."""
    with pytest.raises(ReferralLoopError):
        RetentionPolicy.from_env({RAW_DAYS_ENV: bad, RESOLVED_DAYS_ENV: "365"})
    with pytest.raises(ReferralLoopError):
        RetentionPolicy.from_env({RAW_DAYS_ENV: "30", RESOLVED_DAYS_ENV: bad})


def test_surrounding_whitespace_is_tolerated_but_nothing_else_is():
    """A value pasted into a systemd unit or a .env file carries whitespace.
    That is not the site failing to decide; "3 0" is not a number."""
    assert RetentionPolicy.from_env(
        {RAW_DAYS_ENV: " 30\n", RESOLVED_DAYS_ENV: "\t365 "}
    ) == RetentionPolicy(raw_days=30, resolved_days=365)


def test_a_period_beyond_a_century_is_refused_at_configuration_time():
    """timedelta(days=10**9) raises OverflowError. Better here, with the
    variable named, than inside a purge an operator scheduled at 3am."""
    with pytest.raises(ReferralLoopError):
        RetentionPolicy.from_env({RAW_DAYS_ENV: "1000000000", RESOLVED_DAYS_ENV: "365"})


@pytest.mark.parametrize("bad", [0, -1, True, 30.0, "30", None, 10 ** 9])
def test_validation_holds_on_direct_construction_not_only_through_the_env(bad):
    """from_env is not the only door. A caller constructing the dataclass gets
    the same refusals, or the validation is decoration."""
    with pytest.raises(ReferralLoopError):
        RetentionPolicy(raw_days=bad, resolved_days=365)
    with pytest.raises(ReferralLoopError):
        RetentionPolicy(raw_days=30, resolved_days=bad)


def test_the_v2_named_variable_is_refused_by_name():
    """`REFERRAL_CLOSED_RETENTION_DAYS` is what the plan wrote before the
    ACKNOWLEDGED/CLOSED split. Reading it would let a site configure retention
    under the name of a state this version refuses to enter."""
    with pytest.raises(ReferralLoopError) as exc:
        RetentionPolicy.from_env(
            {RAW_DAYS_ENV: "30", "REFERRAL_CLOSED_RETENTION_DAYS": "365"}
        )
    assert RESOLVED_DAYS_ENV in str(exc.value)


def test_the_policy_is_frozen():
    policy = RetentionPolicy.from_env(GOOD_ENV)
    with pytest.raises(Exception):
        policy.raw_days = 1


# ------------------------------------------------------------ the purgeable set


def test_only_terminal_states_are_purgeable():
    assert set(retention.PURGEABLE_STATES) == set(TERMINAL)


@pytest.mark.parametrize("state", list(NON_TERMINAL) + [LoopState.CLOSED])
def test_no_non_terminal_state_is_in_the_purgeable_set(state):
    """ORPHAN is a result awaiting a human, not a resolution. CLOSED is reserved
    for v2 and unreachable, so v1 must hold no opinion about deleting it."""
    assert state not in retention.PURGEABLE_STATES


def test_the_purgeable_set_partitions_every_state():
    """A state added later lands in neither set until somebody decides, and
    this fails rather than defaulting it into the deletable half."""
    assert set(retention.PURGEABLE_STATES) | set(retention.NEVER_PURGEABLE_STATES) == set(LoopState)
    assert not set(retention.PURGEABLE_STATES) & set(retention.NEVER_PURGEABLE_STATES)


def test_the_shipped_policy_stays_inside_the_stores_floor():
    """The store carries its own refusal, so the two lists exist in two places.
    They are checked against each other rather than trusted to agree -- the same
    treatment the duplicated label patterns in store.py get."""
    assert not set(retention.PURGEABLE_STATES) & store_module._NEVER_DELETABLE
    assert store_module._NEVER_DELETABLE <= set(retention.NEVER_PURGEABLE_STATES)


@pytest.mark.parametrize("state", sorted(s.value for s in NON_TERMINAL) + ["CLOSED"])
def test_the_store_refuses_a_caller_who_says_an_open_state_is_purgeable(tmp_path, state):
    """purge_retention takes the set as an argument, so a caller can hand it
    OPEN. The refusal lives where the write happens and does not depend on the
    policy above it being right -- the same posture append_event takes towards
    the reserved CLOSED event."""
    store = fresh(tmp_path)
    loop_id = aged_loop(store, "L-00000000c001", NON_TERMINAL.get(LoopState(state), "created"),
                        NOW - timedelta(days=9999))

    with pytest.raises(ReferralLoopError) as exc:
        store.purge_retention(
            raw_cutoff=NOW, loop_cutoff=NOW,
            purgeable_states=("ACKNOWLEDGED", state),
        )

    assert state in str(exc.value)
    assert store.replay(loop_id).state is not None


def test_the_store_floor_holds_even_when_it_is_the_only_state_asked_for(tmp_path):
    store = fresh(tmp_path)
    loop_id = aged_loop(store, "L-00000000c002", "orphaned", NOW - timedelta(days=9999))
    with pytest.raises(ReferralLoopError):
        store.purge_retention(raw_cutoff=NOW, loop_cutoff=NOW, purgeable_states=("ORPHAN",))
    assert store.replay(loop_id).state is LoopState.ORPHAN


def test_an_empty_purgeable_set_deletes_no_loop(tmp_path):
    store = fresh(tmp_path)
    aged_loop(store, "L-00000000c003", "acknowledged", LONG_AGO)
    report = store.purge_retention(raw_cutoff=NOW, loop_cutoff=NOW, purgeable_states=())
    assert report["loops_deleted"] == 0


# ------------------------------------------------------------- the raw archive


def test_purge_removes_raw_messages_past_the_window(tmp_path):
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", NOW - timedelta(days=40))
    insert_raw(db, "NEW", YESTERDAY)

    report = purge(store, POLICY, now=NOW)

    assert report["raw_deleted"] == 1
    assert raw_ids(db) == {"NEW"}


def test_a_raw_message_exactly_at_the_cutoff_is_kept(tmp_path):
    """Older *than* the window, not as old as it. One second either side of a
    boundary is the difference between a policy and an off-by-one."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "EDGE", NOW - timedelta(days=30))
    insert_raw(db, "PAST", NOW - timedelta(days=30, seconds=1))

    report = purge(store, POLICY, now=NOW)

    assert report["raw_deleted"] == 1
    assert raw_ids(db) == {"EDGE"}


def test_a_raw_message_with_an_unreadable_timestamp_is_kept(tmp_path):
    """A row whose age cannot be established has no age. Deleting it on the
    strength of a parse failure is the one direction that cannot be undone."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO raw_messages (control_id, payload, received_at) VALUES (?, ?, ?)",
            ("JUNK", "MSH|x", "not-a-timestamp"),
        )
        conn.commit()

    report = purge(store, POLICY, now=NOW)

    assert report["raw_deleted"] == 0
    assert report["raw_retained_unreadable"] == 1
    assert raw_ids(db) == {"JUNK"}


def test_a_null_timestamp_is_kept_rather_than_treated_as_infinitely_old(tmp_path):
    """A NULL reaches Python as None, not as a string. Found by mutation M5:
    the non-string branch of the age check was not covered by any test, so
    flipping it to "old" survived -- and a NULL received_at would then have made
    every such row instantly purgeable."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("DROP TABLE raw_messages")
        conn.execute("CREATE TABLE raw_messages (control_id TEXT PRIMARY KEY, payload TEXT, "
                     "received_at TEXT)")
        conn.execute("CREATE TRIGGER raw_messages_no_delete BEFORE DELETE ON raw_messages "
                     "BEGIN SELECT RAISE(ABORT, 'raw_messages is append-only'); END")
        conn.execute("INSERT INTO raw_messages (control_id, payload, received_at) "
                     "VALUES ('NULLED', 'MSH|x', NULL)")
        conn.commit()

    report = purge(store, POLICY, now=NOW)

    assert report["raw_deleted"] == 0
    assert report["raw_retained_unreadable"] == 1
    assert raw_ids(db) == {"NULLED"}


def test_the_shipped_schema_cannot_produce_a_null_timestamp(tmp_path):
    """The complement of the test above, and the reason it has to rebuild the
    table to get a NULL in. Both timestamp columns are NOT NULL, so the
    non-string branch of the age check is only reachable on a file whose schema
    drifted -- a restore from an older or foreign writer. That is a narrow case,
    which is exactly why nothing covered it until mutation M5 said so."""
    store = fresh(tmp_path)
    aged_loop(store, "L-00000000d001", "acknowledged", LONG_AGO)
    with closing(sqlite3.connect(tmp_path / "loops.db")) as conn:
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("DROP TRIGGER loop_events_no_update")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE loop_events SET occurred_at = NULL")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO raw_messages (control_id, payload, received_at) "
                "VALUES ('X', 'MSH|x', NULL)")


def test_a_naive_archive_timestamp_is_read_as_utc_not_compared_against_an_aware_one(tmp_path):
    """Comparing a naive datetime to an aware one raises TypeError, which inside
    a purge would abort the whole run over one odd row."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO raw_messages (control_id, payload, received_at) VALUES (?, ?, ?)",
            ("NAIVE", "MSH|x", (NOW - timedelta(days=40)).replace(tzinfo=None).isoformat()),
        )
        conn.commit()

    assert purge(store, POLICY, now=NOW)["raw_deleted"] == 1


# --------------------------------------------------------------- terminal loops


@pytest.mark.parametrize("state,event", sorted(TERMINAL.items(), key=lambda kv: kv[0].value))
def test_an_aged_terminal_loop_is_purged(tmp_path, state, event):
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000001", event, LONG_AGO)
    assert store.replay("L-000000000001").state is state

    report = purge(store, POLICY, now=NOW)

    assert report["loops_deleted"] == 1
    assert loop_ids(tmp_path / "loops.db") == set()


@pytest.mark.parametrize("state,event", sorted(NON_TERMINAL.items(), key=lambda kv: kv[0].value))
def test_a_loop_that_is_not_terminal_is_never_deleted_by_age(tmp_path, state, event):
    """The assertion this module exists for. A one-day policy against a loop
    ten thousand days old, and it survives."""
    store = fresh(tmp_path)
    loop_id = aged_loop(store, "L-000000000002", event, NOW - timedelta(days=9999))
    assert store.replay(loop_id).state is state

    report = purge(store, RetentionPolicy(raw_days=1, resolved_days=1), now=NOW)

    assert report["loops_deleted"] == 0
    assert store.replay(loop_id).state is state


def test_an_orphan_is_never_purged_however_old(tmp_path):
    """ORPHAN is a result nobody has placed yet. Deleting it by age is deleting
    a result no human ever looked at -- the failure the product exists for."""
    store = fresh(tmp_path)
    registry = Registry(store)
    orphan_id = registry.orphan(control_id="C1", mrn="MRN1", detail={"result_status": "F"})
    store.append_event(LoopEvent(orphan_id, "orphaned", NOW - timedelta(days=9999), "C2",
                                 {"result_status": "F"}))

    purge(store, RetentionPolicy(raw_days=1, resolved_days=1), now=NOW)

    assert store.replay(orphan_id).state is LoopState.ORPHAN


def test_a_terminal_loop_inside_the_window_is_kept(tmp_path):
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000003", "acknowledged", NOW - timedelta(days=364))
    assert purge(store, POLICY, now=NOW)["loops_deleted"] == 0


def test_age_comes_from_the_newest_event_not_from_creation(tmp_path):
    """A loop opened 400 days ago and acknowledged yesterday is a record the
    site has been working inside its own retention window. Measuring from
    creation would delete it the day it resolved."""
    store = fresh(tmp_path)
    store.append_event(LoopEvent("L-000000000004", "created", LONG_AGO, "C1", {"mrn": "M"}))
    store.append_event(LoopEvent("L-000000000004", "resulted", YESTERDAY, "C2", {"obx11": "F"}))
    store.append_event(LoopEvent("L-000000000004", "acknowledged", YESTERDAY, "C3", {}))

    report = purge(store, POLICY, now=NOW)

    assert report["loops_deleted"] == 0
    assert report["retained_recent_activity"] == 1
    assert store.replay("L-000000000004").state is LoopState.ACKNOWLEDGED


def test_a_recent_non_transitional_event_also_holds_a_terminal_loop(tmp_path):
    """`merged_in` changes no state, so the loop is still ACKNOWLEDGED -- but a
    merge landed on it last week and the record is live identity evidence."""
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000005", "acknowledged", LONG_AGO)
    store.append_event(LoopEvent("L-000000000005", "merged_in", YESTERDAY, "C9",
                                 {"mrn": "MRN2"}))

    assert purge(store, POLICY, now=NOW)["loops_deleted"] == 0


def test_a_future_dated_event_holds_a_terminal_loop(tmp_path):
    """The failure matrix accepts future-dated observations. Taking the newest
    event means a clock skew keeps a record rather than deleting one."""
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000006", "acknowledged", LONG_AGO)
    store.append_event(LoopEvent("L-000000000006", "merged_in", NOW + timedelta(days=5), "C9", {}))

    assert purge(store, POLICY, now=NOW)["loops_deleted"] == 0


def test_a_loop_with_an_unreadable_event_timestamp_is_kept(tmp_path):
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000007", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("DROP TRIGGER loop_events_no_update")
        conn.execute("UPDATE loop_events SET occurred_at = 'nonsense' WHERE loop_id = ?",
                     ("L-000000000007",))
        conn.commit()

    report = purge(store, POLICY, now=NOW)

    assert report["loops_deleted"] == 0
    assert report["retained_unreplayable"] == 1
    assert "L-000000000007" in event_loop_ids(db)


# ----------------------------------- the projection is not what decides a delete


def test_a_projection_rewritten_to_lie_cannot_delete_an_open_loop(tmp_path):
    """The `loops` table is a projection and any connection can write it -- the
    triggers cover the log, not the index over it. So the delete decision is
    re-derived by replaying the events, and a row claiming CANCELLED over a log
    that says OPEN deletes nothing."""
    store = fresh(tmp_path)
    loop_id = aged_loop(store, "L-000000000008", "created", NOW - timedelta(days=9999))
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE loops SET state = 'CANCELLED' WHERE loop_id = ?", (loop_id,))
        conn.commit()
    assert conn is not None

    report = purge(store, POLICY, now=NOW)

    assert report["loops_deleted"] == 0
    assert report["retained_projection_disagreed"] == 1
    assert store.replay(loop_id).state is LoopState.OPEN


def test_a_foreign_row_in_a_state_v1_cannot_replay_is_kept(tmp_path):
    """A `loops` row saying CLOSED can only have come from outside this version.
    Replay refuses it, and a purge does not delete what it cannot read."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO loops (loop_id, mrn, state) VALUES (?, ?, ?)",
            ("L-0000000000ff", "MRN1", "CLOSED"),
        )
        conn.commit()

    report = purge(store, POLICY, now=NOW)

    assert report["loops_deleted"] == 0
    assert "L-0000000000ff" in loop_ids(db)


def test_a_projection_row_with_no_events_is_counted_and_left_alone(tmp_path):
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO loops (loop_id, mrn, state) VALUES (?, ?, ?)",
            ("L-0000000000ee", "MRN1", "CANCELLED"),
        )
        conn.commit()

    report = purge(store, POLICY, now=NOW)

    assert report["loops_without_events"] == 1
    assert report["loops_deleted"] == 0


# ------------------------------------------------------------- what goes with it


def test_purging_a_loop_removes_every_one_of_its_events(tmp_path):
    """Partial is the one outcome that is worse than either whole. Events
    without a projection row get resurrected by rebuild_projection; a projection
    row without events is a loop no query can replay."""
    store = fresh(tmp_path)
    doomed = aged_loop(store, "L-000000000009", "cancelled", LONG_AGO)
    kept = aged_loop(store, "L-00000000000a", "created", LONG_AGO)
    db = tmp_path / "loops.db"

    report = purge(store, POLICY, now=NOW)

    assert report["events_deleted"] == 2
    assert event_loop_ids(db) == {kept}
    assert loop_ids(db) == {kept}
    assert doomed not in event_loop_ids(db)


def test_the_database_is_consistent_after_a_purge(tmp_path):
    store = fresh(tmp_path)
    for i, event in enumerate(TERMINAL.values()):
        aged_loop(store, f"L-00000000010{i}", event, LONG_AGO)
    aged_loop(store, "L-000000000110", "created", LONG_AGO)
    db = tmp_path / "loops.db"

    purge(store, POLICY, now=NOW)

    assert loop_ids(db) == event_loop_ids(db)
    for loop in store.all_loops():
        assert store.replay(loop.loop_id).state is loop.state


def test_rebuild_projection_does_not_resurrect_a_purged_loop(tmp_path):
    store = fresh(tmp_path)
    aged_loop(store, "L-00000000000b", "acknowledged", LONG_AGO)
    aged_loop(store, "L-00000000000c", "created", LONG_AGO)

    purge(store, POLICY, now=NOW)

    assert store.rebuild_projection() == 1
    assert loop_ids(tmp_path / "loops.db") == {"L-00000000000c"}


def test_the_worklist_queries_still_answer_after_a_purge(tmp_path):
    store = fresh(tmp_path)
    aged_loop(store, "L-00000000000d", "acknowledged", LONG_AGO)
    open_id = aged_loop(store, "L-00000000000e", "created", LONG_AGO)

    purge(store, POLICY, now=NOW)

    assert [loop.loop_id for loop in store.open_loops()] == [open_id]
    assert [loop.loop_id for loop in store.all_loops()] == [open_id]
    assert store.resulted_unacknowledged() == []


# -------------------------------------------------------- provenance of a result


def test_an_attached_orphan_whose_target_is_still_live_is_kept(tmp_path):
    """The target's `resulted` event carries `attached_from`. Deleting the
    orphan while the loop it fed is still open leaves a live loop whose result
    came from a record that no longer exists."""
    store = fresh(tmp_path)
    target = aged_loop(store, "L-00000000000f", "created", YESTERDAY)
    orphan = "O-000000000001"
    store.append_event(LoopEvent(orphan, "orphaned", LONG_AGO, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent(orphan, "attached", LONG_AGO, "C2", {"attached_to": target}))

    report = purge(store, POLICY, now=NOW)

    assert report["loops_deleted"] == 0
    assert report["retained_for_provenance"] == 1
    assert store.replay(orphan).state is LoopState.ATTACHED


def test_an_attached_orphan_goes_when_its_target_goes(tmp_path):
    store = fresh(tmp_path)
    target = aged_loop(store, "L-000000000010", "acknowledged", LONG_AGO)
    orphan = "O-000000000002"
    store.append_event(LoopEvent(orphan, "orphaned", LONG_AGO, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent(orphan, "attached", LONG_AGO, "C2", {"attached_to": target}))

    report = purge(store, POLICY, now=NOW)

    assert report["loops_deleted"] == 2
    assert report["retained_for_provenance"] == 0
    assert loop_ids(tmp_path / "loops.db") == set()


def test_an_attached_orphan_whose_target_is_already_gone_is_purged(tmp_path):
    store = fresh(tmp_path)
    orphan = "O-000000000003"
    store.append_event(LoopEvent(orphan, "orphaned", LONG_AGO, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent(orphan, "attached", LONG_AGO, "C2",
                                 {"attached_to": "L-ffffffffffff"}))

    assert purge(store, POLICY, now=NOW)["loops_deleted"] == 1


# ---------------------------------------------- what retention does NOT touch


def test_labels_survive_a_purge(tmp_path):
    """Labels carry no identifier by design -- no MRN, no accession, no actor,
    no free text, day resolution only -- so no retention clock applies to them.
    They are also the evidence a pack release gate vetoes on, and a deletable
    label is a gate that can be passed by deleting the evidence."""
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000011", "acknowledged", LONG_AGO)
    store.record_label(LabelType.ORPHAN_ATTACHED, loop_id="L-000000000011", modality="CT")

    purge(store, POLICY, now=NOW)

    assert len(store.labels()) == 1
    assert store.labels()[0]["loop_id"] == "L-000000000011"


def test_the_alias_table_survives_a_purge(tmp_path):
    """An alias records a permanent fact. Ageing one out silently re-strands
    every loop on the retired identifier -- the invisible-open-loop failure
    section 4 designs against, arriving on a retention timer."""
    store = fresh(tmp_path)
    store.record_alias("MRN_OLD", "MRN_NEW", LONG_AGO, "engine")

    purge(store, POLICY, now=NOW)

    assert store.resolve_mrn("MRN_OLD") == "MRN_NEW"
    assert len(store.alias_events()) == 1


def test_applied_messages_survive_a_purge(tmp_path):
    """The idempotency ledger. Ageing it out re-arms double application of any
    message the engine redelivers under an old control id."""
    store = fresh(tmp_path)
    store.record_applied("CTRL1", "content-key-1", "ORU^R01")

    purge(store, POLICY, now=NOW)

    assert store.control_id_applied("CTRL1")
    assert store.content_key_owner("content-key-1") == "CTRL1"


def test_the_audit_trail_is_not_touched_by_retention(tmp_path, isolated_audit_db):
    """Audit retention is a different and usually longer policy, and
    immutable_audit blocks deletion at its own authorizer. Nothing in this
    module opens that database to delete from it."""
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000012", "acknowledged", LONG_AGO)
    purge(store, POLICY, now=NOW)
    before = len(audit.referral_audit_entries())

    purge(store, POLICY, now=NOW)

    assert len(audit.referral_audit_entries()) == before + 1


def test_the_purge_is_recorded_in_the_audit_trail(tmp_path, isolated_audit_db):
    """A delete path over PHI that leaves no record of having run is the one
    thing an auditor will ask about, and after it runs the evidence is gone."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", LONG_AGO)
    aged_loop(store, "L-000000000013", "acknowledged", LONG_AGO)

    purge(store, POLICY, now=NOW)

    rows = [r for r in audit.referral_audit_entries()
            if r["resource_type"] == "referral_retention"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert '"raw_deleted":1' in rows[0]["detail"]
    assert '"loops_deleted":1' in rows[0]["detail"]
    assert '"raw_retention_days":30' in rows[0]["detail"]


def test_a_dry_run_writes_no_audit_row(tmp_path, isolated_audit_db):
    """The counts a dry run produces are what a purge *would* delete. A row
    carrying them is indistinguishable from one describing a purge that did, and
    an auditor reading `loops_deleted` has to be able to conclude the loops are
    gone."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", LONG_AGO)
    aged_loop(store, "L-00000000002c", "acknowledged", LONG_AGO)

    purge(store, POLICY, now=NOW, dry_run=True)

    assert [r for r in audit.referral_audit_entries()
            if r["resource_type"] == "referral_retention"] == []


def test_a_refused_purge_is_recorded_as_denied(tmp_path, isolated_audit_db):
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DROP TRIGGER loop_events_no_delete")
        conn.commit()

    with pytest.raises(StoreUnavailableError):
        purge(store, POLICY, now=NOW)

    rows = [r for r in audit.referral_audit_entries()
            if r["resource_type"] == "referral_retention"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"


# ------------------------------------------------------- the append-only guards


def test_the_delete_triggers_are_rearmed_after_a_purge(tmp_path):
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000014", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"
    before = triggers(db)

    purge(store, POLICY, now=NOW)

    assert triggers(db) == before
    # Rows on both tables: a BEFORE DELETE trigger is per-row, so an empty table
    # would let the DELETE succeed and this test would pass on a disarmed file.
    aged_loop(store, "L-000000000015", "created", NOW)
    insert_raw(db, "FRESH", NOW)
    with closing(sqlite3.connect(db)) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM loop_events")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM raw_messages")


def test_the_delete_triggers_come_back_when_the_purge_fails_midway(tmp_path, monkeypatch):
    """The window in which the guards are down is one transaction wide, and
    SQLite rolls DDL back with everything else. A crash there must not leave a
    file whose append-only guarantee is silently off."""
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000016", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"
    before = triggers(db)

    def boom(*args, **kwargs):
        raise RuntimeError("purge blew up mid-transaction")

    monkeypatch.setattr(LoopStore, "_delete_loops", boom)

    with pytest.raises(RuntimeError):
        purge(store, POLICY, now=NOW)

    assert triggers(db) == before
    assert store.replay("L-000000000016").state is LoopState.ACKNOWLEDGED
    with closing(sqlite3.connect(db)) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM loop_events")


def test_purge_refuses_a_file_whose_guards_are_already_missing(tmp_path):
    """A file that has already lost its append-only triggers has been altered
    outside this module. Running the one sanctioned delete path over it would
    destroy the evidence of that."""
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000017", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DROP TRIGGER raw_messages_no_delete")
        conn.commit()

    with pytest.raises(StoreUnavailableError) as exc:
        purge(store, POLICY, now=NOW)

    assert "raw_messages_no_delete" in str(exc.value)
    assert store.replay("L-000000000017").state is LoopState.ACKNOWLEDGED


def test_a_purge_never_updates_a_row(tmp_path):
    """The UPDATE guards stay armed for the whole purge -- they are not what
    stands in the way, and disarming more than is needed is how a delete path
    becomes an edit path."""
    store = fresh(tmp_path)
    aged_loop(store, "L-000000000018", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"

    purge(store, POLICY, now=NOW)

    with closing(sqlite3.connect(db)) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%no_update'")}
    assert {"loop_events_no_update", "raw_messages_no_update"} <= names


# ------------------------------------------------------------------- dry run


def test_a_dry_run_reports_what_it_would_delete_and_deletes_nothing(tmp_path):
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", LONG_AGO)
    aged_loop(store, "L-000000000019", "acknowledged", LONG_AGO)

    report = purge(store, POLICY, now=NOW, dry_run=True)

    assert report["dry_run"] is True
    assert report["raw_deleted"] == 1
    assert report["loops_deleted"] == 1
    assert raw_ids(db) == {"OLD"}
    assert loop_ids(db) == {"L-000000000019"}


def test_a_dry_run_never_lowers_a_guard(tmp_path, monkeypatch):
    """Found by mutation M13: with the deletes already skipped, committing or
    rolling back a dry run was indistinguishable -- because the dry run was
    still dropping and recreating the triggers for no reason. A command whose
    whole purpose is to show what a period reaches, before it reaches it, has no
    business disarming the append-only guards to find out."""
    store = fresh(tmp_path)
    aged_loop(store, "L-00000000001e", "acknowledged", LONG_AGO)
    statements = []
    real_connect = LoopStore._connect

    def traced(self):
        conn = real_connect(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(LoopStore, "_connect", traced)

    purge(store, POLICY, now=NOW, dry_run=True)
    assert not [s for s in statements if "DROP TRIGGER" in s.upper()]

    statements.clear()
    purge(store, POLICY, now=NOW)
    assert [s for s in statements if "DROP TRIGGER" in s.upper()], \
        "a real purge does have to drop them"


def test_a_dry_run_runs_against_a_database_another_writer_holds(tmp_path, monkeypatch):
    """The other half of M13. A dry run writes nothing, so it must not take the
    write lock -- an operator has to be able to run it against a live listener,
    which is precisely when they want to know what the period reaches."""
    store = fresh(tmp_path)
    aged_loop(store, "L-00000000001f", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"
    monkeypatch.setattr(LoopStore, "_PURGE_BUSY_TIMEOUT_MS", 200)

    blocker = sqlite3.connect(db, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        report = purge(store, POLICY, now=NOW, dry_run=True)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert report["loops_deleted"] == 1
    assert loop_ids(db) == {"L-00000000001f"}


def test_a_dry_run_still_refuses_a_file_whose_guards_are_missing(tmp_path):
    store = fresh(tmp_path)
    with closing(sqlite3.connect(tmp_path / "loops.db")) as conn:
        conn.execute("DROP TRIGGER loop_events_no_delete")
        conn.commit()
    with pytest.raises(StoreUnavailableError):
        purge(store, POLICY, now=NOW, dry_run=True)


# ------------------------------------------------------------------- the file


def test_a_purge_that_reclaims_shrinks_the_file_and_one_that_does_not_leaves_it(tmp_path):
    """SQLite marks freed pages reusable; the file does not shrink on its own.
    Both numbers are measured here so the claim is a fact about this build
    rather than about SQLite in general."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    for i in range(400):
        insert_raw(db, f"BULK{i}", LONG_AGO, payload="MSH|" + "x" * 4000)
    grown = db.stat().st_size

    kept = purge(store, POLICY, now=NOW, reclaim=False)
    unreclaimed = db.stat().st_size
    for i in range(400):
        insert_raw(db, f"BULK2-{i}", LONG_AGO, payload="MSH|" + "x" * 4000)
    purge(store, POLICY, now=NOW, reclaim=True)
    reclaimed = db.stat().st_size

    assert kept["raw_deleted"] == 400
    assert kept["reclaimed"] is False
    assert unreclaimed >= grown, "SQLite does not give the pages back without VACUUM"
    assert reclaimed < grown, "a reclaiming purge must actually return the disk"


def test_a_purge_whose_reclaim_fails_still_reports_the_delete_it_committed(tmp_path, monkeypatch):
    """The deletion is the compliance-relevant half and it has already
    committed. Raising here would report a completed purge as a failed one and
    put `failure` on the audit row over a delete that fully succeeded."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", LONG_AGO)

    # The volume going away between the commit and the vacuum. The purge opens
    # three connections -- the transaction, the guard re-check, then the
    # reclaim -- and only the last one is refused here.
    real_connect = LoopStore._connect
    opened = []

    def flaky(self):
        opened.append(1)
        if len(opened) >= 3:
            raise StoreUnavailableError("volume disappeared")
        return real_connect(self)

    monkeypatch.setattr(LoopStore, "_connect", flaky)
    report = purge(store, POLICY, now=NOW, reclaim=True)

    assert report["raw_deleted"] == 1
    assert report["reclaimed"] is False
    assert raw_ids(db) == set()


@pytest.mark.parametrize("reclaim", [True, False])
def test_purged_payloads_do_not_survive_in_the_file(tmp_path, reclaim):
    """The point of a retention purge is that the bytes are gone, not that a row
    is unreachable. Freed pages keep their old content unless they are zeroed.

    Parametrised on `reclaim` after mutation M14: with the default VACUUM on,
    turning `secure_delete` off survived, because VACUUM rewrites the file and
    drops the freed content anyway. The zeroing is only load-bearing when the
    file is not being rewritten -- which is exactly the case `--no-reclaim`
    creates, so that is where it has to be asserted."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    sentinel = "ZZQQ-SENTINEL-PAYLOAD-9931"
    insert_raw(db, "SECRET", LONG_AGO, payload=f"MSH|{sentinel}")
    assert sentinel.encode() in db.read_bytes()

    purge(store, POLICY, now=NOW, reclaim=reclaim)

    assert sentinel.encode() not in db.read_bytes()


# ---------------------------------------------------------------- concurrency


def test_a_purge_that_cannot_take_the_write_lock_refuses_and_leaves_the_guards_up(
    tmp_path, monkeypatch
):
    """A live listener is a second writer. The purge takes the database's write
    lock for its whole transaction, so the selection and the delete cannot be
    split by another process applying a message -- and when it cannot take that
    lock it refuses rather than proceeding unprotected."""
    store = fresh(tmp_path)
    aged_loop(store, "L-00000000001a", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"
    monkeypatch.setattr(LoopStore, "_PURGE_BUSY_TIMEOUT_MS", 200)

    blocker = sqlite3.connect(db, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(StoreUnavailableError):
            purge(store, POLICY, now=NOW)
        waited = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    # Bounded by the configured timeout, not merely "it eventually raised".
    # Found by mutation M21: hardcoding a thirty-second wait in place of the
    # tunable left every assertion here passing while an operator's purge hung
    # on a busy file for half a minute with no way to change it.
    assert waited < 5, f"the purge waited {waited:.1f}s against a 0.2s timeout"
    assert triggers(db) >= {"loop_events_no_delete", "raw_messages_no_delete"}
    assert store.replay("L-00000000001a").state is LoopState.ACKNOWLEDGED


def test_nothing_can_write_while_the_purge_holds_the_guards_down(tmp_path, monkeypatch):
    """The window in which the append-only guards are off is the same window in
    which the purge holds the database's write lock, and that is not a
    coincidence -- it is the reason the window is safe. A second connection
    reaching for it during that window is refused by SQLite, so no writer ever
    sees an unguarded file.

    Deterministic rather than raced: the purge is held open inside the window
    while the other connection tries."""
    store = fresh(tmp_path)
    open_id = aged_loop(store, "L-00000000002a", "created", NOW - timedelta(days=9999))
    aged_loop(store, "L-00000000002b", "acknowledged", LONG_AGO)
    db = tmp_path / "loops.db"

    inside = threading.Event()
    release = threading.Event()
    outcome = {}
    real_delete = LoopStore._delete_loops

    def hold(conn, doomed):
        real_delete(conn, doomed)
        inside.set()
        release.wait(20)

    monkeypatch.setattr(LoopStore, "_delete_loops", staticmethod(hold))

    def intruder():
        inside.wait(20)
        conn = sqlite3.connect(db, timeout=0.2)
        try:
            conn.execute("PRAGMA recursive_triggers = ON")
            conn.execute("DELETE FROM loop_events WHERE loop_id = ?", (open_id,))
            conn.commit()
            outcome["result"] = "WROTE THROUGH THE DISARMED WINDOW"
        except sqlite3.Error as exc:
            outcome["result"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()
            release.set()

    thread = threading.Thread(target=intruder)
    thread.start()
    purge(store, POLICY, now=NOW, reclaim=False)
    thread.join(30)

    assert "locked" in outcome["result"].lower(), outcome
    assert store.replay(open_id).state is LoopState.OPEN
    assert triggers(db) >= {"loop_events_no_delete", "raw_messages_no_delete"}


def test_a_message_applied_by_another_process_during_a_purge_is_not_lost(tmp_path):
    """The other half: a writer that arrives while the purge holds the lock
    waits for it, and its loop is still there afterwards."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    aged_loop(store, "L-00000000001b", "acknowledged", LONG_AGO)

    script = textwrap.dedent(f"""
        import sqlite3, sys, time
        conn = sqlite3.connect(r"{db}", timeout=30)
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute(
            "INSERT INTO loop_events (loop_id, event_type, occurred_at, control_id, detail) "
            "VALUES ('L-00000000001c', 'created', '{NOW.isoformat()}', 'X1', '{{}}')"
        )
        conn.commit()
        conn.close()
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr

    purge(store, POLICY, now=NOW)

    assert store.replay("L-00000000001c").state is LoopState.OPEN
    assert "L-00000000001b" not in event_loop_ids(db)


# ------------------------------------------------------------------- reporting


def test_the_report_carries_no_identifier(tmp_path):
    """Every value is an int or a bool. A report is logged and audited, and a
    loop id or an MRN in it would be the channel this codebase closes elsewhere."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", LONG_AGO)
    aged_loop(store, "L-00000000001d", "acknowledged", LONG_AGO)

    report = purge(store, POLICY, now=NOW)

    assert all(isinstance(v, (int, bool)) for v in report.values()), report


def test_a_naive_now_is_treated_as_utc(tmp_path):
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", LONG_AGO)

    assert purge(store, POLICY, now=NOW.replace(tzinfo=None))["raw_deleted"] == 1


def test_purge_defaults_now_to_the_clock(tmp_path):
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    insert_raw(db, "OLD", datetime.now(timezone.utc) - timedelta(days=40))
    insert_raw(db, "NEW", datetime.now(timezone.utc))

    assert purge(store, POLICY)["raw_deleted"] == 1


def test_an_empty_database_purges_to_zero(tmp_path):
    report = purge(fresh(tmp_path), POLICY, now=NOW)
    assert report["raw_deleted"] == 0
    assert report["loops_deleted"] == 0
