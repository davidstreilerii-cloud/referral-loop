"""Resolving our patient at a remote, and the three answers that are not 'no documents'."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from referral_loop.connect.connectors import ConnectorRegistry
from referral_loop.connect.documents import (
    MAX_PAGES,
    MAX_RESOURCES,
    ConnectorCannotResolvePatients,
    FhirRequestFailed,
    PaginationRefused,
    PatientAmbiguousAtConnector,
    PatientNotFoundAtConnector,
    find_candidate_documents,
    resolve_patient,
)

from ._certs import localhost_cert, rsa_keypair
from ._fhirserver import (
    ServerBehaviour,
    bundle,
    diagnostic_report,
    document_reference,
    fhir_server,
    patient,
)

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


def _patient_bundle():
    return {"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle(patient("p1"))}


def test_both_resource_types_are_queried(certs, tmp_path):
    """A consult note arrives as a DocumentReference and a lab result as a DiagnosticReport,
    which is the FHIR analogue of the ORU the HL7 path already closes on. Querying one would
    miss every diagnostic referral."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(document_reference("d1"))
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(diagnostic_report("r1"))
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        got = find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
    assert {r.resource_type for r in got.resources} == {"DocumentReference", "DiagnosticReport"}
    assert got.patient_id == "p1"


def test_a_resolved_patient_with_nothing_filed_returns_empty(certs, tmp_path):
    """The one case where empty is the right answer, and the reason the other three raise."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        got = find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
    assert got.resources == ()
    assert got.patient_id == "p1"


def test_a_next_link_off_the_allowlist_refuses_loudly(certs, tmp_path):
    """A next link is chosen by the remote. Refusing quietly would hand back a truncated result
    set wearing the costume of a complete one."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        document_reference("d1"), next_url="https://evil.example/page2"
    )
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PaginationRefused, match="evil.example"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)


def test_a_self_referential_next_link_refuses(certs, tmp_path):
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        page = f"{base}/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"
        bundles = _patient_bundle()
        bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
            document_reference("d1"), next_url=page
        )
        bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
        _b.bundles.update(bundles)
        registry = _registry(base, key_path, ca)
        with pytest.raises(PaginationRefused, match="itself|loop"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)


def test_the_page_cap_raises_rather_than_truncating(certs, tmp_path):
    """The cap is the reason MAX_PAGES is exported. A server that always hands back a next link
    would otherwise walk forever against a host we do trust -- and stopping quietly at the cap
    would report 'nothing further' over a search that was cut off, which is the same false
    negative as never having searched."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, behaviour):
        behaviour.bundles.update(_patient_bundle())
        # A chain longer than the budget: every page points at another, none is ever the last.
        behaviour.bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
            document_reference("d0"), next_url=f"{base}/DocumentReference?page=1"
        )
        for page in range(1, MAX_PAGES + 3):
            behaviour.bundles[f"/DocumentReference?page={page}"] = bundle(
                document_reference(f"d{page}"),
                next_url=f"{base}/DocumentReference?page={page + 1}",
            )
        registry = _registry(base, key_path, ca)
        with pytest.raises(PaginationRefused, match="pages"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)


def test_a_malformed_resource_is_skipped_and_counted(certs, tmp_path):
    """One bad resource must not hide the good ones -- the posture UnparseableSegmentError
    already takes for a bad HL7 segment -- but the count comes back, not a log line."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        document_reference("d1"), {"resourceType": "DocumentReference", "id": "broken"}
    )
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        got = find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
    assert len(got.resources) == 1
    assert got.skipped_malformed == 1


def test_a_two_hundred_that_is_not_a_bundle_is_not_read_as_zero_results(certs, tmp_path):
    """The critical one. _entries read .get("entry") and turned anything that was not a list
    into [], so a CapabilityStatement returned with status 200 -- valid JSON, wrong resource --
    produced "this connector does not know that patient". The server never said that. A
    response that is not a searchset is a failed question, not an answered one."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={
            "/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": {
                "resourceType": "CapabilityStatement",
                "status": "active",
            }
        }
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(FhirRequestFailed, match="Bundle|searchset"):
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")


def test_a_hop_two_response_that_is_not_a_bundle_is_not_an_empty_search(certs, tmp_path):
    """Same gap one level down, where it is worse: it produced an empty DocumentSearch with
    skipped_malformed == 0 -- a clean bill of health for a question that was never answered."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = {
        "resourceType": "OperationOutcome",
        "issue": [],
    }
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(FhirRequestFailed, match="Bundle|searchset"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)


def test_the_resource_budget_is_shared_across_both_types(certs, tmp_path):
    """Spec 3.4 says the caps apply to the call as a whole so two resource types cannot quietly
    double the budget. The page cap was shared through a mutable list; the resource cap was a
    local list per walk, so it doubled. 499 of each returned 998 against a cap of 500."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    half = MAX_RESOURCES - 1
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        *(document_reference(f"d{i}") for i in range(half))
    )
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        *(diagnostic_report(f"r{i}") for i in range(half))
    )
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PaginationRefused, match="resources"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)


def test_a_resource_of_the_other_readable_type_is_refused_not_relabelled(certs, tmp_path):
    """resource_type was taken from the query rather than the resource, so a DiagnosticReport
    returned by the DocumentReference search was accepted and labelled DocumentReference. This
    module's own comment says provenance that is approximated is not provenance."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        diagnostic_report("r-wrong-type")
    )
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        got = find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
    assert got.resources == ()
    assert got.skipped_malformed == 1, "mislabelling it as the queried type is worse than dropping it"


def test_a_next_link_with_an_unknown_scheme_refuses_as_pagination(certs, tmp_path):
    """endpoint_of raises ConnectorConfigError for a scheme it does not know -- a boundary the
    previous sub-project drew, with a comment predicting this exact caller. _walk caught only
    EgressRefused, so the documented PaginationRefused was not what escaped."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        document_reference("d1"), next_url="ftp://evil.example/page2"
    )
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PaginationRefused, match="scheme|allowlist"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
