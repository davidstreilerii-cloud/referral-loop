from datetime import datetime, timezone

import pytest

from referral_loop.errors import FramingError
from referral_loop.mllp import (
    VT, FS, CR, ack_code, build_ack, deframe, frame,
)
from referral_loop.parse_hl7 import (
    MSH_CONTROL_ID, MSH_MESSAGE_TYPE, parse_hl7_text,
)


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


def test_ack_code_reads_every_code_build_ack_can_emit():
    """`ack_code` lives beside `build_ack` because it is its inverse, and
    because the stream reader has to branch on the outcome of `handle()` --
    an application-level `AR` closes the connection -- without importing the
    listener that calls it."""
    for code in ("AA", "AE", "AR"):
        assert ack_code(build_ack("CTRL1", code)) == code


def test_ack_code_parses_msa2_rather_than_testing_for_a_substring():
    """MSA-2 echoes the inbound control id, which is attacker-influenced.
    `"|AA|" in ack` over that text is a substring test on hostile input;
    sanitize_control_id makes it safe today and parsing keeps it safe if that
    ever changes."""
    assert ack_code(build_ack("AA", "AR")) == "AR"
    assert ack_code("MSH|^~\\&|\r") == "", "no MSA segment means no code"


def test_invalid_utf8_raises_framing_error_not_unicode_error():
    """The listener catches FramingError to answer AR. A UnicodeDecodeError
    would escape that handler and drop the connection."""
    with pytest.raises(FramingError):
        deframe(VT + b"\xff\xfe" + FS + CR)


def test_empty_but_well_formed_frame_returns_empty_string():
    assert deframe(VT + FS + CR) == ""


def test_frame_rejects_embedded_fs():
    """A body containing FS would let a stream reader split one message into
    two -- the truncated half parses cleanly and would be answered AA while the
    remainder is silently discarded."""
    with pytest.raises(FramingError):
        frame("MSH|^~\\&|OK\x1cORC|NW|CRITICAL-ORDER")


def test_deframe_rejects_embedded_fs():
    """Even if a hostile/malformed frame reaches deframe directly (bypassing
    frame()), an embedded FS must not be silently accepted as message content."""
    hostile = VT + b"MSH|^~\\&|OK\x1cORC|NW|CRITICAL-ORDER" + FS + CR
    with pytest.raises(FramingError):
        deframe(hostile)


@pytest.mark.parametrize("hostile", [
    "CTRL|INJECTED",
    "CTRL\rMSH|^~\\&|EVIL|EVIL|||||ADT^A40|FORGED|P|2.5.1",
    "CTRL\nMSA|AA|FORGED",
    "A" * 100,
    "",
    "^~\\&",
    "REF.MSH",
    "MSA",
])
def test_ack_cannot_be_injected_through_control_id(hostile):
    """control_id is MSH-10 of an untrusted inbound message."""
    ack = build_ack(hostile, "AA")
    assert ack.endswith("\r")
    # Exactly two segments, and the MSH must still have its fields in place.
    segments = [s for s in ack.split("\r") if s]
    assert len(segments) == 2
    assert segments[0].split("|")[11] == "2.5.1", "MSH field positions shifted"


def test_sanitized_ack_round_trips_through_our_own_parser():
    """The ACK we emit must parse as the ACK we meant to emit. MSH-10 is now a
    fresh id generated for the ACK itself (not the echoed inbound control id),
    so pin it via the ack_id parameter for a deterministic assertion."""
    ack = build_ack(
        "CTRL\rMSH|^~\\&|EVIL|EVIL|||||ADT^A40|FORGED|P|2.5.1",
        "AA",
        ack_id="ACKFIXED123",
    )
    parsed = parse_hl7_text(ack)
    assert parsed.segments["MSH"][0][MSH_MESSAGE_TYPE] == "ACK"
    assert parsed.segments["MSH"][0][MSH_CONTROL_ID] == "ACKFIXED123"


def test_control_id_that_sanitizes_to_nothing_gets_a_placeholder():
    assert "UNKNOWN" in build_ack("|||", "AE")


def test_ack_msh7_datetime_is_populated():
    """MSH-7 (Date/Time of Message) is required in v2.5.1. A strict
    conformance profile rejects an ACK with it empty, which means the engine
    retries forever -- the exact failure sanitize_control_id exists to avoid."""
    fixed_now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
    ack = build_ack("CTRL1", "AA", now=fixed_now)
    msh = ack.splitlines()[0].split("|")
    assert msh[6] == "20260726120000"


def test_ack_msh10_differs_from_inbound_control_id_msa2_echoes_it():
    """MSH-10 must be a fresh id for the ACK; the inbound control id belongs in
    MSA-2. Reusing the inbound id in MSH-10 lets an engine that de-duplicates
    on MSH-10 drop the ACK as a replay of the original message."""
    ack = build_ack("CTRL1", "AA")
    msh_fields = ack.splitlines()[0].split("|")
    msa_fields = ack.splitlines()[1].split("|")
    assert msh_fields[9] != "CTRL1"
    assert msa_fields[2] == "CTRL1"


def test_two_acks_for_same_inbound_id_have_different_msh10():
    ack1 = build_ack("CTRL1", "AA")
    ack2 = build_ack("CTRL1", "AA")
    msh10_1 = ack1.splitlines()[0].split("|")[9]
    msh10_2 = ack2.splitlines()[0].split("|")[9]
    assert msh10_1 != msh10_2
