"""M3: the PHI database must not be readable by other local accounts.

`sqlite3.connect()` creates a new file with `0666 & ~umask` -- 0644 on a default
Linux install -- and `raw_messages.payload` holds verbatim HL7, so every local
account on the host could read every patient's identifiers and every result.
Volume encryption is no answer to that: the volume is decrypted for as long as
the service runs, which is exactly when the file is there to be read.

**Why every property is asserted twice.** POSIX mode bits are not enforced on
Windows. `os.chmod` there toggles the read-only attribute and nothing else, and
`stat().st_mode` reports 0666 or 0444 whatever was asked for -- so a test that
stats the file is a statement about the platform off POSIX, not about this
code. But a test that only ever runs behind a marker nobody exercises locally
is a test that proves nothing on the machine the code is written on, and this
one was written on win32. So each property gets two tests:

  * the **requested** mode -- what this code asks the operating system for,
    recorded by spying on `os.open` and `os.chmod`. That runs everywhere,
    including win32, and it is the half that catches the regression that
    actually matters: somebody deleting the hardening call.
  * the **resulting** mode -- `stat()` on the real file, skipped off POSIX,
    where the answer would be about the platform. CI runs Linux, so this half
    does run, and it is the half that catches asking for the right thing in a
    way that does not take effect.

Neither alone is honest. Together they say which machine proves what.

The sidecar matters as much as the database. In `journal_mode=delete` -- what
this store runs, pinned by nothing and measured here -- SQLite writes a
`-journal` beside the file for the duration of every write transaction, holding
page images of the very rows being written. A 0600 database beside a 0644
journal is not a fix, so the journal is asserted too, while a transaction holds
it open.
"""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from referral_loop import cli as cli_module
from referral_loop import immutable_audit
from referral_loop.store import LoopStore

# Stated here rather than imported from the code under test: the requirement is
# "owner only", and a test that reads the number out of the module it is testing
# agrees with it by construction.
PHI_FILE_MODE = 0o600
PHI_DIR_MODE = 0o700

# One verbatim message, so the file under test really does hold PHI rather than
# only an empty schema.
CONTROL_ID = "CTRL_M3"
PAYLOAD = "MSH|^~\\&|EHR|HOSP|RIS|HOSP|20260724080000||ORM^O01|CTRL_M3|P|2.5.1\rPID|1||MRN123456^^^HOSP^MR|"

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits are not enforced on this platform; the companion "
           "test asserting the requested mode covers it here",
)


@pytest.fixture()
def requested():
    """Every (absolute path, mode) this process asks the OS for, in order.

    Both creation and after-the-fact tightening are recorded, because the fix
    is allowed to use either and the property is about the mode, not about
    which syscall carried it.
    """
    calls: list[tuple[str, int]] = []
    real_open, real_chmod, real_mkdir = os.open, os.chmod, os.mkdir

    def spy_open(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT and not isinstance(path, int):
            calls.append((os.path.abspath(os.fspath(path)), mode))
        return real_open(path, flags, mode, **kwargs)

    def spy_chmod(path, mode, **kwargs):
        if not isinstance(path, int):
            calls.append((os.path.abspath(os.fspath(path)), mode))
        return real_chmod(path, mode, **kwargs)

    def spy_mkdir(path, mode=0o777, **kwargs):
        calls.append((os.path.abspath(os.fspath(path)), mode))
        return real_mkdir(path, mode, **kwargs)

    os.open, os.chmod, os.mkdir = spy_open, spy_chmod, spy_mkdir
    try:
        yield calls
    finally:
        os.open, os.chmod, os.mkdir = real_open, real_chmod, real_mkdir


def modes_for(calls, path) -> list[int]:
    target = os.path.abspath(os.fspath(path))
    return [mode for recorded, mode in calls if recorded == target]


def other_bits(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode) & 0o077


# ------------------------------------------------------------ the database file


def test_the_phi_database_is_created_with_an_owner_only_mode_requested(tmp_path, requested):
    db = tmp_path / "loops.db"
    LoopStore(db)

    asked = modes_for(requested, db)
    assert asked, (
        "nothing asked the operating system for a mode on the PHI database; "
        "sqlite3.connect() creates it 0666 & ~umask, which is 0644 on a default "
        "Linux install and world-readable"
    )
    assert all(mode == PHI_FILE_MODE for mode in asked), asked


@posix_only
def test_the_phi_database_on_disk_is_unreadable_to_other_accounts(tmp_path):
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.record_raw(CONTROL_ID, PAYLOAD)

    assert other_bits(db) == 0, (
        f"{db} is mode {oct(stat.S_IMODE(os.stat(db).st_mode))}; it holds verbatim "
        f"HL7 and any local account can read it"
    )


@posix_only
def test_the_rollback_journal_beside_it_is_unreadable_too(tmp_path):
    """The sidecar carries the same rows and must carry the same mode.

    Asserted while a write transaction is open, because that is the only moment
    the file exists: `journal_mode=delete` removes it on commit.
    """
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    # Committed first, so the pages the transaction below is about to modify
    # already hold PHI: a rollback journal stores the *pre-image*, so a journal
    # taken against an empty schema would carry nothing and the assertion would
    # be about an empty file.
    store.record_raw(CONTROL_ID, PAYLOAD)
    journal = tmp_path / "loops.db-journal"

    assert sqlite3.connect(db).execute("PRAGMA journal_mode").fetchone()[0] == "delete", (
        "this test is written for a rollback journal; a change to WAL means "
        "-wal and -shm need the same assertion"
    )

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO raw_messages (control_id, assertion_source, payload, received_at) "
            "VALUES (?, ?, ?, ?)",
            (CONTROL_ID + "_B", "local", PAYLOAD, "2026-07-24T08:00:00+00:00"),
        )
        assert journal.exists(), "no rollback journal was written, so nothing was proved"
        assert b"MRN123456" in journal.read_bytes(), (
            "the journal does not hold the row being written, so its mode is not "
            "the thing this test means to assert"
        )
        assert other_bits(journal) == 0, (
            f"{journal} is mode {oct(stat.S_IMODE(os.stat(journal).st_mode))}; it holds "
            f"page images of the rows being written, including verbatim HL7"
        )
    finally:
        conn.rollback()
        conn.close()


