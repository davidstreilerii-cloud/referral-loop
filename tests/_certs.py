"""Certificate and key generation for tests that need a real TLS handshake.

Extracted from test_peer_identity.py when the connector preflight tests needed the same thing.
One generator rather than two: two would drift, and a divergence between the certificates the
inbound tests use and the ones the outbound tests use is exactly the kind of difference that
makes a failure look environmental. tests/_pack.py is the existing precedent for this shape.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

SYNTHETIC = "SYNTHETIC-TEST-ONLY-DO-NOT-TRUST"


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


def rsa_keypair(tmp_path, name: str = "signing"):
    """Returns (private_key_path, public_key_object). 2048 bits -- these are test keys and
    generation time shows up in every test that calls this."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / f"{name}.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path, key.public_key()
