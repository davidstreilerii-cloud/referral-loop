"""The projection to FHIR R4 Task.status, and the properties that justify the dual layer.

Task.status cannot say "accepted but never scheduled" as distinct from "scheduled but the
patient was never seen": SCHEDULED, SEEN and DOCUMENTED all collapse to `in-progress`, and
those three are precisely what the aging agent escalates on. If businessStatus did not carry
that distinction, and if a hold did not preserve the state it was held from, the eleven-state
model would have no justification over adopting Task.status as the vocabulary directly. These
tests are that justification, expressed as something that can fail.
"""

import importlib
import os
from pathlib import Path

import pytest

from referral_loop.core.states import Hold, ReferralState
from referral_loop.errors import ReferralLoopError
from referral_loop.fhir import codesystems
from referral_loop.fhir.codesystems import (
    BUSINESS_STATUS,
    BUSINESS_STATUS_URL_ENV,
    DEFAULT_BUSINESS_STATUS_URL,
)
from referral_loop.fhir.task_status import R4_TASK_STATUS, project

_HOLD = Hold(reason="x", actor="y")


@pytest.mark.parametrize("state", list(ReferralState))
def test_the_projection_is_total(state):
    status, business = project(state, hold=None)
    assert status in R4_TASK_STATUS, f"{state} -> {status} is not an R4 Task.status code"
    assert business is None or isinstance(business, str)


@pytest.mark.parametrize("state", list(ReferralState))
def test_the_projection_is_total_under_hold_as_well(state):
    status, business = project(state, hold=_HOLD)
    assert status == "on-hold"
    assert business is not None, "a hold must not discard which state it was held from"


def test_the_three_states_that_collapse_are_distinguished_by_business_status():
    """SCHEDULED, SEEN and DOCUMENTED all project to in-progress. Those are exactly the
    three distinctions the aging agent escalates on, which is the concrete reason the
    model is dual-layer rather than just adopting Task.status as the vocabulary."""
    collapsing = [ReferralState.SCHEDULED, ReferralState.SEEN, ReferralState.DOCUMENTED]
    projected = [project(s, hold=None) for s in collapsing]
    assert {p[0] for p in projected} == {"in-progress"}
    assert len({p[1] for p in projected}) == 3, "the three must stay distinguishable"


def test_a_held_referral_can_be_told_apart_from_a_referral_held_from_elsewhere():
    a = project(ReferralState.ACCEPTED, hold=_HOLD)
    b = project(ReferralState.SCHEDULED, hold=_HOLD)
    assert a[0] == b[0] == "on-hold"
    assert a[1] != b[1]


def test_every_business_status_code_is_declared_in_our_codesystem():
    declared = {c["code"] for c in BUSINESS_STATUS["concept"]}
    for state in ReferralState:
        for hold in (None, _HOLD):
            _, business = project(state, hold=hold)
            if business is not None:
                assert business in declared, f"{business} is emitted but not declared"


# --- the mapping itself, per design spec section 7 -------------------------------------

# Retyped from the spec's table so that a change to the projection has to be a change to
# the spec as well. Not derived from the implementation in any way -- a table generated
# from the thing it checks proves only that the generator ran.
_SPEC_TABLE_7 = {
    ReferralState.DRAFT: ("draft", None),
    ReferralState.SENT: ("requested", None),
    ReferralState.RECEIVED: ("received", None),
    ReferralState.ACCEPTED: ("accepted", None),
    ReferralState.DECLINED: ("rejected", None),
    ReferralState.SCHEDULED: ("in-progress", "scheduled"),
    ReferralState.SEEN: ("in-progress", "seen"),
    ReferralState.DOCUMENTED: ("in-progress", "documented"),
    ReferralState.RECONCILED: ("completed", None),
    ReferralState.CANCELLED: ("cancelled", None),
    ReferralState.AGED_OUT: ("failed", "aged-out"),
}


