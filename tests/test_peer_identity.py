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

import contextlib
import datetime as dt
import json
import socket
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from healthcare_rag.referral_loop import audit
from healthcare_rag.referral_loop.errors import ReferralLoopError, StoreUnavailableError
from healthcare_rag.referral_loop.events import LoopState
from healthcare_rag.referral_loop.listener import FileDropSource, MessageHandler
from healthcare_rag.referral_loop.mllp import CR, FS, frame
from healthcare_rag.referral_loop.mllp_server import make_mllp_server
from healthcare_rag.referral_loop.peers import (
    CANCEL,
    MERGE,
    PLAINTEXT_LOOPBACK_PEER,
    RESULT,
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

REPO_ROOT = Path(__file__).resolve().parents[2]

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
                "authorities": [MERGE, CANCEL, RESULT],
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


@contextmanager
def caplog_at(handler, level):
    """Collect log records from the server thread for the duration of a block.

    A plain `caplog` fixture works, but these tests need the records from a
    bounded window rather than from the whole test, and the server logs from
    its own threads.
    """
    import logging

    collected: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            collected.append(record.getMessage())

    sink = _Sink(level=getattr(logging, level))
    root = logging.getLogger("healthcare_rag.referral_loop")
    previous = root.level
    root.addHandler(sink)
    root.setLevel(getattr(logging, level))
    try:
        yield collected
    finally:
        root.removeHandler(sink)
        root.setLevel(previous)


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


def test_a_certificate_from_another_ca_is_refused_at_the_handshake(handler, pki, peers,
                                                                   caplog):
    """Same subject, same common name, different issuer -- refused by the CA check.

    The log assertion is the test. Asserting only "no AA" was green when the
    reviewer added the foreign root to `load_verify_locations`, because the
    fingerprint pin declined it one layer later: the test passed while the claim
    in its own docstring was false. Which layer refuses is the difference
    between "this listener trusts one CA" and "this listener trusts any CA and
    then checks a list", and only the first of those is what `CERT_REQUIRED`
    against a pinned file buys.
    """
    forged = client_context(pki, pki.foreign, ca=pki.ca)
    forged.load_cert_chain(str(pki.foreign.cert), str(pki.foreign.key))

    with caplog.at_level("INFO"):
        with serving(handler, peers) as address:
            assert refused(address, order(control_id="ORM_1"), forged)

    assert "TLS handshake with 127.0.0.1 failed" in caplog.text
    assert "maps to a peer" not in caplog.text, (
        "the certificate completed a handshake and was declined by the fingerprint pin "
        "instead, so this listener is trusting an issuer it was never given"
    )
    assert handler.store.raw_count() == 0


def test_the_server_trusts_exactly_the_configured_ca_and_nothing_else(pki, peers):
    """The other half of the same claim, and the half no wire test can show here.

    A behavioural test would need a certificate that verifies under the
    *system* truststore, and this fixture cannot mint one -- both of its roots
    are synthetic and neither is installed anywhere. So the claim "pinned client
    CA only, not the system truststore" is asserted against the context object
    directly: the set of CAs it will accept must be exactly the one file the
    registry named.

    Not a tautology, and it fails on the mutation that matters: an
    `SSLContext` that had also called `load_default_certs()` -- the one-line
    change that would silently make every public CA able to mint an interface
    engine for this listener -- returns hundreds of certificates here instead of
    one.
    """
    trusted = peers.tls_context().get_ca_certs()

    assert len(trusted) == 1, (
        f"the listener trusts {len(trusted)} certificate authorities; it was configured "
        "with one, and every extra one is an issuer that can mint a peer"
    )
    subject = dict(pair for rdn in trusted[0]["subject"] for pair in rdn)
    assert subject["organizationName"] == SYNTHETIC
    assert subject["commonName"] == f"{SYNTHETIC} root"


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


def test_a_registered_peer_without_result_authority_cannot_result_another_feeds_loop(
    handler, pki, peers,
):
    """Exploit 4, closed rather than narrowed.

    The first version of this fix left `ORU^R01` ungated, on the reasoning that
    a result is additive and reversible from the worklist. Measured, that left
    any peer holding any certificate able to move another feed's loop to
    RESULTED with `OBX-11 = F`. The reversibility argument does not survive the
    next step: a RESULTED loop is acknowledgeable, a coordinator acknowledges
    it, and the loop closes over a finding that never arrived -- the record
    leaves the queue exactly as it does under a cancellation, via a human who
    has no way to tell.
    """
    engine = client_context(pki, pki.ris)
    lab = client_context(pki, pki.lab)

    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)
        ack = deliver(address, result(control_id="ORU_1", obx11="F"), lab)

    assert "|AA|" in ack
    assert loops_of(handler)[0].state is LoopState.OPEN, (
        "a peer granted no authorities resulted a loop belonging to another feed"
    )
    assert handler.unauthorized_result_count == 1


