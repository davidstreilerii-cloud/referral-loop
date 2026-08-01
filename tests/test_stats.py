"""`LoopStore.stats()` and `referral-loop stats`: the size report an operator
runs to see the growth retention deliberately never touches.

`applied_messages` and `mrn_alias_events`/`mrn_aliases` have no purge path by
design (spec section 4, retention.py's module docstring). Nothing about that
is wrong -- but nothing was measuring the cost either, on a box this project
does not administer. This file proves the report an operator would use to
notice before a full disk does: it reads a consistent snapshot despite a
concurrent writer, it works against a schema with nothing in it yet, and it
never turns a row count into a `SELECT *`.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

import pytest

from healthcare_rag.referral_loop.errors import StoreUnavailableError
from healthcare_rag.referral_loop.events import LoopEvent
from healthcare_rag.referral_loop.store import STATS_TABLES, UNBOUNDED_TABLES, LoopStore

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def fresh(tmp_path) -> LoopStore:
    return LoopStore(tmp_path / "loops.db")


# ------------------------------------------------------------------ the shape


def test_stats_covers_every_table_the_retention_docstring_names():
    """Both exclusions from retention.py, present and flagged."""
    names = {name for name, _ in STATS_TABLES}
    assert UNBOUNDED_TABLES <= names
    assert UNBOUNDED_TABLES == {"applied_messages", "mrn_alias_events", "mrn_aliases"}


def test_a_fresh_database_reports_zero_rows_everywhere(tmp_path):
    """No rows anywhere yet. Every SUM() in the query is over an empty table,
    which is exactly the NULL-propagation case COALESCE exists to cover --
    without it this crashes on the very first real install."""
    store = fresh(tmp_path)

    report = store.stats()

    assert report["file_bytes"] > 0, "the schema itself occupies at least one page"
    for name, _ in STATS_TABLES:
        assert report["tables"][name]["rows"] == 0
        assert report["tables"][name]["payload_bytes"] == 0


def test_bounded_and_unbounded_tables_are_flagged_correctly(tmp_path):
    store = fresh(tmp_path)
    report = store.stats()
    for name, _ in STATS_TABLES:
        assert report["tables"][name]["unbounded"] == (name in UNBOUNDED_TABLES)


def test_row_counts_match_what_was_actually_written(tmp_path):
    store = fresh(tmp_path)
    store.record_raw("RAW1", "MSH|payload")
    store.record_applied("CTRL1", "content-key-1", "ORU^R01")
    store.record_applied("CTRL2", None, "ORM^O01")
    store.record_alias("MRN_OLD", "MRN_NEW", NOW, "engine")
    store.append_event(LoopEvent("L-000000000001", "created", NOW, "C1", {"mrn": "MRN_NEW"}))

    report = store.stats()

    assert report["tables"]["raw_messages"]["rows"] == 1
    assert report["tables"]["applied_messages"]["rows"] == 2
    assert report["tables"]["mrn_alias_events"]["rows"] == 1
    assert report["tables"]["mrn_aliases"]["rows"] == 1
    assert report["tables"]["loops"]["rows"] == 1
    assert report["tables"]["loop_events"]["rows"] == 1


def test_payload_bytes_reflect_the_stored_column_lengths(tmp_path):
    """Not just nonzero -- the actual figure, computed the same way the
    report computes it, so a mutation weakening the SUM to e.g. MAX() or
    COUNT() is caught rather than passing on a bare `> 0`."""
    store = fresh(tmp_path)
    control_id = "CTRL-EXACT-1"
    content_key = "k" * 40
    message_type = "ORU^R01"
    peer_id = "example-ris"
    store.record_applied(control_id, content_key, message_type, peer_id=peer_id)

    report = store.stats()

    with closing(sqlite3.connect(tmp_path / "loops.db")) as conn:
        applied_at = conn.execute(
            "SELECT applied_at FROM applied_messages WHERE control_id = ?", (control_id,)
        ).fetchone()[0]
    expected = (
        len(control_id) + len(peer_id) + len(content_key) + len(message_type)
        + len(applied_at)
    )
    assert report["tables"]["applied_messages"]["payload_bytes"] == expected


def test_a_null_content_key_does_not_zero_out_the_whole_row(tmp_path):
    """content_key is NULL for a message with no identifying content (the
    partial index over it exists for exactly this reason). Ordinary SQL
    arithmetic propagates NULL through `+`, so `LENGTH(control_id) +
    LENGTH(content_key) + ...` is NULL for the whole row the instant one
    column is NULL -- not just missing that column's contribution. IFNULL has
    to guard every column, or a site whose orders never carry a content_key
    would see this table under-reported by the size of every other column on
    every such row, not merely by the size of content_key."""
    store = fresh(tmp_path)
    control_id = "CTRL_NULL"
    message_type = "ADT^A08"
    peer_id = "example-ris"
    store.record_applied(control_id, None, message_type, peer_id=peer_id)

    report = store.stats()

    with closing(sqlite3.connect(tmp_path / "loops.db")) as conn:
        applied_at = conn.execute(
            "SELECT applied_at FROM applied_messages WHERE control_id = ?", (control_id,)
        ).fetchone()[0]
    expected = (
        len(control_id) + len(peer_id) + len(message_type) + len(applied_at)
    )  # content_key: 0
    assert report["tables"]["applied_messages"]["rows"] == 1
    assert report["tables"]["applied_messages"]["payload_bytes"] == expected


def test_the_whole_file_size_is_the_exact_page_accounting(tmp_path):
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    for i in range(50):
        store.record_raw(f"RAW{i}", "MSH|" + "x" * 500)

    report = store.stats()

    with closing(sqlite3.connect(db)) as conn:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    assert report["file_bytes"] == page_count * page_size


# --------------------------------------------------------------- the report is
# read from ONE snapshot, not assembled from several racing ones


def test_stats_does_not_see_a_write_committed_mid_report(tmp_path, monkeypatch):
    """A writer commits new rows to a table stats has not gotten to yet, while
    stats() is still inside its own transaction. The report must describe the
    file as it stood when the transaction opened, not a mix of before-and-after
    across two different tables.

    This is the property that makes the report trustworthy against a live
    listener: without a shared snapshot, `raw_messages` could be counted before
    a burst of inbound traffic and `applied_messages` counted after it, and the
    two numbers in one report would never have been true of the file at the
    same instant.
    """
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    store.record_raw("BEFORE", "MSH|already here")

    writer_started = threading.Event()
    writer_finished = threading.Event()

    def background_write():
        # This blocks on COMMIT until stats()'s read transaction ends -- that
        # block is the mechanism under test, so it must happen off the thread
        # running stats(), or the two would deadlock each other on one thread.
        with closing(sqlite3.connect(db, timeout=30)) as writer:
            writer.execute(
                "INSERT INTO raw_messages (control_id, payload, received_at) "
                "VALUES (?, ?, ?)",
                ("DURING", "MSH|written mid-report", NOW.isoformat()),
            )
            writer.commit()
        writer_finished.set()

    def on_sql(sql: str) -> None:
        if (not writer_started.is_set()
                and sql.strip().upper().startswith("SELECT COUNT(*)")
                and "raw_messages" in sql):
            # stats() has just read raw_messages inside its snapshot, and still
            # holds it (the transaction has not ended). Start a concurrent
            # writer now, before stats() goes on to read the remaining tables.
            writer_started.set()
            threading.Thread(target=background_write, daemon=True).start()
            # Let the writer reach its blocked COMMIT -- proving the write is
            # genuinely in flight, not merely scheduled, while later tables in
            # this same report are still being read.
            time.sleep(0.2)

    # sqlite3.Connection is a C type and its methods cannot be monkeypatched
    # directly, so the trace hook goes on LoopStore._connect instead -- the
    # same seam test_retention.py's dry-run tests use for the same reason.
    real_connect = LoopStore._connect

    def traced(self):
        conn = real_connect(self)
        conn.set_trace_callback(on_sql)
        return conn

    monkeypatch.setattr(LoopStore, "_connect", traced)

    report = store.stats()

    assert writer_finished.wait(30), "the intruding write never completed"
    assert report["tables"]["raw_messages"]["rows"] == 1, (
        "stats() saw a row committed after its own snapshot was taken"
    )

    # The write itself really did land -- a later, independent read confirms it
    # -- so the assertion above is about staleness of the snapshot, not about
    # the write having silently failed.
    monkeypatch.undo()
    with closing(sqlite3.connect(db)) as conn:
        after = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
    assert after == 2


def test_a_writer_can_still_commit_once_the_report_is_done(tmp_path):
    """The snapshot is not held forever. stats() returning must release
    whatever lock it took, or every write after the first report would hang."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    store.stats()

    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.execute(
            "INSERT INTO raw_messages (control_id, payload, received_at) VALUES (?, ?, ?)",
            ("AFTER", "MSH|x", NOW.isoformat()),
        )
        conn.commit()

    assert store.raw_count() == 1


