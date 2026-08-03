"""What a fetched resource has to have before anything downstream may rely on it."""

from __future__ import annotations

import pytest

from referral_loop.connect.resources import (
    READABLE_TYPES,
    DocumentSearch,
    FetchedResource,
    ResourceMalformed,
    validate,
)


def _doc(**overrides) -> dict:
    base = {
        "resourceType": "DocumentReference",
        "id": "d1",
        "status": "current",
        "subject": {"reference": "Patient/p1"},
        "content": [{"attachment": {"contentType": "application/pdf"}}],
    }
    base.update(overrides)
    return base


def _report(**overrides) -> dict:
    base = {
        "resourceType": "DiagnosticReport",
        "id": "r1",
        "status": "final",
        "subject": {"reference": "Patient/p1"},
    }
    base.update(overrides)
    return base


def test_a_well_formed_document_validates():
    assert validate(_doc())["id"] == "d1"


def test_a_well_formed_report_validates():
    assert validate(_report())["id"] == "r1"


def test_both_readable_types_are_declared():
    assert READABLE_TYPES == ("DocumentReference", "DiagnosticReport")


def test_a_resource_of_another_type_is_refused():
    """A server that ignores a search parameter returns whatever it likes. Accepting it would
    put an Observation into a set the caller believes is documents."""
    with pytest.raises(ResourceMalformed, match="resourceType"):
        validate({"resourceType": "Observation", "id": "o1", "status": "final"})


@pytest.mark.parametrize("missing", ["id", "status", "subject"])
def test_a_document_missing_a_field_the_mapper_needs_is_refused(missing):
    broken = _doc()
    del broken[missing]
    with pytest.raises(ResourceMalformed, match=missing):
        validate(broken)


def test_a_document_with_no_attachment_is_refused():
    """A DocumentReference whose content carries no attachment references no document. It would
    map to an artifact with nothing in it."""
    with pytest.raises(ResourceMalformed, match="attachment"):
        validate(_doc(content=[{}]))


def test_a_report_needs_no_attachment():
    """DiagnosticReport carries its result in presentedForm or result, not content -- requiring
    an attachment here would refuse every valid report."""
    assert validate(_report())


def test_a_search_reports_what_it_discarded():
    """skipped_malformed lives on the result, not in a log: 'we found three documents' must
    never quietly mean 'we found three and threw two away'."""
    search = DocumentSearch(
        resources=(), patient_id="p1", pages_walked=1, skipped_malformed=2, query_urls=("u",)
    )
    assert search.skipped_malformed == 2


def test_a_fetched_resource_records_where_it_came_from():
    r = FetchedResource(
        resource_type="DocumentReference",
        resource=_doc(),
        connector_id="example-med",
        query_url="https://x/DocumentReference?patient=Patient/p1",
        page=2,
    )
    assert r.connector_id == "example-med"
    assert r.page == 2


def test_validate_refuses_the_other_readable_type_when_one_is_expected():
    """Both are readable, but a DiagnosticReport is not a DocumentReference. Accepting it
    because it is in READABLE_TYPES is how the wrong label gets attached."""
    with pytest.raises(ResourceMalformed, match="DocumentReference"):
        validate(_report(), expected="DocumentReference")


def test_validate_without_an_expected_type_still_refuses_an_unreadable_one():
    with pytest.raises(ResourceMalformed, match="resourceType"):
        validate({"resourceType": "Observation", "id": "o1", "status": "final"})
