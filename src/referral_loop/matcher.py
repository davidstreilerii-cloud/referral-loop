"""Resolve an arriving result to an open loop.

A false match is strictly worse than an orphan. An orphan gets human attention;
a false match attributes a result to the wrong order, marks that loop resolved,
and leaves the real loop open *while reporting it handled* -- the tool conceals
the very thing it exists to surface. So below the pack's confidence floor, and
whenever a tier cannot single out one loop, the matcher returns no loop at all
and routes to review (spec sections 5 and 7).

The opposite failure is just as real and much quieter. A matcher that attaches
nothing scores a perfect 0.000 false-match rate: every result orphans, nothing
is mis-attached, and the safety metric reads ideal precisely when the product
has stopped working (spec section 10.4). The pack's `min_auto_match_rate` is the
coverage floor that audits this, measured over a corpus rather than in here --
but it is why every decline in this module is a *narrow* decline, taken on a
named ambiguity, never a blanket refusal to commit.

Every threshold, window, tie-breaker **and field placement** is pack data.
Nothing here is hardcoded clinical judgement, and nothing here knows what field
a site's RIS writes an accession into: that mapping is signed pack data, because
tier *logic* is stable everywhere and field *placement* is not (spec section 5).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .clock import is_readable_clock
from .errors import PackVerificationError
from .events import Loop, LoopState, MatchResult, ParsedMessage
from .pack import RulePack
from .parse_hl7 import OBR_OBSERVATION_DATETIME

logger = logging.getLogger(__name__)

# States that can still receive a result.
#
# ACKNOWLEDGED is a candidate at the exact-identifier tiers only, and that split
# is a safety decision in both directions. It must be a candidate somewhere:
# safety rule 2 says a corrected result on an ACKNOWLEDGED loop reopens review,
# and Registry.record_result implements exactly that branch -- if the matcher
# never handed it an ACKNOWLEDGED loop the branch would be dead code and a
# correction would land in the orphan queue while the loop it corrects went on
# reporting "handled", which is the product's own failure mode.
#
# It must *not* be a candidate at tiers 3-4: reopening a loop a human already
# settled is the highest-consequence transition available here, and tiers 3-4
# are the weakest evidence class (MRN plus a code or a modality inside a time
# window). A genuine correction carries the same order numbers as the result it
# corrects, so tiers 1-2 always fire for it; nothing is lost by refusing to
# unsettle acknowledged work on a heuristic.
#
# CANCELLED is excluded by the failure matrix ("Result for a CANCELLED loop ->
# orphan + flag"), CLOSED is reserved for v2 and unreachable, and ORPHAN,
# DISMISSED and ATTACHED are results, not expectations -- an orphan is retired by
# attachment, never by matching another result onto it, and an already-attached
# one must not be re-matched by a redelivery.
_RESULTABLE_STATES = frozenset({LoopState.OPEN, LoopState.SCHEDULED, LoopState.RESULTED})
_EXACT_TIER_STATES = _RESULTABLE_STATES | frozenset({LoopState.ACKNOWLEDGED})

# Every state match_result can return a loop from. Exported so ingest asks the
# store for exactly this set rather than deciding for itself which loops to
# offer: a listener that supplied a narrower set would silently disable a tier,
# and omitting ACKNOWLEDGED specifically would make safety rule 2 dead code
# without any test in this module noticing.
MATCHABLE_STATES = _EXACT_TIER_STATES

_TIER_PLACER = 1
_TIER_FILLER = 2
_TIER_SERVICE = 3
_TIER_MODALITY = 4
_TIER_ORPHAN = 5

_KNOWN_TIE_BREAKERS = frozenset(
    {"nearest_order_date", "same_ordering_provider", "most_specific_modality"}
)

# Concepts read through the field map. `observation_datetime` is optional: the
# packs shipped against spec section 5 do not carry it, so it falls back to the
# parser's OBR-7 constant. See _observed_at.
_CONCEPT_PLACER = "placer_order_number"
_CONCEPT_FILLER = "filler_order_number"
_CONCEPT_SERVICE = "service_code"
_CONCEPT_MODALITY = "modality"
_CONCEPT_PROVIDER = "ordering_provider"
_CONCEPT_MRN = "mrn"
_CONCEPT_OBSERVED_AT = "observation_datetime"

_FIELD_REF_RE = re.compile(r"^([A-Z][A-Z0-9]{2})-([1-9][0-9]*)(?:\.([1-9][0-9]*))?$")
_HL7_OFFSET_RE = re.compile(r"([+-])(\d{2})(\d{2})$")


@dataclass(frozen=True)
class ResultKey:
    """The allowlisted fields of an arriving ORU that matching is permitted to use.

    This is an allowlist, not a convenience struct. Matching may reason over
    order identifiers, a patient identifier, a service code, a modality and two
    timestamps -- and nothing else. Anything a coordinator would recognise as
    clinical content (the OBX values, the impression, the note segments) never
    reaches this record, so no amount of matcher logic can come to depend on it
    and no match reason can carry it into an audit row.

    `mrn` is the *resolved* surviving identifier. Alias resolution happens once,
    at ingest, before the registry or the matcher sees anything (spec section 4);
    resolving again here would be the second call site the spec ruled out.
    """

    mrn: str
    placer: str = ""
    filler: str = ""
    service_code: str = ""
    modality: str = ""
    observed_at: datetime | None = None
    ordering_provider: str = ""


# --------------------------------------------------------------- field access


def _split_ref(ref: str) -> tuple[str, int, int | None]:
    """`SEG-N` or `SEG-N.C` -> (segment, field index, 1-based component or None).

    load_pack already validates this shape and refuses a segment outside the
    parser allowlist, so a malformed reference here means a RulePack built by
    hand rather than loaded. It is still refused rather than skipped: a typo'd
    placement silently reading nothing is the failure mode the pack signature
    exists to prevent.
    """
    match = _FIELD_REF_RE.match(ref or "")
    if not match:
        raise PackVerificationError(f"Malformed field reference in field_map: {ref!r}")
    segment, field_no, component = match.groups()
    return segment, int(field_no), int(component) if component else None


def field_value(message: ParsedMessage, ref: str) -> str:
    """Read one `SEG-N[.C]` value, or "" when the message does not carry it.

    Never raises on content. A reference naming a segment or field the message
    does not have is not an error -- the caller falls through to the next
    candidate, and if all are absent the concept is simply unavailable and the
    tier does not fire (failure matrix, and pack.field_candidates' contract).

    Three decisions worth stating:

    * **First segment instance only.** An ORU carrying two OBRs is two orders.
      Scanning on to the second instance to fill a field the first left empty
      would build one key out of two orders' identifiers -- a cross-order
      attribution, which is a false match manufactured by the reader. v1 reads
      the first instance and multi-order messages are a documented limitation.
    * **First repetition only.** `~` separates repetitions of a whole field;
      returning `A~B` as an identifier would match neither A nor B.
    * **A bare `SEG-N` returns the field verbatim**, `^` components and all. A
      site whose EI fields carry an assigning authority (`12345^HOSP`) writes
      `OBR-2.1` in its pack rather than getting a silent component guess here --
      which is the whole point of the placement being data.
    """
    segment, index, component = _split_ref(ref)
    instances = message.segments.get(segment) or []
    if not instances:
        return ""
    fields = instances[0]
    if index >= len(fields):
        return ""
    raw = fields[index]
    if not raw:
        return ""
    raw = raw.split("~", 1)[0]
    if component is None:
        return raw.strip()
    parts = raw.split("^")
    if component > len(parts):
        return ""
    return parts[component - 1].strip()


def concept_value(message: ParsedMessage, pack: RulePack, concept: str) -> str:
    """First populated candidate for a concept, per the pack's priority order."""
    for ref in pack.field_candidates(concept):
        value = field_value(message, ref)
        if value:
            return value
    return ""


# ------------------------------------------------------------------ datetimes


def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are treated as UTC so window arithmetic never raises.

    Mixing an aware observation datetime with a naive stored `ordered_at` raises
    TypeError on subtraction, and a TypeError inside matching is an unhandled
    result -- the listener archives the raw and moves on, so the result silently
    never reaches a queue at all. Assuming UTC is consistent with how the store
    and registry write timestamps.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def hl7_datetime(raw: str) -> datetime | None:
    """HL7 TS -> aware datetime, or None when it cannot be trusted.

    None rather than `now()`. Defaulting an unparseable observation datetime to
    the current time invents the one number tiers 3-4 reason over, and invents
    it in the direction that *widens* matching: "now" is inside every window. An
    unreadable timestamp must cost a match, not manufacture one.

    A TS with no offset is site-local time. Treating it as UTC is consistent
    across the order and the result, so the window arithmetic (a difference) is
    unaffected as long as the feed is internally consistent.

    **A syntactically valid year is not a real one.** The window is bounded here
    (`clock.is_readable_clock`) rather than at each caller, because leaving it to
    callers is precisely what failed: the registry's clinical watermark, the
    registry's ordering guard and a loop's `ordered_at` each consumed whatever
    four digits of year could express, and `99991231235959` broke all three in
    different ways. Enforced by the function making the claim, for the same
    reason `staleness.require_thresholds_accepted` is called by `is_stale`
    itself rather than documented as a precondition.
    """
    text = (raw or "").split("~", 1)[0].split("^", 1)[0].strip()
    if not text:
        return None

    tzinfo: timezone = timezone.utc
    offset = _HL7_OFFSET_RE.search(text)
    if offset:
        sign, hours, minutes = offset.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        tzinfo = timezone(-delta if sign == "-" else delta)
        text = text[: offset.start()]

    text = text.split(".", 1)[0]
    if not text.isdigit() or len(text) < 8:
        return None

    padded = text[:14].ljust(14, "0")
    try:
        parsed = datetime(
            int(padded[0:4]), int(padded[4:6]), int(padded[6:8]),
            int(padded[8:10]), int(padded[10:12]), int(padded[12:14]),
            tzinfo=tzinfo,
        )
    except ValueError:
        return None
    # Out of the window is the same answer as unparseable, and for the same
    # reason: None already means "this timestamp cannot be trusted", and every
    # caller already has a defined behaviour for it.
    return parsed if is_readable_clock(parsed) else None


def _observed_at(message: ParsedMessage, pack: RulePack) -> datetime | None:
    """Observation datetime, from the pack when it maps one, else OBR-7.

    Spec section 5's field map names six concepts and not this one, yet tiers 3-4
    key on the window it defines -- so the one placement the tiers depend on was
    the one still hardcoded. Reading it from the field map *when a pack carries
    it* closes that without requiring a pack revision, and without inventing a
    required concept that load_pack does not validate.
    """
    if _CONCEPT_OBSERVED_AT in pack.field_map:
        return hl7_datetime(concept_value(message, pack, _CONCEPT_OBSERVED_AT))
    return hl7_datetime(field_value(message, f"OBR-{OBR_OBSERVATION_DATETIME}"))


def result_key_from_message(
    message: ParsedMessage, pack: RulePack, *, mrn: str | None = None
) -> ResultKey:
    """Build a ResultKey by reading the message through the pack's field map.

    `mrn` overrides what the message carries and is how ingest passes the
    *resolved* surviving identifier down (spec section 4). Left None, the MRN is
    read from the field map like every other concept -- correct for tests and
    for replay of an already-resolved archive, wrong for live ingest, which
    resolves first.
    """
    return ResultKey(
        mrn=mrn if mrn is not None else concept_value(message, pack, _CONCEPT_MRN),
        placer=concept_value(message, pack, _CONCEPT_PLACER),
        filler=concept_value(message, pack, _CONCEPT_FILLER),
        service_code=concept_value(message, pack, _CONCEPT_SERVICE),
        modality=concept_value(message, pack, _CONCEPT_MODALITY),
        observed_at=_observed_at(message, pack),
        ordering_provider=concept_value(message, pack, _CONCEPT_PROVIDER),
    )


# -------------------------------------------------------------------- matching


def _mrn_agrees(loop: Loop, key: ResultKey) -> bool:
    """Do the loop and the result agree on the patient, *where both name one*?

    The exact tiers key on an order number, and an order number is not a
    site-wide identifier: placer numbers are unique per placing application, so
    two feeds numbering from the same seed collide on the digits alone and tier
    1 attaches another patient's loop at full confidence (spec section 5). The
    assigning authority in the field distinguishes them, but that protection is
    *pack data* -- a revision narrowing the candidate to `OBR-2.1` strips the
    namespace through a signed pack edit, which does not pass code review the
    way a code change would. So the tier checks the patient in code as well.

    **"Where both are known" is load-bearing, not softening.** An absent MRN is
    not a value to compare, it is the absence of a constraint: a result with no
    readable `PID-3` must still match an exact accession, or a real class of ORU
    is demoted to an orphan for carrying *less* data -- trading a rare false
    match for a common false negative, which is the fix being worse than the
    bug. Both sides get the same treatment, because a loop can lack one too.

    This is deliberately *not* `loop.mrn == key.mrn`. That form makes two
    absences agree, which lands on the same answer here but is the `"" == ""`
    equivalence class this module refuses everywhere else -- and it would block
    the one-sided absence, which is exactly the case that must not break.

    No alias resolution happens here. Both identifiers are already the surviving
    one: resolution runs once, at ingest, before the registry or the matcher
    sees anything (spec section 4), so re-resolving would be the second call
    site the spec ruled out -- and this comparison therefore cannot
    false-negative on a merge.
    """
    if not loop.mrn or not key.mrn:
        return True
    return loop.mrn == key.mrn


def _in_window(loop: Loop, key: ResultKey, pack: RulePack) -> bool:
    """Per-modality date window (spec section 5). Both halves must be known.

    The window is the loop's modality where known, because it expresses how long
    after an order a study of that kind still results: a stat CT and a screening
    mammogram are not the same question.
    """
    if loop.ordered_at is None or key.observed_at is None:
        return False
    window = timedelta(hours=pack.date_window_hours(loop.modality or key.modality))
    return abs(_as_utc(key.observed_at) - _as_utc(loop.ordered_at)) <= window


def _distance(loop: Loop, key: ResultKey) -> timedelta:
    if loop.ordered_at is None or key.observed_at is None:
        return timedelta.max
    return abs(_as_utc(key.observed_at) - _as_utc(loop.ordered_at))


def _tiebreak(candidates: list[Loop], key: ResultKey, pack: RulePack) -> list[Loop]:
    """Narrow the candidate set using the pack's tie-breakers, in the pack's order.

    Returns the surviving set, not a winner. A tie-breaker that cannot separate
    the field leaves it as it found it, and the caller declines on anything it
    could not narrow to one -- so a mis-spelled or unimplemented tie-breaker
    degrades into extra coordinator work, never into an arbitrary pick.

    No sort decides the outcome. Each rule filters to the equal-best set, so the
    result cannot depend on the order loops arrived in, on dict iteration, or on
    which of two equally good loops the store happened to replay first.
    """
    ranked = list(candidates)
    for rule in pack.tie_breakers:
        if len(ranked) == 1:
            break
        if rule not in _KNOWN_TIE_BREAKERS:
            logger.warning(
                "Rule pack %s names unknown tie-breaker %r; ignored. Ambiguity it "
                "would have resolved now routes to review.", pack.version, rule
            )
            continue
        if rule == "nearest_order_date":
            best = min(_distance(loop, key) for loop in ranked)
            ranked = [loop for loop in ranked if _distance(loop, key) == best]
        elif rule == "same_ordering_provider":
            if not key.ordering_provider:
                continue
            same = [
                loop for loop in ranked
                if loop.ordering_provider and loop.ordering_provider == key.ordering_provider
            ]
            ranked = same or ranked
        elif rule == "most_specific_modality":
            if not key.modality:
                continue
            exact = [loop for loop in ranked if loop.modality == key.modality]
            ranked = exact or ranked
    return ranked


def _resolve(
    hits: list[Loop], key: ResultKey, pack: RulePack, tier: int, reason: str, tiebreak: bool
) -> MatchResult:
    """Turn a tier's hits into a MatchResult, declining rather than guessing.

    Two gates, both returning loop_id None while keeping the tier so the orphan
    queue can be triaged by how close the match came:

    * **Ambiguity.** More than one loop survives. At tiers 1-2 that means two
      open loops share an order number -- a duplicate order or a feed fault, and
      attaching to either is a coin flip dressed as an exact match. At tiers 3-4
      it means the pack's tie-breakers could not separate two equally plausible
      orders. Both need a person.
    * **The confidence floor**, applied at every tier rather than only at 3-4. A
      site that lowers tier_confidence for an identifier tier is saying it does
      not trust that identifier, and the gate must hear that; a floor with
      exceptions is a floor an operator cannot reason about.

    Reasons carry counts and thresholds, never identifiers -- they land in
    audit detail, which spec section 3 requires to be non-identifying.
    """
    ranked = _tiebreak(hits, key, pack) if tiebreak else hits
    # Ambiguity is about distinct loops. Callers assemble candidates from more
    # than one store query (open_loops plus the resulted set), and the same loop
    # arriving twice is a caller's bookkeeping, not two orders competing --
    # declining on it would be a false orphan manufactured by the plumbing.
    if len({loop.loop_id for loop in ranked}) > 1:
        return MatchResult(
            None, tier, 0.0,
            f"{reason}; {len(ranked)} candidate loops remain indistinguishable "
            "-- ambiguous, routed to review",
        )

    # A pack that omits a tier scores it 0.0 and therefore declines, rather than
    # raising KeyError inside ingest.
    confidence = float(pack.tier_confidence.get(tier, 0.0))
    if confidence < pack.confidence_floor:
        return MatchResult(
            None, tier, confidence,
            f"{reason}; confidence {confidence:.2f} below floor "
            f"{pack.confidence_floor:.2f} -- routed to review",
        )
    return MatchResult(ranked[0].loop_id, tier, confidence, reason)


def match_result(key: ResultKey, loops: list[Loop], pack: RulePack) -> MatchResult:
    """Resolve a result to one open loop, or decline.

    Tiers are strictly ordered and the first tier to find any hit is decisive --
    it either resolves, or it declines and matching stops. A lower tier is never
    consulted after a higher one has fired, so no tie-break at tier 3 or 4 can
    overturn what an order number already said, and no weaker tier can win by
    accident.

    A *hit* means the whole tier predicate, not just its identifier. Tiers 1-2
    require the MRN to agree where both sides carry one, so a result whose only
    exact-number match belongs to another patient has not found a hit at all --
    it falls through rather than declining, and the lower tiers still get their
    turn. See _mrn_agrees for why the check is there and why it is conditional.

    Empty is never a match. Every tier requires its key field to be populated,
    so an absent placer cannot match another absent placer -- the `"" == ""` bug
    that turns a whole feed's worth of empty fields into one giant equivalence
    class of false matches.
    """
    exact_candidates = [loop for loop in loops if loop.state in _EXACT_TIER_STATES]
    # Loops dropped from an exact tier because they belonged to another patient.
    # Counted, not returned: a count is safe in a reason that lands in audit
    # detail, and it is the difference between an orphan a coordinator can
    # triage as a feed fault and one that looks like a result nobody ordered.
    #
    # *Distinct* loops, for the same reason the ambiguity gate in _resolve counts
    # distinct loop ids: a loop carrying both order numbers is rejected once at
    # tier 1 and again at tier 2, and reporting two collisions where one loop
    # collided would inflate the number an operator uses to judge how wide the
    # numbering overlap is.
    mrn_rejected: set[str] = set()

    def exact(hits: list[Loop]) -> list[Loop]:
        """Apply the MRN half of the tier-1/2 predicate, recording what it drops."""
        agreeing = []
        for loop in hits:
            if _mrn_agrees(loop, key):
                agreeing.append(loop)
            else:
                mrn_rejected.add(loop.loop_id)
        return agreeing

    # Tier 1 -- placer order number exact, and MRN agrees where both are known.
    #
    # The MRN is part of the *predicate*, so a cross-patient collision means the
    # tier never fires -- it does not fire and then decline. That distinction is
    # deliberate: another patient's numbering scheme must not be able to consume
    # this result's turn at the lower tiers, which is what a decline here would
    # do. Falling through cannot manufacture a match, because every tier below
    # is either MRN-keyed (3-4) or carries this same guard (2).
    if key.placer:
        hits = exact([loop for loop in exact_candidates if loop.placer_order_number == key.placer])
        if hits:
            return _resolve(
                hits, key, pack, _TIER_PLACER, "placer order number exact", tiebreak=False
            )

    # Tier 2 -- filler order number / accession exact, and MRN agrees where both
    # are known.
    if key.filler:
        hits = exact([loop for loop in exact_candidates if loop.filler_order_number == key.filler])
        if hits:
            return _resolve(
                hits, key, pack, _TIER_FILLER, "filler order number exact", tiebreak=False
            )

    same_patient = [
        loop for loop in loops
        if loop.state in _RESULTABLE_STATES and key.mrn and loop.mrn == key.mrn
    ]

    # Tier 3 -- MRN + service code + per-modality date window.
    if key.service_code:
        hits = [
            loop for loop in same_patient
            if loop.service_code == key.service_code and _in_window(loop, key, pack)
        ]
        if hits:
            return _resolve(
                hits, key, pack, _TIER_SERVICE,
                "MRN + service code + date window", tiebreak=True,
            )

    # Tier 4 -- MRN + modality equivalence + date window.
    if key.modality:
        equivalents = pack.equivalent_modalities(key.modality)
        hits = [
            loop for loop in same_patient
            if loop.modality and loop.modality in equivalents and _in_window(loop, key, pack)
        ]
        if hits:
            return _resolve(
                hits, key, pack, _TIER_MODALITY,
                "MRN + modality equivalence + date window", tiebreak=True,
            )

    # Tier 5 -- no match. Orphans are workflow, not failure (spec section 5).
    if mrn_rejected:
        # Two feeds numbering from the same seed is a site-wide fault that gets
        # worse silently, so it is said out loud rather than left to look like a
        # result nobody ordered. Counts only -- no identifiers reach a log line
        # or an audit row (spec section 3).
        logger.warning(
            "%d loop(s) matched an exact order number but belonged to another "
            "patient and were rejected; result routed to the orphan queue. Two "
            "placing systems numbering from the same seed, or a field map "
            "pointing at the wrong field.", len(mrn_rejected),
        )
        return MatchResult(
            None, _TIER_ORPHAN, 0.0,
            f"no candidate loop; {len(mrn_rejected)} exact order-number "
            "match(es) rejected because the MRN disagreed",
        )
    return MatchResult(None, _TIER_ORPHAN, 0.0, "no candidate loop")
