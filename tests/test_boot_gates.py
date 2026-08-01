"""Three boot gates, each proved to fire on its own, plus what an operator sees.

The interesting property is **independence**. A gate that only refuses when the
other two also fail is not a gate, and a suite that only ever tests them from a
cold environment (nothing set, nothing signed, no database) cannot tell the
difference: one `raise` at the top of `boot` would pass every such test. So each
gate here is broken *while the other two are satisfied*, and there is a control
test proving that the same environment with nothing broken actually boots --
without which "it refused" says nothing about why.

The second thing under test is the refusal itself. This is the process an
operator runs at 3am when the interface is down, so a gate failure must be one
line naming the environment variable or file to fix, on stderr, exit 2 -- not a
traceback. `--help` has to work with no pack, no environment and no database,
because it is the first thing they will run.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from healthcare_rag.referral_loop import audit as referral_audit
from healthcare_rag.referral_loop import cli
from healthcare_rag.referral_loop.cli import PUBKEY_ENV, boot, main
from healthcare_rag.referral_loop.errors import (
    PackVerificationError,
    ReferralLoopError,
    StoreUnavailableError,
    ThresholdsNotAcceptedError,
)
from healthcare_rag.referral_loop.events import LoopEvent, LoopState
from healthcare_rag.referral_loop.mllp import deframe, frame
from healthcare_rag.referral_loop.retention import RAW_DAYS_ENV, RESOLVED_DAYS_ENV
from healthcare_rag.referral_loop.store import LoopStore
from tests.referral_loop.test_listener import MRN, ORDERED_AT, order, result

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PACK_DIR = REPO_ROOT / "healthcare_rag" / "referral_loop" / "rules"
SHIPPED_PUBKEY = "adb7af9938740d48d237fc2e191c7a52d41000654d7e8335d08edd51a97ed105"


# --------------------------------------------------------------------- helpers


def _sign_pack_into(directory: Path, overrides: dict | None = None) -> str:
    """A locally-signed copy of the shipped pack, and the public key for it.

    A fresh key per call, so a test that mutates the pack cannot accidentally
    still verify against the shipped signature.
    """
    body = json.loads((SHIPPED_PACK_DIR / "pack.json").read_bytes())
    body.update(overrides or {})
    packed = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    key = Ed25519PrivateKey.generate()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pack.json").write_bytes(packed)
    (directory / "pack.sig").write_bytes(key.sign(packed))
    return key.public_key().public_bytes_raw().hex()


# Captured before anything replaces it, so `_serving` can reach the genuine one
# past the autouse guard below.
_REAL_SERVE_FOREVER = socketserver.BaseServer.serve_forever


@pytest.fixture(autouse=True)
def _no_accidental_serving(monkeypatch):
    """Turn "the refusal did not happen" from a hang into a failure.

    Most tests here call `main(["listen", ...])` or `main(["worklist", ...])`
    expecting a gate to refuse before a socket is ever bound. If the gate stops
    firing, `main` runs to `serve_forever` and blocks the suite **forever**
    rather than failing -- found by mutation M1, which removed the encryption
    gate and hung the run for twenty minutes with two idle processes and no
    output. A suite that hangs when a safety gate breaks is worse than one that
    fails: on CI it reads as an infrastructure problem, and the usual response
    to those is a retry.

    `_serving` puts the real one back for the three tests that genuinely serve.
    """

    def refuse(self, poll_interval=0.5):
        address = self.server_address
        self.server_close()
        raise AssertionError(
            f"main() reached serve_forever and bound {address}; a test that expected a "
            "refusal got a running server instead"
        )

    monkeypatch.setattr(socketserver.BaseServer, "serve_forever", refuse)


@pytest.fixture()
def good_env(monkeypatch):
    """Every gate satisfied. Individual tests break exactly one."""
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    monkeypatch.setenv(PUBKEY_ENV, SHIPPED_PUBKEY)


@pytest.fixture()
def booted(tmp_path, good_env):
    return boot(
        db_path=tmp_path / "loops.db",
        pack_dir=SHIPPED_PACK_DIR,
        public_key_hex=SHIPPED_PUBKEY,
    )


@contextmanager
def _serving(argv: list[str]):
    """Run `main(argv)` on a thread with the server it binds handed back.

    `serve_forever` is intercepted rather than replaced: the real one still
    runs, on a real bound socket, so what the test then inspects is the address
    the process actually bound -- not an argument the test passed in and read
    back out.
    """
    guarded = socketserver.BaseServer.serve_forever
    started = threading.Event()
    captured: dict = {}

    def capture(self, poll_interval=0.5):
        captured["server"] = self
        started.set()
        _REAL_SERVE_FOREVER(self, poll_interval=0.05)

    socketserver.BaseServer.serve_forever = capture
    thread = threading.Thread(target=main, args=(argv,), daemon=True)
    try:
        thread.start()
        assert started.wait(20), "server never started"
        yield captured["server"]
    finally:
        server = captured.get("server")
        if server is not None:
            server.shutdown()
        thread.join(20)
        socketserver.BaseServer.serve_forever = guarded


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ------------------------------------------------------- the plan's two tests


def test_refuses_to_boot_when_encryption_at_rest_is_unverified(tmp_path, monkeypatch):
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr(
        "healthcare_rag.encryption_check._detect_os_encryption", lambda: None
    )
    with pytest.raises(RuntimeError, match="encryption at rest"):
        boot(db_path=tmp_path / "loops.db", pack_dir=tmp_path, public_key_hex="00" * 32)


def test_refuses_to_boot_on_an_invalid_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    with pytest.raises(PackVerificationError):
        boot(db_path=tmp_path / "loops.db", pack_dir=tmp_path, public_key_hex="00" * 32)


# ------------------------------------------------------------ gate independence


def test_the_control_case_boots(booted):
    """Without this, every refusal below proves nothing.

    If the environment these tests call "all gates satisfied" did not in fact
    boot, each independence test would be passing on some unrelated failure and
    the suite would still be green.
    """
    assert booted.pack.version
    assert booted.store is not None and booted.handler is not None


def test_encryption_gate_fires_alone(tmp_path, good_env, monkeypatch):
    """Pack valid, thresholds accepted, encryption unattested."""
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr("healthcare_rag.encryption_check._detect_os_encryption", lambda: None)
    with pytest.raises(RuntimeError, match="encryption at rest"):
        boot(db_path=tmp_path / "loops.db", pack_dir=SHIPPED_PACK_DIR,
             public_key_hex=SHIPPED_PUBKEY)


def test_pack_gate_fires_alone(tmp_path, good_env):
    """Encryption attested, thresholds accepted, one byte of the pack altered."""
    pack_dir = tmp_path / "pack"
    public_key = _sign_pack_into(pack_dir)
    body = bytearray((pack_dir / "pack.json").read_bytes())
    body[-2] ^= 0x01
    (pack_dir / "pack.json").write_bytes(bytes(body))

    with pytest.raises(PackVerificationError, match="signature invalid"):
        boot(db_path=tmp_path / "loops.db", pack_dir=pack_dir, public_key_hex=public_key)


def test_thresholds_gate_fires_alone(tmp_path, good_env, monkeypatch):
    """Encryption attested, pack valid and correctly signed, thresholds unaccepted."""
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    with pytest.raises(ThresholdsNotAcceptedError, match="REFERRAL_THRESHOLDS_ACCEPTED"):
        boot(db_path=tmp_path / "loops.db", pack_dir=SHIPPED_PACK_DIR,
             public_key_hex=SHIPPED_PUBKEY)


def test_thresholds_set_to_something_other_than_1_is_not_acceptance(tmp_path, good_env,
                                                                    monkeypatch):
    """`REFERRAL_THRESHOLDS_ACCEPTED=true` is a plausible typo and must not pass.

    The gate exists so a number is the hospital's clinical decision; a value
    nobody defined quietly meaning "yes" would give that decision back to us.
    """
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "true")
    with pytest.raises(ThresholdsNotAcceptedError):
        boot(db_path=tmp_path / "loops.db", pack_dir=SHIPPED_PACK_DIR,
             public_key_hex=SHIPPED_PUBKEY)


def test_no_database_file_is_created_when_a_gate_refuses(tmp_path, good_env, monkeypatch):
    """A refused boot must not leave a PHI-shaped file on the volume it refused.

    The encryption gate exists to keep this database off an unencrypted disk.
    Creating the store first and checking afterwards would satisfy every
    "raises" assertion above while doing the exact thing the gate forbids.
    """
    db = tmp_path / "nested" / "loops.db"
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr("healthcare_rag.encryption_check._detect_os_encryption", lambda: None)
    with pytest.raises(RuntimeError):
        boot(db_path=db, pack_dir=SHIPPED_PACK_DIR, public_key_hex=SHIPPED_PUBKEY)
    assert not db.exists()
    assert not db.parent.exists(), "the gate ran after the directory was created"


# --------------------------------------------------------- the public key itself


@pytest.mark.parametrize(
    "bad_key",
    ["not-hex-at-all", "abc", "00" * 31, "00" * 33, "0x" + "00" * 32],
    ids=["non-hex", "odd-length", "too-short", "too-long", "0x-prefixed"],
)
def test_a_malformed_public_key_is_a_pack_refusal_not_a_traceback(tmp_path, good_env, bad_key):
    """`bytes.fromhex` and `from_public_bytes` both raise bare ValueError.

    Either would sail straight past every `except PackVerificationError` in the
    boot path and reach an operator as a traceback, for the entirely ordinary
    mistake of pasting a key with the `0x` prefix or a character missing.
    """
    with pytest.raises(PackVerificationError):
        boot(db_path=tmp_path / "loops.db", pack_dir=SHIPPED_PACK_DIR,
             public_key_hex=bad_key)


def test_a_valid_key_that_is_the_wrong_key_is_refused(tmp_path, good_env):
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    with pytest.raises(PackVerificationError, match="signature invalid"):
        boot(db_path=tmp_path / "loops.db", pack_dir=SHIPPED_PACK_DIR, public_key_hex=other)


def test_whitespace_around_the_key_is_tolerated(tmp_path, good_env):
    """A key read from a file or a secret manager routinely carries a newline."""
    stack = boot(db_path=tmp_path / "loops.db", pack_dir=SHIPPED_PACK_DIR,
                 public_key_hex=f"  {SHIPPED_PUBKEY}\n")
    assert stack.pack.version


# --------------------------------------------------- what the operator actually sees


def test_a_gate_failure_prints_one_legible_line_and_exits_2(tmp_path, good_env,
                                                            monkeypatch, capsys):
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    code = main(["listen", "--allow-plaintext", "--db", str(tmp_path / "loops.db"),
                 "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err
    assert "REFERRAL_THRESHOLDS_ACCEPTED" in err
    assert len([line for line in err.splitlines() if line.strip()]) == 1


def test_a_missing_public_key_names_the_variable(tmp_path, good_env, monkeypatch, capsys):
    monkeypatch.delenv(PUBKEY_ENV, raising=False)
    code = main(["listen", "--allow-plaintext", "--db", str(tmp_path / "loops.db")])
    err = capsys.readouterr().err
    assert code == 2
    assert PUBKEY_ENV in err
    assert "Traceback" not in err


def test_an_empty_public_key_is_treated_as_missing(tmp_path, good_env, monkeypatch, capsys):
    """`REFERRAL_PACK_PUBKEY=` in a compose file sets it to the empty string."""
    monkeypatch.setenv(PUBKEY_ENV, "   ")
    code = main(["listen", "--allow-plaintext", "--db", str(tmp_path / "loops.db")])
    assert code == 2
    assert PUBKEY_ENV in capsys.readouterr().err


def test_a_missing_pack_names_the_directory_it_looked_in(tmp_path, good_env, capsys):
    code = main(["listen", "--allow-plaintext", "--db", str(tmp_path / "loops.db"),
                 "--pack-dir", str(tmp_path / "no-pack-here")])
    err = capsys.readouterr().err
    assert code == 2
    assert "no-pack-here" in err
    assert "Traceback" not in err


def test_encryption_refusal_names_the_attestation_variable(tmp_path, good_env,
                                                           monkeypatch, capsys):
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr("healthcare_rag.encryption_check._detect_os_encryption", lambda: None)
    code = main(["listen", "--allow-plaintext", "--db", str(tmp_path / "loops.db"),
                 "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2
    assert "PHI_ENCRYPTION_VERIFIED" in err
    assert "Traceback" not in err


def test_purge_refuses_an_unstated_retention_policy_before_any_gate_runs(
    tmp_path, monkeypatch, capsys
):
    """Spec section 6: retention is configured, not assumed.

    Deliberately answered before the gates and before the pack key is looked
    for. An operator who runs `purge` with no period set needs to hear which
    variables to set, not that their pack is unsigned -- and a `purge` that
    exited 0 while deleting nothing would be the worst possible outcome, since
    retention compliance would then be asserted by a no-op.
    """
    for name in ("PHI_MODE", "PHI_ENCRYPTION_VERIFIED", "REFERRAL_THRESHOLDS_ACCEPTED",
                 PUBKEY_ENV, RAW_DAYS_ENV, RESOLVED_DAYS_ENV):
        monkeypatch.delenv(name, raising=False)
    code = main(["purge", "--db", str(tmp_path / "loops.db")])
    err = capsys.readouterr().err
    assert code == 2
    assert RAW_DAYS_ENV in err and RESOLVED_DAYS_ENV in err
    assert "Traceback" not in err
    assert not (tmp_path / "loops.db").exists(), "a refused purge created a PHI file"


def test_purge_still_refuses_when_only_one_period_is_stated(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(RAW_DAYS_ENV, "30")
    monkeypatch.delenv(RESOLVED_DAYS_ENV, raising=False)
    assert main(["purge", "--db", str(tmp_path / "loops.db")]) == 2
    assert RESOLVED_DAYS_ENV in capsys.readouterr().err


def test_purge_refuses_an_unattested_volume_even_with_a_stated_policy(
    tmp_path, monkeypatch, capsys
):
    """The one gate a purge does run. It opens a file of MRNs and result text to
    decide what to delete, so encryption at rest has to hold first."""
    monkeypatch.setenv(RAW_DAYS_ENV, "30")
    monkeypatch.setenv(RESOLVED_DAYS_ENV, "365")
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr("healthcare_rag.encryption_check._detect_os_encryption", lambda: None)

    code = main(["purge", "--db", str(tmp_path / "loops.db")])

    assert code == 2
    assert "PHI_ENCRYPTION_VERIFIED" in capsys.readouterr().err
    assert not (tmp_path / "loops.db").exists()


def test_purge_needs_no_pack_key_and_no_accepted_thresholds(tmp_path, monkeypatch, capsys):
    """A purge loads no pack and computes no staleness. Putting a site's ability
    to meet its own retention obligation behind a signing key it does not use
    would be gate theatre."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L-00000000aaaa", "created",
                                 datetime.now(timezone.utc) - timedelta(days=800), "C1",
                                 {"mrn": "MRN1"}))
    store.append_event(LoopEvent("L-00000000aaaa", "acknowledged",
                                 datetime.now(timezone.utc) - timedelta(days=800), "C2", {}))

    for name in (PUBKEY_ENV, "REFERRAL_THRESHOLDS_ACCEPTED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(RAW_DAYS_ENV, "30")
    monkeypatch.setenv(RESOLVED_DAYS_ENV, "365")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")

    code = main(["purge", "--db", str(tmp_path / "loops.db")])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "1 resolved loop(s)" in out
    assert store.all_loops() == []


def test_purge_refuses_a_database_that_does_not_exist(tmp_path, monkeypatch, capsys):
    """Every other mode creates its file. A purge has nothing to start, and a
    typo in --db would otherwise build an empty database, purge nothing, print
    "deleted 0" and exit 0 -- with the site's retention obligation reading as
    enforced against a file that has never held a message."""
    monkeypatch.setenv(RAW_DAYS_ENV, "30")
    monkeypatch.setenv(RESOLVED_DAYS_ENV, "365")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")

    missing = tmp_path / "typo" / "loops.db"
    code = main(["purge", "--db", str(missing)])

    assert code == 2
    assert "nothing to purge" in capsys.readouterr().err
    assert not missing.exists()
    assert not missing.parent.exists(), "a refused purge created the data directory"


def test_stats_needs_no_pack_key_and_no_accepted_thresholds(tmp_path, monkeypatch, capsys):
    """A stats report matches nothing and computes no staleness. Putting an
    operator's ability to see their own disk usage behind a signing key it
    does not use would be the same gate theatre purge already refuses."""
    store = LoopStore(tmp_path / "loops.db")
    store.record_applied("CTRL1", "content-key-1", "ORU^R01")

    for name in (PUBKEY_ENV, "REFERRAL_THRESHOLDS_ACCEPTED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")

    code = main(["stats", "--db", str(tmp_path / "loops.db")])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "applied_messages" in out
    assert "1 row(s)" in out


def test_stats_refuses_an_unattested_volume(tmp_path, monkeypatch, capsys):
    """It opens a file of MRNs and result text to sum column lengths, so
    encryption at rest has to hold first -- the same posture purge takes."""
    LoopStore(tmp_path / "loops.db")
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr("healthcare_rag.encryption_check._detect_os_encryption", lambda: None)

    code = main(["stats", "--db", str(tmp_path / "loops.db")])

    assert code == 2
    assert "PHI_ENCRYPTION_VERIFIED" in capsys.readouterr().err


def test_stats_refuses_a_database_that_does_not_exist(tmp_path, monkeypatch, capsys):
    """A typo'd --db would otherwise build an empty database and report "0
    rows in every table" -- readable as "nothing has grown" when the truth is
    "you are not looking at the file the listener writes to"."""
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    missing = tmp_path / "typo" / "loops.db"

    code = main(["stats", "--db", str(missing)])

    assert code == 2
    assert "nothing to report on" in capsys.readouterr().err
    assert not missing.exists()
    assert not missing.parent.exists(), "a refused stats report created the data directory"


def test_stats_reports_row_counts_and_flags_the_tables_retention_never_touches(
    tmp_path, monkeypatch, capsys
):
    store = LoopStore(tmp_path / "loops.db")
    store.record_raw("RAW1", "MSH|payload")
    store.record_applied("CTRL1", "content-key-1", "ORU^R01")
    store.record_alias("MRN_OLD", "MRN_NEW", datetime.now(timezone.utc), "engine")

    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    code = main(["stats", "--db", str(tmp_path / "loops.db")])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "raw_messages" in out and "NOT retention-bounded" not in out.split("raw_messages")[1].split("\n")[0]
    assert "applied_messages" in out
    applied_line = [line for line in out.splitlines() if "applied_messages" in line][0]
    assert "NOT retention-bounded" in applied_line
    alias_line = [line for line in out.splitlines() if "mrn_alias_events" in line][0]
    assert "NOT retention-bounded" in alias_line


def test_stats_never_creates_a_worklist_or_mllp_socket(tmp_path, monkeypatch, capsys):
    """A read-only report has no business binding a port. Guards against a
    refactor that routes `stats` through the same boot path `listen` uses."""
    LoopStore(tmp_path / "loops.db")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    code = main(["stats", "--db", str(tmp_path / "loops.db")])
    assert code == 0


def test_a_dry_run_purge_reports_without_deleting(tmp_path, monkeypatch, capsys):
    store = LoopStore(tmp_path / "loops.db")
    old = datetime.now(timezone.utc) - timedelta(days=800)
    store.append_event(LoopEvent("L-00000000bbbb", "created", old, "C1", {"mrn": "MRN1"}))
    store.append_event(LoopEvent("L-00000000bbbb", "cancelled", old, "C2", {}))

    monkeypatch.setenv(RAW_DAYS_ENV, "30")
    monkeypatch.setenv(RESOLVED_DAYS_ENV, "365")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")

    code = main(["purge", "--db", str(tmp_path / "loops.db"), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "would delete" in out and "1 resolved loop(s)" in out
    assert len(store.all_loops()) == 1


def test_help_works_with_no_pack_no_environment_and_no_database(tmp_path):
    """The first thing an operator runs when a boot fails.

    A subprocess with a scrubbed environment and a working directory holding
    nothing, because `--help` reaching for a pack or a database is exactly the
    failure that leaves someone with no way to find out what the flags are.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in {"PHI_MODE", "PHI_ENCRYPTION_VERIFIED",
                        "REFERRAL_THRESHOLDS_ACCEPTED", PUBKEY_ENV}}
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "healthcare_rag.referral_loop.cli", "--help"],
        capture_output=True, text=True, timeout=180, cwd=tmp_path, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    for mode in ("listen", "filedrop", "worklist", "purge", "stats"):
        assert mode in proc.stdout
    assert PUBKEY_ENV in proc.stdout, "--help must name the environment it requires"
    assert list(tmp_path.iterdir()) == [], "--help created files"


def test_an_unknown_mode_is_refused_by_argparse(tmp_path, good_env):
    with pytest.raises(SystemExit) as exit_info:
        main(["destroy-everything"])
    assert exit_info.value.code == 2


# --------------------------------------------------------- pack version wiring


def test_the_registry_records_the_pack_version_on_labels(booted):
    """Task 15 shipped labels stamped "unknown" because nothing wired this.

    A label the release gate cannot attribute to a pack version is useless to
    it: spec section 7 evaluates a revision by comparing labels across
    versions, and every label reading "unknown" collapses that comparison.
    """
    assert booted.registry.pack_version == booted.pack.version

    booted.handler.handle(result(control_id="ORU_NO_ORDER"))
    orphans = [loop for loop in booted.store.all_loops() if loop.state is LoopState.ORPHAN]
    assert len(orphans) == 1

    booted.registry.dismiss_orphan(
        orphans[0].loop_id, actor="coord1", role="referral_coordinator",
        reason="misrouted from another facility",
    )
    labels = booted.store.labels()
    assert labels and {label["pack_version"] for label in labels} == {booted.pack.version}
    assert "unknown" not in {label["pack_version"] for label in labels}


# ------------------------------------------------------------------ listen mode


def test_listen_mode_carries_message_at_from_msh7_to_the_registry(tmp_path, good_env):
    """Without this the clinical-ordering guard is inert in production.

    The guard refuses a message clinically older than one already applied. It
    compares MSH-7 values, and `message_at=None` fails open by design -- so a
    stack wired without it never compares anything, every out-of-order message
    is accepted, and a SIU landing after an ORU silently regresses a resulted
    loop back to SCHEDULED. The failure is invisible: nothing raises, the
    worklist just becomes wrong.

    Asserted through a real MLLP socket against a server this module started,
    not against a handler the test built.
    """
    port = _free_port()
    argv = ["listen", "--allow-plaintext", "--port", str(port),
            "--db", str(tmp_path / "loops.db"),
            "--pack-dir", str(SHIPPED_PACK_DIR)]

    with _serving(argv) as server:
        host, bound_port = server.server_address[:2]
        with socket.create_connection((host, bound_port), timeout=10) as sock:
            sock.sendall(frame(order(control_id="ORM_1")))
            assert "MSA|AA" in _read_frame(sock)

    store = LoopStore(tmp_path / "loops.db")
    loops = [loop for loop in store.all_loops() if loop.loop_id.startswith("L-")]
    assert len(loops) == 1
    created = [e for e in store.events_for(loops[0].loop_id) if e.event_type == "created"]
    assert created, f"no created event: {[e.event_type for e in store.events_for(loops[0].loop_id)]}"
    assert created[0].detail.get("message_at", "").startswith("2026-07-24T08:00:00"), (
        f"MSH-7 {ORDERED_AT} did not reach the registry: {created[0].detail!r}"
    )


def test_listen_mode_binds_the_host_it_was_given(tmp_path, good_env):
    port = _free_port()
    argv = ["listen", "--allow-plaintext", "--port", str(port), "--db", str(tmp_path / "loops.db"),
            "--pack-dir", str(SHIPPED_PACK_DIR)]
    with _serving(argv) as server:
        assert server.server_address[1] == port


def test_an_occupied_worklist_port_is_a_legible_refusal_not_an_exit(tmp_path, good_env,
                                                                    capsys):
    """The most likely thing to go wrong on a second start.

    `werkzeug.serving.BaseWSGIServer.__init__` answers a failed bind by printing
    `e.strerror` and calling `sys.exit(1)` from inside the constructor. Left
    alone that kills the process with exit 1 and a message that on Windows reads
    "An attempt was made to access a socket in a way forbidden by its access
    permissions" -- naming neither the port nor which of the two servers failed.
    Measured, which is why `_bound` catches `SystemExit`.
    """
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        occupied = held.getsockname()[1]
        code = main(["worklist", "--worklist-port", str(occupied),
                     "--db", str(tmp_path / "loops.db"),
                     "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2
    assert str(occupied) in err, err
    assert "worklist" in err
    assert "Traceback" not in err


def test_an_occupied_mllp_port_is_a_legible_refusal_not_a_traceback(tmp_path, good_env,
                                                                    capsys):
    """A second listener must not quietly steal the port from the first.

    `MLLPServer.allow_reuse_address = True` is set for TIME_WAIT on Linux, and
    on some platforms that flag lets a second process bind a port another one is
    already listening on -- which would split an interface engine's traffic
    between two processes writing the same database. Asserted rather than
    assumed, because "it refused" and "it stole the port" are indistinguishable
    from the exit code alone.
    """
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        occupied = held.getsockname()[1]
        code = main(["listen", "--allow-plaintext", "--port", str(occupied),
                     "--db", str(tmp_path / "loops.db"),
                     "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2, "a second listener bound a port that was already in use"
    assert str(occupied) in err, err
    assert "Traceback" not in err


# ---------------------------------------------------------------- worklist mode


def test_worklist_binds_loopback_without_being_told_to(tmp_path, good_env):
    """The host is never supplied, so the assertion cannot be reading itself back.

    A previous task shipped a test that passed `--host 127.0.0.1` and then
    asserted the bind was `127.0.0.1`, which holds for any implementation that
    binds what it is given -- including one that would happily bind `0.0.0.0`.
    Here the default is used and the *bound socket* is inspected, and the page
    is fetched over it to prove something is actually serving.
    """
    port = _free_port()
    argv = ["worklist", "--worklist-port", str(port),
            "--db", str(tmp_path / "loops.db"), "--pack-dir", str(SHIPPED_PACK_DIR)]

    with _serving(argv) as server:
        host, bound_port = server.server_address[:2]
        assert host in ("127.0.0.1", "::1"), f"worklist bound {host}"
        url = f"http://127.0.0.1:{bound_port}/worklist/"
        with urllib.request.urlopen(url, timeout=10) as page:
            assert page.status == 200


def test_worklist_refuses_a_non_loopback_bind(tmp_path, good_env, capsys):
    """And the refusal is reachable from the command line, which is what makes
    the test above falsifiable rather than decorative."""
    code = main(["worklist", "--worklist-host", "0.0.0.0", "--worklist-port", "0",
                 "--db", str(tmp_path / "loops.db"), "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2
    assert "0.0.0.0" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------- filedrop mode


def test_filedrop_drains_a_directory_and_removes_what_it_accepted(tmp_path, good_env, capsys):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "01_order.hl7").write_bytes(frame(order(control_id="FD_ORM")))
    (drop / "02_result.hl7").write_text(result(control_id="FD_ORU"), encoding="utf-8")

    code = main(["filedrop", "--drop-dir", str(drop),
                 "--db", str(tmp_path / "loops.db"), "--pack-dir", str(SHIPPED_PACK_DIR)])
    assert code == 0
    assert list(drop.iterdir()) == []

    store = LoopStore(tmp_path / "loops.db")
    loops = [loop for loop in store.all_loops() if loop.loop_id.startswith("L-")]
    assert [loop.state for loop in loops] == [LoopState.RESULTED]


def test_filedrop_refuses_a_directory_that_does_not_exist(tmp_path, good_env, capsys):
    """`Path.glob` on a missing directory yields nothing without raising.

    So a typo'd `--drop-dir` would report a successful drain of zero messages
    and be indistinguishable from a quiet feed -- a silent no-op in a product
    whose entire purpose is that results do not silently go missing.
    """
    code = main(["filedrop", "--drop-dir", str(tmp_path / "typo"),
                 "--db", str(tmp_path / "loops.db"), "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2
    assert "typo" in err
    assert "Traceback" not in err


# ------------------------------------------------------------- hostile database


def test_a_partially_initialised_database_is_a_legible_refusal(tmp_path, good_env, capsys):
    """A file where the database should be, holding a table under a name we use.

    An interrupted first start, or a restore that copied half a file. The
    schema is `CREATE TABLE IF NOT EXISTS`, so a `loops` table with the wrong
    columns is not corrected -- it is detected at the first write. The
    requirement is that the process says so rather than running on a schema it
    silently disagrees with.
    """
    db = tmp_path / "half.db"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE loops (loop_id TEXT)")
    conn.commit()
    conn.close()

    code = main(["filedrop", "--drop-dir", str(tmp_path),
                 "--db", str(db), "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "Traceback" not in err


def test_a_database_path_that_is_a_directory_is_a_legible_refusal(tmp_path, good_env, capsys):
    directory = tmp_path / "loops.db"
    directory.mkdir()
    code = main(["listen", "--allow-plaintext", "--db", str(directory), "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err


def test_boot_creates_a_missing_database_directory(tmp_path, good_env):
    """The shipped default is `data/referral_loops.db`, whose parent does not
    exist on a fresh install.

    sqlite3 answers a missing parent with `unable to open database file`, naming
    neither the directory nor the fact that it is missing -- so a first start
    out of the box would fail on a message nobody can act on. Added because
    mutation M14 (removing the mkdir) survived the suite: every other test in
    this file passes a `tmp_path` that already exists, so none of them ever
    exercised the case the default configuration actually hits.
    """
    db = tmp_path / "deep" / "nested" / "loops.db"
    assert not db.parent.exists()
    stack = boot(db_path=db, pack_dir=SHIPPED_PACK_DIR, public_key_hex=SHIPPED_PUBKEY)
    assert db.exists()
    stack.handler.handle(order(control_id="ORM_MKDIR"))
    assert [loop.loop_id for loop in stack.store.all_loops()]


def test_an_uncreatable_database_directory_is_a_legible_refusal(tmp_path, good_env, capsys):
    """A file where the parent directory should be. Portable stand-in for a
    read-only mount: `chmod 0o500` is a no-op for the owner on Windows, so a
    permissions-based version of this test would pass vacuously on this
    platform and prove nothing."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    code = main(["listen", "--allow-plaintext", "--db", str(blocker / "sub" / "loops.db"),
                 "--pack-dir", str(SHIPPED_PACK_DIR)])
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err
    # Exit 2 alone does not distinguish this from letting sqlite3 answer
    # "unable to open database file", which names neither the directory nor
    # what to do about it. Mutation M17 -- swallowing the mkdir failure --
    # survived a version of this test that stopped at the exit code.
    assert "Cannot create the database directory" in err
    assert str(blocker / "sub") in err


def test_the_store_is_reused_across_restarts_rather_than_recreated(tmp_path, good_env):
    """Two boots on one file: the second must see the first one's loops."""
    db = tmp_path / "loops.db"
    first = boot(db_path=db, pack_dir=SHIPPED_PACK_DIR, public_key_hex=SHIPPED_PUBKEY)
    first.handler.handle(order(control_id="ORM_RESTART"))

    second = boot(db_path=db, pack_dir=SHIPPED_PACK_DIR, public_key_hex=SHIPPED_PUBKEY)
    assert [loop.loop_id for loop in second.store.all_loops()
            if loop.loop_id.startswith("L-")] == \
           [loop.loop_id for loop in first.store.all_loops()
            if loop.loop_id.startswith("L-")]


# ------------------------------------------------------------------ what leaks


def test_no_phi_reaches_stderr_or_the_log_on_any_boot_failure(tmp_path, good_env,
                                                              monkeypatch, capsys, caplog):
    """Sentinels planted in every operator-controlled string a refusal can echo.

    Boot processes no message, so the leak to hunt is not a PID field -- it is
    a path or an argument carrying a patient or hospital identifier into a log
    that gets shipped off the box. The MRN sentinel is planted in the paths
    themselves, which is the only way it can be present at boot at all.
    """
    sentinel = "ZZSENTINELMRN9999"
    db = tmp_path / sentinel / "loops.db"
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    code = main(["listen", "--allow-plaintext", "--db", str(db), "--pack-dir", str(SHIPPED_PACK_DIR)])
    assert code == 2
    captured = capsys.readouterr()
    assert sentinel not in captured.err + captured.out + caplog.text, (
        "a path carrying a patient identifier reached the operator surface"
    )


def test_a_refused_pack_load_writes_no_path_into_the_audit_row(tmp_path, good_env,
                                                               isolated_audit_db):
    """`load_pack` audits its refusals. The row must carry no filesystem path.

    An audit database is durable and exportable, which is precisely why
    `pack.py` keeps paths out of it -- a pack directory is the sort of string
    that turns out to contain a hospital's name. Re-asserted here because the
    CLI is the first caller that passes an operator-chosen path in.
    """
    marked = tmp_path / "ZZHOSPITALNAME"
    marked.mkdir()
    with pytest.raises(PackVerificationError):
        boot(db_path=tmp_path / "loops.db", pack_dir=marked, public_key_hex=SHIPPED_PUBKEY)

    import sqlite3

    conn = sqlite3.connect(str(isolated_audit_db))
    rows = conn.execute("SELECT * FROM audit_events").fetchall()
    conn.close()
    assert rows, "load_pack audits its refusals; no row means the audit stopped happening"
    blob = " ".join(str(value) for row in rows for value in row)
    assert "ZZHOSPITALNAME" not in blob
    assert str(tmp_path) not in blob


def test_a_full_run_leaks_no_message_phi_into_the_log(tmp_path, good_env, caplog, capsys):
    """Spec test 14, scoped to what the CLI itself emits.

    A message carrying a name, an MRN and note text is driven all the way
    through a booted stack at INFO -- the level the CLI configures -- and none
    of the three may appear in the log. `filedrop` is the mode that logs a
    *file name*, which is the leak this subsystem creates for itself: a site
    naming its drop files after patients would export PHI through the log
    without a single PID field being involved.
    """
    import logging

    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "order.hl7").write_text(order(control_id="LEAK_ORM"), encoding="utf-8")
    (drop / "res.hl7").write_text(
        result(control_id="LEAK_ORU", value="ZZNOTESENTINEL findings"), encoding="utf-8"
    )

    with caplog.at_level(logging.DEBUG):
        code = main(["filedrop", "--drop-dir", str(drop), "--log-level", "DEBUG",
                     "--db", str(tmp_path / "loops.db"),
                     "--pack-dir", str(SHIPPED_PACK_DIR)])
    assert code == 0
    surface = caplog.text + capsys.readouterr().err
    for sentinel in (MRN, "DOE^JANE", "ZZNOTESENTINEL"):
        assert sentinel not in surface, f"{sentinel!r} reached the log"


