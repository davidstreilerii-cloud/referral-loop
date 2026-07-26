"""MLLP framing, kept separate from the listener so it is testable without a socket."""
from __future__ import annotations

import re

from .errors import FramingError

VT = b"\x0b"   # start block
FS = b"\x1c"   # end block
CR = b"\x0d"   # carriage return

# MSH-10 is at most 20 characters and carries no delimiters. Anything else is
# either a malformed sender or an injection attempt.
_SAFE_CONTROL_ID = re.compile(r"[^A-Za-z0-9._-]")
_MAX_CONTROL_ID = 20


def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


def deframe(payload: bytes) -> str:
    if not payload.startswith(VT):
        raise FramingError("Missing MLLP start block (VT)")
    if not payload.endswith(FS + CR):
        raise FramingError("Missing MLLP end block (FS CR)")
    try:
        return payload[len(VT):-len(FS + CR)].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # The listener catches FramingError and answers AR. A bare
        # UnicodeDecodeError would escape that handler and crash the connection
        # on hostile bytes -- exactly the input this layer exists to reject.
        raise FramingError(f"Payload is not valid UTF-8: {exc}") from exc


def sanitize_control_id(control_id: str) -> str:
    """Make an untrusted MSH-10 safe to interpolate into an ACK.

    control_id comes from the inbound message, so it is attacker-controlled. A
    bare '|' silently shifts every later MSH field; an embedded '\\r' plus a
    fabricated 'MSH|...' forges a second segment, and a parser reading the
    result takes the forged header as authoritative.

    We always return a well-formed ACK -- refusing to answer is not an option,
    because the engine would just retry forever -- so this sanitizes rather
    than raises.
    """
    cleaned = _SAFE_CONTROL_ID.sub("", control_id or "")[:_MAX_CONTROL_ID]
    return cleaned or "UNKNOWN"


def build_ack(control_id: str, code: str) -> str:
    """AA = accepted, AE = error (engine queues and retries), AR = rejected.

    Never return AA for a message that was not durably stored.
    """
    if code not in {"AA", "AE", "AR"}:
        raise ValueError(f"Invalid ACK code: {code}")
    safe_id = sanitize_control_id(control_id)
    return (
        f"MSH|^~\\&|REFERRAL|LOCAL|SENDER|SENDER|||ACK|{safe_id}|P|2.5.1\r"
        f"MSA|{code}|{safe_id}\r"
    )
