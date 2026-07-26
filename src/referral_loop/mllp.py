"""MLLP framing, kept separate from the listener so it is testable without a socket."""
from __future__ import annotations

from .errors import FramingError

VT = b"\x0b"   # start block
FS = b"\x1c"   # end block
CR = b"\x0d"   # carriage return


def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


def deframe(payload: bytes) -> str:
    if not payload.startswith(VT):
        raise FramingError("Missing MLLP start block (VT)")
    if not payload.endswith(FS + CR):
        raise FramingError("Missing MLLP end block (FS CR)")
    return payload[len(VT):-len(FS + CR)].decode("utf-8", errors="strict")


def build_ack(control_id: str, code: str) -> str:
    """AA = accepted, AE = error (engine queues and retries), AR = rejected.

    Never return AA for a message that was not durably stored.
    """
    if code not in {"AA", "AE", "AR"}:
        raise ValueError(f"Invalid ACK code: {code}")
    return (
        f"MSH|^~\\&|REFERRAL|LOCAL|SENDER|SENDER|||ACK|{control_id}|P|2.5.1\r"
        f"MSA|{code}|{control_id}\r"
    )