def test_the_feed_that_holds_result_authority_still_results_its_own_loops(handler, pki,
                                                                          peers):
    """The gate is least privilege, not a wall. The clinical feed still works."""
    engine = client_context(pki, pki.ris)

    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)
        deliver(address, result(control_id="ORU_1", obx11="F"), engine)

    assert loops_of(handler)[0].state is LoopState.RESULTED
    assert handler.unauthorized_result_count == 0


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


def test_every_event_says_who_asserted_it_including_the_ones_no_message_did(handler, pki,
                                                                            peers):
    """`assertion_source` is present on every event, never merely usually.

    A coordinator's acknowledgement carries the reserved `coordinator` rather
    than no key at all. Written down because the alternative gives the field's
    absence two meanings -- "a human did this" and "an ingest path forgot to
    attribute it" -- which are indistinguishable afterwards in an append-only
    log, and the second is the failure this whole change is about.
    """
    engine = client_context(pki, pki.ris)
    with serving(handler, peers) as address:
        deliver(address, order(control_id="ORM_1"), engine)
        deliver(address, result(control_id="ORU_1", obx11="F"), engine)

    loop_id = loops_of(handler)[0].loop_id
    handler.registry.acknowledge(loop_id, actor="A. Coordinator", role="rn",
                                 control_id="ACK_1")

    events = handler.store.events_for(loop_id)
    sources = {e.event_type: e.detail.get("assertion_source") for e in events}
    assert sources == {"created": RIS, "resulted": RIS, "acknowledged": "coordinator"}
    assert all("assertion_source" in e.detail for e in events)


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


def test_the_loopback_opt_in_grants_every_authority_and_says_so(handler, pki):
    """The documented cost of opting out, asserted so it stays documented.

    `PLAINTEXT_LOOPBACK_PEER` holds merge, cancel and result, so exploits 2, 3
    and 4 are open to anything that can reach loopback under
    `--allow-plaintext`. That is the honest encoding -- such a caller can also
    write the drop directory and the database file -- but it is the posture
    every `test_boot_gates` listen test runs under, so it is written down here
    rather than left to be rediscovered.
    """
    assert PLAINTEXT_LOOPBACK_PEER.authorities == frozenset({MERGE, CANCEL, RESULT})

    registry = PeerRegistry.plaintext_loopback()
    with serving(handler, registry) as address:
        assert "|AA|" in deliver(
            address, merge("A40_1", prior=MRN, surviving=SURVIVING_MRN)
        )
    assert handler.store.resolve_mrn(MRN) == SURVIVING_MRN


def test_the_loopback_opt_in_attributes_messages_to_one_named_peer(handler):
    registry = PeerRegistry.plaintext_loopback()
    with serving(handler, registry) as address:
        deliver(address, order(control_id="ORM_1"))

    assert handler.store.assertion_sources() == ["plaintext-loopback"]


