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

# Max fields we index per segment. OBX-11 is the deepest field we read.
_MAX_FIELDS = 32


def _split_fields(segment: str) -> list[str]:
    """Split a segment so that index n holds field n.

    For every segment except MSH, index 0 is the segment id and field n lands
    at index n naturally. MSH shifts by one because MSH-1 *is* the separator;
    parse_hl7_text handles that case separately.
    """
    fields = segment.split("|")
    if len(fields) < _MAX_FIELDS:
        fields.extend([""] * (_MAX_FIELDS - len(fields)))
    return fields[:_MAX_FIELDS]


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
            msh.extend([""] * (_MAX_FIELDS - len(msh)))
            fields = msh[:_MAX_FIELDS]
            control_id = fields[10]
            message_type = fields[9]

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
