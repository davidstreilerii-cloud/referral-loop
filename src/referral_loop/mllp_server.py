"""The MLLP wire: stream reassembly and the loopback TCP listener.

Split out of `listener.py` because it is a different job with a different
failure mode. `listener.py` decides what a *message* means; this module decides
where a message *begins and ends* on a byte stream, and gets that wrong in ways
no amount of correct message handling can recover from. `mllp.py` is
deliberately socket-free so framing stays testable without a network; this is
the socket half, and the two are separated for the same reason.

**The failure this module exists to prevent.** MLLP puts a message between `VT`
and `FS CR`, so a message body that itself contains `FS CR` splits into two
frames on the wire. The first half is a syntactically fine HL7 message with
fewer segments than the sender wrote. Answering it `AA` tells the interface
engine the whole message was delivered while the remainder -- and whatever order
or result it carried -- is discarded. A truncated message that parses cleanly
and gets acknowledged is worse than one that fails loudly, because nothing
downstream can tell it happened. `mllp.frame` refuses to *build* such a message;
that does nothing about one arriving.

Four checks, in the order they can fire, none of them expensive:

  1. **`VT` inside the body.** A start block where there cannot be one.
  2. **The body must end with `CR`.** Every HL7 v2 segment is CR-terminated,
     including the last, so a well-formed frame is `VT ... CR FS CR`. A frame
     split at an embedded `FS` ends on whatever character preceded it inside the
     field. This is the check that catches a truncation *when the remainder has
     not yet arrived* -- the case check 4 cannot see.
  3. **`deframe` refuses an embedded `FS`**, so a body carrying `FS` not
     followed by `CR` is rejected rather than silently truncated.
  4. **The bytes following a frame must begin the next one with `VT`.** The
     remainder of a split message does not. This fires only when those bytes are
     already buffered, which is why check 2 exists.

Checks 2 and 4 overlap on purpose and neither subsumes the other: 4 is exact but
depends on TCP timing, 2 is timing-independent but assumes a conformant sender
terminates its last segment.

**The residual hole, stated exactly, because it was found by probing rather than
by reasoning.** A body containing `CR FS CR` defeats both. The truncated half
then *ends with `CR`*, so check 2 sees a properly terminated last segment and
passes it; and if the remainder has not yet arrived, check 4 has nothing to look
at. The half is answered `AA` and applied. This is not fixable inside a stream
reader: at the instant a frame completes, `VT msg FS CR` followed later by more
bytes is indistinguishable from a legitimate message followed by another one.
The real defences are upstream -- `mllp.frame` refuses to build such a message,
and MLLP requires senders not to put `FS` in a body.

What this module does instead is refuse to let it stay silent. When check 4
fires, the frame accepted immediately before it on the same connection is
retroactively flagged through `MessageHandler.flag_possible_truncation`, naming
its control id, because a desynchronised remainder is evidence that the thing
just acknowledged may have been half a message. That converts an invisible
false accept into an alert with an identifier a human can pull from the archive.

Any check failing desynchronises the stream, so the connection is closed after
the `AR` rather than read on -- the reader can no longer tell where the next
message starts, and guessing is the thing it is refusing to do. The frame
*before* the bad boundary is not applied either, for the same reason.
"""
from __future__ import annotations

import logging
import socket
import socketserver

from .errors import FramingError
from .mllp import CR, FS, VT, deframe, frame
from .parse_hl7 import peek_control_id

logger = logging.getLogger(__name__)

# An unbounded read buffer is a denial of service: a sender that never emits an
# end block would grow it until the process dies, taking every other connection
# with it. 16 MiB is far beyond any real HL7 v2 message, including one carrying
# an embedded report.
MAX_FRAME_BYTES = 16 * 1024 * 1024
RECV_BYTES = 8192

# A connection an engine has forgotten about otherwise holds a thread forever.
RECV_TIMEOUT_SECONDS = 300.0

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


class MLLPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    # Set by make_mllp_server. Annotated rather than assigned so the class
    # carries no shared mutable default across servers.
    message_handler: object
    recv_timeout: float
    max_frame_bytes: int


