"""`referral-loop` -- the entry point that actually constructs the system.

Every module below this one has been exercised only from tests. This is where
they are wired together for a real site, so it is also the only place the three
boot gates can be enforced. All three fail closed, and each one is independent:
breaking any single gate refuses the boot even when the other two are satisfied.

1. **Encryption at rest.** `PHI_MODE=full` is the operating mode (spec section
   3), and `verify_encryption_at_rest` raises unless BitLocker/LUKS/KMS is
   detected or an operator has attested `PHI_ENCRYPTION_VERIFIED=1`. This
   subsystem writes MRNs, order numbers and result text to a SQLite file; that
   file being on an unencrypted volume is a reportable breach waiting for a
   stolen laptop, not a configuration preference.

2. **Pack signature.** A tampered pack could lower the confidence floor or point
   `placer_order_number` at the wrong field, turning every tier-1 match into a
   false match -- the one failure the product exists to prevent (spec sections 7
   and 8). An unsigned or altered pack refuses the boot; it is a safety control
   before it is IP protection.

3. **Threshold acceptance.** Per-modality staleness thresholds ship as defaults,
   and shipping a number silently implies a clinical standard that is the
   hospital's call (spec open question 3). `REFERRAL_THRESHOLDS_ACCEPTED=1` is
   the site saying the numbers are theirs. Gated here as well as inside
   `staleness` so a site cannot run a listener for a week and discover at first
   worklist load that the queue it has been filling cannot be sorted.

The gates run **before** `LoopStore` is constructed. Creating the database file
first would mean a site that fails the encryption gate still has a PHI-shaped
file on an unencrypted volume, which is the exact thing the gate refuses.

What a gate failure prints
--------------------------
One line on stderr naming the environment variable or the file to fix, and exit
2. Not a traceback: an operator diagnosing a refused boot at 3am is not served
by forty frames of `cryptography` internals, and a traceback in a container log
is the sort of thing that gets pasted into a ticket.

Those messages do contain the pack directory and database path the operator
passed in. That is deliberate and is not the same decision as `pack.py`'s
refusal to put a path in an *audit row*: an audit row is a durable, exportable
artifact where a path is both useless and the sort of string that turns out to
contain a hospital's name, whereas boot stderr is the diagnostic surface and
"No pack at <where I looked>" is the entire content of the diagnosis. No
message has been processed at boot, so no clinical PHI exists to leak.
"""
from __future__ import annotations

import argparse
import binascii
import logging
import os
import sys
from pathlib import Path
from typing import NamedTuple

from . import eval as eval_harness
from .encryption_check import verify_encryption_at_rest
from .errors import (
    PackConceptMissingError,
    PackVerificationError,
    ReferralLoopError,
    StoreUnavailableError,
)
from .listener import FileDropSource, MessageHandler
from .mllp_server import make_mllp_server
from .pack import RulePack, load_pack
from .peers import PeerRegistry, load_peer_registry
from .registry import Registry
from .retention import RAW_DAYS_ENV, RESOLVED_DAYS_ENV, RetentionPolicy
from .retention import purge as run_purge
from .staleness import require_thresholds_accepted
from .store import STATS_TABLES, LoopStore
from .worklist import make_worklist_server

logger = logging.getLogger(__name__)

PROG = "referral-loop"
DEFAULT_PACK_DIR = Path(__file__).parent / "rules"
PUBKEY_ENV = "REFERRAL_PACK_PUBKEY"

# Ed25519 public keys are exactly 32 bytes. Checked here rather than left to
# `Ed25519PublicKey.from_public_bytes`, which raises a bare ValueError that
# would escape every `except PackVerificationError` in the boot path and reach
# an operator as a traceback.
_ED25519_PUBLIC_KEY_BYTES = 32

MODES = ("listen", "filedrop", "worklist", "eval", "purge", "stats", "connectors")

# `eval` exit codes. Distinct from _refuse's 2, because "this pack must not ship"
# and "this process could not start" send an operator to different places.
EVAL_ALLOWED = 0
EVAL_BLOCKED = 1