def test_a_peer_repeating_a_refused_assertion_stops_being_written_down(handler, pki,
                                                                       peers):
    """An authenticated peer must not be able to make this listener write forever.

    A contradicted MSH-4 is answered AA and produces no transition, but it did
    produce an audit row and an ERROR line every time -- so a misconfigured or
    hostile feed that could accomplish nothing here could still drive unbounded
    writes into the audit database. It now draws on the same per-address budget
    a malformed frame does.

    The counter is deliberately *not* suppressed: it is how an operator sees
    that the suppression is happening, and a signal that goes quiet under load
    is the one that fails when it matters.
    """
    engine = client_context(pki, pki.ris)
    budget = 3

    with serving(handler, peers, max_rejections_per_peer=budget) as address:
        for index in range(budget + 4):
            forged = order(control_id=f"ORM_{index}").replace(
                "EHR|HOSP", "EHR|SOMEWHERE_ELSE", 1
            )
            assert "|AA|" in deliver(address, forged, engine)

    assert handler.peer_claim_mismatch_count == budget + 4, (
        "the count must survive the suppression; it is what shows it is happening"
    )
    rows = [r for r in audit.referral_audit_entries() if r["resource_type"] == "referral_peer"]
    assert len(rows) == budget, (
        f"{len(rows)} audit rows written against a budget of {budget}; an authenticated "
        "peer can still make this listener write without bound"
    )
    assert loops_of(handler) == []


# =========================================== the handshake is not on the accept path


def _silent_socket(address):
    """A client that connects and then says nothing at all.

    No ClientHello, no certificate, no identity -- the cheapest thing anybody
    can do to a TLS listener, and the whole of the attack this guards.
    """
    sock = socket.create_connection(address, timeout=15)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def test_a_silent_client_mid_handshake_does_not_delay_a_legitimate_delivery(handler, pki,
                                                                            peers):
    """The handshake must not run on the accept thread.

    `socketserver`'s accept loop is single-threaded. With the wrap in
    `get_request` -- which `socketserver` calls from that loop -- one client
    that connected and sent nothing held every other accept for the full
    handshake timeout. Measured at 4.75s against a 5s bound while a legitimate
    mTLS delivery waited: a bounded serial blocker is still a serial blocker,
    and at the 10s default an attacker cycling connections keeps the ingest
    port off the air indefinitely.

    Worse, the budgets that exist to bound exactly this could not see it.
    `verify_request` runs *after* `get_request`, so the connection cap, the
    per-address cap and the ledger slot were all spent after the cost they
    were added to bound.

    The timeout is injected short so the test is quick; the property is the
    ratio between the two numbers, not either one.
    """
    engine = client_context(pki, pki.ris)

    with serving(handler, peers, tls_handshake_timeout=4.0) as address:
        baseline = time.monotonic()
        assert "|AA|" in deliver(address, order(control_id="ORM_1"), engine)
        baseline = time.monotonic() - baseline

        stalled = _silent_socket(address)
        try:
            # Give the accept loop a moment to have taken it. Under the defect
            # this is the point at which the loop is already wedged.
            time.sleep(0.3)
            started = time.monotonic()
            # Distinct order numbers: two orders differing only in MSH-10 hash
            # to one content key and the second is correctly deduped, which
            # would make the loop count below assert nothing.
            assert "|AA|" in deliver(
                address,
                order(control_id="ORM_2", placer="PLACER222", filler="FILLER222"),
                engine,
            )
            blocked = time.monotonic() - started
        finally:
            stalled.close()

    assert blocked < 1.0, (
        f"a legitimate delivery took {blocked:.2f}s while one silent socket was "
        f"mid-handshake (an unobstructed delivery takes {baseline:.2f}s); the handshake "
        "is running on the accept thread and one client that sends nothing is an outage"
    )
    assert len(loops_of(handler)) == 2


def test_a_client_over_its_connection_cap_is_refused_before_any_tls_work(handler, pki,
                                                                        peers):
    """The budget has to be spent before the cost it bounds, not after it.

    `verify_request` runs before `process_request`, so with the handshake moved
    off the accept path a client at its per-address cap is refused having cost
    this listener no handshake at all. Asserted through the log rather than
    through timing: "refused at the cap" and "refused after a handshake" are
    the same outcome to the client and completely different resources here.

    Both clients are silent sockets, deliberately: neither reaches a handshake
    at all if the cap is doing its job, so offering a certificate would only
    obscure which of the two refused them.
    """
    with serving(handler, peers, max_connections_per_peer=1) as address:
        held = _silent_socket(address)
        try:
            time.sleep(0.3)
            with caplog_at(handler, "INFO") as records:
                second = _silent_socket(address)
                second.close()
                time.sleep(0.3)
        finally:
            held.close()

    text = chr(10).join(records)
    assert "connection cap" in text, text
    assert "TLS handshake" not in text, (
        "the second connection paid for a handshake before the cap that was supposed "
        "to refuse it: " + text
    )


