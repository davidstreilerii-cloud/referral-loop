"""Preflight, and the reason it makes two proofs rather than one."""

from __future__ import annotations

import pytest

from referral_loop.connect.connectors import ConnectorRegistry
from referral_loop.connect.preflight import format_report, preflight

from ._certs import localhost_cert, rsa_keypair
from ._fhirserver import ServerBehaviour, fhir_server


def _registry(base_url: str, key_path, ca_file, versions=("4.0.1",)) -> ConnectorRegistry:
    return ConnectorRegistry.from_mapping(
        {
            "connectors": [
                {
                    "connector_id": "example-med",
                    "organization": "Example Medical Center",
                    "vendor": "epic",
                    "fhir_base_url": base_url,
                    "token_url": f"{base_url}/oauth2/token",
                    "fhir_version": list(versions),
                    "auth": {
                        "mode": "smart-backend-services",
                        "client_id": "client-abc",
                        "private_key_file": str(key_path),
                        "key_id": "k1",
                        "algorithm": "RS384",
                        "scopes": ["system/Patient.read"],
                    },
                    "authorities": [],
                    "tls": {"ca_file": str(ca_file)},
                }
            ]
        }
    )


@pytest.fixture
def certs(tmp_path):
    """(certfile, keyfile, ca_file) for a server answering to `localhost`."""
    return localhost_cert(tmp_path)


def test_both_proofs_pass_against_a_healthy_server(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _behaviour):
        reports = preflight(_registry(base, key_path, ca_file))
    assert len(reports) == 1
    assert reports[0].reach.ok
    assert reports[0].credential.ok
    assert reports[0].ok


def test_a_wrong_fhir_version_fails_reach_only(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile, ServerBehaviour(fhir_version="3.0.2")) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert not reports[0].reach.ok
    assert "3.0.2" in reports[0].reach.detail
    assert reports[0].credential.ok, "a version mismatch must not be reported as a credential failure"


def test_an_invalid_client_fails_the_credential_proof_but_not_reach(certs, tmp_path):
    """This is the case the two-proof design exists for. /metadata is unauthenticated on Epic,
    so a single-request preflight would report this connector healthy."""
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(token_status=400, token_body={"error": "invalid_client"})
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert reports[0].reach.ok, "the server is reachable and its version is fine"
    assert not reports[0].credential.ok
    assert "invalid_client" in reports[0].credential.detail
    assert not reports[0].ok


def test_a_redirecting_metadata_endpoint_fails_rather_than_being_followed(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(redirect_metadata_to="https://elsewhere.example/metadata")
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert not reports[0].reach.ok
    assert "redirect" in reports[0].reach.detail.lower()


def test_a_redirecting_token_endpoint_does_not_leak_the_assertion(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(redirect_token_to="https://elsewhere.example/token")
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert not reports[0].credential.ok
    assert "redirect" in reports[0].credential.detail.lower()


def test_every_connector_is_checked_even_after_one_fails(certs, tmp_path):
    """One run should give the whole picture. The common case during setup is several
    connectors wrong in different ways."""
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        data = _registry(base, key_path, ca_file).connectors[0]
        registry = ConnectorRegistry.from_mapping(
            {
                "connectors": [
                    _as_dict(data, "example-med", base, key_path, ca_file),
                    _as_dict(data, "second-site", "https://unreachable.invalid", key_path, ca_file),
                ]
            }
        )
        reports = preflight(registry)
    assert [r.connector_id for r in reports] == ["example-med", "second-site"]
    assert reports[0].ok
    assert not reports[1].ok


def _as_dict(profile, connector_id, base, key_path, ca_file) -> dict:
    return {
        "connector_id": connector_id,
        "organization": profile.organization,
        "vendor": profile.vendor,
        "fhir_base_url": base,
        "token_url": f"{base}/oauth2/token",
        "fhir_version": list(profile.fhir_version),
        "auth": {
            "mode": "smart-backend-services",
            "client_id": profile.auth.client_id,
            "private_key_file": str(key_path),
            "key_id": profile.auth.key_id,
            "algorithm": profile.auth.algorithm,
            "scopes": list(profile.auth.scopes),
        },
        "authorities": [],
        "tls": {"ca_file": str(ca_file)},
    }


def test_the_report_shows_the_two_proofs_on_separate_lines(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        text = format_report(preflight(_registry(base, key_path, ca_file)))
    assert "reach" in text
    assert "credential" in text
    assert "example-med" in text


def test_the_bearer_token_never_appears_in_the_report(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        text = format_report(preflight(_registry(base, key_path, ca_file)))
    assert "test-token" not in text
