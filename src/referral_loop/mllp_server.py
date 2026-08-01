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

**An application-level `AR` closes the connection too**, and for a different
reason: nothing about it desynchronises the stream, but a rejection that leaves
the connection open costs the sender nothing to repeat. Each repetition costs
*us* a SHA-256, a decode and an INSERT committed with an fsync, and the
malformed archive is content-addressed, so varying one byte per frame defeats
the deduplication and writes a row every time. A rejection has to end something.

**The second resource this module bounds is connections.** The buffer cap above
is the same argument one granularity down: a `ThreadingTCPServer` with
`daemon_threads` and no `max_children` gives every accepted connection an OS
thread for as long as the peer holds it, so ten thousand connections dribbling a
byte apiece is ten thousand threads and an fd each, at a cost to the sender of
about thirty-five bytes a second. `RECV_TIMEOUT_SECONDS` does not catch that:
`settimeout` bounds each `recv` call, not the connection, and a peer that speaks
once per window never trips it while never approaching the buffer cap either --
one byte per 290 seconds reaches 16 MiB in roughly 150 years. So there are four
bounds, and each one is a different way of holding a resource:

  * a semaphore over accepted connections, and a per-source-address cap so one
    peer cannot take every slot (both refused in `verify_request`, before a
    thread exists);
  * an absolute connection deadline, checked each iteration against a
    `time.monotonic()` budget rather than trusted to a per-`recv` timeout;
  * a timer on an *incomplete* frame, restarted by each completed one, which is
    what the dribbler above actually defeats;
  * a per-peer rejection budget over a window, so reconnecting to draw another
    rejection stops being free.

Those four budgets stay keyed on the **source address**, and that is a decision
rather than an omission now that connections have identities. They exist to
bound what a peer can consume *before* it is anybody, and the two ends of the
handshake make the argument in opposite directions: a client that fails
authentication has no identity to charge, so an identity-keyed budget would
share one bucket between every failing attacker and every failing
misconfiguration; and a client that succeeds has already spent the accept, the
handshake and the thread that the budget exists to protect. Address is the only
thing available at the moment the answer is needed. Its limits are real -- a NAT
shares one budget between senders and a multi-homed peer gets several -- and
they are limits of counting what can be seen, not reasons to count nothing. The
peer id, once resolved, is logged alongside the address wherever a budget is
spent, so an operator reading the alert is not left with only a number.

**The transport authenticates before it reads.** `get_request` wraps every
accepted socket in a TLS context requiring a client certificate signed by the
site's own CA, and `MLLPRequestHandler.handle` resolves that certificate to a
`PeerIdentity` through the peer registry and closes the connection if it cannot.
Nothing reaches `MessageHandler` unattributed. A site may opt out -- see
`peers.PeerRegistry` -- and then an allowlisted source address is the identity,
enforced in `verify_request` before a thread exists, with the opt-out logged at
WARNING on every start.