def test_an_authenticated_connection_closes_the_socket_it_wrapped(handler, pki, peers,
                                                                  monkeypatch):
    """`wrap_socket` detaches the socket it wraps, so nobody else can close it.

    socketserver calls `shutdown_request` on the object it handed the handler.
    Wrapping on the connection thread means that object is the plain socket, and
    `wrap_socket` has already detached it -- fileno -1 -- so the framework's
    close is a no-op and the descriptor now belongs to the `SSLSocket` alone.

    **The reference this test holds is the experiment.** Left alone, CPython
    refcounts the handler away the moment it returns, `socket.__del__` closes
    the descriptor, and a process-wide handle count stays flat whether or not
    the code ever closes anything -- measured: deleting `_close_secured` passed
    a 25-connection handle-count test unchanged. That is not the property
    working, it is the garbage collector hiding its absence, and a PHI-bearing
    socket held open until a collector happens to run is not a design, it is a
    reprieve that a reference cycle from one traceback removes.

    So the socket is kept alive here and asked directly whether it was closed.
    """
    from healthcare_rag.referral_loop.mllp_server import MLLPRequestHandler

    wrapped = []
    original = MLLPRequestHandler._secure

    def capturing(self, server):
        secured = original(self, server)
        # After _secure, so self.request is the SSLSocket and not the plain
        # socket it replaced.
        wrapped.append(self.request)
        return secured

    monkeypatch.setattr(MLLPRequestHandler, "_secure", capturing)

    engine = client_context(pki, pki.ris)
    try:
        with serving(handler, peers) as address:
            deliver(address, order(control_id="ORM_1"), engine)
            time.sleep(0.3)

        assert len(wrapped) == 1, wrapped
        assert wrapped[0].fileno() == -1, (
            "the SSLSocket the handler wrapped is still open; socketserver cannot close "
            "it, because wrap_socket detached the object socketserver knows about, so "
            "every authenticated connection leaks a descriptor"
        )
    finally:
        for sock in wrapped:
            with contextlib.suppress(OSError):
                sock.close()


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


