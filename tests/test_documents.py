"""Resolving our patient at a remote, and the three answers that are not 'no documents'."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from referral_loop.cli import MODES, main
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


def _connector_mapping(base_url, key_path, ca_file, *, mrn_system="urn:oid:1.2.3"):
    """The connector file's contents, as a mapping.

    Split out of `_registry` so the CLI tests at the bottom of this file can write the *same*
    connector to disk and let `load_connector_registry` read it back. A second literal would
    drift from this one, and the mode under test is only interesting insofar as it reaches the
    same profile these tests already exercise in-process.
    """
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
    return {"connectors": [connector]}


def _registry(base_url, key_path, ca_file, *, mrn_system="urn:oid:1.2.3"):
    return ConnectorRegistry.from_mapping(
        _connector_mapping(base_url, key_path, ca_file, mrn_system=mrn_system)
    )


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
        # Matched on the connector, not the MRN: the identifier is deliberately no
        # longer in the message. See test_a_patient_miss_keeps_the_mrn_out_of_the
        # _exception_text below, and _PatientResolutionRefused for why.
        with pytest.raises(PatientNotFoundAtConnector, match="example-med"):
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


# ------------------------------- the MRN, and the rule this module wrote for it

def test_a_patient_miss_keeps_the_mrn_out_of_the_exception_text(certs, tmp_path):
    """`FhirRequestFailed`'s docstring states the rule for this whole module: an
    MRN must not travel in a diagnostic string, because "there is no logging
    scrubber in this codebase to catch that downstream".

    The two refusals immediately below it broke it. `str(exc)` on a
    `ReferralLoopError` is exactly what every caller in this package logs --
    `registry.py`, `store.py` and `MessageHandler._process` all do -- so an MRN
    in the message is an MRN in a log file the moment anything wires this up.

    Something wires it up now. `cli._run_documents` prints `str(exc)` for this
    refusal and the one below it, so the rule stopped being a precaution and
    became the reason the `documents` mode's output is safe to redirect into a
    file. This test is what holds it there.
    """
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle()}
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PatientNotFoundAtConnector) as caught:
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")

    assert "MRN1" not in str(caught.value)
    assert "example-med" in str(caught.value), "the message must still name the connector"


def test_an_ambiguous_patient_keeps_the_mrn_out_of_the_exception_text(certs, tmp_path):
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={
            "/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle(patient("p1"), patient("p2"))
        }
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PatientAmbiguousAtConnector) as caught:
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")

    assert "MRN1" not in str(caught.value)
    assert "2" in str(caught.value), "the candidate count is the actionable part"


def test_the_mrn_is_still_reachable_on_the_refusal_just_not_in_its_message(certs, tmp_path):
    """Dropped from the message, not from the object. A caller that genuinely
    needs to know which identifier missed can ask for it and decide where it
    goes; the difference is that reaching for it is now a deliberate act rather
    than the default consequence of logging the exception."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle()}
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PatientNotFoundAtConnector) as caught:
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")

    assert caught.value.mrn == "MRN1"
    assert caught.value.connector_id == "example-med"


def test_query_urls_are_documented_as_phi_bearing(certs, tmp_path):
    """`query_urls[0]` embeds the MRN and always has -- `patient_search_url`
    percent-encodes it into the hop-1 URL, and the search deliberately records
    the real URL rather than a reconstruction because "provenance that is
    approximated is not provenance".

    So this is not redacted; it is labelled. A field that carries PHI and does
    not say so is the one that ends up in a log line or a support ticket, and
    the label is what a reviewer wiring this into the worklist has to read past.
    """
    from referral_loop.connect.resources import DocumentSearch

    doc = DocumentSearch.__doc__ or ""
    assert "PHI" in doc and "query_urls" in doc, (
        "query_urls carries an MRN and the dataclass does not say so"
    )


# ------------------------------------- the `documents` mode: the caller this module lacked
#
# Everything above this line exercises `find_candidate_documents` in-process. Nothing in
# `src/` did, which is the whole reason this section exists -- see the module docstring of
# `connect/documents.py` for the argument. These tests go in through `cli.main`, because a
# public entry point is only reachable if the argument parsing, the gate ordering and the
# dispatch all agree with each other, and none of those three is exercised by calling the
# function directly.


