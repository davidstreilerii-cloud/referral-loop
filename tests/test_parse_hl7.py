from healthcare_rag.referral_loop.parse_hl7 import (
    ALLOWED_SEGMENTS,
    MAX_SEGMENTS,
    OBR_FILLER_ORDER_NUMBER,
    OBR_PLACER_ORDER_NUMBER,
    OBX_RESULT_STATUS,
    parse_hl7_text,
    peek_control_id,
    structural_fault,
)

MSH = "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r"

ORU = (
    MSH
    + "PID|1||MRN123456^^^HOSP^MR||DOE^JANE||19800101|F\r"
    + "NK1|1|DOE^JOHN|SPO|555 ELM ST^^SPRINGFIELD^IL^62701\r"
    + "GT1|1||DOE^JOHN^^^^^L|||555 ELM ST^^SPRINGFIELD^IL^62701\r"
    + "OBR|1|PLACER987|FILLER654|71260^CT CHEST W CONTRAST^C4|||20260724080000\r"
    + "OBX|1|TX|71260^CT CHEST^C4||No acute finding||||||F\r"
)

# The auditor's exploit for MSH-2. A parser that reads MSH-2 as "whatever lies
# between the first two pipes" shifts every later field by one: it sees MSH-7
# as RFAC, MSH-9 as "" and MSH-10 as ORU^R01, where a conformant parser sees
# 20260101010101, ORU^R01 and CTRL_REAL. Downstream that archives the message
# under a fabricated control id, makes is_known_type false, and answers AA --
# discarding a clinical result while positively acknowledging it.
SHIFTED_MSH = "MSH|^|\\&|SEND|SFAC|RECV|RFAC|20260101010101||ORU^R01|CTRL_REAL|P|2.5.1\r"


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
    assert obr[OBR_PLACER_ORDER_NUMBER] == "PLACER987"
    assert obr[OBR_FILLER_ORDER_NUMBER] == "FILLER654"
    assert msg.segments["OBX"][0][OBX_RESULT_STATUS] == "F"


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


def test_prefix_colliding_segment_id_is_not_allowlisted():
    """OBXTRA starts with OBX but is not OBX. An allowlist that prefix-matches
    is not an allowlist -- it would let a non-allowlisted segment's content
    reach Python objects."""
    msg = parse_hl7_text(
        "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r"
        "OBXTRA|1|TX|CODE||SENTINEL_LEAK||||||F\r"
    )
    assert msg.segments.get("OBX") is None
    import dataclasses, json
    assert "SENTINEL_LEAK" not in json.dumps(dataclasses.asdict(msg), default=str)


def test_msh_lookalike_does_not_set_a_wrong_control_id():
    """MSH-10 is the idempotency key. A silently wrong value is worse than none."""
    msg = parse_hl7_text("MSHX|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r")
    assert msg.control_id == ""
    assert msg.message_type == ""


def test_bare_three_char_segment_is_still_accepted():
    """len(raw) == 3 is a valid, empty segment -- it must still be flagged for
    review rather than silently dropped by the new exactness check."""
    msg = parse_hl7_text(
        "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r"
        "OBR\r"
    )
    assert "OBR" in msg.flags_for_review


def test_run_together_segment_ids_are_rejected():
    msg = parse_hl7_text("MSHPIDOBROBX\r")
    assert msg.segments == {}
    assert msg.control_id == ""


def test_long_segments_keep_every_field():
    """PV1 and OBR legitimately exceed 32 fields. Dropping the tail silently
    would be the one thing this parser promises not to do."""
    long_pv1 = "PV1|" + "|".join(str(i) for i in range(1, 60))
    msg = parse_hl7_text(
        "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r"
        + long_pv1 + "\r"
    )
    pv1 = msg.segments["PV1"][0]
    assert pv1[59] == "59", "field 59 must survive"


def test_short_segments_index_safely_past_their_last_field():
    msg = parse_hl7_text(
        "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r"
        "OBR|1|PLACER1\r"
    )
    assert msg.segments["OBR"][0][11] == "", "indexing past the end must not raise"


