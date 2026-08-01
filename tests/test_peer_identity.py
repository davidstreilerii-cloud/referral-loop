"""Defect C2: the transport had no identity, so nothing was bound to a peer.

Before this file existed, `client_address`, `getpeername` and `ssl` appeared in
the package only as a connection *budget* -- a count of what an address was
holding, never a claim about who it was. Everything recorded as a message's
origin came out of the message: `MSH-10` keyed the archive and the dedup table,
`MSH-3`/`MSH-4` were never read at all, and `loop_events` carried no actor on
the ingest path. A peer that could reach the port could therefore assert
anything about any patient, and the four tests at the top of this file are the
four ways that mattered.

Every certificate here is generated inside the fixture, at test time, into a
temporary directory, and every subject carries `SYNTHETIC-TEST-ONLY-DO-NOT-TRUST`
in its organization name. No key material is committed, and none of these
certificates chains to anything real.
"""
from __future__ import annotations

import datetime as dt
import json
import socket
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from healthcare_rag.referral_loop import audit
from healthcare_rag.referral_loop.errors import ReferralLoopError
from healthcare_rag.referral_loop.events import LoopState
from healthcare_rag.referral_loop.listener import FileDropSource, MessageHandler
from healthcare_rag.referral_loop.mllp import CR, FS, frame
from healthcare_rag.referral_loop.mllp_server import make_mllp_server
from healthcare_rag.referral_loop.peers import (
    CANCEL,
    MERGE,
    UNATTRIBUTED,
    PeerIdentity,
    PeerRegistry,
    load_peer_registry,
)
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore
from tests.referral_loop.test_listener import (
    MRN,
    RESULTED_AT,
    SURVIVING_MRN,
    merge,
    message,
    msh,
    order,
    pid,
    result,
    scheduling,
)
from tests.referral_loop.test_matcher import PACK

SYNTHETIC = "SYNTHETIC-TEST-ONLY-DO-NOT-TRUST"

# The peer ids the registry file below hands out. `ris` is the clinical feed;
# `lab` is a second authenticated peer holding no destructive authority, which
# is the shape every exploit in this file is measured against -- an attacker who
# has got *a* certificate, not one who has got the engine's.
RIS = "example-ris"
LAB = "example-lab"


# ------------------------------------------------------------------ synthetic PKI


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _subject(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, SYNTHETIC),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _window():
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return now - dt.timedelta(days=1), now + dt.timedelta(days=1)


