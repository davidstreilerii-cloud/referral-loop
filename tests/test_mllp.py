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
