"""Who is on the other end of the wire, and what they are allowed to assert.

Every other module in this subsystem answers "what does this message mean".
This one answers the question that has to be settled first: **whose message is
it**. Before it existed the answer came out of the message -- `MSH-10` keyed the
archive and the idempotency table, `MSH-3`/`MSH-4` were read by nothing, and a
peer that could reach the port could claim any control id, retire any patient
identifier and cancel any loop. A self-asserted origin is not an origin.

Three things live here, and they are together because they are one decision:

  * **`PeerIdentity`** -- a peer id, the organisation behind it, the authorities
    it holds, and optionally what it says about itself in `MSH-3`/`MSH-4` so the
    claim can be checked against the identity rather than believed.
  * **`PeerRegistry`** -- the mapping from a credential to one of those, plus
    the transport policy that decides what counts as a credential at all.
  * The TLS context the listener wraps its sockets in, built from the same file,
    because "which CA may sign a client certificate" and "which certificate is
    which peer" are two halves of one configuration and splitting them across
    two files is how they drift apart.

**A certificate is identified by the SHA-256 of its DER encoding, not by its
subject.** A subject distinguished name is chosen by whoever asks the CA for the
certificate and is reissued -- unchanged -- on every renewal, so pinning it
means any certificate the CA can be persuaded to sign with that CN becomes the
peer. The CA already decides *whether* a client may connect; the fingerprint is
what decides *which peer it is*, and those must not be the same decision. The
cost is that a renewal is a configuration change, which is why a peer may list
several fingerprints: a rotation is the window in which both are present, and it
closes when the old one is removed rather than whenever a certificate expires.

**Authorities are granted, never inferred.** Three of them, and the line
between what needs one and what does not is a single question: can this message
end with a clinically open loop no longer on anybody's queue?

  * `merge` -- `ADT^A40` relinks two charts and re-points every future message
    for the retired identifier. Nothing else in the subsystem changes who a
    record is about.
  * `cancel` -- `SIU^S15` puts a loop in `CANCELLED`, which appears in neither
    `open_loops()` nor `resulted_unacknowledged()`, so a clinically open loop
    leaves every coordinator queue while still waiting on a result.
  * `result` -- `ORU^R01` with `OBX-11 = F` moves a loop to `RESULTED` and makes
    it acknowledgeable. This one was argued the other way first, and the
    argument was wrong. A result looked additive: it puts something *on* a
    queue, it is visible, and `undo_match` reverses it with a label. But the
    reversal depends on somebody noticing, and the thing that happens next is a
    coordinator acknowledging it -- at which point the loop closes and the
    patient's genuinely pending finding reads as handled. The record leaves the
    queue exactly as it does under `cancel`; it just goes through a human who
    has no way to tell. A consequence one step further away is still the
    consequence.

Orders and schedules carry no authority. Both are strictly additive -- an order
opens a loop, a schedule attaches an appointment to one that is already open --
and neither can retire anything. A peer that may send those *is* the clinical
feed, and gating them would be a line in every registry entry that never refused
anything.

The cost of the three is one line per peer in a configuration file, and what it
buys is that a scheduling feed cannot result, a results feed cannot cancel, and
neither can merge. That is the whole of least privilege here.

**Plaintext is an opt-out and is built to look like one.** `PeerRegistry`
refuses to be constructed for a plaintext listener unless the configuration says
`allow_plaintext` *and* names the addresses it will accept -- two independent
statements, so neither is reachable by a typo in the other -- and
`make_mllp_server` logs `describe()` at WARNING on every start. Under that mode
an allowlisted source address is the identity: weaker than a certificate, and
still enough to scope idempotency, which is what actually stops one peer
spending another's control ids.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

from .errors import ReferralLoopError

logger = logging.getLogger(__name__)

# Authorities, by name. A set rather than flags on the identity so an unknown
# one in a config file is a refused boot rather than a silently ignored line.
MERGE = "merge"
CANCEL = "cancel"
RESULT = "result"
AUTHORITIES = frozenset({MERGE, CANCEL, RESULT})

TRANSPORT_MTLS = "mtls"
TRANSPORT_PLAINTEXT = "plaintext"
TRANSPORT_IN_PROCESS = "in-process"
TRANSPORTS = (TRANSPORT_MTLS, TRANSPORT_PLAINTEXT)

# Peer ids this module hands out itself. A configuration file may not reuse one:
# `unattributed` in particular is the scope every row written before transport
# authentication existed was migrated into, and a peer able to write there could
# claim control ids on behalf of the past.
UNATTRIBUTED = "unattributed"
LOCAL = "local"
FILEDROP = "filedrop"
PLAINTEXT_LOOPBACK = "plaintext-loopback"
# Not a peer and never resolved from a connection: the value `loop_events`
# carries when no message asserted the transition at all -- a coordinator
# acknowledging, dismissing, attaching or undoing from the worklist. It exists
# so `assertion_source` is always present. Absent-meaning-human and
# absent-meaning-an-ingest-path-forgot are indistinguishable in an append-only
# log, and a provenance field with two meanings for its own absence is not one.
COORDINATOR = "coordinator"
RESERVED_PEER_IDS = frozenset(
    {UNATTRIBUTED, LOCAL, FILEDROP, PLAINTEXT_LOOPBACK, COORDINATOR}
)

# A peer id is written into two database columns, into every log line about the
# peer, and into an audit row's resource_id. Constrained here so none of those
# has to sanitize it separately.
_PEER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ORGANIZATION = 128

# Loopback in the spellings a client can arrive by. `plaintext_loopback()` is
# the demo and development posture and nothing else.
_LOOPBACK_ADDRESSES = ("127.0.0.1", "::1")

# How long a client has to finish its TLS handshake. socketserver runs
# `get_request` on the accept thread, so an unbounded handshake there is a
# single connection holding up every other accept -- the same resource argument
# the connection ledger makes one level down, at the one point that runs before
# the ledger can see anything.
TLS_HANDSHAKE_SECONDS = 10.0

# TLS 1.2 is the floor. Older versions are not a compatibility question for a
# link that is being configured from scratch on both ends.
_MINIMUM_TLS = ssl.TLSVersion.TLSv1_2


@dataclass(frozen=True)
class PeerIdentity:
    """One authenticated sender. Frozen: an identity that could be edited after
    resolution would be a capability handed to whatever holds a reference."""

    peer_id: str
    organization: str = ""
    transport: str = TRANSPORT_IN_PROCESS
    authorities: frozenset[str] = frozenset()
    # What this peer says about itself in MSH-3 and MSH-4. Empty means the site
    # has not written it down, and an empty expectation is not an expectation of
    # emptiness -- see `claim_mismatch`.
    sending_application: str = ""
    sending_facility: str = ""

    def holds(self, authority: str) -> bool:
        return authority in self.authorities

    def claim_mismatch(self, application: str, facility: str) -> str:
        """Which MSH field contradicts this identity, or "" when none does.

        Only fields the registry declares are checked. A site that has not
        recorded what its engine puts in `MSH-4` gets transport authentication
        without a second gate it cannot yet fill in; a site that has recorded it
        gets a message claiming another facility refused, including one that
        claims nothing -- an absent claim does not satisfy a declared
        expectation, or omitting the field would be the way around the check.

        Compared on the first component, upper-cased and stripped: `MSH-4` is
        commonly `HOSP^1.2.3^ISO`, and an assigning authority appearing or
        disappearing is a sender's encoding choice rather than a change of
        identity. Case is not identity either.
        """
        for label, expected, claimed in (
            ("MSH-3", self.sending_application, application),
            ("MSH-4", self.sending_facility, facility),
        ):
            if not expected:
                continue
            if claimed.strip().upper() != expected.strip().upper():
                return label
        return ""


# The identity of a caller that is already inside the process. Every authority,
# and deliberately: the boundary this module defends is the transport, and code
# holding a `MessageHandler` can call `Registry.merge_patient` directly. A check
# that an in-process caller can step around by using a different method is not a
# control, it is a comment.
LOCAL_PEER = PeerIdentity(
    peer_id=LOCAL, organization="in-process caller", transport=TRANSPORT_IN_PROCESS,
    authorities=AUTHORITIES,
)

# A file in the drop directory. Same reasoning, one step further out: writing
# one requires write access to the PHI volume, which is strictly more privilege
# than any MLLP peer has. Its own id rather than LOCAL's so the archive can tell
# a replayed file from a call.
FILEDROP_PEER = PeerIdentity(
    peer_id=FILEDROP, organization="local file drop", transport=TRANSPORT_IN_PROCESS,
    authorities=AUTHORITIES,
)

# The single identity every connection gets under `plaintext_loopback()`. It
# holds every authority, and that is the honest encoding of what the mode means:
# anything that can reach loopback on this host can also write to the drop
# directory and to the database file, so withholding an authority from it would
# describe a boundary that is not there.
PLAINTEXT_LOOPBACK_PEER = PeerIdentity(
    peer_id=PLAINTEXT_LOOPBACK, organization="unauthenticated loopback",
    transport=TRANSPORT_PLAINTEXT, authorities=AUTHORITIES,
)


@dataclass(frozen=True)
class TLSFiles:
    """Paths, not loaded material. Read once by `tls_context()`."""

    certfile: str
    keyfile: str
    client_ca_file: str


def _refuse(message: str) -> ReferralLoopError:
    return ReferralLoopError(f"Peer registry: {message}")


def _text(value: object, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise _refuse(f"{what} must be a string, not {type(value).__name__}")
    if len(value) > limit:
        raise _refuse(f"{what} is longer than {limit} characters")
    return value.strip()


def _address(value: object) -> str:
    """Normalise a source address, so `::ffff:127.0.0.1` and `127.0.0.1` are one
    entry rather than two that disagree."""
    raw = _text(value, "an address", 64)
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise _refuse(f"{raw!r} is not an IP address ({exc})") from exc
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return str(parsed)


def _identity_from(entry: object, *, transport: str) -> tuple[PeerIdentity, list[str], list[str]]:
    """One config entry -> an identity, its fingerprints and its addresses."""
    if not isinstance(entry, dict):
        raise _refuse(f"every peer must be an object, not {type(entry).__name__}")

    peer_id = _text(entry.get("peer_id", ""), "peer_id", 64)
    if not _PEER_ID_RE.match(peer_id):
        raise _refuse(
            f"peer_id {peer_id!r} must be 1-64 characters of lowercase letters, digits, "
            "'.', '_' or '-', starting with a letter or digit. It is written to the "
            "archive, to the audit trail and to every log line about this peer."
        )
    if peer_id in RESERVED_PEER_IDS:
        raise _refuse(
            f"peer_id {peer_id!r} is reserved for identities this subsystem issues itself "
            f"({', '.join(sorted(RESERVED_PEER_IDS))}); choose another"
        )

    granted = entry.get("authorities", [])
    if not isinstance(granted, list):
        raise _refuse(f"{peer_id}: authorities must be a list")
    unknown = sorted({str(a) for a in granted} - AUTHORITIES)
    if unknown:
        # Fail closed. An authority nobody implements, silently ignored, reads
        # in the config file exactly like one that was granted.
        raise _refuse(
            f"{peer_id}: unknown authorit(ies) {unknown}; this build grants only "
            f"{sorted(AUTHORITIES)}"
        )

    fingerprints = entry.get("certificate_sha256", [])
    if not isinstance(fingerprints, list):
        raise _refuse(f"{peer_id}: certificate_sha256 must be a list")
    cleaned: list[str] = []
    for value in fingerprints:
        digest = _text(value, f"{peer_id}: a fingerprint", 128).replace(":", "").lower()
        if not _FINGERPRINT_RE.match(digest):
            raise _refuse(
                f"{peer_id}: {value!r} is not a SHA-256 fingerprint (64 hex characters, "
                "colons optional)"
            )
        cleaned.append(digest)

    addresses = entry.get("addresses", [])
    if not isinstance(addresses, list):
        raise _refuse(f"{peer_id}: addresses must be a list")

    if transport == TRANSPORT_MTLS and not cleaned:
        raise _refuse(
            f"{peer_id}: an mTLS registry entry must pin at least one certificate "
            "fingerprint, or nothing maps a connection to this peer"
        )

    identity = PeerIdentity(
        peer_id=peer_id,
        organization=_text(entry.get("organization", ""), f"{peer_id}: organization",
                           _MAX_ORGANIZATION),
        transport=transport,
        authorities=frozenset(str(a) for a in granted),
        sending_application=_text(entry.get("sending_application", ""),
                                  f"{peer_id}: sending_application", 64),
        sending_facility=_text(entry.get("sending_facility", ""),
                               f"{peer_id}: sending_facility", 64),
    )
    return identity, cleaned, [_address(a) for a in addresses]


class PeerRegistry:
    """The transport policy and the credential-to-identity map, as one object.

    Constructed through `from_mapping`, `load_peer_registry` or
    `plaintext_loopback`; the initialiser takes already-validated pieces so
    every path into it has been through the same refusals.
    """

    def __init__(
        self,
        *,
        transport: str,
        identities: dict[str, PeerIdentity],
        by_fingerprint: dict[str, str],
        by_address: dict[str, str],
        tls: TLSFiles | None,
    ):
        self.transport = transport
        self._identities = dict(identities)
        self._by_fingerprint = dict(by_fingerprint)
        self._by_address = dict(by_address)
        self.tls = tls

    # ------------------------------------------------------------- constructors

    @classmethod
    def from_mapping(cls, data: object) -> "PeerRegistry":
        if not isinstance(data, dict):
            raise _refuse(f"the registry must be an object, not {type(data).__name__}")

        transport = _text(data.get("transport", TRANSPORT_MTLS), "transport", 32)
        if transport not in TRANSPORTS:
            raise _refuse(f"transport must be one of {list(TRANSPORTS)}, not {transport!r}")

        entries = data.get("peers", [])
        if not isinstance(entries, list) or not entries:
            raise _refuse("at least one peer must be declared; a listener nobody may reach "
                          "is a listener that will be turned off rather than configured")

        identities: dict[str, PeerIdentity] = {}
        by_fingerprint: dict[str, str] = {}
        by_address: dict[str, str] = {}
        for entry in entries:
            identity, fingerprints, addresses = _identity_from(entry, transport=transport)
            if identity.peer_id in identities:
                raise _refuse(f"peer_id {identity.peer_id!r} is declared twice")
            identities[identity.peer_id] = identity
            for digest in fingerprints:
                if digest in by_fingerprint:
                    raise _refuse(
                        f"certificate fingerprint {digest[:16]}... is claimed by both "
                        f"{by_fingerprint[digest]!r} and {identity.peer_id!r}; one "
                        "certificate is one peer"
                    )
                by_fingerprint[digest] = identity.peer_id
            for address in addresses:
                if address in by_address:
                    raise _refuse(
                        f"address {address} is claimed by both {by_address[address]!r} and "
                        f"{identity.peer_id!r}; one address is one peer"
                    )
                by_address[address] = identity.peer_id

        tls = None
        if transport == TRANSPORT_MTLS:
            tls = cls._tls_files(data.get("tls"))
        else:
            if data.get("allow_plaintext") is not True:
                raise _refuse(
                    "a plaintext listener requires \"allow_plaintext\": true. PHI arrives on "
                    "this port and nothing on it is authenticated or encrypted; the flag is "
                    "the site saying so out loud."
                )
            if not by_address:
                raise _refuse(
                    "a plaintext listener requires at least one peer with an address "
                    "allowlist. Without it the opt-in would accept every source on the "
                    "network, which is the state this whole control exists to end."
                )
            unused = sorted(by_fingerprint.values())
            if unused:
                logger.warning(
                    "Peer registry is in plaintext mode, so the certificate fingerprint(s) "
                    "declared for %s are not checked and those peers are identified by "
                    "source address alone.", unused,
                )
        return cls(transport=transport, identities=identities, by_fingerprint=by_fingerprint,
                   by_address=by_address, tls=tls)

    @staticmethod
    def _tls_files(block: object) -> TLSFiles:
        if not isinstance(block, dict):
            raise _refuse(
                "an mTLS registry needs a \"tls\" object naming certfile, keyfile and "
                "client_ca_file. Set \"transport\": \"plaintext\" to opt out of transport "
                "authentication deliberately."
            )
        files = {}
        for name in ("certfile", "keyfile", "client_ca_file"):
            value = _text(block.get(name, ""), f"tls.{name}", 4096)
            if not value:
                raise _refuse(f"tls.{name} is required")
            if not Path(value).is_file():
                raise _refuse(f"tls.{name} is {value!r}, which is not a readable file")
            files[name] = value
        return TLSFiles(**files)

    @classmethod
    def plaintext_loopback(cls) -> "PeerRegistry":
        """The development and demo posture: no TLS, loopback only, one identity.

        Named rather than defaulted. It is reached by typing it at a call site
        or by passing `--allow-plaintext` on the command line, never by omitting
        an argument, and `describe()` says what it is on every start.
        """
        return cls(
            transport=TRANSPORT_PLAINTEXT,
            identities={PLAINTEXT_LOOPBACK: PLAINTEXT_LOOPBACK_PEER},
            by_fingerprint={},
            by_address={a: PLAINTEXT_LOOPBACK for a in map(_address, _LOOPBACK_ADDRESSES)},
            tls=None,
        )

    # ---------------------------------------------------------------- resolution

    @property
    def requires_tls(self) -> bool:
        return self.transport == TRANSPORT_MTLS

    def resolve_certificate(self, der: bytes | None) -> PeerIdentity | None:
        """The peer holding this certificate, or None.

        None covers three cases that are one answer: no certificate was
        presented, the bytes are not one this registry pins, or the peer id it
        pins to has gone. A caller may not distinguish them, because the
        response to all three is to close the connection without reading.
        """
        if not der:
            return None
        digest = hashlib.sha256(der).hexdigest()
        peer_id = self._by_fingerprint.get(digest)
        return self._identities.get(peer_id) if peer_id else None

    def resolve_address(self, host: str) -> PeerIdentity | None:
        """The peer allowlisted at this source address, or None.

        Only consulted under the plaintext opt-in. An address is not an
        identity -- a NAT shares one between senders and a multi-homed sender
        has several -- but it is enough to scope idempotency, and scoped
        idempotency is what stops one peer spending another's control ids.
        """
        try:
            normalised = _address(host)
        except ReferralLoopError:
            return None
        peer_id = self._by_address.get(normalised)
        return self._identities.get(peer_id) if peer_id else None

    def allows_address(self, host: str) -> bool:
        """Whether a plaintext connection from `host` may be accepted at all.

        Separate from `resolve_address` because it is asked in `verify_request`,
        before a thread exists, and answering it there is what keeps a source
        this listener will never serve from costing one.
        """
        return self.resolve_address(host) is not None

    # ------------------------------------------------------------------ transport

    def tls_context(self) -> ssl.SSLContext | None:
        """The server context, or None under the plaintext opt-in.

        `CERT_REQUIRED` against `client_ca_file` alone, which is the point: the
        default truststore would let every public CA mint a peer for this
        listener, and the pinned fingerprints below it would then be the only
        thing between a domain-validated certificate and a patient merge.
        """
        if self.tls is None:
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = _MINIMUM_TLS
        context.verify_mode = ssl.CERT_REQUIRED
        try:
            context.load_cert_chain(self.tls.certfile, self.tls.keyfile)
            context.load_verify_locations(cafile=self.tls.client_ca_file)
        except (ssl.SSLError, OSError, ValueError) as exc:
            raise _refuse(
                f"the TLS material could not be loaded ({exc}); check that certfile and "
                "keyfile are a matching PEM pair and that client_ca_file holds the CA that "
                "signs your interface engine's certificates"
            ) from exc
        return context

    def describe(self) -> str:
        """One line for the startup log. Never contains a path or a key."""
        who = ", ".join(sorted(self._identities))
        if self.requires_tls:
            return (
                f"MLLP transport: mutual TLS, client certificates required and pinned by "
                f"SHA-256. {len(self._identities)} peer(s): {who}."
            )
        return (
            f"MLLP transport: PLAINTEXT. Traffic on this port is not authenticated and not "
            f"encrypted, and PHI crosses it. Accepting {len(self._by_address)} allowlisted "
            f"source address(es) for {len(self._identities)} peer(s): {who}. "
            "This is an explicit opt-out; remove allow_plaintext to require mutual TLS."
        )

    @property
    def peer_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._identities))


def is_peer_id(value: object) -> bool:
    """Whether `value` is a well-formed peer id.

    Exported so `audit.py` can normalise a resource id against the same pattern
    a registry validates against, rather than restating it -- two copies of this
    rule would eventually disagree about what may be written into an audit row.
    """
    return isinstance(value, str) and bool(_PEER_ID_RE.match(value))


def load_peer_registry(path: Path | str) -> PeerRegistry:
    """Read a registry from JSON, or refuse legibly.

    Deliberately not a second signed-pack mechanism. The rule pack is signed
    because it changes how messages are *matched* and ships from us; this file
    says who the site's own interface engines are, is written by the site, and
    lives beside the database on the same volume the encryption gate attests. A
    signature we could not verify against anything a site controls would be
    ceremony.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _refuse(
            f"no peer registry at {path} ({exc}). It maps each interface engine's client "
            "certificate to an identity and the authorities it holds; listen mode cannot "
            "authenticate anything without it."
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _refuse(f"{path} is not valid JSON ({exc})") from exc
    return PeerRegistry.from_mapping(data)


__all__ = [
    "AUTHORITIES",
    "CANCEL",
    "COORDINATOR",
    "FILEDROP",
    "FILEDROP_PEER",
    "LOCAL",
    "LOCAL_PEER",
    "MERGE",
    "RESULT",
    "PLAINTEXT_LOOPBACK",
    "PLAINTEXT_LOOPBACK_PEER",
    "RESERVED_PEER_IDS",
    "TLS_HANDSHAKE_SECONDS",
    "TRANSPORT_MTLS",
    "TRANSPORT_PLAINTEXT",
    "UNATTRIBUTED",
    "PeerIdentity",
    "PeerRegistry",
    "TLSFiles",
    "is_peer_id",
    "load_peer_registry",
]