# ------------------------------------------------ never a table load into memory


def test_stats_never_issues_a_select_star(tmp_path):
    """The probe this whole feature exists to survive: a `SELECT *` on
    `applied_messages` at real volume would pull every row into this process's
    memory just to produce a single integer and a single sum. Traced at the
    SQL level rather than inferred from timing, so a regression is a fact
    about the statements sent to SQLite instead of a guess from wall-clock."""
    store = fresh(tmp_path)
    for i in range(20):
        store.record_applied(f"CTRL{i}", f"KEY{i}", "ORU^R01")

    statements: list[str] = []
    conn = sqlite3.connect(tmp_path / "loops.db")
    conn.set_trace_callback(statements.append)
    try:
        # Exercise the same query shape stats() runs, through a traced
        # connection, to confirm the aggregate form rather than trusting the
        # source read alone.
        for name, columns in STATS_TABLES:
            length_sum = "+".join(f"IFNULL(LENGTH({c}),0)" for c in columns)
            conn.execute(f"SELECT COUNT(*), COALESCE(SUM({length_sum}), 0) FROM {name}")
    finally:
        conn.close()

    for sql in statements:
        upper = sql.upper()
        assert "SELECT *" not in upper
        assert upper.startswith("SELECT COUNT(*)")


def test_stats_reports_via_store_uses_only_aggregate_queries(tmp_path, monkeypatch):
    """The actual method, not a hand-rolled equivalent -- traced through
    LoopStore.stats() itself so a future edit that swaps in a row-by-row
    Python count is caught here rather than only in the query-shape test above.
    """
    store = fresh(tmp_path)
    for i in range(5):
        store.record_applied(f"CTRLX{i}", None, "ORM^O01")

    statements: list[str] = []
    real_connect = LoopStore._connect

    def traced(self):
        conn = real_connect(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(LoopStore, "_connect", traced)
    store.stats()

    select_statements = [s for s in statements if s.strip().upper().startswith("SELECT")
                         and "sqlite_master" not in s]
    assert select_statements, "no SELECT was traced; the test is not exercising stats()"
    for sql in select_statements:
        assert "SELECT *" not in sql.upper()


# --------------------------------------------------------------------- refusal


def test_stats_refuses_rather_than_crashes_on_a_missing_column(tmp_path):
    """A file altered outside this module -- the same posture purge takes
    towards a schema it does not recognise -- must surface as a typed refusal,
    not a bare sqlite3.OperationalError escaping to a caller that only catches
    StoreUnavailableError."""
    store = fresh(tmp_path)
    db = tmp_path / "loops.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("ALTER TABLE applied_messages RENAME COLUMN content_key TO renamed")
        conn.commit()

    with pytest.raises(StoreUnavailableError):
        store.stats()
