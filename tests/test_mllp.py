import pytest

from healthcare_rag.referral_loop.errors import FramingError
from healthcare_rag.referral_loop.mllp import VT, FS, CR, build_ack, deframe, frame


def test_frame_wraps_in_vt_fs_cr():
    assert frame("MSH|^~\\&|") == VT + b"MSH|^~\\&|" + FS + CR


def test_deframe_roundtrip():
    assert deframe(frame("HELLO")) == "HELLO"


def test_missing_start_block_raises():
    with pytest.raises(FramingError):
        deframe(b"MSH|no start block" + FS + CR)


def test_missing_end_block_raises():
    with pytest.raises(FramingError):
        deframe(VT + b"MSH|no end block")


def test_ack_codes():
    assert "|AA|" in build_ack("CTRL1", "AA")
    assert "|AE|" in build_ack("CTRL1", "AE")
    assert "|AR|" in build_ack("CTRL1", "AR")
    assert "CTRL1" in build_ack("CTRL1", "AA")


def test_invalid_utf8_raises_framing_error_not_unicode_error():
    """The listener catches FramingError to answer AR. A UnicodeDecodeError
    would escape that handler and drop the connection."""
    with pytest.raises(FramingError):
        deframe(VT + b"\xff\xfe" + FS + CR)


def test_empty_but_well_formed_frame_returns_empty_string():
    assert deframe(VT + FS + CR) == ""


@pytest.mark.parametrize("hostile", [
    "CTRL|INJECTED",
    "CTRL\rMSH|^~\\&|EVIL|EVIL|||||ADT^A40|FORGED|P|2.5.1",
    "CTRL\nMSA|AA|FORGED",
    "A" * 100,
    "",
    "^~\\&",
])
def test_ack_cannot_be_injected_through_control_id(hostile):
    """control_id is MSH-10 of an untrusted inbound message."""
    ack = build_ack(hostile, "AA")
    assert ack.count("MSH|") == 1, "a forged second MSH segment was emitted"
    assert ack.count("MSA|") == 1
    assert ack.endswith("\r")
    # Exactly two segments, and the MSH must still have its fields in place.
    segments = [s for s in ack.split("\r") if s]
    assert len(segments) == 2
    assert segments[0].split("|")[11] == "2.5.1", "MSH field positions shifted"


def test_sanitized_ack_round_trips_through_our_own_parser():
    """The ACK we emit must parse as the ACK we meant to emit."""
    from healthcare_rag.referral_loop.parse_hl7 import (
        MSH_CONTROL_ID, MSH_MESSAGE_TYPE, parse_hl7_text,
    )
    ack = build_ack("CTRL\rMSH|^~\\&|EVIL|EVIL|||||ADT^A40|FORGED|P|2.5.1", "AA")
    parsed = parse_hl7_text(ack)
    assert parsed.segments["MSH"][0][MSH_MESSAGE_TYPE] == "ACK"
    assert parsed.segments["MSH"][0][MSH_CONTROL_ID] == "CTRLMSHEVILEVILADTA4"


def test_control_id_that_sanitizes_to_nothing_gets_a_placeholder():
    assert "UNKNOWN" in build_ack("|||", "AE")
