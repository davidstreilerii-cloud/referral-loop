"""Two proofs per connector, reported apart.

/metadata is unauthenticated on Epic -- the CapabilityStatement is public. A preflight that
fetched only /metadata would report success for a connector whose signing key is wrong, whose
client id was never registered, or whose scopes were refused: the exact failures preflight
exists to catch.

So reachability and credentials are proven separately and printed on separate lines. Collapsing
them into one "connected" tick is how a connector ships that can reach a server it cannot
authenticate to.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .auth import AuthFailure, acquire_token
from .connectors import ConnectorProfile, ConnectorRegistry
from .egress import ConnectorUnreachable, EgressRefused, fetch

logger = logging.getLogger(__name__)

REACH = "reach"
CREDENTIAL = "credential"

# There is deliberately no VersionMismatch exception. A version disagreement is a *result* --
# one connector of several failed one of its two proofs -- and preflight's whole contract is to
# check every connector and report. An exception here would be a control-flow signal for
# something the caller has to render as data anyway, and the first thing any handler would do
# is convert it back into a ProofResult.


@dataclass(frozen=True)
class ProofResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ConnectorReport:
    connector_id: str
    reach: ProofResult
    credential: ProofResult

    @property
    def ok(self) -> bool:
        return self.reach.ok and self.credential.ok


def check_reach(registry: ConnectorRegistry, profile: ConnectorProfile) -> ProofResult:
    """Reachability, TLS trust, the allowlist path, and an acceptable FHIR version."""
    try:
        response = fetch(registry, profile, profile.metadata_url, headers={"Accept": "application/json"})
    except (EgressRefused, ConnectorUnreachable) as exc:
        return ProofResult(REACH, False, str(exc))

    if response.status != 200:
        return ProofResult(REACH, False, f"{profile.metadata_url} returned {response.status}")

    try:
        statement = json.loads(response.text())
    except json.JSONDecodeError:
        return ProofResult(REACH, False, "the CapabilityStatement is not JSON")

    version = statement.get("fhirVersion")
    if not isinstance(version, str):
        return ProofResult(REACH, False, "the CapabilityStatement declares no fhirVersion")
    if not profile.accepts_version(version):
        return ProofResult(
            REACH,
            False,
            f"endpoint speaks FHIR {version}, this connector accepts "
            f"{', '.join(profile.fhir_version)}",
        )
    return ProofResult(REACH, True, f"FHIR {version}")


def check_credential(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    *,
    now: datetime | None = None,
) -> ProofResult:
    """That the client id is registered, the key matches, and the scopes were granted."""
    try:
        token = acquire_token(registry, profile, now=now)
    except (AuthFailure, EgressRefused, ConnectorUnreachable) as exc:
        return ProofResult(CREDENTIAL, False, str(exc))
    # The token itself is never placed in the detail string -- the report is printed, logged and
    # pasted into tickets.
    return ProofResult(
        CREDENTIAL, True, f"token valid until {token.expires_at.isoformat()}, scopes "
        f"{' '.join(profile.auth.scopes)}"
    )


def preflight(registry: ConnectorRegistry, *, now: datetime | None = None) -> tuple[ConnectorReport, ...]:
    """Check every connector. Never stops at the first failure.

    One run should give the whole picture: during setup the common case is several connectors
    wrong in different ways, and a preflight that aborts turns that into one round trip each.
    """
    reports = []
    for profile in registry.connectors:
        reports.append(
            ConnectorReport(
                connector_id=profile.connector_id,
                reach=check_reach(registry, profile),
                credential=check_credential(registry, profile, now=now),
            )
        )
    return tuple(reports)


def format_report(reports: tuple[ConnectorReport, ...]) -> str:
    lines = []
    for report in reports:
        lines.append(f"{report.connector_id}:")
        for proof in (report.reach, report.credential):
            mark = "ok  " if proof.ok else "FAIL"
            lines.append(f"  [{mark}] {proof.name:<11} {proof.detail}")
    failed = [r.connector_id for r in reports if not r.ok]
    lines.append("")
    lines.append(
        f"{len(reports) - len(failed)}/{len(reports)} connectors passed both proofs"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return "\n".join(lines)
