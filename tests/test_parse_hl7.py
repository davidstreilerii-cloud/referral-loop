from healthcare_rag.referral_loop.parse_hl7 import ALLOWED_SEGMENTS, parse_hl7_text

ORU = (
    "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r"
    "PID|1||MRN123456^^^HOSP^MR||DOE^JANE||19800101|F\r"
    "NK1|1|DOE^JOHN|SPO|555 ELM ST^^SPRINGFIELD^IL^62701\r"
    "GT1|1||DOE^JOHN^^^^^L|||555 ELM ST^^SPRINGFIELD^IL^62701\r"
    "OBR|1|PLACER987|FILLER654|71260^CT CHEST W CONTRAST^C4|||20260724080000\r"
    "OBX|1|TX|71260^CT CHEST^C4||No acute finding||||||F\r"
)


def test_allowlist_is_exactly_the_documented_set():
    assert ALLOWED_SEGMENTS == frozenset(
        {"MSH", "PID", "MRG", "PV1", "ORC", "OBR", "OBX", "SCH", "RF1"}
    )


def test_nk1_and_gt1_are_never_parsed():
    """These carry next-of-kin and guarantor identifiers. We never read them."""
    msg = parse_hl7_text(ORU)
    assert "NK1" not in msg.segments
    assert "GT1" not in msg.segments


def test_parses_control_id_and_type():
    msg = parse_hl7_text(ORU)
    assert msg.control_id == "CTRL0001"
    assert msg.message_type == "ORU^R01"


def test_extracts_allowlisted_fields():
    msg = parse_hl7_text(ORU)
    obr = msg.segments["OBR"][0]
    assert obr[2] == "PLACER987"   # OBR-2 placer order number
    assert obr[3] == "FILLER654"   # OBR-3 filler order number
    assert msg.segments["OBX"][0][11] == "F"  # OBX-11 result status


def test_unparseable_segment_is_skipped_message_kept():
    """Failure matrix: skip the segment, keep the message, flag for review.

    The OBR is replaced with a bare segment id carrying no data fields -- that
    is what 'unparseable' means here. Truncating only part of it would leave a
    parseable segment and the assertion would be vacuous.
    """
    broken = ORU.replace(
        "OBR|1|PLACER987|FILLER654|71260^CT CHEST W CONTRAST^C4|||20260724080000",
        "OBR",
    )
    msg = parse_hl7_text(broken)
    assert msg.control_id == "CTRL0001", "the message survives a bad segment"
    assert "OBR" in msg.flags_for_review
    assert "OBX" in msg.segments, "other segments still parse"


def test_unknown_message_type_never_raises():
    """Failure matrix: ignore, count, never error."""
    unknown = ORU.replace("ORU^R01", "ZZZ^Z99")
    msg = parse_hl7_text(unknown)
    assert msg.message_type == "ZZZ^Z99"
    assert msg.is_known_type is False