@pytest.mark.parametrize("state,expected", sorted(_SPEC_TABLE_7.items(), key=lambda kv: kv[0].value))
def test_the_projection_matches_the_spec_table(state, expected):
    assert project(state, hold=None) == expected


def test_the_spec_table_covers_every_state():
    """Otherwise the parametrisation above shrinks silently when a state is added, and the
    only thing left checking the new state would be the totality test -- which accepts any
    R4 code at all."""
    assert set(_SPEC_TABLE_7) == set(ReferralState)


def test_aged_out_projects_to_a_terminal_status_and_that_is_the_revisitable_call():
    """Spec 7 flags this one as a judgment call. `failed` is terminal and honest for
    reporting; the alternative leaves aged-out referrals in-progress forever. Pinned here
    so that revisiting it is a deliberate edit rather than a drift."""
    assert project(ReferralState.AGED_OUT, hold=None) == ("failed", "aged-out")


# --- the value set is the published one, not ours --------------------------------------


def test_r4_task_status_is_the_published_r4_value_set():
    """Twelve codes, from http://hl7.org/fhir/task-status. Pinned as a literal because the
    totality test asserts membership in it: a code quietly added here would make
    `project()` free to emit something no FHIR server will accept, and the totality test
    would still pass."""
    assert R4_TASK_STATUS == frozenset(
        {
            "draft",
            "requested",
            "received",
            "accepted",
            "rejected",
            "ready",
            "cancelled",
            "in-progress",
            "on-hold",
            "failed",
            "completed",
            "entered-in-error",
        }
    )


def test_the_membership_check_in_the_totality_test_can_fail():
    """`status in R4_TASK_STATUS` proves nothing if R4_TASK_STATUS admits anything."""
    assert "scheduled" not in R4_TASK_STATUS, "an internal state name is not an R4 code"
    assert "" not in R4_TASK_STATUS


def test_two_r4_codes_are_unreachable_from_this_model_and_which_ones():
    """`ready` has no referral meaning here -- nothing in the lifecycle is "the work can
    start now" separately from ACCEPTED -- and `entered-in-error` is a retraction, which is
    a Provenance concern (Plan 2b) rather than a state. Recorded so that the gap is a
    decision on the record instead of an omission nobody noticed."""
    reachable = {project(s, hold=h)[0] for s in ReferralState for h in (None, _HOLD)}
    assert R4_TASK_STATUS - reachable == {"ready", "entered-in-error"}


# --- the CodeSystem, and whether the check against it is worth anything ------------------


def test_the_codesystem_declares_exactly_what_the_projection_can_emit():
    """test_every_business_status_code_is_declared_in_our_codesystem passes trivially if
    project() never emits a business status, and it would pass unfalsifiably if the concept
    list were generated from ReferralState. This pins both ends: every emitted code is
    declared *and* every declared code is emitted, so a dead concept in a published
    CodeSystem is as much a failure as an undeclared emission."""
    emitted = {
        b
        for s in ReferralState
        for h in (None, _HOLD)
        if (b := project(s, hold=h)[1]) is not None
    }
    assert emitted, "nothing is emitted, so the declaration check above is vacuous"
    declared = {c["code"] for c in BUSINESS_STATUS["concept"]}
    assert declared == emitted


def test_the_declaration_check_would_reject_an_undeclared_code():
    """The other half of the same worry: `declared` has to be a set that can say no."""
    declared = {c["code"] for c in BUSINESS_STATUS["concept"]}
    assert "escalated" not in declared
    assert "in-progress" not in declared, "R4 status codes are a different vocabulary"


def test_a_business_status_is_emitted_for_every_state_under_hold_and_they_are_all_distinct():
    """The load-bearing property of the whole dual layer: on-hold discards the state in
    Task.status, so if the eleven holds did not produce eleven distinct business statuses
    the aging thresholds could not tell a hold from ACCEPTED from a hold from SCHEDULED."""
    held = [project(s, hold=_HOLD)[1] for s in ReferralState]
    assert len(set(held)) == len(list(ReferralState)) == 11