def _store_with_one_event(tmp_path):
    """A loop store holding exactly one event, so "unchanged" is a number and not a vacuum."""
    from referral_loop.events import LoopEvent
    from referral_loop.store import LoopStore

    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L-000000000001", "created", _SINCE, "C1", {"mrn": "MRN1"}))
    return store


def _loop_events(store) -> int:
    return store.stats()["tables"]["loop_events"]["rows"]


def _connector_file(tmp_path, base_url, key_path, ca_file, *, mrn_system="urn:oid:1.2.3"):
    path = tmp_path / "connectors.json"
    path.write_text(
        json.dumps(_connector_mapping(base_url, key_path, ca_file, mrn_system=mrn_system)),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def attested(monkeypatch):
    """The encryption gate satisfied by operator attestation, and no pack key anywhere.

    Both halves are assertions in disguise. The attestation is set because this mode *does*
    take the encryption gate -- a run on a box with it unset must refuse, which is its own
    test below. The pack key is removed because this mode has to reach its work without one:
    it loads no pack and matches nothing, exactly as `connectors` does not.
    """
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)


def test_the_documents_mode_finds_candidates_and_writes_no_loop_state(
    certs, tmp_path, capsys, attested
):
    """The read-only claim is the whole point: a search must not be able to move a loop.

    Asserted by counting events before and after rather than by reading the mode's source,
    because a future caller that writes would pass any test that only inspects code. The
    database counted is the one the mode was handed on `--db`, so a mode that opened it and
    appended anything -- an attach, a "we looked" marker, an audit-shaped loop event -- fails
    here rather than at a code review that might not happen.
    """
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    store = _store_with_one_event(tmp_path)
    before = _loop_events(store)

    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        document_reference("d1")
    )
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        diagnostic_report("r1")
    )
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        path = _connector_file(tmp_path, base, key_path, ca)
        code = main([
            "documents", "--connectors", str(path), "--connector", "example-med",
            "--mrn", "MRN1", "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
        ])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "DocumentReference" in out and "d1" in out
    assert "DiagnosticReport" in out and "r1" in out
    assert _loop_events(store) == before, "a document search moved a loop"


def test_the_mode_does_not_print_the_mrn_it_was_given(certs, tmp_path, capsys, attested):
    """`DocumentSearch.query_urls` carries the MRN percent-encoded and says so in its own
    docstring. stdout is the one surface an operator pipes into a file, pastes into a ticket
    and leaves open in a terminal, so the provenance this mode holds is deliberately not the
    provenance it prints."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        document_reference("d1")
    )
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        path = _connector_file(tmp_path, base, key_path, ca)
        code = main([
            "documents", "--connectors", str(path), "--connector", "example-med",
            "--mrn", "MRN1", "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
        ])

    captured = capsys.readouterr()
    assert code == 0
    assert "MRN1" not in captured.out
    assert "MRN1" not in captured.err


def test_a_resolved_patient_with_nothing_filed_still_exits_zero(certs, tmp_path, capsys, attested):
    """Empty is a real answer here and must not look like a failure. The three answers that are
    *not* "nothing was filed" all raise inside the search, so exit 0 with a count of zero is the
    one case where a coordinator may act on the absence."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        path = _connector_file(tmp_path, base, key_path, ca)
        code = main([
            "documents", "--connectors", str(path), "--connector", "example-med",
            "--mrn", "MRN1", "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
        ])

    assert code == 0
    assert "0 candidate" in capsys.readouterr().out