class BootedStack(NamedTuple):
    """The constructed system. A NamedTuple so it unpacks positionally as well."""

    store: LoopStore
    registry: Registry
    handler: MessageHandler
    pack: RulePack


def _public_key(public_key_hex: str) -> bytes:
    """Hex -> 32 raw bytes, or a PackVerificationError naming the problem.

    Every failure here is a refusal to verify the pack, so it is the pack gate's
    failure and carries the pack gate's exception type. A caller that catches
    `PackVerificationError` to refuse the boot must not be blindsided by a
    `ValueError` from `bytes.fromhex` because someone pasted the key with a
    trailing newline or the `0x` prefix.
    """
    cleaned = "".join(public_key_hex.split())
    try:
        raw = bytes.fromhex(cleaned)
    except (ValueError, binascii.Error) as exc:
        raise PackVerificationError(
            f"{PUBKEY_ENV} is not hexadecimal ({exc}); expected "
            f"{_ED25519_PUBLIC_KEY_BYTES * 2} hex characters of Ed25519 public key"
        ) from exc
    if len(raw) != _ED25519_PUBLIC_KEY_BYTES:
        raise PackVerificationError(
            f"{PUBKEY_ENV} is {len(raw)} bytes; an Ed25519 public key is "
            f"{_ED25519_PUBLIC_KEY_BYTES} bytes ({_ED25519_PUBLIC_KEY_BYTES * 2} hex characters)"
        )
    return raw


def boot(db_path: Path | str, pack_dir: Path | str, public_key_hex: str) -> BootedStack:
    """Run all three boot gates, then build the stack. Raises rather than degrading.

    Gate order is deliberate but not load-bearing for independence -- each gate
    is checked against a state where the other two pass, so none of them is
    riding on another's failure. Encryption goes first because it is the one
    that must hold before anything touches disk.
    """
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"))
    pack = load_pack(Path(pack_dir), _public_key(public_key_hex))
    require_thresholds_accepted()

    store = LoopStore(_prepared_db_path(db_path))
    # The whole reason Task 15's labels recorded pack_version="unknown": nothing
    # constructed a Registry with the pack it was running. A label that cannot
    # be attributed to the rules that produced it is useless to the pack release
    # gate (spec section 7), which evaluates a revision by comparing labels
    # across versions.
    registry = Registry(store, pack_version=pack.version)
    handler = MessageHandler(store=store, registry=registry, pack=pack)
    return BootedStack(store, registry, handler, pack)


def _prepared_db_path(db_path: Path | str) -> Path:
    """Ensure the database's directory exists, as a typed failure if it cannot.

    sqlite3 answers a missing parent directory with `unable to open database
    file`, which names neither the directory nor the fact that it is missing --
    and the default `data/referral_loops.db` has a parent that does not exist on
    a fresh install. Creating it is the app owning its own data directory; the
    encryption gate has already passed by the time this runs, so this cannot
    create a PHI file on a volume that failed attestation.
    """
    path = Path(db_path)
    parent = path.parent
    if parent and not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreUnavailableError(
                f"Cannot create the database directory {parent} ({exc}); "
                "create it and make it writable by this process"
            ) from exc
        logger.info("Created database directory %s", parent)
    return path


# --------------------------------------------------------------------- modes


def _peer_registry(args) -> PeerRegistry:
    """The transport policy for listen mode, or a refusal naming what is missing.

    Three states and no fourth. `--peers` names a registry file, which decides
    for itself whether it is mutual TLS or an explicit plaintext opt-in.
    `--allow-plaintext` with no file is the demo and development posture:
    loopback only, one named identity, and a WARNING on every start. Neither is
    a refused boot, because a listener with no transport policy would otherwise
    be the thing this whole change exists to stop shipping.
    """
    if args.peers:
        return load_peer_registry(Path(args.peers))
    if args.allow_plaintext:
        return PeerRegistry.plaintext_loopback()
    raise ReferralLoopError(
        "listen mode needs a transport policy. Pass --peers FILE with the client "
        "certificate fingerprint and authorities of each interface engine, or "
        "--allow-plaintext to run an unauthenticated loopback-only listener for a demo "
        "or for local development. There is no default: PHI crosses this port, and a "
        "listener that authenticates nothing must be asked for out loud."
    )


