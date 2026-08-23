"""HL7 v2 parser built on a segment allowlist.

Only the segments enumerated in ALLOWED_SEGMENTS are ever turned into Python
objects. Everything else -- next of kin in NK1, guarantor in GT1, free-text
notes in NTE -- is dropped at the door, so there is nothing downstream to leak.

This is deliberately not a redaction pass. A redactor is a denylist, and a
denylist fails silently the first time a sending system emits an unexpected
segment carrying an identifier. Same shape as revenue_integrity/ingest_835.py.

MRG is allowlisted because ADT^A40 merges require it (spec section 4 rule 3),
and ORC for order control on ORM/OMG. Omitting either would silently break a
documented requirement.
"""
from __future__ import annotations

import re

from .events import ParsedMessage

ALLOWED_SEGMENTS = frozenset({"MSH", "PID", "MRG", "PV1", "ORC", "OBR", "OBX", "SCH", "RF1"})

# The only MSH-2 this parser accepts: component, repetition, escape,
# subcomponent, in that order and exactly four characters long.
#
# This is a constraint this project imposes, not a property of HL7. The standard
# lets a sender declare its own delimiters in MSH-2, and v2.7 went further and
# added an optional *fifth* character (truncation, "#") -- so a conformant v2.7
# message that uses it is refused here. Note where: its first four characters are
# `^~\&`, so it clears the content check and is caught by the separator check at
# offset 8 instead. That is a known and accepted cost, taken deliberately,
# because the alternative is worse in two directions:
#
#   * Honouring a redeclared set means every split downstream would have to read
#     its delimiter from the header. Every component split here (message type,
#     MRN, universal service id) is a literal "^" split, so a parser that
#     *accepted* a different set without threading it through would read the
#     message with confidently wrong clinical identifiers rather than refuse it
#     -- a false match manufactured by the reader, which is the failure this
#     subsystem exists to prevent.
#   * Accepting a variable-width MSH-2 makes the offset of every following field
#     depend on sender-supplied content. That is a parser-differential surface:
#     this parser and the interface engine upstream of it would disagree about
#     where MSH-9 ends and MSH-10 begins for the same bytes, and the disagreement
#     is chosen by whoever sent them. Fixed offsets are what make the disagreement
#     impossible rather than merely unlikely.
#
# So the width is pinned as well as the content -- see the MSH-1/MSH-2/separator
# triple in structural_fault, which is what makes the fixed offsets sound.
ENCODING_CHARACTERS = "^~\\&"

KNOWN_MESSAGE_TYPES = frozenset(
    {"REF^I12", "ORM^O01", "OMG^O19", "ORU^R01", "SIU^S12", "SIU^S15", "ADT^A40"}
)

# Named field positions, so callers never spell an index literal. HL7 fields are
# 1-indexed and (for non-MSH segments) land at the same index in our split, so
# these read the same as the spec does.
# MSH-3 and MSH-4 are what a message says about who sent it. Named here, and
# read by the listener as *claims* to be checked against the peer the transport
# authenticated -- never as an identity of their own.
MSH_SENDING_APPLICATION = 3
MSH_SENDING_FACILITY = 4
MSH_DATETIME = 7
MSH_MESSAGE_TYPE = 9
MSH_CONTROL_ID = 10

PID_PATIENT_ID = 3

MRG_PRIOR_PATIENT_ID = 1

ORC_ORDER_CONTROL = 1

OBR_PLACER_ORDER_NUMBER = 2
OBR_FILLER_ORDER_NUMBER = 3
OBR_UNIVERSAL_SERVICE_ID = 4
OBR_OBSERVATION_DATETIME = 7

OBX_OBSERVATION_IDENTIFIER = 3
OBX_OBSERVATION_VALUE = 5
OBX_RESULT_STATUS = 11

# The most segments one message may carry. Real HL7 v2 messages are small:
# even a large ORU^R01 -- a microbiology sensitivity panel, a genomic report --
# runs to a few hundred OBX, so a few thousand is an order of magnitude of
# headroom over anything conformant. A cap is needed at all because the only
# limit upstream is mllp_server's frame cap, which bounds the bytes read
# and not the objects retained, and the server threads connections without a
# cap of its own: without this, one frame of `OBX|1` retained ~1.1 GB.
MAX_SEGMENTS = 5000

# The most characters of MSH-10 anything downstream will ever see.
#
# The number is HL7's own: MSH-10 is an ST of length 20, so a conformant sender
# is never truncated and one that is has already left the standard behind.
#
# It is enforced *here*, at the two places a control id is read out of a
# message, and that is the whole point of the constant. `mllp.sanitize_control_id`
# capped it at the same 20 characters and did so only in `build_ack`, so the ACK
# was bounded and nothing else was: the value that reached `raw_messages.
# control_id`, `loop_events.control_id`, the coordinator page's `recorded_by`
# and every operator log line was bounded only by MAX_FRAME_BYTES -- four
# mebibytes, per message, from a sender that never has to send a valid one.
# A bound applied at the last mile is not a bound on the value; it is a bound on
# one rendering of it.
#
# `mllp.py` reads this constant rather than restating it. Two spellings of the
# same limit would drift, and the drift would be silent in the worst way: an ACK
# echoing MSA-2 at one length while the archive keys the message at another
# leaves an operator holding an acknowledgement that finds nothing.
MAX_CONTROL_ID = 20