def test_a_patient_the_connector_does_not_know_exits_one_not_zero(certs, tmp_path, capsys, attested):
    """The distinction the whole module is built on, carried out to an exit code. "We asked and
    the answer is not usable" is not "we asked and there was nothing", so it cannot share an exit
    code with it -- a script that treats both as 0 has learned the wrong fact."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle()}
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        path = _connector_file(tmp_path, base, key_path, ca)
        code = main([
            "documents", "--connectors", str(path), "--connector", "example-med",
            "--mrn", "MRN1", "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
        ])

    out = capsys.readouterr().out
    assert code == 1
    assert "example-med" in out
    assert "MRN1" not in out


def test_a_preflight_only_connector_refuses_before_it_signs_anything(tmp_path, capsys, attested):
    """No server is started, and that is the assertion. A connector with no declared identifier
    system cannot be asked about a patient at all, so the refusal has to come before the JWT is
    built and the token call made -- otherwise the operator's first evidence of a configuration
    mistake is a credential exchange with a site they were never able to query."""
    key_path, _ = rsa_keypair(tmp_path)
    path = _connector_file(
        tmp_path, "https://unreachable.invalid/api/FHIR/R4", key_path, None, mrn_system=None
    )
    code = main([
        "documents", "--connectors", str(path), "--connector", "example-med",
        "--mrn", "MRN1", "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "identifier_systems" in err
    assert "MRN1" not in err


def test_an_unknown_connector_refuses_and_names_the_configured_ones(tmp_path, capsys, attested):
    key_path, _ = rsa_keypair(tmp_path)
    path = _connector_file(tmp_path, "https://unreachable.invalid/api/FHIR/R4", key_path, None)
    code = main([
        "documents", "--connectors", str(path), "--connector", "typo-med",
        "--mrn", "MRN1", "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "typo-med" in err and "example-med" in err


def test_an_unparseable_since_refuses_rather_than_searching_from_nowhere(tmp_path, capsys, attested):
    """A window this mode could not parse must not become a window it invented. Every other
    reading of a bad `--since` -- clamp it, default it, drop it -- searches a period the operator
    did not ask for and reports the result as though they had."""
    key_path, _ = rsa_keypair(tmp_path)
    path = _connector_file(tmp_path, "https://unreachable.invalid/api/FHIR/R4", key_path, None)
    code = main([
        "documents", "--connectors", str(path), "--connector", "example-med",
        "--mrn", "MRN1", "--since", "last tuesday", "--db", str(tmp_path / "loops.db"),
    ])
    assert code == 2
    assert "--since" in capsys.readouterr().err


def test_it_refuses_without_an_mrn(tmp_path, capsys, attested):
    """argparse would default `--mrn` to the empty string and the search would go out asking for
    a patient identified by nothing, which some servers answer with every patient they have."""
    key_path, _ = rsa_keypair(tmp_path)
    path = _connector_file(tmp_path, "https://unreachable.invalid/api/FHIR/R4", key_path, None)
    code = main([
        "documents", "--connectors", str(path), "--connector", "example-med",
        "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
    ])
    assert code == 2
    assert "--mrn" in capsys.readouterr().err


def test_it_refuses_without_a_window(tmp_path, capsys, attested):
    """Omitted is refused for the same reason unparseable is. A default `--since` would be
    this command deciding how far back a referral stays interesting, which is a clinical
    judgement the site makes -- the same argument `staleness` makes for its thresholds, and
    the reason those are gated behind an acceptance variable rather than shipped as fact."""
    key_path, _ = rsa_keypair(tmp_path)
    path = _connector_file(tmp_path, "https://unreachable.invalid/api/FHIR/R4", key_path, None)
    code = main([
        "documents", "--connectors", str(path), "--connector", "example-med",
        "--mrn", "MRN1", "--db", str(tmp_path / "loops.db"),
    ])
    assert code == 2
    assert "--since" in capsys.readouterr().err


def test_it_refuses_when_encryption_at_rest_is_not_attested(tmp_path, capsys, monkeypatch):
    """The one gate this mode does take. It pulls consult notes and lab results into this
    process and prints them, and an operator redirects that stdout onto the volume `--db`
    names."""
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr("referral_loop.encryption_check._detect_os_encryption", lambda _p: None)
    key_path, _ = rsa_keypair(tmp_path)
    path = _connector_file(tmp_path, "https://unreachable.invalid/api/FHIR/R4", key_path, None)
    code = main([
        "documents", "--connectors", str(path), "--connector", "example-med",
        "--mrn", "MRN1", "--since", "2026-01-01", "--db", str(tmp_path / "loops.db"),
    ])
    assert code == 2
    assert "PHI_ENCRYPTION_VERIFIED" in capsys.readouterr().err


def test_documents_is_a_mode():
    assert "documents" in MODES