def test_filedrop_file_names_are_an_operator_hazard_not_a_silent_leak(tmp_path, good_env,
                                                                     caplog):
    """A rejected file *is* named in the log, and that is recorded here on purpose.

    `FileDropSource` logs the path of anything it quarantines or defers. A site
    that names drop files after patients therefore exports an identifier
    through the log. That is a documented deployment constraint rather than a
    defect this task can fix -- the alternative, logging nothing, would leave an
    operator unable to find the file that was rejected -- and it is asserted
    here so a security review meets it in the suite rather than discovering it.
    """
    import logging

    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "ZZPATIENTNAME.hl7").write_bytes(b"\x0bnot a message at all")

    with caplog.at_level(logging.DEBUG):
        main(["filedrop", "--drop-dir", str(drop),
              "--db", str(tmp_path / "loops.db"), "--pack-dir", str(SHIPPED_PACK_DIR)])

    assert "ZZPATIENTNAME" in caplog.text, (
        "if this ever stops holding, FileDropSource changed and the deployment note "
        "about drop-file naming can be revisited"
    )


# ------------------------------------------------------------------- no egress


def test_booting_and_draining_opens_no_outbound_connection(tmp_path, good_env, monkeypatch):
    """Spec test 12 at the level that constructs the system.

    Every module proved this for itself. The CLI is the first thing that wires
    them together, and wiring is where an outbound call would be introduced --
    a metrics push, a licence check, a phone-home on boot.
    """
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "a.hl7").write_text(order(control_id="NOEGRESS_1"), encoding="utf-8")

    real_connect = socket.socket.connect

    def refuse(self, address):  # pragma: no cover - the assertion is the point
        raise AssertionError(f"outbound connection attempted to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    try:
        code = main(["filedrop", "--drop-dir", str(drop),
                     "--db", str(tmp_path / "loops.db"),
                     "--pack-dir", str(SHIPPED_PACK_DIR)])
    finally:
        monkeypatch.setattr(socket.socket, "connect", real_connect)
    assert code == 0


def test_the_cli_imports_no_model_client(tmp_path):
    """Structural half of spec test 13, at the entry point.

    The slim image has no `anthropic` installed at all, so this is what makes
    "no model calls" true rather than enforced. In the dev environment
    `anthropic` *is* importable via the parent package's shim, so the assertion
    is that `cli` itself neither imports nor holds a client.
    """
    source = Path(cli.__file__).read_text(encoding="utf-8")
    for forbidden in ("anthropic", "claude_cli", "chromadb", "sentence_transformers"):
        assert forbidden not in source


# ------------------------------------------------------------------- utilities


def _read_frame(sock: socket.socket, timeout: float = 10.0) -> str:
    sock.settimeout(timeout)
    buffer = b""
    while b"\x1c\r" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    return deframe(buffer)


def test_audit_module_is_the_referral_one(booted):
    """Guards the conftest isolation this file relies on for the audit test."""
    assert referral_audit._module().__name__.endswith("immutable_audit")


def test_boot_is_a_named_tuple_that_unpacks(booted):
    store, registry, handler, pack = booted
    assert (store, registry, handler, pack) == (
        booted.store, booted.registry, booted.handler, booted.pack
    )


def test_store_unavailable_is_a_referral_loop_error():
    """The boot path catches ReferralLoopError; StoreUnavailableError must be one
    or a broken database would escape as a traceback."""
    assert issubclass(StoreUnavailableError, ReferralLoopError)