def _run_listen(stack: BootedStack, args, host: str, port: int) -> int:
    peers = _peer_registry(args)
    server = _bound(
        lambda: make_mllp_server(stack.handler, host=host, port=port, peers=peers),
        what=f"MLLP listener on {host}:{port}",
    )
    try:
        logger.info("MLLP listening on %s:%d", *server.server_address[:2])
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting the listener down")
    finally:
        server.server_close()
    return 0


def _run_filedrop(stack: BootedStack, drop_dir: Path | str) -> int:
    """Drain a watched directory once, then exit.

    Also the replay mechanism pack evaluation needs (spec section 7): replaying
    the raw archive against a candidate pack is a file source, not a socket.

    A missing directory is refused rather than drained. `Path.glob` on a
    directory that does not exist yields nothing without raising, so a typo'd
    `--drop-dir` would report "Processed 0 messages" and look like a quiet feed
    -- indistinguishable from working, which is the failure mode this whole
    subsystem exists to avoid.
    """
    directory = Path(drop_dir)
    if not directory.is_dir():
        raise StoreUnavailableError(
            f"Drop directory {directory} does not exist or is not a directory; "
            "create it, or point --drop-dir at the directory the engine writes to"
        )
    source = FileDropSource(stack.handler, directory)
    accepted = source.drain()
    logger.info(
        "Drained %s: %d accepted, %d deferred (left for the next drain), %d rejected",
        directory, accepted, source.deferred_count, source.rejected_count,
    )
    return 0


def _run_eval(stack: BootedStack, args, public_key_hex: str) -> int:
    """Replay the labeled corpus through the booted pack and apply the gate.

    This is the operator's half of spec section 7: a pack revision is justified
    by replay evidence or it does not ship. Without a command an operator can
    actually run, "rules as signed data" is a claim nobody at the site can check.

    Two answers, and they are deliberately different exit codes. **2** is a
    refusal -- the candidate does not clear criterion 4's absolute floor, which is
    true of it alone and needs no baseline. **1** is the release gate blocking a
    comparison against a named baseline. **0** means it may ship.

    The candidate is evaluated on its own *first*. A pack that attaches nothing
    would otherwise reach the comparison and could pass it whenever the baseline
    also attached nothing, and the two of them would ship each other.

    Nothing this prints can carry an identifier: every line comes from
    `format_report`, which reads only floats and ints off `EvalResult`, or from a
    gate reason built from the same. The corpus itself holds PHI when it was
    reconstructed from the site archive, and never leaves this process.
    """
    cases = eval_harness.synthetic_corpus()
    if not args.synthetic_only:
        cases = cases + eval_harness.corpus_from_site(stack.store)
    labels = stack.store.labels()

    print(f"{PROG}: corpus of {len(cases)} labeled case(s); "
          f"{sum(1 for c in cases if c.source == 'site')} reconstructed from site labels")

    candidate = eval_harness.replay(cases, stack.pack, labels=labels)
    print(eval_harness.format_report(candidate, title="candidate"))

    meets, why = eval_harness.check_release_criteria(candidate, stack.pack)
    print(f"{PROG}: {why}")
    if not meets:
        return 2

    if not args.baseline_pack_dir:
        print(f"{PROG}: no --baseline-pack-dir given, so the release gate was not applied. "
              "A pack ships on a measured delta against the pack it replaces.")
        return EVAL_ALLOWED

    try:
        baseline_pack = load_pack(Path(args.baseline_pack_dir), _public_key(public_key_hex))
    except PackConceptMissingError as exc:
        # The signature verified and the field map is well-formed; this pack is
        # older than the build, not corrupt. Reworded here rather than in
        # `load_pack` because the two callers need opposite advice: a *running*
        # site on such a pack has to fix the pack it is running, while a gate
        # has a second, cheaper way out -- pick a later baseline. An operator
        # reading the load-time wording at 3am would go looking for a tamper.
        concepts = ", ".join(exc.missing)
        raise PackVerificationError(
            f"the baseline pack at {args.baseline_pack_dir} verified, but its field_map "
            f"predates this build: it does not name {concepts}, which this build reads on "
            f"every message. It is too old to replay the corpus through, not corrupt. "
            f"Either re-sign that baseline with {concepts} added to its field_map, or gate "
            f"against a later baseline that already carries it."
        ) from exc
    baseline = eval_harness.replay(cases, baseline_pack, labels=labels)
    print(eval_harness.format_report(baseline, title="baseline"))

    allowed, reason = eval_harness.gate_pack_release(baseline, candidate, pack=stack.pack)
    print(f"{PROG}: {reason}")
    return EVAL_ALLOWED if allowed else EVAL_BLOCKED


