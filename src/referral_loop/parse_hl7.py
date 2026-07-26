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

from .events import ParsedMessage

ALLOWED_SEGMENTS = frozenset({"MSH", "PID", "MRG", "PV1", "ORC", "OBR", "OBX", "SCH", "RF1"})

KNOWN_MESSAGE_TYPES = frozenset(
    {"REF^I12", "ORM^O01", "OMG^O19", "ORU^R01", "SIU^S12", "SIU^S15", "ADT^A40"}
)

# Named field positions, so callers never spell an index literal. HL7 fields are
# 1-indexed and (for non-MSH segments) land at the same index in our split, so
# these read the same as the spec does.
MSH_MESSAGE_TYPE = 9
MSH_CONTROL_ID = 10

PID_PATIENT_ID = 3

MRG_PRIOR_PATIENT_ID = 1

OBR_PLACER_ORDER_NUMBER = 2
OBR_FILLER_ORDER_NUMBER = 3
OBR_UNIVERSAL_SERVICE_ID = 4
OBR_OBSERVATION_DATETIME = 7

OBX_RESULT_STATUS = 11

# Every segment is padded to at least this width so field access by index is
# always safe. Segments longer than this keep all their fields -- PV1 runs to
# ~52 and OBR to ~49 in real traffic, and silently dropping the tail would
# violate this module's own "flag, don't silently drop" rule.
_MIN_PADDED_FIELDS = 32


def _split_fields(segment: str) -> list[str]:
    """Split a segment so that index n holds field n, padded but never truncated.

    For every segment except MSH, index 0 is the segment id and field n lands
    at index n naturally. MSH shifts by one because MSH-1 *is* the separator;
    parse_hl7_text handles that case separately.
    """
    fields = segment.split("|")
    if len(fields) < _MIN_PADDED_FIELDS:
        fields.extend([""] * (_MIN_PADDED_FIELDS - len(fields)))
    return fields


def parse_hl7_text(text: str) -> ParsedMessage:
    """Parse a single HL7 v2 message. Never raises on content -- see failure matrix."""
    raw_segments = [s for s in text.replace("\n", "\r").split("\r") if s.strip()]

    segments: dict[str, list[list[str]]] = {}
    flags: list[str] = []
    control_id = ""
    message_type = ""

    for raw in raw_segments:
        # Exact segment-id match, not a prefix match. `raw[:3] in ALLOWED_SEGMENTS`
        # alone would ingest "OBXTRA|..." as an OBX, letting a non-allowlisted
        # segment's content reach Python objects -- the one thing the allowlist
        # exists to prevent. HL7 v2 ids are always exactly 3 characters, so a
        # conformant sender never trips this; that is precisely the reasoning this
        # module rejects for denylists, so it is enforced rather than assumed.
        seg_id = raw[:3]
        if seg_id not in ALLOWED_SEGMENTS or not (len(raw) == 3 or raw[3] == "|"):
            continue
        fields = _split_fields(raw)

        if seg_id == "MSH":
            # MSH-1 is the field separator itself, so MSH fields shift by one.
            msh = [raw[:3], "|"] + raw[4:].split("|")
            if len(msh) < _MIN_PADDED_FIELDS:
                msh.extend([""] * (_MIN_PADDED_FIELDS - len(msh)))
            fields = msh
            control_id = fields[MSH_CONTROL_ID]
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
