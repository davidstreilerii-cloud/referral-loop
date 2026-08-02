"""The CodeSystem we publish, for the distinctions FHIR's own vocabularies cannot carry.

`Task.status` is a required binding to a twelve-code value set, and that set collapses
SCHEDULED, SEEN and DOCUMENTED into `in-progress` and every hold into `on-hold`. Those are
the four distinctions the aging thresholds act on, so they have to survive the projection
somewhere; `Task.businessStatus` is the extension point FHIR provides for exactly this, and
it takes a CodeableConcept from a system of the publisher's choosing.

Written out by hand rather than generated from ReferralState. The two lists are the same
eleven codes today, and generating one from the other would be shorter -- but a published
CodeSystem is an external contract and an enum member is an internal name, and deriving the
first from the second means a refactor of the second silently republishes the first under
different codes. tests/test_fhir_task_status.py keeps them in step instead, by asserting
that the declared set is exactly the set the projection can emit: a new state fails the
test rather than quietly appearing in the CodeSystem.
"""

from __future__ import annotations

from typing import Any

# The canonical identity of this code system. Every stored Coding refers back to it, so
# changing it after anything has been published invalidates all of them -- it is a rename
# of the vocabulary, not of a string. Pinned by a test for that reason.
#
# No domain is registered for this project yet, so this host is scoped to the project name
# and is an identifier rather than a resolvable endpoint (FHIR permits that; canonical URLs
# need to be unique, not fetchable). If the deploying organisation publishes under its own
# namespace, that has to happen before an external system stores its first Coding.
BUSINESS_STATUS_URL = "https://referral-loop.health/fhir/CodeSystem/referral-business-status"
BUSINESS_STATUS_VERSION = "1.0.0"

# One concept per code project() can emit. `content: complete` is the claim that this is
# the whole system and not an excerpt, which is what lets a consumer treat an unrecognised
# code as an error rather than as something it merely has not fetched yet.
BUSINESS_STATUS: dict[str, Any] = {
    "resourceType": "CodeSystem",
    "url": BUSINESS_STATUS_URL,
    "version": BUSINESS_STATUS_VERSION,
    "name": "ReferralBusinessStatus",
    "title": "Referral business status",
    "status": "active",
    "experimental": False,
    "caseSensitive": True,
    "content": "complete",
    "description": (
        "The referral lifecycle state, carried on Task.businessStatus because Task.status "
        "cannot express it. Read it together with Task.status, not instead of it: under a "
        "hold Task.status is on-hold and this code is the state the referral was held from."
    ),
    "concept": [
        {
            "code": "draft",
            "display": "Draft",
            "definition": "Composed but not yet sent to a receiving organisation.",
        },
        {
            "code": "sent",
            "display": "Sent",
            "definition": "Transmitted to the receiving organisation; no acknowledgement yet.",
        },
        {
            "code": "received",
            "display": "Received",
            "definition": (
                "The receiving organisation has acknowledged receipt but has not yet accepted "
                "or declined the referral."
            ),
        },
        {
            "code": "accepted",
            "display": "Accepted",
            "definition": "The receiving organisation has taken the referral on; nothing is scheduled yet.",
        },
        {
            "code": "scheduled",
            "display": "Scheduled",
            "definition": "An appointment exists. The patient has not been seen.",
        },
        {
            "code": "seen",
            "display": "Seen",
            "definition": "The encounter happened. No document has come back yet.",
        },
        {
            "code": "documented",
            "display": "Documented",
            "definition": (
                "A result or consult note has arrived and is attached. No clinician has "
                "reviewed it against the referral yet."
            ),
        },
        {
            "code": "reconciled",
            "display": "Reconciled",
            "definition": (
                "A human has reviewed the returned document against the original question and "
                "closed the loop. Never asserted by the system on its own."
            ),
        },
        {
            "code": "declined",
            "display": "Declined",
            "definition": "The receiving organisation refused the referral. Terminal.",
        },
        {
            "code": "cancelled",
            "display": "Cancelled",
            "definition": "The referring side withdrew the referral. Terminal.",
        },
        {
            "code": "aged-out",
            "display": "Aged out",
            "definition": (
                "No counterparty signal within the configured window; closed by timeout rather "
                "than by anyone deciding anything. Terminal, and distinguishable from a real "
                "outcome, which is the point of having the code."
            ),
        },
    ],
}