def _run_purge(args) -> int:
    """Enforce the site's retention policy. Two gates, and deliberately not three.

    **The policy is read first, before anything touches disk.** An operator who
    has not stated a retention period needs to hear that, not that their pack is
    unsigned -- and `RetentionPolicy.from_env` reads only the environment, so
    answering it first costs nothing and creates nothing.

    **The encryption gate still runs before `LoopStore` is constructed**, for
    the same reason it does in `boot`: this opens a file full of MRNs and result
    text, and a purge on an unattested volume would be reading PHI off it to
    decide what to delete.

    The other two gates are skipped, and that is a judgement rather than an
    oversight. The pack gate exists because a tampered pack causes false
    matches; a purge loads no pack and matches nothing. The threshold gate
    exists because staleness implies a clinical standard; a purge computes no
    staleness. Requiring either would be gate theatre -- and worse, it would put
    a site's ability to meet its own retention obligation behind a signing key
    that has nothing to do with it.

    `--dry-run` is not a convenience. This is the one command in the subsystem
    that destroys clinical records, and an operator should be able to see what a
    period actually reaches before it reaches it.

    **A database that does not exist is refused, not created.** Every other mode
    creates its file, because a fresh install has to start somewhere. A purge
    has nothing to start: a typo in `--db` would otherwise build an empty
    database, purge nothing from it, print "deleted 0" and exit 0 -- and the
    site's retention obligation would read as met against a file that has never
    held a message. That is `_run_filedrop`'s missing-directory case with a
    compliance record attached to it.
    """
    policy = RetentionPolicy.from_env()
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"))

    db_path = Path(args.db)
    if not db_path.is_file():
        raise StoreUnavailableError(
            f"No database at {db_path}, so there is nothing to purge. Point --db at the file "
            "the listener writes to. It is not created here: purging a database this command "
            "just made would report a retention policy as enforced against a file that has "
            "never held a message."
        )
    store = LoopStore(db_path)
    report = run_purge(store, policy, dry_run=args.dry_run, reclaim=not args.no_reclaim)

    verb = "would delete" if args.dry_run else "deleted"
    print(
        f"{PROG}: retention {'dry run' if args.dry_run else 'purge'} complete "
        f"(raw {policy.raw_days}d, resolved {policy.resolved_days}d): {verb} "
        f"{report['raw_deleted']} archived message(s) and {report['loops_deleted']} "
        f"resolved loop(s) carrying {report['events_deleted']} event(s)."
    )
    print(
        f"{PROG}: kept {report['retained_recent_activity']} terminal loop(s) with activity "
        f"inside the window, {report['retained_for_provenance']} attached to a loop that is "
        f"staying, {report['retained_projection_disagreed']} whose event log says they are "
        f"not terminal, and {report['retained_unreplayable']} that could not be replayed. "
        "No open loop is ever deleted by age."
    )
    return 0


