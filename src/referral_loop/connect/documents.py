"""Pull the documents that could close an open loop.

The search is two hops, and the first one is the whole design. A chained
`DocumentReference?patient.identifier=...` would be one request, but chained parameter support
varies between servers and a server that does not support it usually does not say so -- it
ignores the parameter and returns an unfiltered Bundle, or nothing. Both are indistinguishable
from a correct empty answer, and this is the one product that cannot afford that confusion.

So: resolve the patient by identifier, then query by reference. Four outcomes, three of which
raise, so that only the fourth can hand back an empty result:

  * the connector declares no identifier system -- we could not ask
  * the patient is unknown here             -- we asked, they have no such person
  * the identifier matches several          -- we asked, and the answer is not usable
  * the patient resolved and nothing filed  -- we asked, and there is nothing. Empty is correct.

A caller handed an empty list cannot tell those apart, and they are opposite facts about a
referral loop. Design spec section 1.3.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import quote

from ..errors import ReferralLoopError
from .auth import TokenCache, acquire_token
from .connectors import ConnectorProfile, ConnectorRegistry
from .egress import EgressRefused, Response
from .resources import READABLE_TYPES, DocumentSearch, FetchedResource, ResourceMalformed, validate
from .retry import fetch_retrying

logger = logging.getLogger(__name__)


class ConnectorCannotResolvePatients(ReferralLoopError):
    """The connector declares no identifier system, so no query can be built.

    Raised rather than returning an empty result, because empty would say "the specialist filed
    nothing" when the truth is "we never asked".
    """


class PatientNotFoundAtConnector(ReferralLoopError):
    """The identifier matched nobody here. Not the same as having no documents."""


class PatientAmbiguousAtConnector(ReferralLoopError):
    """The identifier matched more than one patient. Refused rather than resolved.

    Picking one is how another patient's consult note gets attached to this referral. Choosing
    between candidates is the identity-resolution slice's problem, not this one's.
    """


class FhirRequestFailed(ReferralLoopError):
    """A request failed in a way retrying will not fix.

    Carries the OperationOutcome's severity and code, which are enumerated FHIR values, and
    never its diagnostics -- servers routinely echo the failing request there and ours contains
    an MRN. There is no logging scrubber in this codebase to catch that downstream.
    """


def _outcome_summary(response: Response) -> str:
    """severity/code only. See FhirRequestFailed: diagnostics is not safe to carry."""
    try:
        payload = json.loads(response.text())
    except json.JSONDecodeError:
        return "unparseable response body"
    issues = payload.get("issue") if isinstance(payload, dict) else None
    if not isinstance(issues, list) or not issues:
        return "no issue reported"
    parts = []
    for issue in issues:
        if isinstance(issue, dict):
            parts.append(f"{issue.get('severity', '?')}/{issue.get('code', '?')}")
    return ", ".join(parts) or "no issue reported"


def _authorized_get(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    url: str,
    *,
    cache: TokenCache,
) -> Mapping[str, object]:
    # One token per search rather than one per request. A twenty-page walk would otherwise sign
    # twenty JWTs and make twenty token calls against an authorization server that just issued a
    # perfectly good credential -- and TokenCache existed with no caller until this used it.
    token = cache.get(profile.connector_id, lambda: acquire_token(registry, profile))
    response = fetch_retrying(
        registry,
        profile,
        url,
        headers={
            "Accept": "application/fhir+json",
            "Authorization": f"Bearer {token.value}",
        },
    )
    if response.status != 200:
        raise FhirRequestFailed(
            f"{profile.connector_id}: request failed with {response.status} "
            f"({_outcome_summary(response)})"
        )
    try:
        payload = json.loads(response.text())
    except json.JSONDecodeError as exc:
        raise FhirRequestFailed(f"{profile.connector_id}: response was not JSON") from exc
    if not isinstance(payload, dict):
        raise FhirRequestFailed(f"{profile.connector_id}: response was not a JSON object")
    return payload


def _entries(bundle: Mapping[str, object]) -> list[Mapping[str, object]]:
    entry = bundle.get("entry")
    if not isinstance(entry, list):
        return []
    return [e["resource"] for e in entry if isinstance(e, dict) and isinstance(e.get("resource"), dict)]


def patient_search_url(profile: ConnectorProfile, mrn: str) -> str:
    """Hop 1's URL, built in one place so the search can record what it actually asked.

    The `system|value` separator in a FHIR token search is percent-encoded, and both halves go
    through quote(safe='') so a system URI's colons and slashes survive intact.
    """
    system = profile.mrn_system
    if system is None:
        raise ConnectorCannotResolvePatients(
            f"{profile.connector_id}: no identifier_systems.mrn declared, so this connector "
            "cannot be asked about our patients. It is preflight-only."
        )
    return (
        f"{profile.fhir_base_url}/Patient"
        f"?identifier={quote(system, safe='')}%7C{quote(mrn, safe='')}"
    )


def resolve_patient(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    *,
    mrn: str,
    cache: TokenCache | None = None,
) -> str:
    """Hop 1. Returns the remote's Patient id, or raises -- never returns nothing."""
    system = profile.mrn_system
    url = patient_search_url(profile, mrn)  # raises ConnectorCannotResolvePatients if undeclared
    found = _entries(_authorized_get(registry, profile, url, cache=cache or TokenCache()))

    if not found:
        raise PatientNotFoundAtConnector(
            f"{profile.connector_id} does not know patient {mrn} in {system}. This is not the "
            "same as having no documents for them."
        )
    if len(found) > 1:
        raise PatientAmbiguousAtConnector(
            f"{profile.connector_id} matched {len(found)} patients for {mrn} in {system}; "
            "refusing to choose between them"
        )

    patient_id = found[0].get("id")
    if not isinstance(patient_id, str) or not patient_id:
        raise FhirRequestFailed(f"{profile.connector_id}: resolved Patient carries no id")
    return patient_id