def test_the_codesystem_is_a_resource_with_a_stable_canonical_and_a_version():
    """The canonical url is the identity every stored Coding refers back to. Renaming it
    after publication silently invalidates them all, so the *shipped default* is still
    pinned as a literal: changing it should require editing a test that says why.

    What is no longer pinned is `BUSINESS_STATUS["url"]` against that literal -- a site may
    override it (see the block at the bottom of this file). The default is what an
    unconfigured deployment publishes, and that is the value this pins."""
    assert BUSINESS_STATUS["resourceType"] == "CodeSystem"
    assert (
        DEFAULT_BUSINESS_STATUS_URL
        == "https://referral-loop.health/fhir/CodeSystem/referral-business-status"
    )
    assert BUSINESS_STATUS["url"] == DEFAULT_BUSINESS_STATUS_URL
    assert BUSINESS_STATUS["version"] == "1.0.0"
    assert BUSINESS_STATUS["content"] == "complete"


def test_every_concept_carries_a_definition():
    """A published code with no definition is a code the receiving site has to guess at,
    and guessing is what businessStatus exists to remove."""
    for concept in BUSINESS_STATUS["concept"]:
        assert concept["display"].strip()
        assert concept["definition"].strip()


# --- the canonical is configurable, and the default is the interoperable one --------------
#
# The override is read once, at import, so these tests reload the module. The fixture puts
# both the environment and the module back afterwards: a leaked override would leave every
# later test in this session asserting against a canonical no deployment actually publishes.


@pytest.fixture
def published_under():
    """Reload codesystems.py with the override set (or unset), then restore both."""
    original = os.environ.get(BUSINESS_STATUS_URL_ENV)

    def _load(value: str | None):
        if value is None:
            os.environ.pop(BUSINESS_STATUS_URL_ENV, None)
        else:
            os.environ[BUSINESS_STATUS_URL_ENV] = value
        return importlib.reload(codesystems)

    yield _load

    if original is None:
        os.environ.pop(BUSINESS_STATUS_URL_ENV, None)
    else:
        os.environ[BUSINESS_STATUS_URL_ENV] = original
    importlib.reload(codesystems)


def test_the_shipped_default_is_published_when_nothing_is_configured(published_under):
    """Unset is the case that has to work, because it is the case that interoperates. Two
    sites that both leave this alone publish the same canonical, and a receiver can tell
    that their `seen` codes are the same concept."""
    mod = published_under(None)
    assert mod.BUSINESS_STATUS_URL == DEFAULT_BUSINESS_STATUS_URL
    assert mod.BUSINESS_STATUS["url"] == DEFAULT_BUSINESS_STATUS_URL


def test_a_site_override_is_honoured_and_reaches_the_published_resource(published_under):
    """Not just the constant -- the CodeSystem a receiver is handed. A resource that kept
    the default while the constant moved would be the drift this is meant to prevent."""
    override = "https://fhir.example-hospital.org/CodeSystem/referral-business-status"
    mod = published_under(override)
    assert mod.BUSINESS_STATUS_URL == override
    assert mod.BUSINESS_STATUS["url"] == override
    assert mod.BUSINESS_STATUS["url"] != DEFAULT_BUSINESS_STATUS_URL


def test_a_urn_uuid_override_is_honoured(published_under):
    """A site with no domain it can promise to keep has one legitimate way to mint a
    canonical, and it is this. `urn:uuid:` has no host and no path in the http sense, so a
    validator written only against https would reject the one form such a site can use."""
    override = "urn:uuid:53fefa32-fcbb-4ff8-8a92-55ee120877b7"
    mod = published_under(override)
    assert mod.BUSINESS_STATUS_URL == override
    assert mod.BUSINESS_STATUS["url"] == override


def test_a_urn_oid_override_is_honoured(published_under):
    """The other urn form a hospital plausibly already has an assigned arc under."""
    override = "urn:oid:2.16.840.1.113883.3.9999.1"
    mod = published_under(override)
    assert mod.BUSINESS_STATUS["url"] == override