def _self_signed(common_name: str):
    key = _key()
    start, end = _window()
    cert = (
        x509.CertificateBuilder()
        .subject_name(_subject(common_name))
        .issuer_name(_subject(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _issued(ca_key, ca_cert, common_name: str, *, san=()):
    key = _key()
    start, end = _window()
    builder = (
        x509.CertificateBuilder()
        .subject_name(_subject(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(san)), critical=False)
    return key, builder.sign(ca_key, hashes.SHA256())


@dataclass(frozen=True)
class Material:
    """One certificate on disk, plus the fingerprint a registry would pin."""

    cert: Path
    key: Path
    fingerprint: str


def _write(directory: Path, stem: str, key, cert) -> Material:
    cert_path = directory / f"{stem}.synthetic.crt"
    key_path = directory / f"{stem}.synthetic.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return Material(cert_path, key_path, cert.fingerprint(hashes.SHA256()).hex())


class Pki:
    """A throwaway CA, a server certificate, and four client certificates."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        ca_key, ca_cert = _self_signed(f"{SYNTHETIC} root")
        self.ca = _write(directory, "ca", ca_key, ca_cert)

        stranger_key, stranger_cert = _self_signed(f"{SYNTHETIC} other root")
        self.other_ca = _write(directory, "other-ca", stranger_key, stranger_cert)

        self.server = _write(
            directory,
            "server",
            *_issued(ca_key, ca_cert, "localhost", san=(x509.DNSName("localhost"),)),
        )
        self.ris = _write(directory, "ris", *_issued(ca_key, ca_cert, "example-ris"))
        self.lab = _write(directory, "lab", *_issued(ca_key, ca_cert, "example-lab"))
        self.unregistered = _write(
            directory, "unregistered", *_issued(ca_key, ca_cert, "nobody-in-particular")
        )
        self.foreign = _write(
            directory,
            "foreign",
            *_issued(stranger_key, stranger_cert, "example-ris"),
        )


@pytest.fixture(scope="session")
def pki(tmp_path_factory) -> Pki:
    return Pki(tmp_path_factory.mktemp("synthetic-pki"))


# ------------------------------------------------------------------- registries


def _registry_mapping(pki: Pki) -> dict:
    return {
        "transport": "mtls",
        "tls": {
            "certfile": str(pki.server.cert),
            "keyfile": str(pki.server.key),
            "client_ca_file": str(pki.ca.cert),
        },
        "peers": [
            {
                "peer_id": RIS,
                "organization": "Example Radiology",
                "certificate_sha256": [pki.ris.fingerprint],
                "sending_application": "EHR",
                "sending_facility": "HOSP",
                "authorities": [MERGE, CANCEL],
            },
            {
                "peer_id": LAB,
                "organization": "Example Labs",
                "certificate_sha256": [pki.lab.fingerprint],
                "authorities": [],
            },
        ],
    }


@pytest.fixture()
def peers(pki) -> PeerRegistry:
    return PeerRegistry.from_mapping(_registry_mapping(pki))


@pytest.fixture()
def store(tmp_path):
    return LoopStore(tmp_path / "loops.db")


@pytest.fixture()
def handler(store):
    return MessageHandler(store=store, registry=Registry(store), pack=PACK)


# ----------------------------------------------------------------- wire helpers


@contextmanager
def serving(handler, peers: PeerRegistry, **kwargs):
    server = make_mllp_server(handler, host="127.0.0.1", port=0, peers=peers, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def client_context(pki: Pki, material: Material | None, *, ca: Material | None = None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(str((ca or pki.ca).cert))
    if material is not None:
        context.load_cert_chain(str(material.cert), str(material.key))
    return context


def _read_ack(sock) -> str:
    sock.settimeout(15)
    buffer = b""
    while not buffer.endswith(FS + CR):
        try:
            chunk = sock.recv(4096)
        except (OSError, socket.timeout):
            break
        if not chunk:
            break
        buffer += chunk
    return buffer.decode("utf-8", errors="replace")


def deliver(address, text: str, context=None) -> str:
    """One message on its own connection; returns whatever came back."""
    raw = socket.create_connection(address, timeout=15)
    try:
        sock = raw if context is None else context.wrap_socket(raw, server_hostname="localhost")
    except (ssl.SSLError, OSError):
        raw.close()
        raise
    try:
        sock.sendall(frame(text))
        return _read_ack(sock)
    finally:
        sock.close()


def refused(address, text: str, context=None) -> bool:
    """Whether the listener declined to accept this message.

    Both spellings count. Under TLS 1.3 a client finishes its own handshake
    before the server has validated its certificate, so a refused client may see
    the failure as an exception on the next read *or* as a closed connection
    with no ACK on it -- which of the two arrives depends on the protocol
    version and the platform. The property under test is the server's, and it is
    the same either way: nothing was acknowledged.
    """
    try:
        return "|AA|" not in deliver(address, text, context)
    except (ssl.SSLError, OSError):
        return True


def loops_of(handler):
    return [loop for loop in handler.store.all_loops() if loop.loop_id.startswith("L-")]


def unknown_type_message(control_id: str) -> str:
    """Well formed, parses, means nothing to this listener.

    `QRY^A19` is outside `parse_hl7.KNOWN_MESSAGE_TYPES`, so `_process` marks it
    applied and answers AA without a transition. That is the cheapest way to
    plant a control id: no order numbers, no patient, nothing that could fail a
    later check.
    """
    return message(msh("QRY^A19", control_id, RESULTED_AT), pid(MRN))


# =========================================================== exploit 1: pre-claim


def test_a_second_peer_cannot_pre_claim_the_control_id_of_a_real_result(handler, pki, peers):
    """The one that destroys a clinical result and leaves an INFO line.

    Interface engines number `MSH-10` sequentially or from a template, so the
    next control id a real feed will use is guessable. Before this fix,
    `applied_messages.control_id` was a global primary key: anybody who could
    reach the port could insert the id first with a message this listener does
    not act on, and the genuine `ORU^R01` that followed was counted as a
    duplicate, answered `AA`, and never applied. The engine is told its result
    was delivered; no loop moves; the only trace is
    "Duplicate MSH-10 ...: no-op" at INFO, which is indistinguishable from the
    ordinary chatter of an engine replaying its outbound queue.

    Delivered over two mutually authenticated connections, because the fix is
    that the two connections are two *peers*: the attacker's claim lands in its
    own scope and the engine's control ids are untouched by it.
    """
    engine = client_context(pki, pki.ris)
    attacker = client_context(pki, pki.lab)

    with serving(handler, peers) as address:
        assert "|AA|" in deliver(address, order(control_id="ORM_1"), engine)
        assert loops_of(handler)[0].state is LoopState.OPEN

        # The attacker guesses the control id the engine will stamp on the
        # result and spends it on a message that changes nothing.
        assert "|AA|" in deliver(address, unknown_type_message("ORU_1"), attacker)

        assert "|AA|" in deliver(address, result(control_id="ORU_1"), engine)

    assert loops_of(handler)[0].state is LoopState.RESULTED, (
        "a peer that never sent the order was able to consume the control id the "
        "real result would arrive under, and the result was discarded as a duplicate"
    )
    assert handler.duplicate_control_id_count == 0


def test_a_second_peer_cannot_pre_claim_the_content_key_of_a_real_result(store):
    """The same attack through the other dedup path.

    `content_key` exists because engines re-stamp `MSH-10` on retry, so the
    listener also refuses content it has already applied. That check had a
    single global unique index behind it, which made it the second way to spend
    a message that has not arrived yet. Asserted at the store, because the
    index is the thing under test: an in-process check that agreed with a
    database that did not would still lose the race between two processes.
    """
    assert store.record_applied("A", "shared-content", "ORU^R01", peer_id=LAB) is True
    assert store.record_applied("B", "shared-content", "ORU^R01", peer_id=RIS) is True

    assert store.content_key_owner("shared-content", peer_id=RIS) == "B"
    assert store.content_key_owner("shared-content", peer_id="somebody-else") is None


def test_one_peer_still_cannot_repeat_its_own_control_id_or_content(store):
    """Scoping dedup must not switch it off. Same peer, same key, refused."""
    assert store.record_applied("CTRL", "content", "ORU^R01", peer_id=RIS) is True
    assert store.record_applied("CTRL", None, "ORU^R01", peer_id=RIS) is False
    assert store.record_applied("OTHER", "content", "ORU^R01", peer_id=RIS) is False
    assert store.control_id_applied("CTRL", peer_id=RIS) is True
    assert store.control_id_applied("CTRL", peer_id=LAB) is False


# ======================================================= exploit 2: patient merge


def test_adt_a40_from_a_peer_without_merge_authority_changes_nothing(handler, pki, peers):
    """A merge silently re-points every future message for one chart at another.

    `_apply_merge` writes an `mrn_aliases` row, and from then on every message
    naming the retired identifier resolves to the survivor -- including the
    loops that already existed, which are carried across. It is the highest
    value message in the subsystem and it was reachable by anyone who could
    open a socket. Now it needs an authority the peer registry grants by name.
    """
    lab = client_context(pki, pki.lab)

    with serving(handler, peers) as address:
        ack = deliver(address, merge("A40_1", prior=MRN, surviving=SURVIVING_MRN), lab)

    assert "|AA|" in ack, "well formed but refused is AA; the engine must not retry it forever"
    assert handler.store.alias_count() == 0, "an unauthorized peer merged two charts"
    assert handler.unauthorized_merge_count == 1


def test_adt_a40_from_the_peer_that_holds_merge_authority_still_works(handler, pki, peers):
    """The authority is a gate, not a wall: the clinical feed still merges."""
    engine = client_context(pki, pki.ris)

    with serving(handler, peers) as address:
        assert "|AA|" in deliver(
            address, merge("A40_1", prior=MRN, surviving=SURVIVING_MRN), engine
        )

    assert handler.store.resolve_mrn(MRN) == SURVIVING_MRN
    assert handler.unauthorized_merge_count == 0


def test_a_refused_merge_is_written_to_the_immutable_audit_trail(handler, pki, peers):
    """An attempted identity merge is exactly what an auditor asks about later.

    The refusal has to outlive the process, and `loop_events` is the wrong place
    for it -- nothing was merged, so there is no loop to hang it on.
    """
    lab = client_context(pki, pki.lab)
    with serving(handler, peers) as address:
        deliver(address, merge("A40_1", prior=MRN, surviving=SURVIVING_MRN), lab)

    rows = [r for r in audit.referral_audit_entries() if r["resource_type"] == "referral_peer"]
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "denied"
    assert LAB in rows[0]["resource_id"]
    assert MRN not in json.dumps(rows[0]), "no identifier may reach the audit row"


# ==================================================== exploit 3: mass cancellation


def test_siu_s15_from_a_peer_without_cancel_authority_leaves_the_loop_open(handler, pki, peers):
    """`CANCELLED` appears in neither `open_loops()` nor
    `resulted_unacknowledged()`, so a cancellation removes a clinically open
    loop from every coordinator queue while it is still waiting on a result.
    A results-only feed has no business emitting one."""
    engine = client_context(pki, pki.ris)
    lab = client_context(pki, pki.lab)

    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)
        ack = deliver(address, scheduling("S15_1", "SIU^S15", placer="PLACER987"), lab)

    assert "|AA|" in ack
    assert loops_of(handler)[0].state is LoopState.OPEN
    assert handler.unauthorized_cancel_count == 1


def test_siu_s15_from_the_peer_that_holds_cancel_authority_still_cancels(handler, pki, peers):
    engine = client_context(pki, pki.ris)

    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)
        deliver(address, scheduling("S15_1", "SIU^S15", placer="PLACER987"), engine)

    assert loops_of(handler)[0].state is LoopState.CANCELLED


# ======================================================== exploit 4: forged result


def test_an_unregistered_certificate_never_gets_to_send_a_message(handler, pki, peers):
    """A certificate the CA signed is not an identity this listener knows.

    The client authenticates -- the handshake completes, because the CA is the
    one in the truststore -- and is then refused at the registry, before a byte
    of HL7 is read. Chaining to the CA is necessary and not sufficient.
    """
    engine = client_context(pki, pki.ris)
    stranger = client_context(pki, pki.unregistered)

    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)
        assert refused(address, result(control_id="ORU_1", obx11="F"), stranger)

    assert loops_of(handler)[0].state is LoopState.OPEN
    assert handler.store.applied_count() == 1, "only the engine's order was applied"


def test_a_certificate_from_another_ca_cannot_complete_the_handshake(handler, pki, peers):
    """Same subject, same common name, different issuer. CERT_REQUIRED against
    a pinned CA file is what makes the fingerprint pin meaningful: without it an
    attacker would only need to mint a certificate naming itself."""
    forged = client_context(pki, pki.foreign, ca=pki.ca)
    forged.load_cert_chain(str(pki.foreign.cert), str(pki.foreign.key))

    with serving(handler, peers) as address:
        assert refused(address, order(control_id="ORM_1"), forged)

    assert handler.store.raw_count() == 0


def test_a_client_offering_no_certificate_is_refused_at_the_handshake(handler, pki, peers,
                                                                      caplog):
    """`verify_mode = CERT_REQUIRED`, and refused by TLS rather than by the registry.

    Asserting only "no AA" would pass with `CERT_NONE` set, because the registry
    then resolves no peer and closes the connection anyway -- measured, not
    assumed: relaxing `verify_mode` left this test green until it started
    reading the log. Which layer refuses is the whole point. `get_request` runs
    on the accept thread and drops the socket before `verify_request`, before a
    connection slot, and before a thread; a registry refusal has already paid
    for all three.
    """
    anonymous = client_context(pki, None)

    with caplog.at_level("INFO"):
        with serving(handler, peers) as address:
            assert refused(address, order(control_id="ORM_1"), anonymous)

    assert "TLS handshake with 127.0.0.1 failed" in caplog.text
    assert "maps to a peer" not in caplog.text, (
        "the connection reached the peer registry, so it completed a handshake it "
        "should not have been able to complete"
    )
    assert handler.store.raw_count() == 0


def test_a_plaintext_client_gets_nothing_from_an_mtls_listener(handler, pki, peers):
    with serving(handler, peers) as address:
        raw = socket.create_connection(address, timeout=15)
        try:
            raw.sendall(frame(order(control_id="ORM_1")))
            assert "|AA|" not in _read_ack(raw)
        finally:
            raw.close()

    assert handler.store.raw_count() == 0


# ================================================= assertion source and MSH claims


def test_the_archive_and_the_event_name_the_authenticated_peer(handler, pki, peers):
    """`assertion_source` is the transport's answer, never the message's.

    Before this, the only recorded origin of a message was `MSH-10` -- twenty
    characters the sender chose. A row that says who asserted a thing is what
    makes every other control in this file auditable after the fact.
    """
    engine = client_context(pki, pki.ris)
    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)

    assert handler.store.assertion_sources() == [RIS]

    loop = loops_of(handler)[0]
    created = handler.store.events_for(loop.loop_id)[0]
    assert created.detail["assertion_source"] == RIS


def test_the_sending_facility_is_recorded_as_a_claim_beside_it(handler, pki, peers):
    """MSH-3 and MSH-4 are kept, and kept as *claims*: what the message said,
    stored next to who actually said it, so the two can be compared later."""
    engine = client_context(pki, pki.ris)
    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)

    created = handler.store.events_for(loops_of(handler)[0].loop_id)[0]
    assert created.detail["sending_application_claim"] == "EHR"
    assert created.detail["sending_facility_claim"] == "HOSP"


def test_a_sending_facility_that_contradicts_the_peer_is_refused(handler, pki, peers):
    """A self-asserted facility must never override the transport identity.

    The registry declares what `example-ris` sends. A message from that
    certificate claiming to be another hospital is either a misrouted feed or a
    peer reaching past its own scope, and neither may produce a transition.
    """
    engine = client_context(pki, pki.ris)
    forged = order(control_id="ORM_1").replace("EHR|HOSP", "EHR|SOMEWHERE_ELSE", 1)

    with serving(handler, peers) as address:
        ack = deliver(address, forged, engine)

    assert "|AA|" in ack
    assert loops_of(handler) == []
    assert handler.peer_claim_mismatch_count == 1


def test_a_peer_that_declares_no_facility_is_not_cross_checked(handler, pki, peers):
    """The check is opt-in per peer. A site that has not written down what its
    engine sends gets transport authentication without a second gate it cannot
    yet fill in -- an empty expectation must not become an expectation of
    emptiness."""
    lab = client_context(pki, pki.lab)
    with serving(handler, peers) as address:
        assert "|AA|" in deliver(address, order(control_id="ORM_1"), lab)

    assert len(loops_of(handler)) == 1
    assert handler.peer_claim_mismatch_count == 0


# ============================================================== transport policy


def test_a_server_cannot_be_built_without_a_transport_policy(handler):
    """mTLS by default means there is no default. `peers` is required, so a
    listener that authenticates nothing cannot be reached by omission."""
    with pytest.raises(TypeError):
        make_mllp_server(handler, host="127.0.0.1", port=0)


def test_plaintext_needs_the_flag_and_an_allowlist(pki):
    """Two independent things, so neither alone is enough."""
    with pytest.raises(ReferralLoopError, match="allow"):
        PeerRegistry.from_mapping(
            {
                "transport": "plaintext",
                "peers": [{"peer_id": "engine", "addresses": ["10.1.1.1"]}],
            }
        )
    with pytest.raises(ReferralLoopError, match="address"):
        PeerRegistry.from_mapping(
            {
                "transport": "plaintext",
                "allow_plaintext": True,
                "peers": [{"peer_id": "engine"}],
            }
        )


def test_a_plaintext_listener_refuses_an_address_it_was_not_given(handler, caplog):
    registry = PeerRegistry.from_mapping(
        {
            "transport": "plaintext",
            "allow_plaintext": True,
            "peers": [{"peer_id": "engine", "addresses": ["10.99.99.99"]}],
        }
    )
    with caplog.at_level("INFO"):
        with serving(handler, registry) as address:
            raw = socket.create_connection(address, timeout=15)
            try:
                raw.sendall(frame(order(control_id="ORM_1")))
                assert "|AA|" not in _read_ack(raw)
            finally:
                raw.close()

    assert handler.store.raw_count() == 0
    # And refused in `verify_request`, which is the half that matters: that runs
    # before `process_request`, so an address this listener will never serve
    # never costs it a connection slot or a thread. Asserting only "no AA" was
    # green with the allowlist check deleted, because the peer registry then
    # declined it one layer later -- after both had already been spent.
    assert "not on the source-address allowlist" in caplog.text
    assert "maps to a peer" not in caplog.text


def test_the_plaintext_opt_in_warns_every_time_a_listener_starts(handler, caplog):
    registry = PeerRegistry.plaintext_loopback()
    with caplog.at_level("WARNING"):
        with serving(handler, registry) as address:
            assert "|AA|" in deliver(address, order(control_id="ORM_1"))

    warnings = "\n".join(
        record.getMessage() for record in caplog.records if record.levelname == "WARNING"
    )
    assert "plaintext" in warnings.lower()
    assert "not authenticated" in warnings.lower()


def test_the_loopback_opt_in_attributes_messages_to_one_named_peer(handler):
    registry = PeerRegistry.plaintext_loopback()
    with serving(handler, registry) as address:
        deliver(address, order(control_id="ORM_1"))

    assert handler.store.assertion_sources() == ["plaintext-loopback"]


# ============================================================ registry validation


def test_the_registry_refuses_an_authority_it_does_not_define(pki):
    mapping = _registry_mapping(pki)
    mapping["peers"][1]["authorities"] = ["merge", "rewrite-history"]
    with pytest.raises(ReferralLoopError, match="rewrite-history"):
        PeerRegistry.from_mapping(mapping)


def test_the_registry_refuses_two_peers_sharing_a_certificate(pki):
    mapping = _registry_mapping(pki)
    mapping["peers"][1]["certificate_sha256"] = [pki.ris.fingerprint]
    with pytest.raises(ReferralLoopError, match="fingerprint"):
        PeerRegistry.from_mapping(mapping)


def test_the_registry_refuses_a_reserved_peer_id(pki):
    mapping = _registry_mapping(pki)
    mapping["peers"][1]["peer_id"] = UNATTRIBUTED
    with pytest.raises(ReferralLoopError, match="reserved"):
        PeerRegistry.from_mapping(mapping)


def test_a_registry_file_round_trips(tmp_path, pki):
    path = tmp_path / "peers.json"
    path.write_text(json.dumps(_registry_mapping(pki)), encoding="utf-8")
    registry = load_peer_registry(path)
    assert registry.resolve_certificate(_der(pki.ris)).peer_id == RIS
    assert registry.resolve_certificate(_der(pki.unregistered)) is None


def _der(material: Material) -> bytes:
    return x509.load_pem_x509_certificate(material.cert.read_bytes()).public_bytes(
        serialization.Encoding.DER
    )


def test_a_missing_registry_file_is_a_legible_refusal(tmp_path):
    with pytest.raises(ReferralLoopError, match="peer registry"):
        load_peer_registry(tmp_path / "nope.json")


# ============================================================== other ingest paths


def test_a_file_drop_message_is_attributed_to_the_file_drop_peer(handler, tmp_path):
    """A drop directory is not a network peer and must not borrow one's name.

    It is inside the trust boundary -- writing a file there needs write access
    to the PHI volume -- so it carries every authority, and it says so under its
    own identifier rather than under a peer that could also arrive on a socket.
    """
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "a.hl7").write_text(order(control_id="ORM_1"), encoding="utf-8")

    assert FileDropSource(handler, drop).drain() == 1
    assert handler.store.assertion_sources() == ["filedrop"]


def test_an_in_process_call_is_attributed_to_the_in_process_peer(handler):
    handler.handle(order(control_id="ORM_1"))
    assert handler.store.assertion_sources() == ["local"]


# ================================================================ existing databases


def test_a_database_written_before_this_change_still_opens_and_dedups(tmp_path):
    """Sites have these files already, and the archive is append-only.

    The legacy rows keep deduping -- reads consult the peer's own scope and the
    `unattributed` scope both -- so an upgrade cannot cause a message that was
    already applied to be applied a second time. Nothing writes to that scope
    afterwards, so it cannot be a way back into the pre-claim.
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE raw_messages (
            control_id TEXT PRIMARY KEY, payload TEXT NOT NULL, received_at TEXT NOT NULL);
        CREATE TABLE applied_messages (
            control_id TEXT PRIMARY KEY, content_key TEXT,
            message_type TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL);
        CREATE UNIQUE INDEX idx_applied_content_key
            ON applied_messages(content_key) WHERE content_key IS NOT NULL;
        INSERT INTO raw_messages VALUES ('OLD_1', 'MSH|...', '2026-01-01T00:00:00+00:00');
        INSERT INTO applied_messages VALUES ('OLD_1', 'old-content', 'ORU^R01',
                                             '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = LoopStore(path)

    assert store.raw_count() == 1
    assert store.assertion_sources() == [UNATTRIBUTED]
    assert store.control_id_applied("OLD_1", peer_id=RIS) is True
    assert store.content_key_owner("old-content", peer_id=RIS) == "OLD_1"
    assert store.record_applied("NEW_1", "new-content", "ORU^R01", peer_id=RIS) is True


def test_an_upgraded_database_keeps_its_append_only_triggers(tmp_path):
    """The migration rebuilds two append-only tables. A rebuild that dropped
    the guards would leave the archive editable by any connection -- the exact
    hole the schema triggers exist to close, opened by the fix for another one."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE raw_messages (
            control_id TEXT PRIMARY KEY, payload TEXT NOT NULL, received_at TEXT NOT NULL);
        CREATE TABLE applied_messages (
            control_id TEXT PRIMARY KEY, content_key TEXT,
            message_type TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL);
        INSERT INTO raw_messages VALUES ('OLD_1', 'MSH|...', '2026-01-01T00:00:00+00:00');
        INSERT INTO applied_messages VALUES ('OLD_1', 'old-content', 'ORU^R01',
                                             '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    LoopStore(path)

    foreign = sqlite3.connect(path)
    try:
        for statement in (
            "DELETE FROM raw_messages",
            "UPDATE raw_messages SET payload = 'x'",
            "DELETE FROM applied_messages",
            "UPDATE applied_messages SET content_key = 'x'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                foreign.execute(statement)
    finally:
        foreign.close()


# =================================================================== peer identity


def test_a_peer_holds_only_the_authorities_it_was_granted():
    identity = PeerIdentity(peer_id="p", authorities=frozenset({MERGE}))
    assert identity.holds(MERGE) is True
    assert identity.holds(CANCEL) is False


def test_a_declared_expectation_is_not_satisfied_by_an_absent_claim():
    """A message omitting MSH-4 must not pass a peer that declares one; "absent"
    is not "matching"."""
    identity = PeerIdentity(peer_id="p", sending_facility="HOSP")
    assert identity.claim_mismatch("ANY", "HOSP") == ""
    assert identity.claim_mismatch("ANY", "") == "MSH-4"
    assert identity.claim_mismatch("ANY", "hosp") == "", "case is not identity"
