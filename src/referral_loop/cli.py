"""`referral-loop` -- the entry point that actually constructs the system.

Every module below this one has been exercised only from tests. This is where
they are wired together for a real site, so it is also the only place the four
boot gates can be enforced. All four fail closed, and each one is independent:
breaking any single gate refuses the boot even when the others are satisfied.

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

4. **A writable audit trail.** `audit.py` writes best-effort on purpose: an
   unwritable audit database must not stop a coordinator acknowledging a result,
   because a wedged safety worklist is worse than a missing compliance row. That
   trade is only defensible while the missing row is *loud*, and the loudest
   thing a running process can do about it is an ERROR line. `REFERRAL_AUDIT_DB`
   defaults to a path derived from the package's own location, which is right in
   a source checkout and wrong -- unwritable, or somewhere nobody looks -- from
   an installed one, so a `pip install` that forgets the variable produces a
   permanently empty compliance trail and runs otherwise perfectly. Gated on
   whether the resolved path can actually be written, never on whether the
   variable is set: a checkout must keep working, and the variable is not the
   question. `audit.verify_audit_db_writable` answers it by creating the
   database, which is the only answer that is not a guess.

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
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from . import audit
from . import eval as eval_harness
from .encryption_check import verify_encryption_at_rest
from .errors import (
    PackConceptMissingError,
    PackVerificationError,
    ReferralLoopError,
    StoreUnavailableError,
)
from .listener import FileDropSource, MessageHandler, operational_report
from .mllp_server import make_mllp_server
from .pack import RulePack, load_pack
from .peers import PeerRegistry, load_peer_registry
from .phi_files import create_private_directory
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

MODES = ("listen", "filedrop", "worklist", "eval", "purge", "stats", "connectors",
         "documents", "health", "rebuild")

# `eval` exit codes. Distinct from _refuse's 2, because "this pack must not ship"
# and "this process could not start" send an operator to different places.
EVAL_ALLOWED = 0
EVAL_BLOCKED = 1

# `documents` exit codes, and the distinction is the one `connect/documents.py`
# exists to make. **0 means the search completed**, including the case where it
# completed and found nothing -- a resolved patient with nothing filed is a real
# answer a coordinator may act on. **1 means the search did not complete**: the
# connector does not know this patient, matched several, failed the request, or
# handed back a `next` link the walk would not follow. Sharing an exit code
# between those two would hand a script the one confusion this module is built to
# prevent, which is "nobody documented anything" reported for "we never got an
# answer". 2 stays what it is everywhere else here: the command could not start.
DOCUMENTS_SEARCHED = 0
DOCUMENTS_UNANSWERED = 1


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
    """Run all four boot gates, then build the stack. Raises rather than degrading.

    Gate order is deliberate but not load-bearing for independence -- each gate
    is checked against a state where the others pass, so none of them is riding
    on another's failure. Encryption goes first because it is the one that must
    hold before anything touches disk.

    The audit gate's position *is* load-bearing, in both directions, and it is
    the only one of the four that is pinned on both sides. It goes **after**
    encryption at rest, because unlike the other three it creates a file, and the
    audit database is PHI-adjacent -- it names loops, actors, roles and times --
    so writing one onto an unattested volume is a smaller version of what gate 1
    refuses. It goes **before** the pack gate, because `load_pack` is itself an
    audited action: run last, it produced a `PACK_LOADED` row that failed, an
    ERROR line about a dropped audit write, and a `write_failures` of 1, all on
    the way to a boot that was going to be refused anyway. A gate that dirties
    the counter it exists to protect is answering after the question was asked.
    It still runs before `LoopStore`, so a refused boot has produced no clinical
    file.
    """
    # The resolved path, not the mode alone. The gate derives the volume it
    # inspects from this argument; without it the Windows branch guessed at a
    # drive and passed on the wrong one. See encryption_check.
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"), db_path)
    logger.info("Referral audit trail: %s", audit.verify_audit_db_writable())
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

    Owner-only, at 0700, and every intermediate level too -- audit finding M3.
    The database file carries its own 0600, so this is the second line rather
    than the first, but a directory nothing else can traverse is what covers
    the files SQLite creates for itself inside it. A directory that already
    exists is left exactly as it is; see `phi_files.create_private_directory`
    for why this process re-permissions only what it created.
    """
    path = Path(db_path)
    parent = path.parent
    if parent and not parent.exists():
        try:
            create_private_directory(parent)
        except OSError as exc:
            raise StoreUnavailableError(
                f"Cannot create the database directory {parent} ({exc}); "
                "create it and make it writable by this process"
            ) from exc
        logger.info("Created database directory %s, readable only by this account", parent)
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
        # The counters die with the process, so they are said out loud before it
        # ends. Not a substitute for `health` -- which is how an operator asks
        # while the thing is still running -- but the one moment at which nobody
        # is going to ask and the numbers are about to be gone. A listener that
        # spent a week answering AE to every third message should not take that
        # fact to the grave with it.
        logger.info("Listener stopping. %s", operational_report(stack.handler))
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
    # A drain is a whole process's lifetime, so this is that process's entire
    # operational history and there is no later moment to ask for it. The three
    # numbers above are about files; the report below is about what the messages
    # inside them did, which is a different question and the one that says
    # whether a replay actually landed.
    logger.info("Drain complete. %s", operational_report(stack.handler))
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

    **Nor does it leave the volume.** The harness writes one throwaway SQLite
    database per case, and those cases are reconstructed from the raw archive, so
    they hold verbatim HL7. Left to itself the harness puts them under
    `tempfile.gettempdir()`; this passes the site database's own directory
    instead, which the encryption gate has already attested and which
    `_prepared_db_path` has already made 0700. `store._reclaim` makes the same
    move for the same reason -- "a copy of the PHI file landing in /tmp would
    undo the gate".

    `--scratch-dir` overrides it for a site that wants the replay somewhere else
    -- a bigger disk, a second encrypted volume -- and carries the harness's
    "empty or absent" refusal with it. **One code path, and `--synthetic-only`
    does not get its own.** A synthetic corpus carries no PHI and genuinely does
    not need the attested volume, so the argument for branching is real; the
    argument against is that the branch would make the safe location conditional
    on a flag, and the flag would then be one edit away from selecting it for a
    site corpus too. A single location that is always correct cannot be made
    wrong by a later change to when it applies.
    """
    cases = eval_harness.synthetic_corpus()
    if not args.synthetic_only:
        cases = cases + eval_harness.corpus_from_site(stack.store)
    labels = stack.store.labels()

    print(f"{PROG}: corpus of {len(cases)} labeled case(s); "
          f"{sum(1 for c in cases if c.source == 'site')} reconstructed from site labels")

    scratch = dict(
        scratch_dir=Path(args.scratch_dir) if args.scratch_dir else None,
        scratch_parent=None if args.scratch_dir else Path(args.db).parent,
    )
    candidate = eval_harness.replay(cases, stack.pack, labels=labels, **scratch)
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
    baseline = eval_harness.replay(cases, baseline_pack, labels=labels, **scratch)
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
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"), args.db)

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
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"), args.db)

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


def _run_health(stack: BootedStack) -> int:
    """Print the operational counters and the audit trail's state. Changes nothing.

    The mode exists because nineteen counters were being maintained for nobody.
    `MessageHandler` declares one per distinct operational fact -- unknown
    message types, duplicate control ids, refused merges, deferred archive
    writes -- each with a comment arguing why it deserves to be told apart from
    its neighbours, and not one of them reached a surface an operator could
    read. `audit.write_failures()` was worse: the whole fail-open argument in
    `audit.py` rests on a dropped compliance row being loud, and the counter
    carrying that loudness had no consumer outside the test suite.

    **A separate process reports its own numbers, and the report says so.** These
    counters are in-memory and per-process, so running `health` in one terminal
    does not read the listener running in another; that would need a metrics
    exporter, which is a deployment decision this subsystem does not get to make
    on a site's behalf. What it is honestly good for is three things: the audit
    trail's path and dropped-write count, which are process-independent facts
    about a file; a `filedrop` or in-process run whose numbers *are* this
    process's; and telling an operator that a counter by that name exists at all,
    which is the difference between a number they can ask about and one they
    cannot. The listener says the same thing into its log on the way down.

    All four gates ran to get here, which is deliberate: a health report from a
    process that could not verify its own pack would be reporting on a system
    nobody should be running, and the refusal is the more useful answer.
    """
    print(f"{PROG}: {operational_report(stack.handler)}")
    print(f"{PROG}: pack {stack.pack.version} verified; all four boot gates passed.")
    print(f"{PROG}: counters above are this process's. Disk growth is `{PROG} stats`.")
    return 0


def _run_rebuild(args) -> int:
    """Rebuild the loops projection from the event log. The repair tool, reachable.

    `LoopStore.rebuild_projection` has existed since the projection did, with a
    docstring naming a real disaster: a restore that replays `loop_events` into a
    fresh file leaves `loops` empty, so `open_loops()` returns nothing while the
    events sit right there and every referral in the site vanishes from the
    worklist silently. That is the exact failure this product exists to prevent.
    It had no caller in `src/` -- tests only -- which made it either dead code or
    a capability an operator was assumed to have and could not reach. Both
    readings are worse than not having it, so it gets a command.

    **Same two gates as purge and stats, and for the same reasons.** The
    encryption gate runs because this reads an event log full of MRNs and result
    text and writes rows derived from it. The pack gate does not apply: a rebuild
    matches nothing, and putting a site's recovery from a restore behind a
    signing key would be the gate theatre `_run_purge` already refuses -- with a
    sharper edge, because the moment a site needs this is the moment after a
    disaster, which is not the moment to discover the pack key is on the machine
    that burned down. The threshold gate does not apply: it computes no
    staleness.

    **A database that does not exist is refused, not created**, exactly as purge
    and stats refuse one. A typo'd `--db` would otherwise create an empty file,
    rebuild the zero loops in it, and report "0 loop(s) rebuilt" -- which reads
    as "there was nothing to repair" when it means "you repaired the wrong file",
    and the operator running this has just been told their worklist is empty.

    **No dry run, and no confirmation.** Every other destructive-looking command
    here has one; this is not destructive. It reads `loop_events`, which is
    append-only and which it does not write to, and recomputes each `loops` row
    from it -- the same `_materialize` every ordinary transition already calls,
    just for every loop at once. There is no input that makes it lose anything,
    because the source of truth is untouched and the output is a pure function of
    it. Running it when it was not needed rewrites every row with the value it
    already had.

    Running it against a *live* listener is a different question and the answer
    is "it is safe, and it is still not what you want". Each loop is replayed and
    written inside one transaction, so a loop that changes mid-rebuild ends up
    with either the old row or the new one, and the next event for that loop
    re-materializes it correctly either way. Nothing is corrupted; the only cost
    is that a rebuild racing a busy feed proves less than one run against a quiet
    system, which is the situation an operator is in after a restore anyway.
    """
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"), args.db)

    db_path = Path(args.db)
    if not db_path.is_file():
        raise StoreUnavailableError(
            f"No database at {db_path}, so there is no event log to rebuild from. Point "
            "--db at the file the listener writes to. It is not created here: rebuilding "
            "a database this command just made would report a repaired projection over a "
            "file that has never held a message."
        )
    store = LoopStore(db_path)
    rebuilt = store.rebuild_projection()
    print(
        f"{PROG}: rebuilt {rebuilt} loop projection row(s) from the event log in "
        f"{db_path}. loop_events was read and not written; every row above is derived "
        "from it, so this is repeatable and changes nothing when nothing was wrong."
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


def _search_date(value: str, flag: str) -> datetime | None:
    """An ISO 8601 date or timestamp, or a refusal naming the flag. Never a guess.

    Every other reading of an unparseable window -- clamp it, default it, drop the bound --
    searches a period the operator did not ask for and then reports the result as though they
    had. That is the same false negative `connect/documents.py` refuses everywhere else: an
    answer about a window nobody chose is indistinguishable from an answer about theirs.
    """
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReferralLoopError(
            f"{flag} is not an ISO 8601 date or timestamp ({exc}). Give a date such as "
            "2026-01-01, or an instant such as 2026-01-01T14:00:00+00:00. It is not "
            "defaulted or corrected here: a search window this command guessed at would "
            "report on a period the operator never asked about."
        ) from exc


def _run_documents(args: argparse.Namespace) -> int:
    """Ask one connector what it has filed for one patient. Reads; never writes.

    This is `connect/documents.find_candidate_documents` reaching an operator. Until it
    existed the function had tests and no caller in `src/` at all, which reads to a reviewer
    as either dead code or an unfinished thought -- and the honest answer was neither. The
    two-hop search and its four outcomes are the part of this system with the most FHIR
    exposure and the least standards ambiguity, and a capability nobody can invoke is a
    capability nobody can check. `_run_rebuild` above was written out of the same argument.

    **Read-only, and structurally so.** No `Registry` is constructed here and no `LoopStore`
    is opened, so there is no object in scope through which a loop could be created, moved or
    acknowledged. That is deliberate and it is the mode's whole safety claim: a coordinator
    running a search to see what a specialist has filed must not thereby change what the
    worklist says. Attaching a found document to a loop is the next sub-project's decision --
    it needs an authority check, provenance and a transition -- and a search command that
    quietly did it would be making that decision on a site's behalf without one.

    **One gate, not four, and it is the encryption gate.** This pulls consult notes and
    diagnostic reports into the process, so it handles PHI even though it never writes any:
    an operator redirects this output into a file, and the file lands on the volume `--db`
    names. Gating on that path is the site attesting that this machine may hold PHI at rest
    at all. The other three do not apply, on exactly the reasoning `_run_purge` sets out --
    no pack is loaded and nothing is matched, so the signature gate would be gate theatre;
    no staleness is computed, so the threshold gate has nothing to accept; and the audit gate
    guards a trail this command does not write to.

    **The MRN travels on argv and that is the sharp edge of this command.** It is visible to
    `ps` and to Task Manager for the life of the process and it lands in shell history, which
    is a weaker position than anything else in this subsystem gives an identifier. It is
    accepted because the alternative -- a search command that cannot be told who to search
    for -- is not a command, and because this is an interactive diagnostic run by an operator
    on the site's own host rather than something a scheduler runs. What follows from it is
    that nothing this mode *prints* puts the identifier anywhere new: not the MRN, not the
    remote's Patient id, and not `DocumentSearch.query_urls`, which carries the MRN
    percent-encoded into the hop-1 URL and says so in its own docstring. The provenance is
    held and returned; stdout is not the surface it goes out on.
    """
    from .connect.connectors import load_connector_registry
    from .connect.documents import find_candidate_documents

    # Argument validation first, the same way `_run_purge` answers the retention policy before
    # anything touches disk: these checks read only argv, create nothing and fetch nothing, and
    # an operator who mistyped a date needs to hear that rather than a message about a volume.
    mrn = args.mrn.strip()
    if not mrn:
        raise ReferralLoopError(
            "documents mode needs --mrn: the patient identifier to resolve at the connector, "
            "in that connector's declared identifier_systems.mrn. There is no default, and an "
            "empty value is not a narrower search -- an identifier token with an empty value "
            "is a Patient search some servers answer with everyone they have."
        )
    since = _search_date(args.since, "--since")
    if since is None:
        raise ReferralLoopError(
            "documents mode needs --since: the start of the window to search, as a date "
            "(2026-01-01). There is no default because a default would be this command "
            "choosing how far back a referral stays interesting, which is the site's call."
        )
    until = _search_date(args.until, "--until")

    # Before the connector file is read and long before a token is signed: nothing has been
    # fetched yet, so a box that cannot attest encryption at rest has not yet been handed PHI.
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"), args.db)

    registry = load_connector_registry(args.connectors)
    # Raises ConnectorConfigError naming every configured id when this one is a typo, which is
    # the answer an operator wants -- `connectors` mode prints the same list.
    profile = registry.get(args.connector)
    if not profile.is_queryable:
        # `find_candidate_documents` raises ConnectorCannotResolvePatients for this too, and
        # would raise it here. Checked first anyway, because that refusal arrives after the
        # JWT has been built and the token exchanged: an operator's first evidence of a
        # misconfiguration should not be a credential handshake with a site they were never
        # able to query. `connectors` mode already prints which connectors are in this state.
        raise ReferralLoopError(
            f"{profile.connector_id} declares no identifier_systems.mrn, so it cannot be "
            "asked about one of our patients at all. It is preflight-only: reachability and "
            f"credentials can be proven with `{PROG} connectors`, and nothing more."
        )

    try:
        search = find_candidate_documents(registry, profile, mrn=mrn, since=since, until=until)
    except ReferralLoopError as exc:
        # Printed and returned rather than raised, the same choice `_run_connectors` makes and
        # for the same reason: this is an answer about a remote system, not a failed start, and
        # `_refuse`'s "refusing to start" would be the wrong sentence in front of it. Every one
        # of these messages is built by `connect/documents.py` to name the connector and never
        # the identifier -- see `_PatientResolutionRefused` -- so printing it is safe here.
        print(f"{PROG}: {exc}")
        return DOCUMENTS_UNANSWERED

    print(
        f"{PROG}: {profile.connector_id} answered for the window given: "
        f"{len(search.resources)} candidate(s) over {search.pages_walked} page(s), "
        f"{search.skipped_malformed} malformed resource(s) skipped."
    )
    for found in search.resources:
        # The remote's own resource id and the page it came from. Neither identifies a patient,
        # and both are what an operator needs to go look the document up at the source.
        print(f"{PROG}:   {found.resource_type} {found.resource.get('id', '?')} "
              f"(page {found.page})")
    if not search.resources:
        print(
            f"{PROG}: the patient resolved and nothing is filed in that window. This is the "
            "one empty answer this command will give: a connector that does not know the "
            "patient, matches several, or fails the search exits 1 instead."
        )
    print(
        f"{PROG}: read-only. No loop was created, moved or acknowledged, and nothing was "
        f"written to {args.db}. Attaching any of the above to a referral is a separate, "
        "audited action that this command deliberately cannot take."
    )
    print(
        f"{PROG}: the identifier searched for, the remote's patient id and the query URLs are "
        "held on the result and deliberately not printed -- query_urls carries the identifier "
        "percent-encoded, and stdout gets redirected into files and pasted into tickets."
    )
    return DOCUMENTS_SEARCHED


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
            "rules/pack.json staleness_hours, and a writable audit trail -- set "
            "REFERRAL_AUDIT_DB on any installed deployment; the default is derived "
            "from the package's own location and is only right in a source checkout. "
            f"purge mode additionally requires "
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
             "credentials are proven separately -- and exit nonzero if any failed. "
             "documents: ask one connector (--connector) what it has filed for one "
             "patient (--mrn) since a date (--since). Read-only: it opens no loop "
             "database and can move nothing. Exit 0 means the search completed, "
             "including completing with nothing found; exit 1 means it did not "
             "complete, which is the opposite fact. "
             "health: print this process's ingest counters and the audit trail's "
             "path and dropped-write count. rebuild: reconstruct the loops "
             "projection from the event log, for a restore that left the worklist "
             "empty. It reads the log and does not write to it.",
    )
    parser.add_argument("--db", default="data/referral_loops.db",
                        help="SQLite file on an encrypted volume (default: %(default)s)")
    parser.add_argument("--connectors", default="connectors.json",
                        help="connectors and documents modes: JSON file of outbound FHIR "
                             "endpoints (default: %(default)s)")
    parser.add_argument("--connector", default="",
                        help="documents mode: which connector_id from --connectors to "
                             "search. Required, and never inferred even when the file "
                             "holds exactly one -- a search is addressed to a named site")
    parser.add_argument("--mrn", default="",
                        help="documents mode: the patient identifier to resolve at that "
                             "connector, in its declared identifier_systems.mrn. Note that "
                             "this puts an identifier on the process command line, where "
                             "ps and shell history can see it; the mode is an interactive "
                             "operator diagnostic for that reason, not something to "
                             "schedule. Nothing it prints repeats the value")
    parser.add_argument("--since", default="",
                        help="documents mode: start of the window to search, as an ISO "
                             "8601 date (2026-01-01) or instant. Required and never "
                             "defaulted: how far back a referral stays interesting is a "
                             "clinical judgement, not this command's. Note the search "
                             "widens it to midnight of that day")
    parser.add_argument("--until", default="",
                        help="documents mode: optional end of the window, same format. "
                             "Omitted, the search is open-ended forwards")
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
    # 5055, and `make_worklist_server` now agrees. It defaulted to 5057 until
    # publication, so an in-process caller that omitted the argument bound a port
    # nothing else in the project used. One number now. Some worklist tests still
    # spell 5057 in URLs -- those exercise Host and Origin handling, where the
    # port is incidental and any value works.
    parser.add_argument("--worklist-port", type=int, default=5055,
                        help="worklist mode: port (default: %(default)s)")
    parser.add_argument("--baseline-pack-dir", default="",
                        help="eval mode: the pack --pack-dir is measured against. Omitted, "
                             "the candidate is only checked against the absolute floor "
                             "(spec section 10.4 criterion 4) and no gate is applied")
    parser.add_argument("--scratch-dir", default="",
                        help="eval mode: directory the replay's throwaway per-case "
                             "databases are created under. Must be empty or not exist -- "
                             "it is never the site's database directory. Defaults to the "
                             "parent of --db, which the encryption gate has already "
                             "attested; those databases are rebuilt from the raw archive "
                             "and hold real HL7, so the default keeps them on that volume "
                             "rather than in the OS temporary directory. Either way they "
                             "are removed when the run ends")
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
    # touches no database, no pack and no PHI, so not one of the four boot gates is relevant
    # to what it does. documents sits beside it: it does handle PHI and therefore takes the
    # encryption gate itself, but it loads no pack, computes no staleness and writes no audit
    # row, so the remaining three would be gate theatre in front of a read-only search -- and
    # a pack key it does not use must not stand between a coordinator and the question "has
    # the specialist filed anything". See _run_purge, _run_stats, _run_connectors and
    # _run_documents for which gates each one runs and why.
    if args.mode in ("purge", "stats", "connectors", "documents", "rebuild"):
        try:
            if args.mode == "purge":
                return _run_purge(args)
            if args.mode == "stats":
                return _run_stats(args)
            if args.mode == "rebuild":
                return _run_rebuild(args)
            if args.mode == "documents":
                return _run_documents(args)
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
        if args.mode == "health":
            return _run_health(stack)
        return _run_worklist(stack, args.worklist_host, args.worklist_port)
    except ReferralLoopError as exc:
        return _refuse(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