def test_the_shipped_default_passes_the_check_applied_to_an_override(published_under):
    """Otherwise the validator could be arbitrarily strict and nobody would notice, because
    the default reaches the resource without going through it."""
    mod = published_under(DEFAULT_BUSINESS_STATUS_URL)
    assert mod.BUSINESS_STATUS["url"] == DEFAULT_BUSINESS_STATUS_URL


@pytest.mark.parametrize(
    "override",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("\t\n", id="tab-and-newline"),
    ],
)
def test_an_empty_override_refuses_at_import(published_under, override):
    """An empty value is somebody's `export REFERRAL_BUSINESS_STATUS_URL=$SOME_UNSET_VAR`.
    Falling back to the default there would be defensible; publishing an empty canonical
    would not, and treating it as "unset" hides that the deployment's config is broken."""
    with pytest.raises(ReferralLoopError) as exc:
        published_under(override)
    assert BUSINESS_STATUS_URL_ENV in str(exc.value)


@pytest.mark.parametrize(
    "override",
    [
        pytest.param("not a url", id="prose"),
        pytest.param("referral-loop.health/fhir/CodeSystem/x", id="no-scheme"),
        pytest.param("/fhir/CodeSystem/referral-business-status", id="relative-path"),
        pytest.param("https:", id="scheme-only"),
        pytest.param("urn:", id="urn-with-no-namespace"),
        pytest.param("https://example.org/cs with a space", id="embedded-space"),
        pytest.param("https://example.org/cs|1.0.0", id="version-pipe"),
    ],
)
def test_a_malformed_override_refuses_at_import(published_under, override):
    """A canonical that is not a URI is not a canonical. Refusing at import names the
    variable; publishing it means a receiving system stores Codings against a system
    identifier that resolves to nothing and matches nothing."""
    with pytest.raises(ReferralLoopError) as exc:
        published_under(override)
    assert BUSINESS_STATUS_URL_ENV in str(exc.value)


def test_surrounding_whitespace_is_stripped_rather_than_refused(published_under):
    """A trailing space in a .env file is never a decision, and `RetentionPolicy.from_env`
    strips its values the same way. Whitespace *inside* the url is a different case and is
    refused above -- there is no url it could have been."""
    mod = published_under("  https://fhir.example-hospital.org/CodeSystem/x\n")
    assert mod.BUSINESS_STATUS["url"] == "https://fhir.example-hospital.org/CodeSystem/x"


def test_the_refusal_is_not_something_every_override_gets(published_under):
    """The two refusal tests above prove nothing if the validator rejects everything."""
    mod = published_under("https://fhir.example-hospital.org/CodeSystem/x")
    assert mod.BUSINESS_STATUS["url"] == "https://fhir.example-hospital.org/CodeSystem/x"


def test_the_canonical_is_written_out_exactly_once_in_the_source_tree():
    """One source of truth, checked as a property of the tree rather than trusted.

    `task_status.py` emits no `system` today -- it returns bare code strings -- so there is
    nothing to keep in step yet. The moment an emitter does need one, the way it will be
    written is a second copy of the literal next to the Coding, and the two constants will
    then drift the first time a site sets the override: the CodeSystem published under the
    site's canonical, the Codings emitted under ours. This fails on the copy, before the
    drift exists to be found."""
    src = Path(__file__).resolve().parent.parent / "src" / "referral_loop"
    counted = {
        path.relative_to(src).as_posix(): path.read_text(encoding="utf-8").count(
            DEFAULT_BUSINESS_STATUS_URL
        )
        for path in src.rglob("*.py")
    }
    occurrences = {name: n for name, n in counted.items() if n}
    assert occurrences == {"fhir/codesystems.py": 1}, (
        "the canonical url is written out more than once, so an override reaches one copy "
        f"and not the other: {occurrences}. The second copy is the drift; there is exactly "
        "one assignment, DEFAULT_BUSINESS_STATUS_URL, and everything else reads "
        "BUSINESS_STATUS_URL."
    )
