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
    """One search at one connector, with the provenance to say what was actually asked.

    **`query_urls` carries PHI and that is deliberate.** Its first entry is the hop-1
    Patient search, which embeds the MRN percent-encoded into the query string; every
    entry after it names a `Patient/<id>` at the remote. It is not redacted because the
    field exists to record the real URL rather than a reconstruction -- provenance that is
    approximated is not provenance, and this is the string a coordinator reads to see what
    was asked on their behalf.

    So it is labelled instead, and the label is the control. Treat it exactly as the
    MRN it contains: it belongs on an encrypted volume beside the loop, never in a log
    line, an audit row, an exported report or a support ticket. `documents.py` keeps the
    MRN out of its own exception messages for this reason; a field that carries one
    without saying so is how it gets put back.
    """

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


def validate(resource: Mapping[str, object], *, expected: str | None = None) -> Mapping[str, object]:
    """Refuse anything the mapper could not use. Returns the resource unchanged."""
    kind = resource.get("resourceType")
    if kind not in READABLE_TYPES:
        # A server that ignores a search parameter returns whatever it likes. Accepting it would
        # put an Observation into a set the caller believes is documents.
        raise ResourceMalformed(
            f"resourceType {kind!r} is not one of {READABLE_TYPES}"
        )

    if expected is not None and kind != expected:
        # Both types are readable, which is exactly why this check is needed: without it a
        # DiagnosticReport returned by the DocumentReference search is accepted and then
        # labelled DocumentReference by the caller, because the label came from the query.
        raise ResourceMalformed(
            f"expected {expected} but the server returned {kind} {resource.get('id', '?')}"
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