The handshake happens on the accept thread, before `verify_request` can charge
anything for it, which is where `TLS_HANDSHAKE_SECONDS` comes in: a client that
opens a connection and then stalls mid-handshake would otherwise hold up every
other accept. That is the same resource argument the budgets make, applied at
the one point that runs before they can see anything.
"""
from __future__ import annotations

import logging
import select
import socket
import socketserver
import ssl
import threading
import time

from .errors import FramingError
from .mllp import CR, FS, VT, ack_code, deframe, frame
from .parse_hl7 import peek_control_id
from .peers import TLS_HANDSHAKE_SECONDS, PeerIdentity, PeerRegistry

logger = logging.getLogger(__name__)

# An unbounded read buffer is a denial of service: a sender that never emits an
# end block would grow it until the process dies, taking every other connection
# with it. 16 MiB is far beyond any real HL7 v2 message, including one carrying
# an embedded report.
MAX_FRAME_BYTES = 16 * 1024 * 1024
RECV_BYTES = 8192

# A connection an engine has forgotten about otherwise holds a thread forever.
# Applied with settimeout, so this bounds one recv call and not the connection;
# CONNECTION_DEADLINE_SECONDS and FIRST_FRAME_SECONDS are what bound the
# connection.
RECV_TIMEOUT_SECONDS = 300.0

# Connections accepted at once, across all peers. A real interface engine holds
# one to a handful of persistent connections and a file-drop pilot holds none,
# so this is two orders of magnitude of headroom over any real deployment while
# keeping threads, stacks and descriptors inside what one process carries.
MAX_CONNECTIONS = 64

# ... and per source address, because a global cap alone lets one peer take
# every slot, which is the same outage reached by a shorter route. Same headroom
# per sender: an engine that needs more than eight simultaneous connections to
# one v1 listener is not a configuration this was built for.
MAX_CONNECTIONS_PER_PEER = 8

# The whole connection, from accept. Generous, because MLLP senders hold their
# connections open and reconnect transparently -- an hour costs a conformant
# engine one reconnect and bounds a wedged connection to something an operator
# can wait out rather than to nothing at all.
CONNECTION_DEADLINE_SECONDS = 3600.0

# How long a frame may stay incomplete. Timed from the first byte of a frame and
# restarted by each completed one, so a sender pausing between whole messages is
# unaffected and one dribbling bytes inside a frame forever is not. A real
# message is written in a single call and lands within a network hop; a minute
# covers a pathologically fragmented sender by three orders of magnitude.
FIRST_FRAME_SECONDS = 60.0

# Rejections one peer may draw inside REJECTION_WINDOW_SECONDS before it stops
# being read at all. Closing the connection per rejection is not a rate limit on
# its own, because reconnecting costs an attacker nothing. A conformant sender
# draws zero; a misconfigured one draws them at the rate its outbound queue
# retries, and no engine retries eight times inside a minute. Over budget, the
# peer is refused at accept until the window passes -- brief, and a feed drawing
# eight rejections a minute is already not working.
MAX_REJECTIONS_PER_PEER = 8
REJECTION_WINDOW_SECONDS = 60.0

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


class _PeerLedger:
    """What each source address is currently holding and has recently spent.

    One instance per server, touched from the accept thread and from every
    connection thread, so every field is read and written under one lock --
    `count += 1` on a shared attribute is a load, an add and a store, and a
    connection cap that loses increments is not a cap.

    Keyed on the source address, which is all the peer identity this listener
    has (see the module docstring). Rejection timestamps are pruned on every
    admission, so the table holds only peers that misbehaved inside the window
    rather than growing with every address ever seen.
    """

    def __init__(self, *, max_total: int, max_per_peer: int,
                 max_rejections: int, window: float):
        self._lock = threading.Lock()
        # Bounded, so a release without a matching admission raises here rather
        # than quietly inflating the cap.
        self._slots = threading.BoundedSemaphore(max_total)
        self._max_per_peer = max_per_peer
        self._max_rejections = max_rejections
        self._window = window
        self._open: dict[str, int] = {}
        self._rejections: dict[str, list[float]] = {}

    def admit(self, peer: str) -> bool:
        """Take a slot for `peer`, or refuse. Never blocks: a refused connection
        must be closed, not queued behind the ones already being abused."""
        with self._lock:
            self._prune(time.monotonic())
            if len(self._rejections.get(peer, ())) >= self._max_rejections:
                return False
            if self._open.get(peer, 0) >= self._max_per_peer:
                return False
            if not self._slots.acquire(blocking=False):
                return False
            self._open[peer] = self._open.get(peer, 0) + 1
            return True

    def release(self, peer: str) -> None:
        with self._lock:
            remaining = self._open.get(peer, 0) - 1
            if remaining > 0:
                self._open[peer] = remaining
            else:
                self._open.pop(peer, None)
            self._slots.release()

    def note_rejection(self, peer: str) -> int:
        """Charge a rejection to `peer`; returns how many it has in the window."""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._rejections.setdefault(peer, []).append(now)
            return len(self._rejections[peer])

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for peer in list(self._rejections):
            kept = [when for when in self._rejections[peer] if when >= cutoff]
            if kept:
                self._rejections[peer] = kept
            else:
                del self._rejections[peer]


class MLLPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    # Set by make_mllp_server. Annotated rather than assigned so the class
    # carries no shared mutable default across servers.
    message_handler: object
    recv_timeout: float
    max_frame_bytes: int
    desync_grace: float
    connection_deadline: float
    first_frame_timeout: float
    max_rejections_per_peer: int
    rejection_window: float
    peers: _PeerLedger
    peer_registry: PeerRegistry
    tls_context: ssl.SSLContext | None
    tls_handshake_timeout: float

    def get_request(self):
        """Accept, and authenticate the transport before anything else sees it.

        socketserver calls this on the accept thread and catches `OSError` from
        it, treating the connection as never having happened -- which is exactly
        the disposal a failed handshake wants, and `ssl.SSLError` is an
        `OSError`. A client with no certificate, or one signed by a CA the site
        did not name, never becomes a request.

        The timeout is set before the wrap and cleared after it. A handshake is
        the one thing this class does on the accept thread, so an unbounded one
        is a single stalled client holding up every other accept; the connection
        deadline and the idle allowance take over from here.
        """
        request, client_address = super().get_request()
        if self.tls_context is None:
            return request, client_address
        request.settimeout(self.tls_handshake_timeout)
        try:
            secured = self.tls_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            logger.warning(
                "TLS handshake with %s failed (%s); the connection is dropped unread. A "
                "peer drawing this repeatedly is either misconfigured or has no certificate "
                "this listener accepts.", client_address[0], exc,
            )
            request.close()
            raise
        secured.settimeout(None)
        return secured, client_address

    def verify_request(self, request, client_address) -> bool:
        """Refuse a connection before it costs a thread.

        socketserver spawns the connection thread in `process_request`, which
        runs only if this returns True, so this is the one place a connection
        can be refused without first paying for the resource it was opened to
        consume. The slot taken here is returned in `MLLPRequestHandler.handle`.

        Under the plaintext opt-in this is also where the source-address
        allowlist is enforced, and it is enforced *first*: a source this
        listener will never serve must not be able to spend a connection slot,
        which is the whole difference between an allowlist and a log line.
        Under mTLS the certificate has already been checked by `get_request`;
        which peer it *is* is settled on the connection thread.
        """
        peer = client_address[0]
        if not self.peer_registry.requires_tls and not self.peer_registry.allows_address(peer):
            logger.error(
                "Refusing a plaintext connection from %s: it is not on the source-address "
                "allowlist. Alert: this listener is running without transport "
                "authentication and something outside the allowlist is speaking to it.",
                peer,
            )
            return False
        if self.peers.admit(peer):
            return True
        logger.warning(
            "Refusing a connection from %s: it is over its connection or its recent "
            "rejection budget. A peer that keeps drawing this is either misconfigured "
            "or not an interface engine.", peer,
        )
        return False


class MLLPRequestHandler(socketserver.BaseRequestHandler):
    """One connection. Reassembles frames and answers each one exactly once."""

    def handle(self) -> None:
        server: MLLPServer = self.server  # type: ignore[assignment]
        try:
            peer = self._resolve(server)
            if peer is None:
                return
            self._serve(server, peer)
        finally:
            # Pairs with the slot taken in verify_request, which is the only
            # path that reaches here. A cap whose slots are not returned shrinks
            # to zero over an afternoon of ordinary traffic -- an outage
            # arriving by way of the fix for one.
            server.peers.release(self.client_address[0])

    def _resolve(self, server: MLLPServer) -> PeerIdentity | None:
        """Which peer this connection is, or None and it is closed unread.

        Chaining to the site's CA got the client this far; it does not say which
        peer it is. That is the registry's answer, and it is a different
        question deliberately -- a CA that can be persuaded to sign one more
        certificate would otherwise be a CA that can mint interface engines.

        Nothing is acknowledged on the way out. A refused connection has read
        nothing, so a legitimate sender whose certificate was rotated without
        the registry being updated still holds its message and redelivers it
        once somebody fixes the configuration.
        """
        address = self.client_address[0]
        registry = server.peer_registry
        if registry.requires_tls:
            certificate = self.request.getpeercert(binary_form=True)
            peer = registry.resolve_certificate(certificate)
        else:
            peer = registry.resolve_address(address)
        if peer is None:
            logger.error(
                "Closing a connection from %s: it presented no credential this registry "
                "maps to a peer. Alert: the certificate is trusted by the configured CA but "
                "is not one of the pinned interface engines, or its peer entry has been "
                "removed. Nothing was read and nothing was acknowledged.", address,
            )
            return None
        logger.info("Connection from %s authenticated as %s", address, peer.peer_id)
        return peer

    def _serve(self, server: MLLPServer, peer: PeerIdentity) -> None:
        handler = server.message_handler
        buffer = b""
        # The frame accepted most recently on this connection. If the stream
        # turns out to be desynchronised, that frame is the one most likely to
        # have been half a message. See the module docstring.
        last_accepted = ""
        opened = time.monotonic()
        # When the frame currently being assembled first had bytes, or None
        # when nothing is part-read. Reset by every completed frame, so this
        # times "no complete frame since", not "connection age".
        frame_started: float | None = None

        while True:
            budget = self._budget(server, opened, frame_started)
            if budget <= 0:
                self._close_expired(server, opened, frame_started, buffer)
                return
            # The idle allowance and the two lifetime bounds are different
            # questions and the socket can only be asked one of them at a time,
            # so it is asked whichever expires first. Set each iteration
            # because the budget shrinks and the allowance does not.
            self.request.settimeout(min(server.recv_timeout, budget))
            try:
                chunk = self.request.recv(RECV_BYTES)
            except socket.timeout:
                if self._budget(server, opened, frame_started) <= 0:
                    self._close_expired(server, opened, frame_started, buffer)
                else:
                    logger.warning(
                        "Connection idle for %ss; closing with %d unacknowledged byte(s) "
                        "discarded", server.recv_timeout, len(buffer),
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
            if frame_started is None:
                frame_started = time.monotonic()
            if len(buffer) > server.max_frame_bytes:
                self._reject(server, peer, handler.reject_malformed(
                    buffer[: server.max_frame_bytes],
                    f"no end block within {server.max_frame_bytes} bytes",
                    peer=peer,
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
                    self._reject(server, peer, handler.reject_malformed(
                        buffer, "bytes after an end block do not begin a new frame",
                        peer=peer,
                    ))
                    return
                buffer = remainder
                ack, intact = self._ack_for(handler, candidate, peer)
                if not intact:
                    # ANY framing rejection immediately after an accepted frame
                    # is the same evidence, not just check 4's. The `CR FS CR`
                    # case lands here rather than there: the truncated half is
                    # accepted, and the remainder then arrives as its own
                    # apparent frame and fails check 2. Flagging only on check 4
                    # missed exactly the case the flag exists for.
                    self._flag(handler, last_accepted)
                    self._reject(server, peer, ack)
                    return
                self._send(ack)
                # Only a frame the store actually took. The truncation alert
                # tells a human to review the archived raw for this control id,
                # and a frame answered AE has no archive row -- failing to write
                # it is what AE means -- so naming one sends somebody looking
                # for a message that was never stored. Cleared rather than left
                # at the previous value for the same reason: what the flag
                # claims is that the frame immediately before the bad boundary
                # may have been half a message.
                last_accepted = ""
                if ack_code(ack) == "AA":
                    last_accepted = peek_control_id(
                        candidate[len(VT):-2].decode("utf-8", errors="replace")
                    )
                # A completed frame restarts the incomplete-frame timer, so a
                # sender making progress is never closed by it and one that
                # never completes a frame always is.
                frame_started = time.monotonic() if buffer else None

    @staticmethod
    def _budget(server: MLLPServer, opened: float, frame_started: float | None) -> float:
        """Seconds this connection may still spend, by whichever bound is nearest."""
        now = time.monotonic()
        left = server.connection_deadline - (now - opened)
        if frame_started is not None:
            left = min(left, server.first_frame_timeout - (now - frame_started))
        return left

    def _close_expired(self, server: MLLPServer, opened: float,
                       frame_started: float | None, buffer: bytes) -> None:
        """Log which bound ran out. Nothing is acknowledged, so the engine still
        owns whatever was in flight and will redeliver it."""
        now = time.monotonic()
        if now - opened >= server.connection_deadline:
            reason = f"open for {now - opened:.1f}s against a {server.connection_deadline}s budget"
        else:
            reason = (
                f"no complete frame for {now - (frame_started or now):.1f}s against a "
                f"{server.first_frame_timeout}s allowance"
            )
        logger.warning(
            "Closing the connection from %s (%s); %d unacknowledged byte(s) discarded. A "
            "per-recv timeout does not bound a connection, and a peer that speaks just often "
            "enough to keep one fresh would hold a thread indefinitely.",
            self.client_address[0], reason, len(buffer),
        )

    def _reject(self, server: MLLPServer, peer: PeerIdentity, ack: str) -> None:
        """Answer a rejection and charge it to the source address.

        Every caller returns immediately afterwards, so the connection ends
        here whether the rejection was a framing one or an application-level
        one. The charge is what makes reconnecting to draw another cost
        something; `verify_request` is where it is spent.

        Charged to the address, not to `peer.peer_id`, for the reason the module
        docstring gives: the budget bounds what an unauthenticated client can
        consume, and an unauthenticated client has no identity to charge. The
        identity is named in the alert, because an operator reading "eight
        rejections from 10.2.0.7" and an operator reading "eight rejections from
        example-lab" are looking for different things.
        """
        address = self.client_address[0]
        count = server.peers.note_rejection(address)
        self._send(ack)
        if count >= server.max_rejections_per_peer:
            logger.error(
                "%s (peer %s) has drawn %d rejection(s) within %ss and is over budget; "
                "further connections from that address are refused until the window passes. "
                "Alert: this is either a badly misconfigured sender or a peer probing the "
                "listener.",
                address, peer.peer_id, count, server.rejection_window,
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
    def _ack_for(handler, candidate: bytes, peer: PeerIdentity) -> tuple[str, bool]:
        """(ACK to send, whether the stream may be read on)."""
        body = candidate[len(VT):-2] if candidate.startswith(VT) else b""
        if VT in body:
            return handler.reject_malformed(
                candidate, "a start block appears inside the message body", peer=peer,
            ), False
        if not body.endswith(CR):
            return handler.reject_malformed(
                candidate,
                "the last segment is not CR-terminated, so this frame ends somewhere the "
                "sender did not put an end block -- most likely an embedded FS CR splitting "
                "one message into two",
                peer=peer,
            ), False
        try:
            text = deframe(candidate)
        except FramingError as exc:
            return handler.reject_malformed(candidate, str(exc), peer=peer), False
        ack = handler.handle(text, peer=peer)
        # An application-level AR desynchronises nothing, so this frame could
        # be followed by another -- and while it was, one connection could loop
        # malformed frames indefinitely, each one costing a SHA-256, a decode
        # and an fsynced INSERT, with the content-addressed archive defeated by
        # varying a single byte. Framing rejections have always closed the
        # connection; this makes the cost of a rejection the same either way.
        return ack, ack_code(ack) != "AR"

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
    peers: PeerRegistry,
    tls_handshake_timeout: float = TLS_HANDSHAKE_SECONDS,
    recv_timeout: float = RECV_TIMEOUT_SECONDS,
    max_frame_bytes: int = MAX_FRAME_BYTES,
    desync_grace: float = DESYNC_GRACE_SECONDS,
    max_connections: int = MAX_CONNECTIONS,
    max_connections_per_peer: int = MAX_CONNECTIONS_PER_PEER,
    connection_deadline: float = CONNECTION_DEADLINE_SECONDS,
    first_frame_timeout: float = FIRST_FRAME_SECONDS,
    max_rejections_per_peer: int = MAX_REJECTIONS_PER_PEER,
    rejection_window: float = REJECTION_WINDOW_SECONDS,
) -> MLLPServer:
    """Bind loopback by default. v1 makes no outbound connection to anyone, us included.

    A non-loopback bind is warned about rather than refused: binding is ingress,
    not egress, and a real interface engine lives on another host -- but a v1
    pilot that did not mean to expose a PHI-bearing port should hear about it in
    its own log rather than find out from someone else's scan.

    `peers` is **required**, and that is the shape of "mutual TLS by default":
    there is no default, so a listener that authenticates nothing cannot be
    reached by leaving an argument out. A caller that wants one asks for
    `PeerRegistry.plaintext_loopback()` by name, or writes `allow_plaintext`
    into a registry file, and either way `describe()` says so at WARNING here on
    every start -- the log line an operator sees is the one they can act on
    months after the configuration decision was made.
    """
    if host not in _LOOPBACK:
        logger.warning(
            "MLLP listener binding non-loopback address %r. v1 is specified as a local "
            "listener; make sure this port is deliberately reachable and firewalled.", host,
        )
    # Built before the bind: TLS material that cannot be loaded should refuse
    # the listener rather than leave a port open that fails every handshake.
    tls_context = peers.tls_context()
    logger.log(
        logging.INFO if peers.requires_tls else logging.WARNING, "%s", peers.describe()
    )
    server = MLLPServer((host, port), MLLPRequestHandler)
    server.peer_registry = peers
    server.tls_context = tls_context
    server.tls_handshake_timeout = tls_handshake_timeout
    server.message_handler = handler
    server.recv_timeout = recv_timeout
    server.max_frame_bytes = max_frame_bytes
    server.desync_grace = desync_grace
    server.connection_deadline = connection_deadline
    server.first_frame_timeout = first_frame_timeout
    server.max_rejections_per_peer = max_rejections_per_peer
    server.rejection_window = rejection_window
    server.peers = _PeerLedger(
        max_total=max_connections,
        max_per_peer=max_connections_per_peer,
        max_rejections=max_rejections_per_peer,
        window=rejection_window,
    )
    return server
