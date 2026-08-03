"""The transition log, and the two invariants that make it worth having.

Design spec section 9.3. The chain is gapless and the projection equals the fold of its
own events; together those recover most of what full event sourcing gives definitionally,
without rewriting registry.py.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from referral_loop.core.states import ReferralState
from referral_loop.core.transitions import (
    ActorRef,
    AssertionSource,
    Evidence,
    EvidenceKind,
    Transition,
)
from referral_loop.errors import StoreUnavailableError
from referral_loop.store import LoopStore

T0 = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return LoopStore(tmp_path / "loops.db")


def _t(to_state=ReferralState.SCHEDULED, source=AssertionSource.RECEIVING_ORG, **kw):
    base = dict(
        actor=ActorRef(kind="device", id="referral-loop"),
        evidence=(),
        occurred_at=T0,
        recorded_at=T0,
        hold=None,
        rationale=None,
    )
    base.update(kw)
    return Transition(to_state=to_state, assertion_source=source, **base)


def test_the_table_and_its_guards_exist_on_a_fresh_database(store):
    names = {r[0] for r in store._read(
        "SELECT name FROM sqlite_master WHERE name LIKE 'transition_events%'")}
    assert names == {
        "transition_events", "transition_events_no_delete", "transition_events_no_update"}


def test_the_log_is_append_only_against_any_connection(store, tmp_path):
    """Asserted on a bare sqlite3 connection, not one LoopStore opened.

    The authorizer only binds to connections this class opens; a second process opening
    the file would bypass it entirely. The triggers travel with the database, and they are
    the actual guarantee -- so they are what this tests.
    """
    store.append_transition("R1", _t())
    raw = sqlite3.connect(tmp_path / "loops.db")
    for sql in ("DELETE FROM transition_events",
                "UPDATE transition_events SET to_state = 'cancelled'"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(sql)
    raw.close()


def test_seq_starts_at_one_and_increments(store):
    for expected in (1, 2, 3):
        assert store.append_transition("R1", _t()) == expected


def test_two_referrals_number_their_chains_independently(store):
    store.append_transition("R1", _t())
    store.append_transition("R1", _t())
    assert store.append_transition("R2", _t()) == 1


def test_the_event_chain_is_gapless_for_every_referral(store):
    """Spec 9.3 invariant 1: MAX(seq) == COUNT(*) per referral."""
    for referral_id in ("R1", "R2", "R3"):
        for _ in range(4):
            store.append_transition(referral_id, _t())
    rows = store._read(
        "SELECT referral_id, MAX(seq), COUNT(*) FROM transition_events GROUP BY referral_id")
    assert rows, "the fixture wrote nothing, so this proved nothing"
    for referral_id, highest, count in rows:
        assert highest == count, f"{referral_id} has a gap: max seq {highest}, {count} events"


def test_two_writers_racing_on_one_referral_do_not_both_win(store):
    """UNIQUE(referral_id, seq) is optimistic concurrency, not just integrity.

    Two MLLP connections applying to the same referral: one loses the insert and retries.
    Without the constraint the second silently reuses the first's seq, and the chain
    stops being a chain. Simulated by claiming a seq that is already taken, which is
    exactly what a racing writer computes from a stale read.
    """
    store.append_transition("R1", _t())
    with pytest.raises(StoreUnavailableError):
        store._append_transition_at_seq("R1", _t(), seq=1)
    assert store.transition_count("R1") == 1


def test_the_projected_state_equals_the_fold_of_its_own_events(store):
    """Spec 9.3 invariant 2, on the log's own terms: the newest event's to_state is the
    state the chain folds to, and it is reachable by replaying from the beginning."""
    path = [ReferralState.SCHEDULED, ReferralState.SEEN, ReferralState.DOCUMENTED]
    for state in path:
        store.append_transition("R1", _t(to_state=state))
    folded = store.fold_transitions("R1")
    assert folded is path[-1]
    rows = store._read(
        "SELECT to_state FROM transition_events WHERE referral_id = ? ORDER BY seq", ("R1",))
    assert [r[0] for r in rows] == [s.value for s in path]


def test_an_empty_chain_folds_to_nothing_rather_than_to_a_default(store):
    """A referral with no transitions has no state to report. Defaulting to DRAFT would
    invent a claim the log never made."""
    assert store.fold_transitions("R-NONE") is None


def test_who_asserted_the_change_is_recorded_on_every_row(store):
    """The whole point of the table. A row that cannot say who asserted it is the gap
    loop_events has, carried forward."""
    store.append_transition("R1", _t(source=AssertionSource.SYSTEM_INFERRED))
    row = store._read(
        "SELECT assertion_source, actor_kind, actor_id FROM transition_events")[0]
    assert tuple(row) == ("system-inferred", "device", "referral-loop")


def test_evidence_is_stored_as_its_references_and_never_its_content(store):
    """Evidence.ref is a hash or a rule id by construction. Storing anything else here
    would put clinical narrative into an append-only table."""
    store.append_transition("R1", _t(evidence=(
        Evidence(kind=EvidenceKind.MATCH, ref="sha256:aa", spans=(), confidence=0.93),
    )))
    stored = store._read("SELECT evidence FROM transition_events")[0][0]
    assert "sha256:aa" in stored and "0.93" in stored


def test_a_referral_id_holds_a_loop_id_and_that_is_deliberate(store):
    """The column is named for the target vocabulary because the table is append-only and
    a later rename would be the _widen_key hazard on the provenance log. Pinned so the
    inconsistency is not "fixed" by someone reading it as a mistake."""
    store.append_transition("L-0001", _t())
    assert store._read("SELECT referral_id FROM transition_events")[0][0] == "L-0001"


# ------------------------------------------------------------------ crash safety

_CRASH_SCRIPT = '''
import os, sqlite3, sys
sys.path.insert(0, {src!r})

class Dying(sqlite3.Connection):
    def executescript(self, sql, *args, **kwargs):
        if "transition_events" in sql:
            os._exit(7)
        return super().executescript(sql, *args, **kwargs)

_real_connect = sqlite3.connect
sqlite3.connect = lambda *a, **k: _real_connect(*a, **{{**k, "factory": Dying}})

from referral_loop.store import LoopStore
LoopStore({db!r})
print("SURVIVED")
'''


def test_a_process_killed_before_the_transition_table_lands_leaves_the_archive_intact(
    tmp_path,
):
    """The hazard that actually exists here, as opposed to the one _widen_key has.

    Adding transition_events is an idempotent CREATE TABLE IF NOT EXISTS, not a rebuild:
    no rename, no copy, no `__legacy` aside, and so no half-applied state to roll back.
    What still has to hold is that dying on the boot that would have created it leaves
    everything already in the file untouched, and that the next boot completes.

    The assertion is the row count, not that the file opens. The _widen_key defect's whole
    character was that the reopen *succeeded* -- exit 0, no warning -- while reporting
    raw_count() == 0, so "the database is readable" would have passed with the archive
    emptied.
    """
    import subprocess
    import sys
    from pathlib import Path

    db = tmp_path / "loops.db"
    seeded = LoopStore(db)
    seeded.record_raw("C1", r"MSH|^~\&|payload-one")
    seeded.record_raw("C2", r"MSH|^~\&|payload-two")
    assert seeded.raw_count() == 2

    # Drop the table so the next boot is one that must create it, then die during that.
    raw = sqlite3.connect(db)
    raw.executescript(
        "DROP TRIGGER transition_events_no_delete;"
        "DROP TRIGGER transition_events_no_update;"
        "DROP TABLE transition_events;"
    )
    raw.commit()
    raw.close()

    script = tmp_path / "crash.py"
    src = str(Path(__file__).resolve().parents[1] / "src")
    script.write_text(_CRASH_SCRIPT.format(src=src, db=str(db)), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 7, f"the child did not die at the kill point: {proc.stderr}"
    assert "SURVIVED" not in proc.stdout

    reopened = LoopStore(db)
    assert reopened.raw_count() == 2, "the archive lost rows to a crash that touched no archive"
    assert sorted(r[0] for r in reopened._read("SELECT control_id FROM raw_messages")) == [
        "C1", "C2"]
    names = {r[0] for r in reopened._read(
        "SELECT name FROM sqlite_master WHERE name LIKE 'transition_events%'")}
    assert names == {
        "transition_events", "transition_events_no_delete", "transition_events_no_update"}, (
        "the reopen did not finish the job the crashed boot started")
    assert reopened.transition_count("R1") == 0
