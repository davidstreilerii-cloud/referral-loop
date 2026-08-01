"""Ingest: where the rest of this subsystem meets an interface engine.

**ACK ordering is the load-bearing property.** The raw message is persisted
durably *before* anything is acknowledged. Acknowledging and then failing during
parse means the engine considers the message delivered and it is gone -- a
silently lost clinical result in a system whose entire purpose is not losing
results. So:

  * `AA` only after a durable write.
  * `AE` when the store cannot write, so the engine queues and retries.
  * `AR` on malformed framing -- a message that will never become acceptable and
    must not be retried forever.

Three ordering decisions follow from that and are each defended where they are
implemented:

  1. **Identity is resolved once, here, before the registry or the matcher sees
     anything** (spec section 4). Resolving inside `open_loop` and again inside
     the matcher would be two call sites that eventually disagree about who a
     message is about, and the disagreement shows up as a loop on a worklist
     nobody reads. The raw archive keeps the message verbatim; everything
     downstream sees the surviving identifier.
  2. **Field placement comes from the signed pack**, never from an index spelled
     out here. Which field a site's RIS writes an accession into is exactly the
     thing that varies between hospitals (spec section 5), so this module reads
     concepts through `matcher.result_key_from_message` and owns no field map of
     its own.
  3. **MSH-7 travels with every message-driven transition.** A listener that did
     not pass it would leave the registry's staleness watermark with nothing to
     compare and let a clinically older message silently regress a loop. Because
     it now always travels, the registry no longer fails open on an unreadable
     one for a destructive transition -- a blank `MSH-7` used to switch the guard
     off outright (see `Registry._refuse_if_stale`).

**Content-key idempotency.** `MSH-10` dedup alone is insufficient, and the
design manufactures the gap itself: section 6 requires `AE` so the engine queues
and retries, and many engines stamp a fresh control id on retry. The same result
then returns with a new `MSH-10`, passes the duplicate check, and produces a
second `resulted` transition or a second orphan -- backpressure turned into a
source of double-counting. Every message therefore also carries a content key, a
hash over its identifying tuple, and a message whose key is already on file is a
no-op, logged, and counted *separately* from an `MSH-10` duplicate: ordinary
engine chatter and a retry configuration are different operational facts.

Two properties that key must have, both asserted in the tests:

  * It is computed from **resolved** values, or the same result under a retired
    and a surviving MRN hashes differently and dedup fails exactly when a merge
    has happened.
  * A **genuine amendment is not swallowed**. `OBX-11 = C` with a different
    `OBX` value produces a different key and correctly reopens under safety rule
    2. Dedup that ate corrections would be far worse than the double-counting it
    fixes.

Only the digest is ever stored. The `OBX` values that go into it are clinical
content and never reach the database, the logs, or an audit row.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

from .clock import MAX_CLOCK_SKEW, is_future_dated
from .errors import (
    CircularMergeError,
    FramingError,
    MrnRetiredError,
    ReferralLoopError,
    StaleMessageError,
    StoreUnavailableError,
)
from .events import Loop, ParsedMessage
from .matcher import (
    MATCHABLE_STATES,
    ResultKey,
    concept_value,
    field_value,
    hl7_datetime,
    match_result,
    result_key_from_message,
)
# `ack_code` is re-exported: it moved down to `mllp` so the stream reader can
# branch on an ACK without importing this module, and the ingest API this
# subsystem documents is still `listener`.
from .mllp import VT, ack_code, build_ack, deframe  # noqa: F401
# Re-exported: the ingest API this subsystem documents is `listener`, and the
# socket layer lives in its own module because stream reassembly fails in ways
# message handling cannot recover from (see mllp_server). Importers get one
# name; reviewers get two files.
from .mllp_server import make_mllp_server  # noqa: F401
from .pack import RulePack
from .parse_hl7 import (
    MRG_PRIOR_PATIENT_ID,
    MSH_DATETIME,
    OBX_OBSERVATION_IDENTIFIER,
    OBX_OBSERVATION_VALUE,
    OBX_RESULT_STATUS,
    ORC_ORDER_CONTROL,
    msh_segment_count,
    parse_hl7_text,
    peek_control_id,
    structural_fault,
)
from .registry import CORRECTED, FINAL, PRELIMINARY, Registry
from .store import LoopStore

logger = logging.getLogger(__name__)

ORDER_TYPES = ("REF^I12", "ORM^O01", "OMG^O19")
RESULT_TYPE = "ORU^R01"
SCHEDULE_TYPE = "SIU^S12"
CANCEL_TYPE = "SIU^S15"
MERGE_TYPE = "ADT^A40"

# The tiers whose evidence is an exact order identifier. Scheduling and
# cancellation are only ever applied to a loop named this strongly, or to the
# single unambiguous open loop -- never on a tier-3/4 heuristic. See
# _target_loop.
_EXACT_TIERS = (1, 2)

# Concepts read through the pack's field map. `prior_patient_id` is optional in
# exactly the way `observation_datetime` is in the matcher: spec section 5's
# field map names six concepts and not this one, so a pack that carries it wins
# and a pack that does not falls back to the standard MRG-1 placement, without
# inventing a required concept load_pack does not validate.
_CONCEPT_MRN = "mrn"
_CONCEPT_PRIOR_MRN = "prior_patient_id"
_DEFAULT_PRIOR_MRN_REF = f"MRG-{MRG_PRIOR_PATIENT_ID}.1"

_MSH_DATETIME_REF = f"MSH-{MSH_DATETIME}"
_ORDER_CONTROL_REF = f"ORC-{ORC_ORDER_CONTROL}"

# Version tag inside the content key. A change to what the key hashes over must
# not silently make every message in flight look new *or* look like a duplicate
# of something it is not; bumping this makes the discontinuity explicit and
# dated in the changelog rather than inferred from a spike in the counters.
_CONTENT_KEY_VERSION = "rl-content-v1"

# Archive key for bytes that never became a message. Content-addressed so a
# sender retransmitting the same garbage does not fill the archive with rows,
# and so the same bytes are recognisably the same incident.
_MALFORMED_PREFIX = "MALFORMED-"
_BASE64_PREFIX = "BASE64:"
_TRUNCATED_MARKER = "TRUNCATED"

# The most of a malformed frame that is archived. Content-addressing dedups
# *identical* retransmits only, so a sender that varies one byte per frame gets
# a new row every time, and at the frame cap each row costs a SHA-256 over
# 16 MiB, a 16 MiB decode and a 16 MiB INSERT committed under
# `PRAGMA synchronous = FULL` -- one fsync apiece. That is the archive
# amplifying the attack it exists to record.
#
# 64 KiB because the archive's purpose is showing a sender exactly what
# arrived, and every conformant HL7 v2 message this listener will see fits
# inside it several times over: mllp_server's 16 MiB frame cap is sized for an
# ORU carrying an embedded report, not for a header a sender got wrong. Past
# the cap the row still carries the full SHA-256 and the original length, so it
# identifies the exact bytes; what is dropped is the tail of a frame nobody can
# act on. `reject_malformed`'s "lossless" promise is now bounded, and says so.
_MALFORMED_ARCHIVE_BYTES = 64 * 1024

# Free space below which malformed frames stop being archived at all. A full
# PHI volume makes every `record_raw` raise `StoreUnavailableError`, so the
# listener answers AE and the engine queues and retries the *live clinical
# feed* indefinitely -- the outage is caused by whatever filled the disk, and
# attacker-controlled bytes must not be what does. When the evidence of a bad
# sender and the next real result compete for the last of the volume, the
# result wins. `retention.py` is a scheduled purge with no default period, so
# it is not a quota and bounds none of this.
_ARCHIVE_DISK_FLOOR_BYTES = 64 * 1024 * 1024

# Bounded, because a resolution that keeps moving is a merge storm a human needs
# to see rather than something to spin on. See _open_loop_retrying.
_MRN_RESOLUTION_ATTEMPTS = 3


def _prior_mrn(message: ParsedMessage, pack: RulePack) -> str:
    """MRG-1: the identifier an ADT^A40 retires.

    Deliberately *not* resolved through the alias table. It is the identifier
    being retired, so resolving it would turn "A retires into B" into "B retires
    into B" the moment the merge is already on file -- a self-merge the registry
    logs and discards, which would make a redelivered A40 a silent no-op instead
    of the idempotent one it is. store._apply_alias resolves both endpoints
    itself, where the compression can see them.
    """
    if _CONCEPT_PRIOR_MRN in pack.field_map:
        return concept_value(message, pack, _CONCEPT_PRIOR_MRN)
    return field_value(message, _DEFAULT_PRIOR_MRN_REF)


def _obx_tuples(message: ParsedMessage) -> list[list[str]]:
    """(identifier, value, status) for every OBX, in order.

    Every OBX, not the first: two results differing only in their second
    observation are two different reports, and a key built from the first alone
    would swallow the second. This is the one place the listener reads a field
    by number rather than through the field map, and deliberately so -- OBX-3,
    OBX-5 and OBX-11 are fixed by the HL7 standard rather than by how a site's
    interface was built, and the field map exists for the placements that vary.
    """
    return [
        [
            segment[OBX_OBSERVATION_IDENTIFIER],
            segment[OBX_OBSERVATION_VALUE],
            segment[OBX_RESULT_STATUS],
        ]
        for segment in message.segments.get("OBX", [])
    ]


def content_key(message: ParsedMessage, pack: RulePack, *, mrn: str) -> str | None:
    """A hash over the message's identifying tuple, or None when it has none.

    `mrn` must be the **resolved** surviving identifier. Hashing what the
    message said instead would make the same result under a retired and a
    surviving MRN two different keys, so dedup would fail precisely when a merge
    has happened -- the case it exists for.

    Beyond the tuple spec section 6 names (filler, placer, MRN, the OBX set),
    three fields are included because leaving them out creates collisions
    between messages that are not duplicates, and a false duplicate is a
    silently discarded message:

      * **message type** -- an `SIU^S12` and an `ADT^A40` for a patient with no
        order numbers otherwise hash identically.
      * **the retired MRN** -- two A40s merging different patients into the same
        survivor otherwise collide, and the second one's loops stay stranded.
      * **ORC-1 order control** -- a new order and a cancellation carry the same
        identifiers. v1 does not act on ORC-1, so this only ever *narrows* what
        counts as a duplicate, which is the safe direction to be wrong in.

    Returns None when nothing identifying is present. An empty tuple would hash
    to one value shared by every such message, making all but the first a
    duplicate -- dedup silently becoming a drop.

    JSON rather than a delimiter join: a separator character appearing inside a
    field makes ("A|B", "C") and ("A", "B|C") the same key, and HL7 escape
    sequences put arbitrary bytes in fields.
    """
    placer = concept_value(message, pack, "placer_order_number")
    filler = concept_value(message, pack, "filler_order_number")
    prior = _prior_mrn(message, pack)
    obx = _obx_tuples(message)

    if not any((placer, filler, mrn, prior)) and not obx:
        return None

    payload = json.dumps(
        [
            _CONTENT_KEY_VERSION,
            message.message_type,
            field_value(message, _ORDER_CONTROL_REF),
            placer,
            filler,
            mrn,
            prior,
            obx,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MessageHandler:
    """Parses and applies one message. Returns the ACK string to send.

    One instance is shared by every connection a `ThreadingTCPServer` accepts,
    so `_lock` covers check-then-write across the two dedup paths and the
    transition they guard. Without it, two connections delivering the same
    result both miss the content-key check, both apply, and the mechanism meant
    to stop double-counting is defeated by the concurrency it was added for. The
    scope is one process, exactly like `Registry._lock`; two processes on one
    database file are serialized by the unique index on `applied_messages`
    rather than by this, which stops the second row but not the second
    transition -- a deployment constraint, not something this module can
    express.
    """

    def __init__(self, store: LoopStore, registry: Registry, pack: RulePack):
        self.store = store
        self.registry = registry
        self.pack = pack
        self._lock = threading.RLock()
        # An attribute rather than the constant alone, so a deployment on a
        # small volume can lower it and a test can raise it above any real disk.
        self.archive_disk_floor_bytes = _ARCHIVE_DISK_FLOOR_BYTES

        # Counters, not metrics plumbing. Each one is a distinct operational
        # fact somebody would act on differently.
        self.unknown_type_count = 0
        self.duplicate_control_id_count = 0
        self.duplicate_content_key_count = 0
        self.store_failure_count = 0
        self.parse_failure_count = 0
        self.apply_failure_count = 0
        self.stale_message_count = 0
        self.circular_merge_count = 0
        self.framing_error_count = 0
        self.matched_count = 0
        self.orphan_count = 0
        self.untargeted_count = 0
        self.unreadable_status_count = 0
        self.mrn_reresolution_count = 0
        self.mrn_retired_count = 0
        self.suspect_truncation_count = 0
        # Results that matched an exact order number and named no patient, so
        # the matcher declined them into the queue rather than attaching them
        # (matcher._unattributable). A subset of orphan_count and worth its own
        # number: every one of these is a result that used to auto-attach at
        # full confidence, and a rising count means either a sender has started
        # omitting PID segments or somebody is guessing accession numbers.
        self.unattributable_result_count = 0
        # A clock, not a message, is what went wrong here: an OBR-7 dated beyond
        # MAX_CLOCK_SKEW, declined so the loop ages from ingest rather than never
        # (see _ordered_at). Its MSH-7 counterpart -- a stamp dropped rather than
        # allowed to poison a watermark -- is Registry.future_dated_message_count,
        # because that decision belongs to the state machine and the CLI reaches
        # it without passing through here.
        self.future_dated_order_count = 0

    # --------------------------------------------------------------- entry point

    def handle(self, text: str) -> str:
        """Persist, then parse, then apply. Never the other way round."""
        control_id = peek_control_id(text)

        # Before the archive, because none of this is a message we could act
        # on. An MSH declaring a non-standard encoding set means every "^"
        # split we would then perform reads the wrong characters, and a frame
        # over the segment cap is one we refuse to hold in memory rather than
        # parse partially.
        #
        # First, and specifically before the MSH count: over the cap,
        # parse_hl7 stops scanning, so a message whose only MSH sits past the
        # cap would otherwise be reported to an operator as "found 0 MSH
        # segments" -- true of the truncated view, and a wrong lead for
        # somebody debugging the sender that emitted it.
        fault = structural_fault(text)
        if fault:
            return self.reject_malformed(text.encode("utf-8", errors="replace"), fault)

        # Zero MSH segments is not HL7, and more than one is two messages
        # inside a single frame. Either way the control id we would key the
        # archive on is not the one the sender used for what we would then
        # process.
        count = msh_segment_count(text)
        if count != 1:
            return self.reject_malformed(
                text.encode("utf-8", errors="replace"),
                f"expected exactly one MSH segment, found {count}",
            )

        # Also before the archive, and for the same reason. `record_raw`
        # refuses an empty MSH-10 -- idempotency cannot be promised without a
        # key -- and that refusal used to arrive as a StoreUnavailableError, so
        # this answered AE. AE means "queue and retry", and an empty MSH-10 is
        # a permanent property of these bytes: they will never become
        # acceptable. An engine retries a queued AE at the head of its outbound
        # queue, so one such message stops the entire clinical feed behind it,
        # forever, and nothing is archived because the insert never happened --
        # a whole-interface outage from 60 bytes, reachable by accident from a
        # misconfigured sender. AR, and the bytes are kept under the
        # content-addressed malformed key so the evidence survives the refusal.
        if not control_id:
            return self.reject_malformed(
                text.encode("utf-8", errors="replace"),
                "MSH-10 is empty, so this message cannot be keyed and no promise can be "
                "made about processing it exactly once",
            )

        # 1. Durable write. Everything after this point may fail without losing
        #    the message: it is on disk and replayable.
        try:
            is_new_raw = self.store.record_raw(control_id, text)
        except StoreUnavailableError as exc:
            self.store_failure_count += 1
            logger.error(
                "Durable write failed for %r (%s); answering AE so the engine queues",
                control_id, exc,
            )
            return build_ack(control_id, "AE")

        if not is_new_raw:
            logger.info("Control id %r was already archived; re-checking whether it applied",
                        control_id)

        # 2. Parse and apply, under the lock that makes the dedup checks mean
        #    something when two connections race.
        try:
            with self._lock:
                return self._process(control_id, text)
        except StoreUnavailableError as exc:
            # The archive holds the message but the transition did not land.
            # AE, and because applied_messages (not raw_messages) is the dedup
            # key, the redelivery is re-applied rather than no-oped.
            self.store_failure_count += 1
            logger.error(
                "Store failed while applying %r (%s); answering AE, message archived for retry",
                control_id, exc,
            )
            return build_ack(control_id, "AE")

    def _process(self, control_id: str, text: str) -> str:
        if self.store.control_id_applied(control_id):
            self.duplicate_control_id_count += 1
            logger.info("Duplicate MSH-10 %r: no-op", control_id)
            return build_ack(control_id, "AA")

        try:
            message = parse_hl7_text(text)
        except Exception:
            # parse_hl7_text promises never to raise on content. Caught anyway:
            # if that promise ever breaks, the failure must not escape past the
            # archive and take the connection with it.
            self.parse_failure_count += 1
            logger.exception("Parse failed for %r; raw archived for replay", control_id)
            return build_ack(control_id, "AA")

        if not message.is_known_type:
            # Failure matrix: ignore, count, never error. Marked applied with no
            # content key -- an unrecognised message must not own a key that a
            # message we do understand would then collide with.
            self.unknown_type_count += 1
            logger.info("Unknown message type %r: counted, ignored", message.message_type)
            self.store.record_applied(control_id, None, message.message_type)
            return build_ack(control_id, "AA")

        if message.flags_for_review:
            logger.warning(
                "Message %r carried %d unparseable segment(s) %s; kept and flagged",
                control_id, len(message.flags_for_review), message.flags_for_review,
            )

        # Identity, resolved exactly once, here (spec section 4).
        submitted_mrn = concept_value(message, self.pack, _CONCEPT_MRN)
        mrn = self.store.resolve_mrn(submitted_mrn)
        if mrn != submitted_mrn:
            logger.info(
                "Message %r carries a retired identifier; resolved to the surviving one",
                control_id,
            )

        key = content_key(message, self.pack, mrn=mrn)
        if key is not None:
            owner = self.store.content_key_owner(key)
            if owner is not None:
                self.duplicate_content_key_count += 1
                logger.warning(
                    "Content-key duplicate: %r repeats content already applied by %r under a "
                    "different MSH-10. No second transition. Repeated occurrences indicate a "
                    "retry configuration re-stamping control ids, not ordinary chatter.",
                    control_id, owner,
                )
                self.store.record_applied(control_id, None, message.message_type)
                return build_ack(control_id, "AA")

        try:
            self._apply(message, mrn=mrn, submitted_mrn=submitted_mrn)
        except StoreUnavailableError:
            raise                      # handle() answers AE
        except MrnRetiredError as exc:
            # Spec test 7 / failure matrix: refuse the write, the engine
            # retries, and the next resolution is current. AE, never AA -- this
            # message has produced no transition and answering AA would drop a
            # referral loop on the floor at the exact moment a merge made it
            # invisible to the surviving patient. Safe to ask for a redelivery
            # because dedup is keyed on `applied_messages`, so a retry under the
            # same MSH-10 is re-applied rather than no-oped, and a retry under a
            # fresh one finds no content key because none was recorded.
            self.mrn_retired_count += 1
            logger.error(
                "Refusing to open a loop for %r on an identifier retired since ingest (%s); "
                "answering AE so the engine redelivers", control_id, exc,
            )
            return build_ack(control_id, "AE")
        except StaleMessageError as exc:
            # Refused, not dropped: the raw is archived and this is routed for
            # human review. Not marked applied, so a redelivery re-evaluates
            # against a watermark that may since have moved -- re-refusing costs
            # a log line and swallowing it could cost a result.
            self.stale_message_count += 1
            logger.warning("Refused a clinically older message %r: %s", control_id, exc)
            return build_ack(control_id, "AA")
        except CircularMergeError as exc:
            self.circular_merge_count += 1
            logger.error("Refused ADT^A40 %r: %s", control_id, exc)
            return build_ack(control_id, "AA")
        except ReferralLoopError as exc:
            self.apply_failure_count += 1
            logger.error("Could not apply %r: %s. Raw archived and flagged.", control_id, exc)
            return build_ack(control_id, "AA")
        except Exception:
            self.apply_failure_count += 1
            logger.exception("Unexpected failure applying %r; raw archived for replay", control_id)
            return build_ack(control_id, "AA")

        self.store.record_applied(control_id, key, message.message_type)
        return build_ack(control_id, "AA")

    def flag_possible_truncation(self, control_id: str) -> None:
        """Retroactively flag a message that may have been half of one.

        Called by the stream reader when the bytes following an accepted frame
        turn out not to begin a new frame. A body carrying `CR FS CR` produces a
        truncated half that is indistinguishable from a whole message at the
        moment it completes -- it ends with `CR`, it parses, and it has already
        been answered `AA`. The residual hole cannot be closed in the reader
        (see mllp_server), so the requirement here is that it not be *silent*:
        the control id is named, the raw is in the archive verbatim, and a human
        can compare the two.
        """
        with self._lock:
            # Called straight from a socket thread, like framing_error_count.
            # `+=` on an int attribute is a load, an add and a store, so
            # concurrent connections lose increments -- and this is one of the
            # two numbers that would tell an operator an attack is under way,
            # under exactly the concurrency an attack produces.
            self.suspect_truncation_count += 1
        logger.error(
            "Message %r was acknowledged and applied, and the bytes that followed it did not "
            "begin a new frame. It may have been the first half of a message split by an "
            "embedded FS CR. Review the archived raw for this control id; any loop or result "
            "it produced was derived from a possibly incomplete message.",
            control_id,
        )

    # ---------------------------------------------------------------- malformed

    def _archivable(self, raw: bytes, digest: str) -> str:
        """The archive's view of a malformed frame: bounded, and honest about it.

        Within `_MALFORMED_ARCHIVE_BYTES` this is exactly what arrived. Beyond
        it the prefix is kept and the row records the full digest and the
        original length, so the bytes stay identifiable even though they are no
        longer all present. Bytes that are not UTF-8 are base64-encoded rather
        than replaced, because the value of archiving a malformed frame is
        showing the sender what it actually sent -- a frame cut at the cap in
        the middle of a multi-byte character takes the base64 branch, which is
        lossless for what it holds rather than approximate.
        """
        head = raw[:_MALFORMED_ARCHIVE_BYTES]
        try:
            payload = head.decode("utf-8")
        except UnicodeDecodeError:
            payload = _BASE64_PREFIX + base64.b64encode(head).decode("ascii")
        if len(raw) <= _MALFORMED_ARCHIVE_BYTES:
            return payload
        return (
            f"{payload}\r{_TRUNCATED_MARKER}: {len(raw)} bytes received, "
            f"{_MALFORMED_ARCHIVE_BYTES} archived, sha256={digest}\r"
        )

    def _archive_has_room(self) -> bool:
        """Whether there is enough of the volume left to spend on evidence.

        See `_ARCHIVE_DISK_FLOOR_BYTES`. A failure to *measure* free space is
        not evidence of a full disk, so it archives and says so -- refusing on
        an unreadable measurement would discard the only copy of what a sender
        emitted on the strength of a guess, which is the opposite of what this
        path is for.
        """
        try:
            free = shutil.disk_usage(Path(self.store.db_path).parent).free
        except OSError as exc:
            logger.error("Could not measure free space (%s); archiving anyway", exc)
            return True
        if free >= self.archive_disk_floor_bytes:
            return True
        logger.error(
            "Only %d byte(s) free, below the %d-byte archive floor: not archiving this "
            "malformed frame. A full volume makes every durable write fail, which answers "
            "AE to the live feed and asks the engine to retry all of it forever; the "
            "remaining space belongs to clinical messages.",
            free, self.archive_disk_floor_bytes,
        )
        return False

    def reject_malformed(self, raw: bytes, reason: str) -> str:
        """Failure matrix: `AR`, archive raw, alert.

        `AR` rather than `AE` because these bytes will never become acceptable
        and an engine told to queue would redeliver them forever, wedging the
        interface behind a message that cannot be processed.

        The archive is lossless up to `_MALFORMED_ARCHIVE_BYTES` and identifies
        the bytes exactly beyond it; see `_archivable`. It is skipped entirely
        when the volume is nearly full; see `_archive_has_room`. Both bound what
        an unauthenticated peer can make this path write, and neither changes
        the answer: it is `AR` either way.
        """
        with self._lock:
            # Non-atomic `+=` called straight from a socket thread; see
            # flag_possible_truncation. This is the number that says a sender
            # has started emitting frames we cannot trust, so it must not
            # undercount when several connections are doing it at once.
            self.framing_error_count += 1
        digest = hashlib.sha256(raw).hexdigest()
        control_id = _MALFORMED_PREFIX + digest[:32]
        payload = self._archivable(raw, digest)
        archived = False
        if self._archive_has_room():
            try:
                self.store.record_raw(control_id, payload)
                archived = True
            except StoreUnavailableError as exc:
                # Still AR. The alternative is AE, which asks for the redelivery
                # of bytes that cannot be processed either way.
                logger.error("Could not archive a malformed frame (%s): %s", control_id, exc)
        logger.error(
            # "archived as" is a claim about a write that has two ways of not
            # happening, and an operator who goes looking for a row this line
            # promised is being sent to the archive by its own alert.
            "Malformed framing (%s); %s and answering AR. Alert: a sender is "
            "emitting frames this listener cannot trust.",
            reason,
            f"archived as {control_id}" if archived else f"NOT archived ({control_id})",
        )
        # Echo whatever control id is legible, so the engine can correlate the
        # rejection with what it sent. build_ack sanitizes it: the value is
        # attacker-controlled and this one came out of a frame we have already
        # declared malformed, which is the least trustworthy input in the
        # system. Illegible (base64-archived) bytes yield "", and build_ack
        # turns that into UNKNOWN -- an ACK is always well-formed, because an
        # engine that cannot parse our reply just retries forever.
        return build_ack(peek_control_id(payload), "AR")

    # ----------------------------------------------------------------- dispatch

    def _apply(self, message: ParsedMessage, *, mrn: str, submitted_mrn: str) -> None:
        handlers = {
            **{t: self._apply_order for t in ORDER_TYPES},
            RESULT_TYPE: self._apply_result,
            SCHEDULE_TYPE: self._apply_schedule,
            CANCEL_TYPE: self._apply_cancel,
            MERGE_TYPE: self._apply_merge,
        }
        handler = handlers.get(message.message_type)
        if handler is None:
            # is_known_type and this table disagreeing would silently drop a
            # documented message type, so it is a counted anomaly, not a pass.
            self.unknown_type_count += 1
            logger.warning(
                "Message type %r is known to the parser but has no handler; ignored",
                message.message_type,
            )
            return
        handler(message, mrn=mrn, submitted_mrn=submitted_mrn)

    def _message_at(self, message: ParsedMessage) -> datetime | None:
        """MSH-7, or None when it cannot be read.

        None means "unknown" -- empty, not a timestamp, or a year no clock could
        produce (`clock.is_readable_clock`). It is not a fail-open: the registry
        refuses an unknown clock on a destructive transition once the loop
        carries a watermark, because a blank MSH-7 was enough to disable the
        ordering guard entirely. Passing it at all is the point: without it the
        guard never compares anything.
        """
        return hl7_datetime(field_value(message, _MSH_DATETIME_REF))

    def _apply_order(self, message: ParsedMessage, *, mrn: str, submitted_mrn: str) -> None:
        key = result_key_from_message(message, self.pack, mrn=mrn)
        self._open_loop_retrying(
            mrn=mrn,
            submitted_mrn=submitted_mrn,
            control_id=message.control_id,
            placer_order_number=key.placer,
            filler_order_number=key.filler,
            service_code=key.service_code,
            modality=key.modality,
            # Populated, and its absence is not cosmetic: the pack's second
            # tie-breaker is `same_ordering_provider`, so a listener that never
            # wrote this field would make that rule a permanent no-op and every
            # ambiguity it should have resolved would route to a coordinator.
            ordering_provider=key.ordering_provider,
            ordered_at=self._ordered_at(key.observed_at, message.control_id),
            message_at=self._message_at(message),
        )

    def _ordered_at(self, observed_at: datetime | None, control_id: str) -> datetime | None:
        """OBR-7, unless it is dated further ahead than a clock can be wrong.

        `staleness.age()` clamps a future `ordered_at` to zero -- correctly; the
        failure matrix says accept the message, clamp for staleness math, and
        flag elsewhere. Nothing flagged. So an `OBR-7` of `20991231120000` opened
        a loop reporting `0.0 h` that could never age out, never turn red and
        never rise: `is_stale` permanently False, `staleness_ratio` permanently
        0.0, which `worklist._sort_key` maps to `-0.0` -- dead last, forever, with
        no STALE badge, no counter and no log line. On a queue of a few hundred
        that loop is functionally invisible. This is the flag, not a second clamp.

        Declining the value leaves `open_loop` to default `ordered_at` to the
        moment of ingest, which is what it already does for an order carrying no
        `OBR-7` at all: the loop then ages from when we first heard of it and
        reaches the worklist's stale band on the ordinary schedule. Honest, and
        the opposite of invisible.
        """
        if observed_at is None or not is_future_dated(observed_at):
            return observed_at
        self.future_dated_order_count += 1
        logger.warning(
            "Order %r carries an observation time beyond the clock-skew window (%s); opening "
            "the loop without it, so it ages from ingest instead of never (%d so far). Either "
            "a sender's clock is wrong or a message is forged; both need a human.",
            control_id, MAX_CLOCK_SKEW, self.future_dated_order_count,
        )
        return None

    def _open_loop_retrying(self, *, mrn: str, **kwargs) -> str:
        """Open a loop, re-resolving if the MRN retired underneath us.

        `open_loop` refuses an MRN that stopped being current between ingest
        resolution and the write (spec section 4, test 7). The refusal is
        correct and the message is retryable, and the engine *is* told to
        redeliver when this exhausts -- but the window is microseconds and a
        local re-resolve costs a database read, where a round trip through the
        interface engine's retry queue costs seconds to minutes of a referral
        loop not existing. So: recover here if we can, hand it back if we
        cannot. `MrnRetiredError` propagates in that case and the caller answers
        AE.

        Only `MrnRetiredError` is retried. Every other refusal `open_loop`
        raises -- an empty MRN, a loop id already in use -- will fail identically
        on the next attempt, and retrying it would turn a clear error into three
        of them.
        """
        for attempt in range(_MRN_RESOLUTION_ATTEMPTS):
            try:
                return self.registry.open_loop(mrn=mrn, **kwargs)
            except MrnRetiredError:
                current = self.store.resolve_mrn(mrn)
                if current == mrn or attempt == _MRN_RESOLUTION_ATTEMPTS - 1:
                    # Either the alias table disagrees with the guard -- a race
                    # we cannot settle locally -- or we have retried enough.
                    raise
                self.mrn_reresolution_count += 1
                logger.warning(
                    "MRN retired between ingest and loop creation; re-resolving (attempt %d)",
                    attempt + 2,
                )
                mrn = current
        raise AssertionError("unreachable")  # pragma: no cover

    def _result_status(self, message: ParsedMessage) -> str:
        """The weakest OBX-11 in the report, never a default of `F`.

        Safety rule 1 is an allowlist: only an explicitly final or corrected
        read may be acknowledged, so an absent, unrecognised or future-dialect
        status has to fail *safe*. Defaulting it to `F` -- as an earlier draft of
        this module did -- makes an unreadable read acknowledgeable, which is
        the malpractice scenario the rule exists to prevent.

        A correction wins outright, because safety rule 2 must fire on it. Then
        one preliminary anywhere in a multi-OBX report makes the whole report
        preliminary: a report is not final while any part of it is pending.
        """
        statuses = [triple[2].strip().upper() for triple in _obx_tuples(message)]
        if CORRECTED in statuses:
            return CORRECTED
        if statuses and all(status == FINAL for status in statuses):
            return FINAL
        unreadable = [s for s in statuses if s not in (PRELIMINARY, FINAL, CORRECTED)]
        if unreadable or not statuses:
            self.unreadable_status_count += 1
            logger.warning(
                "Result status unreadable (%d OBX segment(s), unrecognised values present: %s); "
                "treating as preliminary, which can reach RESULTED but never ACKNOWLEDGED",
                len(statuses), bool(unreadable),
            )
        return PRELIMINARY

    def _candidates(self, key: ResultKey) -> list[Loop]:
        """The loops an arriving result could match, and no others.

        Every candidate this narrowing drops is one the matcher could not have
        returned anyway: tiers 3-4 are keyed on the patient, and tiers 1-2 on an
        order number the message names. So this is not a policy about matching,
        it is the same predicate expressed where the rows are, and it is here
        rather than left to the matcher because a full-table scan should not be
        the posture ingest takes towards a message it has not authenticated.

        `MATCHABLE_STATES` rather than `open_loops()`: an ACKNOWLEDGED loop must
        stay a candidate at the exact tiers or a correction lands in the orphan
        queue while the loop it corrects goes on reporting "handled". The states
        are the matcher's to choose; only the rows are narrowed here.

        The order numbers are still looked up across patients, so a loop
        belonging to somebody else that carries this result's accession is
        *seen* -- and reported as a collision by the matcher -- instead of
        quietly missing from the query. Narrowing to the patient alone would
        have silently retired that warning.

        A key naming neither a patient nor an order number can satisfy no tier,
        so it gets no candidates rather than all of them. That is the same
        answer by a shorter route, and it is the route that stays right if a
        tier is ever added: an unnarrowed `loops_in_states` is every loop in the
        site, which is exactly what defect H3 needed to work.
        """
        if not (key.mrn or key.placer or key.filler):
            return []
        return self.store.loops_in_states(
            MATCHABLE_STATES, mrn=key.mrn, order_numbers=(key.placer, key.filler)
        )

    def _apply_result(self, message: ParsedMessage, *, mrn: str, submitted_mrn: str) -> None:
        key = result_key_from_message(message, self.pack, mrn=mrn)
        obx11 = self._result_status(message)
        message_at = self._message_at(message)

        outcome = match_result(key, self._candidates(key), self.pack)

        if outcome.loop_id is None:
            self.orphan_count += 1
            if outcome.patient_unverified:
                # Warning, where an ordinary orphan is info: the orphan queue
                # absorbs this one either way, but "an order number matched and
                # nothing said whose it was" is a fact about the feed that
                # somebody has to act on. Control id and count only -- no MRN,
                # and there is none to print in any case.
                self.unattributable_result_count += 1
                logger.warning(
                    "Result %r matched an exact order number at tier %d but names no patient; "
                    "left in the coordinator queue instead of attached (%d so far). Either a "
                    "sender is omitting PID segments or an order number is being guessed; both "
                    "need a human.",
                    message.control_id, outcome.tier, self.unattributable_result_count,
                )
            logger.info(
                "Result %r routed to the orphan queue at tier %d: %s",
                message.control_id, outcome.tier, outcome.reason,
            )
            self.registry.orphan(
                control_id=message.control_id,
                mrn=key.mrn,
                submitted_mrn=submitted_mrn,
                # Allowlisted, non-identifying values only (spec section 3).
                # match_reason carries counts and thresholds by construction.
                detail={
                    "modality": key.modality,
                    "service_code": key.service_code,
                    "placer_order_number": key.placer,
                    "filler_order_number": key.filler,
                    "ordering_provider": key.ordering_provider,
                    "match_tier": outcome.tier,
                    "match_reason": outcome.reason,
                    "result_status": obx11,
                },
                message_at=message_at,
            )
            return

        self.matched_count += 1
        self.registry.record_result(
            outcome.loop_id, obx11=obx11, control_id=message.control_id, message_at=message_at,
            # The tier that produced this attribution, recorded on the event so
            # that if a coordinator later says the match was wrong, the label
            # names which rule misfired (spec section 7). Without it the most
            # valuable label the system produces would say a false match
            # happened without saying where, and a pack revision would have
            # nothing to act on. An integer, so it is non-identifying by shape.
            match_tier=outcome.tier,
        )

    def _target_loop(self, message: ParsedMessage, mrn: str, what: str) -> str | None:
        """The one loop a scheduling message is about, or None.

        At most one, always. An earlier draft applied the transition to every
        open loop for the patient, which for `SIU^S15` means one cancellation
        retiring unrelated open orders -- and `CANCELLED` appears in neither
        `open_loops()` nor `resulted_unacknowledged()`, so those loops leave
        every worklist while remaining clinically open. That is the failure this
        product exists to prevent, caused by the product.

        Evidence, in order: an exact order identifier the message names, or the
        single unambiguous open loop this patient has. Anything else declines
        and flags. A declined schedule costs a coordinator a lookup; a wrong
        cancellation costs a patient a missed finding, and the asymmetry decides
        it -- the same posture as the matcher's confidence floor.
        """
        key = result_key_from_message(message, self.pack, mrn=mrn)
        # `open_loops("")` is not "this patient's open loops", it is *every*
        # open loop in the site -- the mrn argument is a filter, and an absent
        # filter does not filter. A scheduling message carrying no readable
        # PID-3 therefore arrived at the single-open-loop fallback below holding
        # the whole site's work, and on a site with one open loop it cancelled
        # it. A message that names no patient gets no patient's loops.
        open_loops = self.store.open_loops(mrn) if mrn else []

        if key.placer or key.filler:
            outcome = match_result(key, open_loops, self.pack)
            if outcome.loop_id is not None and outcome.tier in _EXACT_TIERS:
                return outcome.loop_id
            self.untargeted_count += 1
            logger.warning(
                "%s %r names an order number that resolves to no single open loop "
                "(tier %d: %s); no loop changed, flagged for review",
                what, message.control_id, outcome.tier, outcome.reason,
            )
            return None

        if len(open_loops) == 1:
            return open_loops[0].loop_id

        self.untargeted_count += 1
        logger.warning(
            "%s %r carries no order number and the patient has %d open loops; "
            "no loop changed, flagged for review",
            what, message.control_id, len(open_loops),
        )
        return None

    def _apply_schedule(self, message: ParsedMessage, *, mrn: str, submitted_mrn: str) -> None:
        loop_id = self._target_loop(message, mrn, "SIU^S12")
        if loop_id is None:
            return
        self.registry.schedule(
            loop_id, control_id=message.control_id, message_at=self._message_at(message)
        )

    def _apply_cancel(self, message: ParsedMessage, *, mrn: str, submitted_mrn: str) -> None:
        loop_id = self._target_loop(message, mrn, "SIU^S15")
        if loop_id is None:
            return
        self.registry.cancel(
            loop_id, control_id=message.control_id, message_at=self._message_at(message)
        )

    def _apply_merge(self, message: ParsedMessage, *, mrn: str, submitted_mrn: str) -> None:
        prior = _prior_mrn(message, self.pack)
        moved = self.registry.merge_patient(
            prior_mrn=prior,
            surviving_mrn=mrn,
            control_id=message.control_id,
            message_at=self._message_at(message),
        )
        logger.info("ADT^A40 %r: carried %d loop(s)", message.control_id, len(moved))


class FileDropSource:
    """Read messages from a watched directory.

    Not speculative (spec open question 1): replaying the raw archive to
    evaluate a pack revision (section 7) needs a non-socket source regardless of
    how a pilot ingests, and a site can pilot from a drop directory before IT
    schedules an interface build.

    A file's fate follows its ACK, which is the same contract the wire gets:

      * `AA` -- processed, and the file is removed.
      * `AE` -- the store could not take it. **Left in place**, so the next drain
        retries. Deleting here would be the file-drop spelling of acknowledging
        what could not be stored.
      * `AR` -- malformed. Renamed aside rather than deleted or retried: it will
        never be acceptable, so retrying it every drain forever is noise, and
        deleting the evidence of a misconfigured sender helps nobody.

    Name order, not modification time: a coordinator or a replay names files in
    the order they should be applied, and mtime granularity puts an order and
    the result that closes it in an arbitrary sequence when both are written in
    the same second.

    One deployment constraint this class cannot express: a writer that creates
    the file and then fills it can be drained mid-write. Sites must write to a
    temporary name and rename into place, which is atomic on both POSIX and
    Windows within a volume.
    """

    PATTERN = "*.hl7"
    REJECTED_SUFFIX = ".rejected"

    def __init__(self, handler: MessageHandler, directory: Path | str,
                 *, delete_on_accept: bool = True):
        self.handler = handler
        self.directory = Path(directory)
        self.delete_on_accept = delete_on_accept
        self.accepted_count = 0
        self.deferred_count = 0
        self.rejected_count = 0

    def drain(self) -> int:
        """Process every pending file. Returns how many were accepted."""
        accepted = 0
        for path in sorted(self.directory.glob(self.PATTERN)):
            if self._process(path):
                accepted += 1
        return accepted

    def _process(self, path: Path) -> bool:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            self.deferred_count += 1
            logger.error("Could not read %s (%s); left in place for the next drain", path, exc)
            return False

        try:
            text = deframe(raw) if raw.startswith(VT) else raw.decode("utf-8")
        except (FramingError, UnicodeDecodeError) as exc:
            self.handler.reject_malformed(raw, f"{path.name}: {exc}")
            self._quarantine(path)
            self.rejected_count += 1
            return False

        code = ack_code(self.handler.handle(text))
        if code == "AA":
            self.accepted_count += 1
            if self.delete_on_accept:
                path.unlink(missing_ok=True)
            return True
        if code == "AR":
            self._quarantine(path)
            self.rejected_count += 1
            return False
        self.deferred_count += 1
        logger.warning("%s could not be stored (%s); left in place for the next drain", path, code)
        return False

    def _quarantine(self, path: Path) -> None:
        target = path.with_name(path.name + self.REJECTED_SUFFIX)
        try:
            path.replace(target)
        except OSError as exc:   # pragma: no cover - filesystem-specific
            logger.error("Could not quarantine %s (%s); leaving it in place", path, exc)