class MLLPRequestHandler(socketserver.BaseRequestHandler):
    """One connection. Reassembles frames and answers each one exactly once."""

    def handle(self) -> None:
        server: MLLPServer = self.server  # type: ignore[assignment]
        handler = server.message_handler
        self.request.settimeout(server.recv_timeout)
        buffer = b""
        # The frame accepted most recently on this connection. If the stream
        # turns out to be desynchronised, that frame is the one most likely to
        # have been half a message. See the module docstring.
        last_accepted = ""

        while True:
            try:
                chunk = self.request.recv(RECV_BYTES)
            except socket.timeout:
                logger.warning(
                    "Connection idle for %ss; closing with %d unacknowledged byte(s) discarded",
                    server.recv_timeout, len(buffer),
                )
                return
            except OSError as exc:
                logger.warning("Connection error (%s); %d byte(s) discarded", exc, len(buffer))
                return

            if not chunk:
                if buffer:
                    # Nothing was acknowledged, so the engine still owns this
                    # message and will redeliver it. Discarding a half-message
                    # is correct; processing one is the failure above.
                    logger.error(
                        "Peer closed mid-message; %d byte(s) discarded unacknowledged",
                        len(buffer),
                    )
                return

            buffer += chunk
            if len(buffer) > server.max_frame_bytes:
                self._send(handler.reject_malformed(
                    buffer[: server.max_frame_bytes],
                    f"no end block within {server.max_frame_bytes} bytes",
                ))
                return

            while True:
                end = buffer.find(FS + CR)
                if end < 0:
                    break
                candidate, remainder = buffer[: end + 2], buffer[end + 2:]
                if remainder and not remainder.startswith(VT):
                    # Check 4. Deliberately before `candidate` is handled: once
                    # the stream is known to be desynchronised, the frame in
                    # hand cannot be trusted to be whole either.
                    self._flag(handler, last_accepted)
                    self._send(handler.reject_malformed(
                        buffer, "bytes after an end block do not begin a new frame"
                    ))
                    return
                buffer = remainder
                ack, intact = self._ack_for(handler, candidate)
                if not intact:
                    # ANY framing rejection immediately after an accepted frame
                    # is the same evidence, not just check 4's. The `CR FS CR`
                    # case lands here rather than there: the truncated half is
                    # accepted, and the remainder then arrives as its own
                    # apparent frame and fails check 2. Flagging only on check 4
                    # missed exactly the case the flag exists for.
                    self._flag(handler, last_accepted)
                    self._send(ack)
                    return
                self._send(ack)
                last_accepted = peek_control_id(
                    candidate[len(VT):-2].decode("utf-8", errors="replace")
                )

    @staticmethod
    def _flag(handler, last_accepted: str) -> None:
        """Flag the previous frame when this one turns out to be unframeable.

        False alarms are possible and accepted: a connection carrying one good
        message followed by an unrelated malformed one flags the good message
        too. That costs a coordinator a look at an archived message that turns
        out to be fine. Not flagging costs a truncated clinical result applied
        silently, and the asymmetry decides it -- the same reasoning the matcher
        uses to prefer an orphan over a false match.
        """
        if last_accepted:
            handler.flag_possible_truncation(last_accepted)

    @staticmethod
    def _ack_for(handler, candidate: bytes) -> tuple[str, bool]:
        """(ACK to send, whether the stream may be read on)."""
        body = candidate[len(VT):-2] if candidate.startswith(VT) else b""
        if VT in body:
            return handler.reject_malformed(
                candidate, "a start block appears inside the message body"
            ), False
        if not body.endswith(CR):
            return handler.reject_malformed(
                candidate,
                "the last segment is not CR-terminated, so this frame ends somewhere the "
                "sender did not put an end block -- most likely an embedded FS CR splitting "
                "one message into two",
            ), False
        try:
            text = deframe(candidate)
        except FramingError as exc:
            return handler.reject_malformed(candidate, str(exc)), False
        return handler.handle(text), True

    def _send(self, ack: str) -> None:
        try:
            self.request.sendall(frame(ack))
        except (OSError, FramingError) as exc:  # pragma: no cover - peer-dependent
            logger.error("Could not send ACK (%s); the engine will redeliver", exc)


def make_mllp_server(
    handler,
    host: str = "127.0.0.1",
    port: int = 2575,
    *,
    recv_timeout: float = RECV_TIMEOUT_SECONDS,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> MLLPServer:
    """Bind loopback by default. v1 makes no outbound connection to anyone, us included.

    A non-loopback bind is warned about rather than refused: binding is ingress,
    not egress, and a real interface engine lives on another host -- but a v1
    pilot that did not mean to expose a PHI-bearing port should hear about it in
    its own log rather than find out from someone else's scan.
    """
    if host not in _LOOPBACK:
        logger.warning(
            "MLLP listener binding non-loopback address %r. v1 is specified as a local "
            "listener; make sure this port is deliberately reachable and firewalled.", host,
        )
    server = MLLPServer((host, port), MLLPRequestHandler)
    server.message_handler = handler
    server.recv_timeout = recv_timeout
    server.max_frame_bytes = max_frame_bytes
    return server