def _legacy_database(path, *, raw_rows=2) -> None:
    """A database in the shape this subsystem shipped before peer scoping."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE raw_messages (
            control_id TEXT PRIMARY KEY, payload TEXT NOT NULL, received_at TEXT NOT NULL);
        CREATE TABLE applied_messages (
            control_id TEXT PRIMARY KEY, content_key TEXT,
            message_type TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL);
        CREATE TRIGGER raw_messages_no_delete BEFORE DELETE ON raw_messages
        BEGIN SELECT RAISE(ABORT, 'raw_messages is append-only'); END;
        CREATE TRIGGER raw_messages_no_update BEFORE UPDATE ON raw_messages
        BEGIN SELECT RAISE(ABORT, 'raw_messages is append-only'); END;
        """
    )
    for index in range(raw_rows):
        conn.execute(
            "INSERT INTO raw_messages VALUES (?, ?, ?)",
            (f"OLD_{index}", f"MSH|archived clinical message {index}",
             "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


# The child process for the crash test. Opens the store -- which runs the
# migration -- and dies with `os._exit` partway through it, so no `finally`, no
# atexit hook and no SQLite cleanup runs. That is the distinction that matters:
# an in-process `raise` would let the connection close normally and roll back
# for reasons the real failure would not supply.
_CRASH_SCRIPT = """
import os, sqlite3, sys
sys.path.insert(0, {repo!r})

# sqlite3.Connection is an immutable type, so the seam is a connection factory
# rather than a patched method.
class Dying(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith({trigger!r}):
            os._exit(7)
        return super().execute(sql, *args, **kwargs)

_real_connect = sqlite3.connect
sqlite3.connect = lambda *a, **k: _real_connect(*a, **{{**k, "factory": Dying}})

from healthcare_rag.referral_loop.store import LoopStore
LoopStore({db!r})
print("SURVIVED")
"""


def _crash_during_migration(tmp_path, db_path, trigger_sql: str):
    import subprocess

    script = tmp_path / "crash.py"
    script.write_text(
        _CRASH_SCRIPT.format(repo=str(REPO_ROOT), db=str(db_path), trigger=trigger_sql),
        encoding="utf-8",
    )
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          timeout=120)


@pytest.mark.parametrize(
    "trigger_sql",
    ["ALTER TABLE RAW_MESSAGES", "INSERT INTO RAW_MESSAGES", "DROP TABLE RAW_MESSAGES__LEGACY"],
)
def test_a_process_killed_mid_migration_leaves_the_archive_intact(tmp_path, trigger_sql):
    """The migration rebuilds an append-only PHI archive. It must be atomic.

    Measured, not assumed: without an explicit transaction, `DROP TRIGGER`,
    `ALTER TABLE ... RENAME` and `CREATE TABLE` each committed on their own --
    Python's sqlite3 opens a transaction before DML, and none of those three is
    DML. A process killed after the copy and before the commit therefore left a
    durably renamed `raw_messages__legacy`, a durably created empty
    `raw_messages`, and a reopen that *succeeded*, reported `raw_count() == 0`
    and never looked at the aside table again. An append-only archive of
    clinical messages, emptied silently and permanently, with the operator's
    only signal a row count they have no baseline for.

    Three kill points, one per phase the rebuild passes through, because the
    interesting failure is not any single statement -- it is that the sequence
    has no boundary. The second one is the whole `INSERT ... SELECT` over the
    archive, which on a real site is the widest window of the three.
    """
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)

    proc = _crash_during_migration(tmp_path, db_path, trigger_sql)
    assert proc.returncode == 7, (
        f"the child was expected to die at {trigger_sql!r}; it said {proc.stdout!r} "
        f"{proc.stderr!r}"
    )

    # Before anything reopens it: the file on disk must still be a database
    # nothing can delete from. `DROP TRIGGER` is the first statement the rebuild
    # issues, and outside a transaction it committed on its own -- leaving a
    # durable window in which the archive carried no append-only guard at all.
    # That is the operation shape `_purge_guards` exists to refuse, arriving
    # through a migration instead of a purge.
    import sqlite3

    inspect = sqlite3.connect(db_path)
    try:
        armed = {name for (name,) in inspect.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    finally:
        inspect.close()
    assert {"raw_messages_no_delete", "raw_messages_no_update"} <= armed, (
        f"the crash left the archive without its append-only triggers: {sorted(armed)}"
    )

    # Reopening runs the migration again, from the top, on an unchanged database.
    store = LoopStore(db_path)
    assert store.raw_count() == 2, (
        "archived clinical messages did not survive a crash during the migration"
    )
    assert store.assertion_sources() == [UNATTRIBUTED]
    assert sorted(r["control_id"] for r in store._read(
        "SELECT control_id FROM raw_messages")) == ["OLD_0", "OLD_1"]


def test_a_stranded_legacy_table_refuses_the_boot_rather_than_reporting_an_empty_archive(
    tmp_path,
):
    """The last line of defence behind the transaction above.

    If a `*__legacy` table is ever on disk, some archive rows are in it and
    `_migrate` will not look: the live table now has the peer column, so the
    migration is skipped and the rows are invisible forever. Refusing the boot
    and naming the table is the difference between an operator who restores a
    backup and an operator who reads `raw_count() == 0` as a quiet week.
    """
    import sqlite3

    db_path = tmp_path / "stranded.db"
    LoopStore(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE raw_messages__legacy (control_id TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(StoreUnavailableError, match="raw_messages__legacy"):
        LoopStore(db_path)


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