def test_a_database_left_open_by_an_earlier_build_is_tightened_on_open(tmp_path, requested):
    """The file already exists, so creating it privately cannot help.

    This is the upgrade case and the one a fix that only handles creation
    misses entirely: every database written before this change is 0644 and
    stays that way until something takes the bits off.
    """
    db = tmp_path / "loops.db"
    db.touch()
    os.chmod(db, 0o644)
    requested.clear()

    LoopStore(db)

    asked = modes_for(requested, db)
    assert asked, "the existing file's mode was never touched, so it is still 0644"
    assert all(mode == PHI_FILE_MODE for mode in asked), asked
    if os.name == "posix":
        assert other_bits(db) == 0


# ------------------------------------------------------------- the directory


def test_the_database_directory_the_cli_creates_is_owner_only(tmp_path, requested):
    db = tmp_path / "fresh" / "referral_loops.db"

    cli_module._prepared_db_path(db)

    assert db.parent.is_dir()
    asked = modes_for(requested, db.parent)
    assert asked, (
        "the data directory was created with the default mode; on a default "
        "umask that is 0755, so every local account can traverse it and open "
        "whatever SQLite leaves inside"
    )
    assert all(mode == PHI_DIR_MODE for mode in asked), asked


@posix_only
def test_the_database_directory_on_disk_is_not_traversable_by_others(tmp_path):
    db = tmp_path / "fresh" / "referral_loops.db"

    cli_module._prepared_db_path(db)

    assert other_bits(db.parent) == 0, (
        f"{db.parent} is mode {oct(stat.S_IMODE(os.stat(db.parent).st_mode))}"
    )


def test_an_existing_directory_is_left_alone(tmp_path, requested):
    """Only the directory this code creates is this code's to re-permission.

    A deployment that points `--db` at a path inside a directory it does not
    own -- a mount point, a shared volume -- must not have that directory
    silently taken private underneath it. The file's own 0600 is what carries
    the guarantee there.
    """
    db = tmp_path / "loops.db"          # tmp_path already exists
    requested.clear()

    cli_module._prepared_db_path(db)

    assert modes_for(requested, tmp_path) == []


# ------------------------------------------------------------- the audit file


def test_the_audit_database_is_created_with_an_owner_only_mode_requested(
    tmp_path, requested, monkeypatch
):
    """PHI-adjacent rather than PHI, and hardened for the same reason.

    Its rows name loops, actors and actions; spec test 14 greps its raw bytes
    for planted identifiers precisely because a leak into it would be a leak
    into a file with no other protection.
    """
    path = tmp_path / "audit" / "audit_trail.db"
    monkeypatch.setattr(immutable_audit, "AUDIT_DB", str(path))

    immutable_audit.init_audit_db()

    file_modes = modes_for(requested, path)
    dir_modes = modes_for(requested, path.parent)
    assert file_modes and all(mode == PHI_FILE_MODE for mode in file_modes), file_modes
    assert dir_modes and all(mode == PHI_DIR_MODE for mode in dir_modes), dir_modes
