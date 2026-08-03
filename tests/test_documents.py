"""Resolving our patient at a remote, and the three answers that are not 'no documents'."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from referral_loop.connect.connectors import ConnectorRegistry
from referral_loop.connect.documents import (
    ConnectorCannotResolvePatients,
    PatientAmbiguousAtConnector,
    PatientNotFoundAtConnector,
    resolve_patient,
)

from ._certs import localhost_cert, rsa_keypair
from ._fhirserver import ServerBehaviour, bundle, fhir_server, patient

_SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _registry(base_url, key_path, ca_file, *, mrn_system="urn:oid:1.2.3"):
    connector = {
        "connector_id": "example-med",
        "organization": "Example Medical Center",
        "vendor": "epic",
        "fhir_base_url": base_url,
        "token_url": f"{base_url}/oauth2/token",
        "fhir_version": ["4.0.1"],
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
    if mrn_system is not None:
        connector["identifier_systems"] = {"mrn": mrn_system}
    return ConnectorRegistry.from_mapping({"connectors": [connector]})


@pytest.fixture
def certs(tmp_path):
    return localhost_cert(tmp_path)


def test_a_connector_with_no_declared_system_refuses_rather_than_returning_nothing(certs, tmp_path):
    """The load-bearing case. Returning an empty result here would tell a coordinator the
    specialist never documented anything, when in fact we never asked."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        registry = _registry(base, key_path, ca, mrn_system=None)
        with pytest.raises(ConnectorCannotResolvePatients, match="identifier_systems"):
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")


def test_one_match_resolves(certs, tmp_path):
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle(patient("p1"))}
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        assert resolve_patient(registry, registry.get("example-med"), mrn="MRN1") == "p1"


def test_no_match_is_not_the_same_as_no_documents(certs, tmp_path):
    """A patient unknown here and a patient with nothing filed are opposite facts about a
    referral. One says look elsewhere; the other says the specialist never documented."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle()}
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PatientNotFoundAtConnector, match="MRN1"):
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")


def test_two_matches_refuse_rather_than_pick(certs, tmp_path):
    """Choosing between candidates is how another patient's consult note gets attached to this
    referral. A wrong match is worse than no match."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={
            "/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle(patient("p1"), patient("p2"))
        }
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PatientAmbiguousAtConnector, match="2"):
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")


def test_the_mrn_does_not_reach_the_logs_on_failure(certs, tmp_path, caplog):
    """OperationOutcome.diagnostics echoes the failing request and ours carries an MRN. There is
    no logging scrubber in this codebase to catch it downstream."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        operation_outcome={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "processing",
                    "diagnostics": "failed searching Patient?identifier=urn:oid:1.2.3|MRN1",
                }
            ],
        }
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with caplog.at_level("DEBUG"):
            with pytest.raises(Exception):
                resolve_patient(registry, registry.get("example-med"), mrn="MRN1")
    assert "MRN1" not in caplog.text, "the MRN reached the logs via diagnostics"
