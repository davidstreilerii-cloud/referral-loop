"""MLLP framing, kept separate from the listener so it is testable without a socket.

Invariant a message body must never violate: it must not contain FS
(``\\x1c``). The MLLP terminator is FS CR, so an embedded FS lets a stream
reader split one message into two -- the first half parses cleanly and would
be answered AA while the remainder (and whatever real order or result it
carried) is silently discarded. `frame` and `deframe` both refuse rather than
let that happen; Task 10's listener inherits the invariant by construction
instead of having to remember to enforce it itself.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from .errors import FramingError

VT = b"\x0b"   # start block
FS = b"\x1c"   # end block
CR = b"\x0d"   # carriage return

# MSH-10 is at most 20 characters and carries no delimiters. Anything else is
# either a malformed sender or an injection attempt.
_SAFE_CONTROL_ID = re.compile(r"[^A-Za-z0-9._-]")
_MAX_CONTROL_ID = 20


def frame(message: str) -> bytes:
    """Wrap a message in MLLP framing.

    Rejects a body containing FS: the terminator is FS CR, so an embedded FS
    would let a stream reader split one message into two. The truncated half
    parses cleanly and would be answered AA while the remainder is discarded --
    a false accept plus a silently lost result.
    """
    payload = message.encode("utf-8")
    if FS in payload:
        raise FramingError("Message body contains the MLLP end block (FS); refusing to frame")
    return VT + payload + FS + CR


def deframe(payload: bytes) -> str:
    """Unwrap an MLLP frame back to the message text it carried.

    Raises FramingError -- never a bare exception -- on any malformed input,
    so a caller (the listener) can catch one type and answer AR.
    """
    if not payload.startswith(VT):
        raise FramingError("Missing MLLP start block (VT)")
    if not payload.endswith(FS + CR):
        raise FramingError("Missing MLLP end block (FS CR)")
    body = payload[len(VT):-len(FS + CR)]
    if FS in body:
        raise FramingError("Message body contains an embedded FS; frame boundary is ambiguous")
    try:
        return body.decode("utf-8", errors="strict")
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
    than raises. Coerced through str() first so a non-str caller cannot raise
    TypeError against that same always-well-formed promise.
    """
    cleaned = _SAFE_CONTROL_ID.sub("", str(control_id or ""))[:_MAX_CONTROL_ID]
    return cleaned or "UNKNOWN"


def build_ack(control_id: str, code: str, now: datetime | None = None,
              ack_id: str | None = None) -> str:
    """AA = accepted, AE = error (engine queues and retries), AR = rejected.

    Never return AA for a message that was not durably stored.

    MSH-10 is a fresh id for this ACK; the inbound control id is echoed in
    MSA-2. Reusing it in MSH-10 makes engines that de-duplicate on MSH-10 drop
    our ACKs as replays. Injectable parameters keep this deterministic in tests.
    """
    if code not in {"AA", "AE", "AR"}:
        raise ValueError(f"Invalid ACK code: {code}")
    safe_id = sanitize_control_id(control_id)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    own_id = sanitize_control_id(ack_id or f"ACK{uuid.uuid4().hex[:16]}")
    return (
        f"MSH|^~\\&|REFERRAL|LOCAL|SENDER|SENDER|{stamp}||ACK|{own_id}|P|2.5.1\r"
        f"MSA|{code}|{safe_id}\r"
    )
