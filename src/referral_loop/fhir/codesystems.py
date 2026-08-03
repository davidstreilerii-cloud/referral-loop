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

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..errors import ReferralLoopError

# The canonical identity of this code system. Every stored Coding refers back to it, so
# changing it after anything has been published invalidates all of them -- it is a rename
# of the vocabulary, not of a string.
#
# No domain is registered for this project yet, so this host is scoped to the project name
# and is an identifier rather than a resolvable endpoint (FHIR permits that; canonical URLs
# need to be unique, not fetchable).
#
# **A canonical url names the vocabulary, not the deployment.** These eleven codes mean the
# same thing at every site running this software, so the default is the product's own url
# and every unconfigured deployment publishes the same one. That is the whole return on
# publishing a CodeSystem: a receiving organisation handed
# `{system: <this>, code: "seen"}` from two different hospitals can tell it is one concept.
# Two sites that each mint their own url produce two systems that no receiver can equate,
# which is the state of affairs businessStatus exists to get out of.
DEFAULT_BUSINESS_STATUS_URL = "https://referral-loop.health/fhir/CodeSystem/referral-business-status"

# Overridable, because a hospital that must publish under its own namespace has a real
# reason to (a policy that forbids asserting a vendor identifier, an existing terminology
# server that owns the arc) -- and the cost above is theirs to weigh, not ours to refuse.
# Set it before an external system stores its first Coding; after that it is a rename of
# something already in someone else's database.
#
# **This one defaults on purpose, and that is the opposite of what three of its neighbours
# do.** REFERRAL_RAW_RETENTION_DAYS (retention.py) and REFERRAL_THRESHOLDS_ACCEPTED
# (staleness.py) refuse to default, because there the safe value is the *site's* -- guessing
# a retention period holds PHI past what a policy allows, and guessing a staleness threshold
# invents a clinical standard. Here the safe value is the *shared* one, and demanding that
# every site state a url would guarantee the fragmentation the paragraph above describes.
# If you are copying a pattern into this file, the retention one is the wrong pattern.
# REFERRAL_AUDIT_DB (immutable_audit.py) is the one this follows: a module-level constant,
# read once at import, shipped default, env override.
BUSINESS_STATUS_URL_ENV = "REFERRAL_BUSINESS_STATUS_URL"

# `|` is excluded because FHIR spells a versioned canonical `<url>|<version>`: a url
# containing one is unparseable as the thing it claims to be. Whitespace is excluded because
# a canonical with a space in it is a config file that lost a quote, not an identifier.
_FORBIDDEN_IN_CANONICAL = "|"


def _checked_url(name: str, value: str, default: str = DEFAULT_BUSINESS_STATUS_URL) -> str:
    """A canonical url, or a refusal naming the variable that carries it.

    Refuses at import rather than at first publication. The failure mode being bought off
    is silent: a broken canonical does not raise anywhere -- it serialises fine, the
    CodeSystem is well-formed, and the damage is only visible in a receiving system that
    stored Codings against a system identifier matching nothing.
    """
    text = value.strip()
    if not text:
        raise ReferralLoopError(
            f"{name} is set but empty. An empty canonical url is not a canonical url, and "
            f"this refuses to read it as 'unset' -- an empty value is far more often an "
            f"unset shell variable interpolated into a config than a decision. Unset "
            f"{name} entirely to publish the shipped default "
            f"({default})."
        )
    if any(character.isspace() for character in text):
        # Surrounding whitespace is stripped rather than refused -- a trailing space in a
        # .env file is never a decision, and RetentionPolicy.from_env strips its values the
        # same way. Whitespace *inside* is refused, because there is no url it could have
        # been and guessing which half was meant is the guess this module does not make.
        raise ReferralLoopError(
            f"{name} must not contain whitespace; got {value!r}."
        )
    if any(character in text for character in _FORBIDDEN_IN_CANONICAL):
        raise ReferralLoopError(
            f"{name} must not contain {_FORBIDDEN_IN_CANONICAL!r}; got {value!r}. FHIR "
            f"writes a versioned canonical as '<url>|<version>', so a url containing one "
            f"cannot be told from a url that already carries its version."
        )

    parts = urlsplit(text)
    if not parts.scheme:
        raise ReferralLoopError(
            f"{name} must be an absolute URI with a scheme; got {value!r}. A relative "
            f"reference is resolved against whatever a receiving system happens to consider "
            f"its base, which is not an identity."
        )
    if not (parts.netloc or parts.path):
        # `urn:` and `https:` parse to a scheme and nothing else. A scheme alone identifies
        # a naming system, not a name within it -- so `urn:uuid:<uuid>` is accepted here
        # (path is `uuid:<uuid>`) and bare `urn:` is not.
        raise ReferralLoopError(
            f"{name} is a scheme with nothing after it; got {value!r}. "
            f"'urn:uuid:<uuid>' is a valid canonical for a site with no domain; 'urn:' "
            f"alone is not."
        )
    return text