def _run_stats(args) -> int:
    """Report row counts and approximate payload bytes per table, and the
    database's exact file size. Read-only: never deletes or ages out a row.

    Exists because `applied_messages` and `mrn_alias_events`/`mrn_aliases` are
    deliberately excluded from retention (see retention.py's module docstring)
    and therefore have no bound at all -- on a box this project does not
    administer. Nobody can act on a growth cost that has never been measured,
    and a site should see it coming before a full disk does the telling.
    Measured projections and the verdict on whether it matters at all live in
    BUILD_LOG.md; this is the tool that lets a site check its own numbers
    against them rather than trust ours.

    **Same two gates as purge, and for the same reason.** The encryption gate
    runs because this opens a file holding PHI-shaped columns to sum their
    byte lengths -- no value is ever printed, but the file is still read for
    it. The pack gate does not apply: a stats report matches nothing. The
    threshold gate does not apply: it computes no staleness. Requiring either
    would put an operator's ability to see their own disk usage behind a
    signing key that has nothing to do with it -- the same gate-theatre
    argument `_run_purge` makes for itself.

    **A database that does not exist is refused, not created.** Every mode but
    purge and this one creates its file, because a fresh install has to start
    somewhere. A typo'd `--db` here would otherwise build an empty database
    and report "0 rows in every table" -- readable as "nothing has grown yet"
    when the truth is "you are not looking at the file the listener writes
    to". That is `_run_purge`'s missing-database case, and the report this
    command produces is exactly the kind of number that gets pasted into a
    ticket without anyone checking the path first.
    """
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"))

    db_path = Path(args.db)
    if not db_path.is_file():
        raise StoreUnavailableError(
            f"No database at {db_path}, so there is nothing to report on. Point --db at "
            "the file the listener writes to."
        )
    store = LoopStore(db_path)
    report = store.stats()

    print(f"{PROG}: storage report for {db_path} (whole file: {report['file_bytes']:,} bytes)")
    for name, _columns in STATS_TABLES:
        info = report["tables"][name]
        flag = "  [NOT retention-bounded]" if info["unbounded"] else ""
        print(
            f"{PROG}:   {name:<18} {info['rows']:>12,} row(s)  "
            f"~{info['payload_bytes']:>14,} payload byte(s){flag}"
        )
    print(
        f"{PROG}: payload bytes are LENGTH() of stored columns -- a lower bound that "
        "excludes the SQLite record header, page overhead and every index on the table. "
        "The whole-file size above is exact. See retention.py for what stays unbounded and "
        "why, and BUILD_LOG.md for measured growth projections at several message volumes."
    )
    return 0


def _run_connectors(args: argparse.Namespace) -> int:
    """Preflight every configured connector.

    Deliberately prints the report to stdout and returns a code rather than raising: an
    operator setting up three sites wants all three verdicts, and the exit code is for the
    script that wrapped the command.

    Cross-checks connector ids against the peer registry when `--peers` names one. That check
    is the only caller of warn_on_peer_collisions, and it says so when it does *not* run --
    a silent skip would make the warning look like a clean bill of health when it is actually
    an absence of evidence, which is the same reasoning the README applies to the image tests
    that skip when no Docker daemon is reachable.
    """
    from .connect.connectors import load_connector_registry
    from .connect.preflight import format_report, preflight
    from .peers import load_peer_registry

    registry = load_connector_registry(args.connectors)

    if args.peers:
        registry.warn_on_peer_collisions(load_peer_registry(args.peers).peer_ids)
    else:
        print("peer id cross-check: skipped, no --peers given\n")

    unqueryable = [c.connector_id for c in registry.connectors if not c.is_queryable]
    if unqueryable:
        # Stated at configuration time rather than discovered by a query returning nothing.
        # These connectors can prove they are reachable and that our credentials work, and
        # cannot be asked about a patient at all.
        print(
            "preflight-only (no identifier_systems.mrn, cannot be queried for documents): "
            + ", ".join(unqueryable)
            + "\n"
        )

    reports = preflight(registry)
    print(format_report(reports))
    return 0 if all(r.ok for r in reports) else 1


def _run_worklist(stack: BootedStack, host: str, port: int) -> int:
    """Serve the coordinator worklist. Loopback by refusal, not by convention.

    The host is passed through rather than hardcoded, so `make_worklist_server`
    is the single place that decides what may be bound -- and so the refusal is
    reachable from the command line and therefore falsifiable. Hardcoding
    `127.0.0.1` here would make any test of "does it bind loopback" a test of
    the literal three lines above it.
    """
    server = _bound(
        lambda: make_worklist_server(stack.store, stack.registry, stack.pack,
                                     host=host, port=port),
        what=f"worklist on {host}:{port}",
    )
    try:
        logger.info("Coordinator worklist on http://%s:%d", *server.server_address[:2])
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting the worklist down")
    finally:
        server.server_close()
    return 0


