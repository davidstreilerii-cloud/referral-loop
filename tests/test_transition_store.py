"""The transition log, and the two invariants that make it worth having.

Design spec section 9.3. The chain is gapless and the projection equals the fold of its
own events; together those recover most of what full event sourcing gives definitionally,
without rewriting registry.py.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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


# --------------------------------------------------- what BEGIN IMMEDIATE buys

def test_begin_immediate_takes_the_write_lock_at_the_start_and_a_plain_begin_does_not(
    tmp_path,
):
    """The semantics the writer's transaction mode relies on, established directly.

    No threads and no sleeps: two connections and an explicit order, deterministic on any
    box. A deferred BEGIN acquires nothing until its first write, so both writers would
    read the same MAX(seq), both compute N+1, and the second would take SQLITE_BUSY on
    lock upgrade -- leaving the caller to be correct about busy-retry. BEGIN IMMEDIATE
    moves the contention to the start, where blocking is all it costs.
    """
    db = tmp_path / "loops.db"
    LoopStore(db)

    holder = sqlite3.connect(db, isolation_level=None)
    contender = sqlite3.connect(db, isolation_level=None, timeout=0.05)
    try:
        holder.execute("BEGIN IMMEDIATE")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            contender.execute("BEGIN IMMEDIATE")

        # The same contention under a deferred BEGIN is not detected at all: it opens
        # happily and only discovers the conflict when it tries to write.
        contender.execute("BEGIN")
        contender.execute("ROLLBACK")

        holder.execute("COMMIT")
        contender.execute("BEGIN IMMEDIATE")
        contender.execute("COMMIT")
    finally:
        holder.close()
        contender.close()


def test_the_writer_opens_its_transaction_immediate_rather_than_deferred():
    """Asserted on the source, because the behavioural difference cannot be observed
    without concurrency.

    The test above proves what the two modes mean; this proves which one the writer
    issues. A behavioural test would need a second thread to watch the loser block and
    then compute seq = N+2, and this suite has no threading -- a flaky lock test would
    buy less than it cost. Together the pair is what makes the docstring's claim checkable
    rather than argued.
    """
    import ast
    import inspect
    from pathlib import Path

    source = Path(inspect.getfile(LoopStore)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    writer = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_write_transition"
    )
    literals = [
        node.value for node in ast.walk(writer)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.upper().startswith("BEGIN")
    ]
    assert literals == ["BEGIN IMMEDIATE"], (
        f"the transition writer must open IMMEDIATE, not deferred; found {literals}"
    )


# ------------------------------------------- 4b: the write, not just the decision


def _registry(tmp_path):
    from referral_loop.registry import Registry
    store = LoopStore(tmp_path / "dual.db")
    return store, Registry(store)


def test_every_routed_transition_lands_in_both_logs(tmp_path):
    """loop_events still drives replay; transition_events is the provenance log. Both
    exist until Plan 2c collapses them, so both must be written -- and this is what makes
    the section 9.3 invariants checkable on data that arrived the way production data
    does, rather than on rows a test appended by hand.
    """
    store, registry = _registry(tmp_path)
    loop_id = registry.open_loop(mrn="M1", modality="CT", control_id="C1")
    registry.schedule(loop_id, control_id="C2")
    registry.record_result(loop_id, obx11="F", control_id="C3")
    registry.acknowledge(loop_id, actor="a", role="r", control_id="C4")

    # open_loop is not routed yet, so it writes only loop_events; the three routed
    # transitions after it must each have produced a provenance row.
    assert store.transition_count(loop_id) == 3
    assert store.fold_transitions(loop_id) is ReferralState.RECONCILED


def test_a_rejected_transition_leaves_no_event_and_no_state_change(tmp_path):
    """The half that would have been silently untrue.

    A test asserting only that the state did not move passes on a partial write -- the
    projection rolled back, the provenance row left behind. Both logs are checked, because
    the failure this guards is a transition_events row for something that never happened.
    """
    store, registry = _registry(tmp_path)
    loop_id = registry.open_loop(mrn="M1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="a", role="r", control_id="C3")

    events_before = len(store.events_for(loop_id))
    transitions_before = store.transition_count(loop_id)

    with pytest.raises(Exception):
        registry.acknowledge(loop_id, actor="a", role="r", control_id="C4")

    assert len(store.events_for(loop_id)) == events_before
    assert store.transition_count(loop_id) == transitions_before


def test_the_gapless_invariant_holds_across_a_populated_store(tmp_path):
    """Spec 9.3 invariant 1, over many referrals driven through the registry rather than
    through append_transition. A hand-built fixture exercises the sequence its author
    imagined; every ordering hazard this project has found was a sequence nobody imagined
    until something produced it."""
    store, registry = _registry(tmp_path)
    for n in range(60):
        loop_id = registry.open_loop(mrn=f"M{n}", modality="CT", control_id=f"O{n}")
        registry.schedule(loop_id, control_id=f"S{n}")
        registry.record_result(loop_id, obx11="F", control_id=f"R{n}")
        if n % 2:
            registry.acknowledge(loop_id, actor="a", role="r", control_id=f"A{n}")

    rows = store._read(
        "SELECT referral_id, MAX(seq), COUNT(*) FROM transition_events GROUP BY referral_id")
    assert len(rows) == 60, "the fixture did not populate the log, so this proved nothing"
    for referral_id, highest, count in rows:
        assert highest == count, f"{referral_id}: max seq {highest}, {count} events"


def test_the_projection_equals_the_fold_across_a_populated_store(tmp_path):
    """Spec 9.3 invariant 2, on the same populated store: what the registry reports and
    what the transition chain folds to must agree for every referral."""
    from referral_loop.migration import canonical_state

    store, registry = _registry(tmp_path)
    for n in range(40):
        loop_id = registry.open_loop(mrn=f"M{n}", modality="CT", control_id=f"O{n}")
        registry.schedule(loop_id, control_id=f"S{n}")
        if n % 3 == 0:
            registry.cancel(loop_id, control_id=f"X{n}", message_at=None)
        else:
            registry.record_result(loop_id, obx11="F", control_id=f"R{n}")

    checked = 0
    for loop in store.all_loops():
        folded = store.fold_transitions(loop.loop_id)
        if folded is None:
            continue
        assert folded is canonical_state(loop.state), (
            f"{loop.loop_id}: projection {loop.state} folds to {folded}")
        checked += 1
    assert checked == 40, f"only {checked} referrals had a chain to check"


def test_an_unscheduled_referral_is_the_one_place_invariant_two_does_not_hold(tmp_path):
    """The known exception to the test above, pinned rather than left to be rediscovered.

    `registry.unschedule` asserts ACCEPTED on the transition chain and projects the loop
    to `LoopState.OPEN`, which `canonical_state` reads back as SENT. That is not a bug in
    either write: the legacy nine-state vocabulary has no member for ACCEPTED at all --
    `migration.WITHOUT_LEGACY_SOURCE` names it as one of the six the old machine cannot
    represent -- and OPEN is the only projection that keeps a referral whose appointment
    was cancelled on `open_loops()`, which is the entire point of the method.

    So the divergence is the cost of the two vocabularies coexisting, and it is bounded:
    it lasts until Plan 2c collapses the two logs and `LoopState` goes away, at which
    point ACCEPTED is expressible and this test should fail and be deleted. Written as a
    test so that day is loud. It is deliberately NOT a widening of the invariant above --
    that one must keep holding for every other path.
    """
    from referral_loop.migration import canonical_state

    store, registry = _registry(tmp_path)
    loop_id = registry.open_loop(mrn="M1", modality="CT", control_id="O1")
    registry.schedule(loop_id, control_id="S1")
    registry.unschedule(loop_id, control_id="X1")

    assert store.fold_transitions(loop_id) is ReferralState.ACCEPTED
    assert canonical_state(registry.get(loop_id).state) is ReferralState.SENT
    assert [loop.loop_id for loop in store.open_loops("M1")] == [loop_id], (
        "the projection the divergence buys: the referral is still on the worklist"
    )


def test_a_failure_writing_the_provenance_row_rolls_back_the_event_too(tmp_path, monkeypatch):
    """The "one transaction" claim, made observable.

    Without this, splitting append_event into two commits -- loop_events first, the
    provenance row after -- leaves every test in this file green, because nothing fails
    between them. That is the same shape as the BEGIN IMMEDIATE gap: a property argued in
    a docstring and checked by nothing.

    So the provenance insert is made to fail, and the assertion is that the loop_events
    append and the projection update went with it. A partial write here is worse than a
    failed one: the loop would advance with no provenance for how, which is precisely the
    record this table exists to keep.
    """
    store, registry = _registry(tmp_path)
    loop_id = registry.open_loop(mrn="M1", modality="CT", control_id="C1")
    events_before = len(store.events_for(loop_id))
    state_before = registry.get(loop_id).state

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("provenance insert failed")

    monkeypatch.setattr(LoopStore, "_insert_transition", boom)
    with pytest.raises(Exception):
        registry.schedule(loop_id, control_id="C2")

    assert len(store.events_for(loop_id)) == events_before, (
        "the loop_events append survived a failed provenance write")
    assert registry.get(loop_id).state is state_before
    assert store.transition_count(loop_id) == 0


# ------------------------------------------- what the failure paths may say

_SENTINEL_TEXT = "ZZSENTINELNARRATIVE"


def _loaded(**kw):
    """A transition carrying sentinel text in every free-text field it has."""
    return _t(rationale=_SENTINEL_TEXT,
              evidence=(Evidence(kind=EvidenceKind.DOCUMENT, ref=_SENTINEL_TEXT,
                                 spans=None, confidence=None),),
              **kw)


def test_no_transition_write_failure_repeats_the_free_text_it_was_carrying(store):
    """`rationale` is coordinator free text and `Evidence.ref` is caller-supplied, so both
    are the fields a clinical detail would arrive in. Neither may reach an exception
    message: every caller of this logs the string, and a confirmed finding in this
    codebase is that identifiers reach application logs exactly that way.

    Both failure paths are exercised -- the losing writer's IntegrityError and the generic
    sqlite failure -- because they are separate messages and only one of them names the
    referral at all.
    """
    store.append_transition("R1", _loaded())

    with pytest.raises(StoreUnavailableError) as caught:
        store._append_transition_at_seq("R1", _loaded(), seq=1)
    for rendering in (str(caught.value), repr(caught.value), str(caught.value.args)):
        assert _SENTINEL_TEXT not in rendering, f"free text reached {rendering!r}"


def test_the_generic_write_failure_says_nothing_about_the_transition(store, monkeypatch):
    """The second path. It names neither the referral nor anything off the transition --
    only that a write failed and what SQLite said about it."""
    import sqlite3 as _sqlite

    def boom(*_a, **_k):
        raise _sqlite.OperationalError("disk I/O error")

    monkeypatch.setattr(LoopStore, "_insert_transition", boom)
    with pytest.raises(StoreUnavailableError) as caught:
        store.append_transition("R1", _loaded())
    message = str(caught.value)
    assert _SENTINEL_TEXT not in message
    assert "disk I/O error" in message


def test_the_transition_write_interpolates_only_non_identifying_values():
    """Read as source, because the behavioural tests above can only exercise the paths
    they thought of. This bounds what *any* future message on these paths may name:
    the referral id, the seq, and the database's own error text.

    `referral_id` holds a minted loop id today, which is why this is not a live leak --
    but that is a property of the caller, not of the message, and callers change. This is
    what would fail if a later edit reached for `transition.rationale` to make a failure
    more diagnosable.
    """
    import ast
    import inspect

    source = Path(inspect.getfile(LoopStore)).read_text(encoding="utf-8")
    writer = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "_write_transition")
    named = {n.id for n in ast.walk(writer) if isinstance(n, ast.Name)}
    interpolated = {n.value.id for n in ast.walk(writer)
                    if isinstance(n, ast.FormattedValue) and isinstance(n.value, ast.Name)}
    assert interpolated <= {"referral_id", "seq", "exc"}, (
        f"a failure message interpolates something outside the allowlist: "
        f"{sorted(interpolated - {'referral_id', 'seq', 'exc'})}")
    assert "transition" in named, "this test is only meaningful while the writer sees one"