# A segment is a run of characters between line breaks. Matched lazily rather
# than with str.split so a frame far over MAX_SEGMENTS is abandoned as soon as
# the cap is passed instead of being materialised in full first -- the split
# list is itself a multiple of the frame size.
_SEGMENT = re.compile(r"[^\r\n]+")


class _Fields(list):
    """A segment's fields, where reading past the last one yields "".

    Callers index by field number -- OBX-11 is read on every result -- and
    plenty of conformant segments simply stop earlier than the field being
    read. The obvious way to make that safe is to pad every segment out to a
    fixed width, which is what this module used to do, but that allocates for
    fields nobody sent: padding to 32 measured 69.7x amplification against the
    raw bytes, so a 16 MiB frame retained ~1.1 GB (the cap has since come
    down to 4 MiB; the amplification is what mattered). Answering "" from
    __getitem__ costs nothing and keeps exactly the same contract, including
    for the callers that index without a bounds check of their own.

    Slices and iteration keep list semantics, so the natural width is what a
    caller sees in len() and in `fields[1:]` -- the trailing "" a pad would
    have added were never data.
    """

    __slots__ = ()

    def __getitem__(self, index):
        """Field n, or "" when the sender stopped before field n.

        Only a non-negative integer index is answered that way. Field numbers
        are non-negative by definition, so a negative index is not a short
        segment but a caller computing the wrong thing, and a slice is no
        longer field-indexed at all -- its element 0 is not field 0 -- which
        is why a slice stays a plain list rather than inheriting a promise
        that would then mean something different. Masking either would hide a
        bug here instead of a sender's omission.
        """
        if isinstance(index, int) and index >= len(self):
            return ""
        return super().__getitem__(index)


def _split_fields(segment: str) -> _Fields:
    """Split a segment so that index n holds field n, never truncated.

    For every segment except MSH, index 0 is the segment id and field n lands
    at index n naturally. MSH shifts by one because MSH-1 *is* the separator;
    _split_msh_fields handles that case.

    Segments longer than the fields we name keep all of them -- PV1 runs to
    ~52 and OBR to ~49 in real traffic, and silently dropping the tail would
    violate this module's own "flag, don't silently drop" rule.
    """
    return _Fields(segment.split("|"))


def _split_msh_fields(segment: str) -> _Fields:
    """MSH-1 is the field separator itself, so MSH fields shift by one.

    MSH-1 and MSH-2 are read by *offset*, never by splitting on "|". MSH-1 is
    the single separator character at offset 3 and MSH-2 the four encoding
    characters at offsets 4-7, with MSH-3 starting after the pipe at offset 8.
    Splitting the whole segment on "|" instead treats MSH-2 as "whatever lies
    between the first two pipes", which lets a sender put a separator inside
    it and renumber every later field by one: MSH-10 is then read out of MSH-9,
    so the message is archived under a control id nobody sent and its real
    type never matches -- a clinical result discarded under a positive
    acknowledgement. structural_fault refuses such a message outright; this
    stays positional regardless, because the rejection still has to report the
    control id that actually arrived.
    """
    return _Fields([segment[:3], segment[3:4], segment[4:8]] + segment[9:].split("|"))


def _iter_segments(text: str) -> list[str]:
    """Segments of a message, in order, with blank lines dropped.

    Exact segment-id match, not a prefix match. `raw[:3] in ALLOWED_SEGMENTS`
    alone would ingest "OBXTRA|..." as an OBX, letting a non-allowlisted
    segment's content reach Python objects -- the one thing the allowlist
    exists to prevent. HL7 v2 ids are always exactly 3 characters, so a
    conformant sender never trips this; that is precisely the reasoning this
    module rejects for denylists, so it is enforced rather than assumed.

    At most MAX_SEGMENTS + 1 segments are returned, the extra one being the
    evidence that the cap was passed. Detecting that is structural_fault's
    job and refusing the message is the listener's: handle() calls
    structural_fault before anything else reads the text, so nothing over the
    cap is parsed, counted or acted on there.

    A caller that skips structural_fault -- eval replaying an archive, a test
    calling parse_hl7_text directly -- does see the truncated list as though
    it were whole. That is a real gap and not a comment about one, and it is
    left open deliberately: closing it here means either raising, which
    parse_hl7_text promises never to do, or returning a sentinel every caller
    must remember to check, which is the same gap moved one level up. The cap
    is admission control at the listener, and this is where that is written
    down rather than assumed.
    """
    kept: list[str] = []
    for match in _SEGMENT.finditer(text):
        segment = match.group()
        if not segment.strip():
            continue
        kept.append(segment)
        if len(kept) > MAX_SEGMENTS:
            break
    return kept


def _is_segment(raw: str, seg_id: str) -> bool:
    return raw[:3] == seg_id and (len(raw) == 3 or raw[3] == "|")