def _bound(make, *, what: str):
    """Bind a server, turning a bind failure into a legible refusal.

    An occupied port otherwise arrives as `OSError: [WinError 10048]` or
    `[Errno 98]` with a traceback and no mention of which port. That is the most
    likely thing to go wrong on a second start, so it gets the same one-line
    treatment as a failed gate.

    `SystemExit` is caught alongside `OSError`, and that is not defensive
    padding. `werkzeug.serving.BaseWSGIServer.__init__` handles a failed bind by
    printing `e.strerror` and calling `sys.exit(1)` itself -- measured, not
    assumed. So an occupied worklist port would otherwise kill the process with
    exit 1 and a message that on Windows reads "An attempt was made to access a
    socket in a way forbidden by its access permissions", naming neither the
    port nor the fact that it is the worklist. A library reaching for the
    process's exit protocol from inside a constructor does not get to keep it.
    """
    try:
        return make()
    except OSError as exc:
        raise ReferralLoopError(f"Cannot start the {what}: {exc}") from exc
    except SystemExit as exc:
        raise ReferralLoopError(
            f"Cannot start the {what}: the address could not be bound "
            f"(the server library exited with {exc.code}); the port is most likely in use"
        ) from exc


# ---------------------------------------------------------------------- main


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        # "no network egress" was true until connect/ existed. Narrowed rather than dropped:
        # the property that remains is enforced by an allowlist and an AST closure test, and
        # --help is the more authoritative of the two places this claim lives.
        description="Deterministic HL7 v2 referral-loop tracker. On-premise, no model "
                    "calls, and egress only to endpoints named in the connector file.",
        epilog=(
            f"Required environment: {PUBKEY_ENV} (pack signing public key, hex), "
            "PHI_ENCRYPTION_VERIFIED=1 or OS-detected encryption at rest, "
            "REFERRAL_THRESHOLDS_ACCEPTED=1 once the site has reviewed "
            f"rules/pack.json staleness_hours. purge mode additionally requires "
            f"{RAW_DAYS_ENV} and {RESOLVED_DAYS_ENV}, which have no defaults."
        ),
    )
    parser.add_argument(
        "mode", choices=list(MODES),
        help="listen: MLLP server. filedrop: drain a watched directory once. "
             "worklist: coordinator queue on localhost. eval: replay the labeled "
             "corpus through --pack-dir and apply the release gate against "
             "--baseline-pack-dir. purge: enforce the site's retention policy, "
             "which it refuses to run without. stats: report row counts and "
             "approximate on-disk size per table, including the tables retention "
             "deliberately never touches. "
             "connectors: check every configured FHIR endpoint -- reachability and "
             "credentials are proven separately -- and exit nonzero if any failed.",
    )
    parser.add_argument("--db", default="data/referral_loops.db",
                        help="SQLite file on an encrypted volume (default: %(default)s)")
    parser.add_argument("--connectors", default="connectors.json",
                        help="connectors mode: JSON file of outbound FHIR endpoints "
                             "(default: %(default)s)")
    parser.add_argument("--pack-dir", default=str(DEFAULT_PACK_DIR),
                        help="directory holding pack.json and pack.sig (default: the shipped pack)")
    parser.add_argument("--drop-dir", default="data/dropbox",
                        help="filedrop mode: directory of *.hl7 files (default: %(default)s)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="listen mode: MLLP bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=2575,
                        help="listen mode: MLLP port (default: %(default)s)")
    parser.add_argument("--peers", default="",
                        help="listen mode: JSON peer registry mapping each interface "
                             "engine's client certificate (SHA-256 fingerprint) to an "
                             "identity and the authorities it holds. Also carries the TLS "
                             "certificate, key and client CA. Required unless "
                             "--allow-plaintext")
    parser.add_argument("--allow-plaintext", action="store_true",
                        help="listen mode: run WITHOUT mutual TLS. Loopback only, one "
                             "identity, and a warning on every start. PHI crosses this "
                             "port unauthenticated and unencrypted; this exists so a demo "
                             "and local development stay workable, not for a deployment. "
                             "Ignored when --peers is given, which states its own transport")
    parser.add_argument("--worklist-host", default="127.0.0.1",
                        help="worklist mode: bind address. Non-loopback is refused -- the "
                             "page has no authentication (default: %(default)s)")
    # 5055 here, but `make_worklist_server`'s own signature defaults to 5057 and
    # the worklist tests use 5057 throughout. The two have disagreed since both
    # were written, and this path wins in practice: _run_worklist passes
    # args.worklist_port through, so the library default is only ever reached by
    # an in-process caller that omits the argument. Recorded rather than
    # reconciled -- picking one is a code change, and the extraction that found
    # this makes none. See the matching note in worklist.py.
    parser.add_argument("--worklist-port", type=int, default=5055,
                        help="worklist mode: port (default: %(default)s)")
    parser.add_argument("--baseline-pack-dir", default="",
                        help="eval mode: the pack --pack-dir is measured against. Omitted, "
                             "the candidate is only checked against the absolute floor "
                             "(spec section 10.4 criterion 4) and no gate is applied")
    parser.add_argument("--synthetic-only", action="store_true",
                        help="eval mode: skip the cases reconstructed from this site's own "
                             "coordinator labels. The site corpus is the valuable half -- it "
                             "is real interface quirks from real traffic -- so this is for "
                             "reproducing the shipped baseline, not for a release decision")
    parser.add_argument("--dry-run", action="store_true",
                        help="purge mode: report what the configured periods reach and "
                             "delete nothing. The selection is identical to a real run")
    parser.add_argument("--no-reclaim", action="store_true",
                        help="purge mode: skip the VACUUM. Deleted pages are already zeroed, "
                             "but the file keeps its size until it is reclaimed")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="default: %(default)s")
    return parser