# A page budget for the whole call, not per resource type, so two types cannot quietly double
# it. Exceeding it raises: a truncated search that reports "nothing further" is the same false
# negative as never having searched.
MAX_PAGES = 20
MAX_RESOURCES = 500


class PaginationRefused(ReferralLoopError):
    """A next link led somewhere it should not, or the walk ran past its budget."""


def _next_url(bundle: Mapping[str, object]) -> str | None:
    links = bundle.get("link")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("relation") == "next":
            url = link.get("url")
            return url if isinstance(url, str) and url else None
    return None


def _walk(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    first_url: str,
    *,
    budget: list[int],
    cache: TokenCache,
) -> tuple[list[tuple[Mapping[str, object], int]], list[str], int]:
    """Follow next links, returning (resources, urls_visited, pages).

    `budget` is a one-element list shared across both resource-type walks, so the cap applies to
    the call rather than to each type.
    """
    collected: list[tuple[Mapping[str, object], int]] = []
    visited: list[str] = []
    url: str | None = first_url
    pages = 0

    while url is not None:
        if budget[0] <= 0:
            raise PaginationRefused(
                f"{profile.connector_id}: exceeded {MAX_PAGES} pages. Refusing rather than "
                "returning a truncated result, which would read as 'nothing further'."
            )
        if url in visited:
            raise PaginationRefused(
                f"{profile.connector_id}: next link points at itself ({url}); pagination loop"
            )

        # check_allowed runs inside fetch, so a next link off the allowlist refuses here rather
        # than being followed. It is a URL the remote chose.
        try:
            payload = _authorized_get(registry, profile, url, cache=cache)
        except EgressRefused as exc:
            raise PaginationRefused(
                f"{profile.connector_id}: next link left the allowlist: {exc}"
            ) from exc

        visited.append(url)
        budget[0] -= 1
        pages += 1
        # Paired with the page it came from, so FetchedResource.page is the real page rather
        # than arithmetic over an index -- provenance that is guessed is not provenance.
        collected.extend((resource, pages) for resource in _entries(payload))
        if len(collected) > MAX_RESOURCES:
            raise PaginationRefused(
                f"{profile.connector_id}: more than {MAX_RESOURCES} resources; refusing rather "
                "than truncating"
            )
        url = _next_url(payload)

    return collected, visited, pages


def find_candidate_documents(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    *,
    mrn: str,
    since: datetime,
    until: datetime | None = None,
) -> DocumentSearch:
    """Hop 1 then hop 2, for both readable resource types."""
    cache = TokenCache()
    patient_id = resolve_patient(registry, profile, mrn=mrn, cache=cache)

    window = f"&date=ge{since.date().isoformat()}"
    if until is not None:
        window += f"&date=le{until.date().isoformat()}"

    budget = [MAX_PAGES]
    resources: list[FetchedResource] = []
    # The real hop-1 URL, not a reconstruction: provenance that is approximated is not
    # provenance, and this is the string a coordinator reads to see what was actually asked.
    urls: list[str] = [patient_search_url(profile, mrn)]
    skipped = 0
    pages_walked = 0

    for kind in READABLE_TYPES:
        first = (
            f"{profile.fhir_base_url}/{kind}"
            f"?patient={quote('Patient/' + patient_id, safe='')}{window}"
        )
        found, visited, pages = _walk(registry, profile, first, budget=budget, cache=cache)
        urls.extend(visited)
        pages_walked += pages
        for raw, page in found:
            try:
                validate(raw)
            except ResourceMalformed as exc:
                # Skipped and counted, not fatal. One malformed resource must not hide the
                # others -- and the count returns on the result rather than only in a log.
                logger.warning("%s: discarding a malformed resource: %s", profile.connector_id, exc)
                skipped += 1
                continue
            resources.append(
                FetchedResource(
                    resource_type=kind,
                    resource=raw,
                    connector_id=profile.connector_id,
                    query_url=first,
                    page=page,
                )
            )

    return DocumentSearch(
        resources=tuple(resources),
        patient_id=patient_id,
        pages_walked=pages_walked,
        skipped_malformed=skipped,
        query_urls=tuple(urls),
    )