def _configured_url(env: Mapping[str, str] | None = None, *,
                    variable: str = BUSINESS_STATUS_URL_ENV,
                    default: str = DEFAULT_BUSINESS_STATUS_URL) -> str:
    """The canonical this deployment publishes. Unset means the shipped default.

    Takes the mapping as an argument, matching RetentionPolicy.from_env, so the resolution
    is reachable without mutating the process environment.
    """
    env = os.environ if env is None else env
    override = env.get(variable)
    if override is None:
        return default
    return _checked_url(variable, override, default)


# The single source of truth for the url, for this resource and for any future emitter of a
# Coding in this system. `task_status.py` returns bare code strings and emits no `system`
# today; when one is needed it must read this name rather than restate the literal, or an
# override would move the CodeSystem and leave the Codings behind.
BUSINESS_STATUS_URL = _configured_url()
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


# ---------------------------------------------------------------- activity codes

# Design spec 8.2. What a transition *did*, as opposed to what state it left the referral
# in -- Provenance.activity is a verb and Task.businessStatus is a noun, and the two are
# not the same vocabulary even though they are derived from the same transition.
DEFAULT_ACTIVITY_URL = "https://referral-loop.health/fhir/CodeSystem/referral-activity"

# A second env var rather than a shared base-namespace one, deliberately. Consolidating
# would change REFERRAL_BUSINESS_STATUS_URL, which is on canonical-model-v1 and which
# another branch may already be configuring. At two vocabularies this is repetition; a
# third is the trigger to consolidate, because at three it is a pattern nobody chose.
ACTIVITY_URL_ENV = "REFERRAL_ACTIVITY_URL"

# Same defaulting posture as the business statuses: unset publishes the shipped default,
# because the safe value is the one every deployment shares. Overriding buys local
# identity at the cost of Codings that no other site can resolve -- see the warning on
# BUSINESS_STATUS_URL_ENV, which applies here unchanged.
ACTIVITY_URL = _configured_url(variable=ACTIVITY_URL_ENV, default=DEFAULT_ACTIVITY_URL)
ACTIVITY_VERSION = "1.0.0"

# Eleven from spec 8.2, plus `draft` and `receive`. Those two are an addition rather than
# an oversight being copied: 8.2's list has no verb for DRAFT or RECEIVED, and a
# projection that is total over ReferralState needs one for every state it can be handed.
# Recorded here rather than silently -- the alternative was emitting a Provenance with no
# activity, which is a resource that says something happened without saying what.
ACTIVITY_CODES: dict[str, str] = {
    "draft": "Referral drafted, not yet sent",
    "submit": "Referral sent to the receiving organisation",
    "receive": "Receipt acknowledged by the receiving organisation",
    "accept": "Referral accepted by the receiving organisation",
    "decline": "Referral refused by the receiving organisation",
    "schedule": "Appointment scheduled",
    "see": "Patient seen",
    "document": "Documentation returned",
    "reconcile": "Loop closed by a coordinator at the referring site",
    "cancel": "Referral withdrawn",
    "age-out": "Referral timed out with no counterparty signal",
    "hold": "Referral suspended",
    "release": "Suspension lifted",
}

REFERRAL_ACTIVITY: dict[str, Any] = {
    "resourceType": "CodeSystem",
    "url": ACTIVITY_URL,
    "version": ACTIVITY_VERSION,
    "name": "ReferralActivity",
    "status": "active",
    "content": "complete",
    "concept": [{"code": code, "display": display} for code, display in ACTIVITY_CODES.items()],
}