def _refuse(message: str, stream=None) -> int:
    """One line, exit 2. The operator-facing half of failing closed."""
    print(f"{PROG}: refusing to start: {message}", file=stream or sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Answered before the pack key is even looked for, deliberately. Neither purge nor stats
    # loads a pack, and an operator whose retention period is unset -- or who just wants to see
    # how big their database has gotten -- needs to hear that rather than a message about a
    # signing key. connectors joins them for the same reason and a stronger one: preflight
    # touches no database, no pack and no PHI, so not one of the three boot gates is relevant
    # to what it does. See _run_purge, _run_stats and _run_connectors for which gates each runs.
    if args.mode in ("purge", "stats", "connectors"):
        try:
            if args.mode == "purge":
                return _run_purge(args)
            if args.mode == "stats":
                return _run_stats(args)
            return _run_connectors(args)
        except (ReferralLoopError, RuntimeError) as exc:
            return _refuse(str(exc))

    public_key_hex = os.environ.get(PUBKEY_ENV, "")
    if not public_key_hex.strip():
        return _refuse(
            f"{PUBKEY_ENV} is not set. It must hold the hex-encoded Ed25519 public key "
            "for the rule pack; without it the pack signature cannot be verified and an "
            "altered pack could cause false matches."
        )

    try:
        stack = boot(args.db, args.pack_dir, public_key_hex)
    except (ReferralLoopError, RuntimeError) as exc:
        # RuntimeError is what encryption_check raises. Catching it broadly means
        # an unexpected RuntimeError inside a gate also refuses the boot, which is
        # the direction to fail in.
        return _refuse(str(exc))

    logger.info("Booted on pack %s in %s mode", stack.pack.version, args.mode)

    try:
        if args.mode == "listen":
            return _run_listen(stack, args, args.host, args.port)
        if args.mode == "filedrop":
            return _run_filedrop(stack, args.drop_dir)
        if args.mode == "eval":
            return _run_eval(stack, args, public_key_hex)
        return _run_worklist(stack, args.worklist_host, args.worklist_port)
    except ReferralLoopError as exc:
        return _refuse(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
