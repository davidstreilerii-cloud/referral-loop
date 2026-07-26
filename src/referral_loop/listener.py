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
  3. **MSH-7 travels with every message-driven transition.** The registry's
     staleness watermark fails open on `message_at=None`, so a listener that did
     not pass it would leave the guard inert for live traffic and let a
     clinically older message silently regress a loop.

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
import threading
from datetime import datetime
from pathlib import Path

from .errors import (
    CircularMergeError,
    FramingError,
    MrnRetiredError,
    ReferralLoopError,
    StaleMessageError,
    StoreUnavailableError,
)
from .events import ParsedMessage
from .matcher import (
    MATCHABLE_STATES,
    concept_value,
    field_value,
    hl7_datetime,
    match_result,
    result_key_from_message,
)
from .mllp import VT, build_ack, deframe
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

# Bounded, because a resolution that keeps moving is a merge storm a human needs
# to see rather than something to spin on. See _open_loop_retrying.
_MRN_RESOLUTION_ATTEMPTS = 3


def ack_code(ack: str) -> str:
    """`AA` / `AE` / `AR` from an ACK, or "" if it carries no MSA.

    Callers branch on the outcome (FileDropSource decides whether to delete a
    file on it), and `"|AA|" in ack` is a substring test over attacker-influenced
    text -- MSA-2 echoes the inbound control id. mllp.sanitize_control_id makes
    that safe today; parsing the field the ACK actually means keeps it safe if
    that ever changes.
    """
    for line in ack.replace("\n", "\r").split("\r"):
        if line.startswith("MSA|"):
            fields = line.split("|")
            return fields[1] if len(fields) > 1 else ""
    return ""


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

    # --------------------------------------------------------------- entry point

    def handle(self, text: str) -> str:
        """Persist, then parse, then apply. Never the other way round."""
        control_id = peek_control_id(text)

        # Before the archive, because this is not a message: zero MSH segments
        # is not HL7, and more than one is two messages inside a single frame.
        # Either way the control id we would key the archive on is not the one
        # the sender used for what we would then process.
        count = msh_segment_count(text)
        if count != 1:
            return self.reject_malformed(
                text.encode("utf-8", errors="replace"),
                f"expected exactly one MSH segment, found {count}",
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
        self.suspect_truncation_count += 1
        logger.error(
            "Message %r was acknowledged and applied, and the bytes that followed it did not "
            "begin a new frame. It may have been the first half of a message split by an "
            "embedded FS CR. Review the archived raw for this control id; any loop or result "
            "it produced was derived from a possibly incomplete message.",
            control_id,
        )

    # ---------------------------------------------------------------- malformed

    def reject_malformed(self, raw: bytes, reason: str) -> str:
        """Failure matrix: `AR`, archive raw, alert.

        `AR` rather than `AE` because these bytes will never become acceptable
        and an engine told to queue would redeliver them forever, wedging the
        interface behind a message that cannot be processed.

        The archive is lossless. Bytes that are not UTF-8 are base64-encoded
        rather than replaced, because the whole value of archiving a malformed
        frame is being able to show the sender exactly what arrived.
        """
        self.framing_error_count += 1
        control_id = _MALFORMED_PREFIX + hashlib.sha256(raw).hexdigest()[:32]
        try:
            payload = raw.decode("utf-8")
        except UnicodeDecodeError:
            payload = _BASE64_PREFIX + base64.b64encode(raw).decode("ascii")
        try:
            self.store.record_raw(control_id, payload)
        except StoreUnavailableError as exc:
            # Still AR. The alternative is AE, which asks for the redelivery of
            # bytes that cannot be processed either way.
            logger.error("Could not archive a malformed frame (%s): %s", control_id, exc)
        logger.error(
            "Malformed framing (%s); archived as %s and answering AR. Alert: a sender is "
            "emitting frames this listener cannot trust.", reason, control_id,
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

        None means "unknown", and the registry's watermark deliberately fails
        open on it -- an unreadable clock must not refuse traffic. Passing it at
        all is the point: without it the guard never compares anything, and a
        clinically older message silently regresses a loop.
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
            ordered_at=key.observed_at,
            message_at=self._message_at(message),
        )

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

    def _apply_result(self, message: ParsedMessage, *, mrn: str, submitted_mrn: str) -> None:
        key = result_key_from_message(message, self.pack, mrn=mrn)
        obx11 = self._result_status(message)
        message_at = self._message_at(message)

        # MATCHABLE_STATES rather than open_loops(): an ACKNOWLEDGED loop must
        # stay a candidate at the exact tiers or a correction lands in the
        # orphan queue while the loop it corrects goes on reporting "handled".
        candidates = self.store.loops_in_states(MATCHABLE_STATES)
        outcome = match_result(key, candidates, self.pack)

        if outcome.loop_id is None:
            self.orphan_count += 1
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
        open_loops = self.store.open_loops(mrn)

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
