"""What comes back, and the shape it has to have first.

Structural validation, not profile conformance. US Core would need a validator dependency and
profile packages, and test_install_closure asserts the dependency surface stays small -- that is
its own unit. What is checked here is exactly the set of fields the canonical mapper in the next
sub-project reads, so that a resource which passes here cannot fail there for a missing field.

These stay FHIR. Mapping onto Referral and InboundArtifact is the next sub-project's job, and
stopping at this boundary is what lets the read client be tested without importing core/.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import ReferralLoopError

READABLE_TYPES = ("DocumentReference", "DiagnosticReport")


class ResourceMalformed(ReferralLoopError):
    """A resource is missing something downstream needs.

    Not fatal to a search -- documents.py skips and counts these, the same posture
    UnparseableSegmentError already takes for a bad HL7 segment. One malformed resource must not
    hide the nine good ones.
    """


@dataclass(frozen=True)
class FetchedResource:
    resource_type: str
    resource: Mapping[str, object]
    connector_id: str
    query_url: str
    page: int


@dataclass(frozen=True)
class DocumentSearch:
    resources: tuple[FetchedResource, ...]
    patient_id: str
    pages_walked: int
    skipped_malformed: int
    query_urls: tuple[str, ...]


def _require(resource: Mapping[str, object], field: str) -> object:
    if field not in resource or resource[field] in (None, "", [], {}):
        raise ResourceMalformed(
            f"{resource.get('resourceType', 'resource')} {resource.get('id', '?')} "
            f"has no {field}"
        )
    return resource[field]


def validate(resource: Mapping[str, object]) -> Mapping[str, object]:
    """Refuse anything the mapper could not use. Returns the resource unchanged."""
    kind = resource.get("resourceType")
    if kind not in READABLE_TYPES:
        # A server that ignores a search parameter returns whatever it likes. Accepting it would
        # put an Observation into a set the caller believes is documents.
        raise ResourceMalformed(
            f"resourceType {kind!r} is not one of {READABLE_TYPES}"
        )

    for field in ("id", "status", "subject"):
        _require(resource, field)

    if kind == "DocumentReference":
        content = _require(resource, "content")
        if not isinstance(content, list) or not any(
            isinstance(item, dict) and item.get("attachment") for item in content
        ):
            # Content with no attachment references no document; it would map to an artifact
            # with nothing in it. DiagnosticReport is exempt: it carries its result in
            # presentedForm or result, so requiring an attachment would refuse every valid one.
            raise ResourceMalformed(
                f"DocumentReference {resource.get('id', '?')} has no content[].attachment"
            )

    return resource