# ------------------------------------------------------- MSH-1 and MSH-2 by offset


def test_msh_fields_are_read_by_offset_not_by_splitting_on_the_separator():
    """MSH-1 is one character at offset 3; MSH-2 is four at offsets 4-7.

    Reading them by splitting on "|" lets a sender put a separator inside
    MSH-2 and renumber every later field. MSH-10 is the idempotency key and
    MSH-9 decides whether the message is handled at all, so a shift there is
    not a parse quirk -- it is a discarded clinical result.
    """
    msg = parse_hl7_text(SHIFTED_MSH)
    assert msg.message_type == "ORU^R01", "MSH-9, not the field one to its left"
    assert msg.control_id == "CTRL_REAL", "MSH-10, not the message type"


def test_peek_and_parse_agree_on_a_shifted_msh():
    """The archive is keyed on peek_control_id and the pipeline on the parse.
    They must not disagree about which value MSH-10 holds."""
    assert peek_control_id(SHIFTED_MSH) == parse_hl7_text(SHIFTED_MSH).control_id


def test_a_non_standard_encoding_set_is_a_structural_fault():
    """Fail closed. Every component split downstream -- message type, MRN,
    service id -- assumes `^~\\&`. A sender declaring anything else is not
    speaking the dialect this parser reads, so reading it anyway would produce
    confidently wrong clinical identifiers rather than an error."""
    assert structural_fault(SHIFTED_MSH) != ""


def test_a_bare_msh_segment_is_a_structural_fault():
    """A three-character segment id is a valid empty segment everywhere else,
    but an MSH with no field separator and no encoding characters declares
    nothing at all."""
    assert structural_fault("MSH\r") != ""


def test_a_conformant_message_has_no_structural_fault():
    assert structural_fault(ORU) == ""


# ------------------------------------------------------------------ segment cap


def test_segments_beyond_the_cap_are_a_structural_fault():
    """Only a 16 MiB frame limit sits upstream, and the server threads
    connections without a cap, so segment count is the bound that stops one
    frame from retaining hundreds of megabytes."""
    assert structural_fault(MSH + "OBX|1\r" * MAX_SEGMENTS) != ""


def test_a_message_at_the_cap_is_accepted():
    """The cap has to be reachable, not merely survivable: a real ORU carrying
    a large panel must not be refused for being large."""
    assert structural_fault(MSH + "OBX|1\r" * (MAX_SEGMENTS - 1)) == ""


def test_short_segments_are_not_expanded_to_a_fixed_width():
    """Padding every segment to 32 fields measured 69.7x amplification: 1.2 MB
    of `OBX|1` retained 83.6 MB, so a 16 MiB frame retained ~1.1 GB. Field
    access past the end still returns "" -- see the safe-indexing test -- but
    nothing is allocated to make that true."""
    msg = parse_hl7_text(MSH + "OBX|1\r" * 100)
    assert [len(fields) for fields in msg.segments["OBX"]] == [2] * 100


# ---------------------------------------------------------------- on the wire


def test_the_listener_answers_ar_to_every_structural_fault(tmp_path):
    """AR, not AE. These bytes will never become acceptable, and an engine told
    to queue would redeliver them forever behind a message it cannot send.

    The listener is imported inside the test rather than at module scope: this
    file tests the parser, but a fault the listener never consults is not a
    defense, so one test crosses the seam.
    """
    from healthcare_rag.referral_loop.listener import MessageHandler, ack_code
    from healthcare_rag.referral_loop.registry import Registry
    from healthcare_rag.referral_loop.store import LoopStore
    from tests.referral_loop.test_matcher import PACK

    store = LoopStore(tmp_path / "loops.db")
    handler = MessageHandler(store=store, registry=Registry(store), pack=PACK)

    assert ack_code(handler.handle(SHIFTED_MSH)) == "AR"
    assert ack_code(handler.handle(MSH + "OBX|1\r" * MAX_SEGMENTS)) == "AR"
    assert store.raw_count() == 2, "failure matrix: AR, archive raw, alert"
