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
     remainder of a split message does not.

Checks 2 and 4 overlap on purpose and neither subsumes the other: 4 is exact but
depends on TCP timing, 2 is timing-independent but assumes a conformant sender
terminates its last segment.

**The hole checks 2 and 4 both miss, and what closes most of it.** A body
containing `CR FS CR` defeats check 2: the truncated half then *ends with `CR`*,
so check 2 sees a properly terminated last segment and passes it. Check 4 would
catch it -- the remainder begins `ORC|...`, not `VT` -- but only if those bytes
are in the buffer at the moment the frame completes. Measured against the
original implementation, when the two halves landed in separate `recv` calls the
half was answered `AA` and applied: ``acks=['AA','AR'], loops:1``.

So check 4 is given something to look at. **When a frame completes with nothing
behind it, the reader waits a bounded moment for the bytes that would prove the
stream is still in sync, before the frame is applied or acknowledged.** This is
sound because of an asymmetry in how the two cases arise: a truncated half and
its remainder are two pieces of a *single* `write` by a sender that believes it
is emitting one message, so the remainder is already in flight and needs no
round trip -- whereas a sender pipelining a genuine second message has typically
not written it yet, because MLLP senders block on the ACK. Waiting therefore
finds the evidence in the case that matters and finds nothing in the case that
does not.

The wait is `select` with a finite timeout and the reader always proceeds when
it expires, so a sender blocked on the ACK is never deadlocked; the cost is
bounded added latency (`DESYNC_GRACE_SECONDS`) on the last frame of each
delivery, and the check is skipped entirely when bytes are already buffered, so
pipelining is unaffected. Set `desync_grace=0` to opt out.

**What still gets through.** A remainder delayed *longer* than the grace window
-- a sender or intermediary that splits its own write and then stalls -- still
produces an `AA` on the half. So does a sender that emits the half and then
never sends the remainder at all. The first is caught late: when the remainder
does arrive and proves the stream desynchronised, the frame accepted immediately
before it is retroactively flagged through
`MessageHandler.flag_possible_truncation`, naming its control id, which converts
an invisible false accept into an alert a human can pull from the archive. The
second leaves no evidence at all and is not detectable in a stream reader.

Any check failing desynchronises the stream, so the connection is closed after
the `AR` rather than read on -- the reader can no longer tell where the next
message starts, and guessing is the thing it is refusing to do. The frame
*before* the bad boundary is not applied either, for the same reason.
"""
from __future__ import annotations

import logging
import select
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

# How long to wait, after a frame completes with an empty buffer behind it, for
# the bytes that would prove the stream is still synchronised (check 4). The
# remainder of a message split by an embedded FS CR is part of the same sender
# write and is already in flight, so it arrives within a network hop; a genuine
# next message usually has not been written yet, because MLLP senders block on
# the ACK. This is therefore the delay that separates the two, and it is spent
# only on the last frame of a delivery -- pipelined frames have their successor
# already buffered and skip the wait. It is bounded, and the reader always
# proceeds when it expires, so a sender waiting on the ACK cannot deadlock it.
DESYNC_GRACE_SECONDS = 0.05

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


class MLLPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    # Set by make_mllp_server. Annotated rather than assigned so the class
    # carries no shared mutable default across servers.
    message_handler: object
    recv_timeout: float
    max_frame_bytes: int
    desync_grace: float


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
                if not remainder:
                    # Nothing behind the frame, so check 4 has nothing to look
                    # at -- the state in which a `CR FS CR` truncation used to
                    # be answered AA. Wait a bounded moment for the remainder,
                    # which for a split message is already in flight. Bounded
                    # and always proceeds on expiry: a sender blocked on the ACK
                    # is delayed, never deadlocked.
                    remainder = self._lookahead(server)
                    buffer = candidate + remainder
                if remainder and not remainder.startswith(VT):
                    # Check 4. Deliberately before `candidate` is handled: once
                    # the stream is known to be desynchronised, the frame in
                    # hand cannot be trusted to be whole either -- and because
                    # this now runs before the ACK, the truncated half is
                    # refused rather than flagged after the fact.
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

    def _lookahead(self, server: MLLPServer) -> bytes:
        """Bytes already behind a completed frame, waiting at most `desync_grace`.

        Returns `b""` on timeout, on end-of-stream, and on a socket error, so
        every path leads back to "acknowledge the frame in hand". That is the
        deadlock guard and it is deliberately the fallback rather than a special
        case: a sender that is blocked waiting for this ACK must get it, and the
        only thing this method is allowed to do about a silent peer is give up.

        `select` rather than a timed `recv` so the connection's own
        `recv_timeout` is never disturbed -- a temporarily lowered timeout that
        an exception path failed to restore would turn a 300-second idle
        allowance into a 50-millisecond one and drop live connections.
        """
        grace = server.desync_grace
        # `<= 0` rather than `< 0`: zero means off, and off means no syscall.
        # Mutation M3 (`< 0`, so zero still polls with a zero timeout) survives
        # the suite and is left surviving deliberately -- it is strictly safer
        # than this, never less so, and the only case it changes is bytes
        # already in the kernel buffer, which no deterministic test can stage.
        # An explicit opt-out that quietly still costs a syscall per frame is
        # the worse of two harmless options, so this one is the documented one.
        if grace <= 0:
            return b""
        try:
            ready, _, _ = select.select([self.request], [], [], grace)
        except (OSError, ValueError):  # pragma: no cover - peer-dependent
            return b""
        if not ready:
            return b""
        try:
            return self.request.recv(RECV_BYTES)
        except OSError:  # pragma: no cover - peer-dependent
            return b""

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
    desync_grace: float = DESYNC_GRACE_SECONDS,
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
    server.desync_grace = desync_grace
    return server