def _msh_segments(text: str) -> list[str]:
    return [raw for raw in _iter_segments(text) if _is_segment(raw, "MSH")]


def msh_segment_count(text: str) -> int:
    """How many MSH segments this text carries.

    The listener refuses anything other than exactly one. Zero is not an HL7
    message; more than one is two messages inside a single MLLP frame, which
    is a framing fault -- and whichever MSH loses would have its control id,
    its clinical timestamp and its message type silently discarded.
    """
    return len(_msh_segments(text))


def structural_fault(text: str) -> str:
    """Why this text cannot be trusted as one HL7 message, or "" if it can.

    Same division of labour as msh_segment_count: this module decides, the
    listener rejects. Every fault named here is a permanent property of the
    bytes rather than of our state, so the listener answers AR -- an engine
    told to queue would redeliver them forever, wedging the interface behind a
    message that can never be processed.
    """
    segments = _iter_segments(text)
    if len(segments) > MAX_SEGMENTS:
        return f"more than {MAX_SEGMENTS} segments in one message"

    # Every MSH, not only the one peek_control_id would pick. This does not
    # depend on the caller having already refused a multi-MSH frame, and a
    # header that declares nothing readable is worth naming wherever it sits.
    for raw in segments:
        if not _is_segment(raw, "MSH"):
            continue
        # _is_segment admits a bare three-character "MSH", which carries no
        # separator and no encoding characters at all -- hence the slice
        # rather than an index, and hence this check being reachable.
        if raw[3:4] != "|":
            return "MSH-1 is not the field separator '|'"
        if raw[4:8] != ENCODING_CHARACTERS:
            # !r, not str: this is unvalidated sender bytes on its way into an
            # operator's log line, and repr escapes the CR that would otherwise
            # forge a second line there.
            return (
                f"MSH-2 at offsets 4-7 is {raw[4:8]!r}; this parser accepts only "
                f"{ENCODING_CHARACTERS!r}"
            )
        # The separator that closes MSH-2, and the offset that makes the width
        # constraint real rather than nominal. Checking only the four characters
        # above is not enough: HL7 v2.7 defines a fifth encoding character
        # ("#", for truncation), whose first four are conformant, so a message
        # carrying it would pass the check above while the separator -- and
        # therefore every field _split_msh_fields reads from offset 9 -- has
        # moved one position right. That renumbers MSH-10 out of MSH-9, which
        # is the same discarded-result-under-AA outcome as a "|" inside MSH-2.
        # These three offsets together are what make the split sound; no two
        # of them are.
        if raw[8:9] != "|":
            return f"MSH-2 is not closed by a field separator at offset 8, found {raw[8:9]!r}"
    return ""


def peek_control_id(text: str) -> str:
    """MSH-10 without building a ParsedMessage.

    Needed because the raw message must be durably archived *before* it is
    parsed, and the archive is keyed on the control id. Deliberately expressed
    in terms of the same helpers parse_hl7_text uses: a second hand-rolled MSH
    splitter here would eventually disagree with the parser about which control
    id a message carries, and the archive would then be keyed on something the
    rest of the pipeline never sees.
    """
    found = _msh_segments(text)
    if not found:
        return ""
    return _bounded_control_id(_split_msh_fields(found[-1])[MSH_CONTROL_ID])


def _bounded_control_id(value: str) -> str:
    """MSH-10, cut to MAX_CONTROL_ID. The one place either reader applies it.

    Truncated rather than refused. A control id longer than the standard allows
    is a sender being sloppy far more often than a sender being hostile, and
    refusing the message would discard a clinical result over a field that is
    only ever a handle -- the failure this whole subsystem exists to prevent.
    Truncation keeps the message and bounds the handle.

    Both readers go through here so `peek_control_id` and `parse_hl7_text`
    cannot disagree: the archive is keyed on the first and every event carries
    the second, and a message filed under a key the pipeline never sees is
    `test_peek_and_parse_agree_on_a_shifted_msh`'s defect wearing a new hat.
    """
    return value[:MAX_CONTROL_ID]


def parse_hl7_text(text: str) -> ParsedMessage:
    """Parse a single HL7 v2 message. Never raises on content -- see failure matrix."""
    segments: dict[str, list[list[str]]] = {}
    flags: list[str] = []
    control_id = ""
    message_type = ""

    for raw in _iter_segments(text):
        seg_id = raw[:3]
        if seg_id not in ALLOWED_SEGMENTS or not _is_segment(raw, seg_id):
            continue
        fields = _split_fields(raw)

        if seg_id == "MSH":
            fields = _split_msh_fields(raw)
            control_id = _bounded_control_id(fields[MSH_CONTROL_ID])
            message_type = fields[MSH_MESSAGE_TYPE]

        # A segment with no data fields is malformed: skip it, flag it, keep going.
        if seg_id != "MSH" and not any(fields[1:]):
            flags.append(seg_id)
            continue

        segments.setdefault(seg_id, []).append(fields)

    return ParsedMessage(
        control_id=control_id,
        message_type=message_type,
        is_known_type=message_type in KNOWN_MESSAGE_TYPES,
        segments=segments,
        flags_for_review=tuple(flags),
    )
