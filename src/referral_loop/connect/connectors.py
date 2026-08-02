"""Who we call, and what we are willing to believe from them.

The mirror of peers.py, and deliberately shaped like it. That module answers "whose message is
this and what may they assert"; this one answers "who are we calling, how do we prove ourselves,
and what may we believe back". A reader who has understood one should recognise the other:
constrained id, reserved ids refused, authorities drawn from the same frozenset, and no default
for any value whose default would be a decision.

**Authorities are granted here for the same reason they are granted in peers.py.** A FHIR
endpoint that can move a referral's state is asserting exactly what an MLLP peer asserts, and a
rule that survives only one transport was never a rule. Nothing in this sub-project reads a
resource, so a read-only connector grants none -- but the field is not optional, because the day
an adapter lets a fetched DocumentReference close a loop, that has to be a line someone wrote.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import ReferralLoopError
from ..peers import AUTHORITIES, RESERVED_PEER_IDS

# The only auth mode in this sub-project. A tuple rather than a bare constant so an unknown
# value in a config file is a refused boot rather than a silently ignored line -- same shape as
# peers.TRANSPORTS.
SMART_BACKEND_SERVICES = "smart-backend-services"
AUTH_MODES = (SMART_BACKEND_SERVICES,)

# RSA only. Epic's published guidance is RS384; RS256 is accepted because some endpoints only
# offer it. ES384 is deliberately absent -- unverified against any endpoint we can reach, and an
# algorithm list is not the place to guess.
ALGORITHMS = ("RS384", "RS256")

# Identical to peers._PEER_ID_RE, and identical for the same reason: the id is written into
# audit rows and into every log line about the connector.
_CONNECTOR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_ORGANIZATION = 128
_MAX_VENDOR = 64
_MAX_URL = 512

_DEFAULT_PORTS = {"https": 443, "http": 80}

logger = logging.getLogger(__name__)


class ConnectorConfigError(ReferralLoopError):
    """The connector file is wrong. Refuse the boot; a default would be us deciding."""


def _refuse(message: str) -> ConnectorConfigError:
    return ConnectorConfigError(message)


def _require(entry: dict, key: str, what: str) -> object:
    if key not in entry:
        raise _refuse(f"{what}: {key} is required and has no default")
    return entry[key]


def _text(value: object, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _refuse(f"{what} must be a non-empty string")
    stripped = value.strip()
    if len(stripped) > limit:
        raise _refuse(f"{what} exceeds {limit} characters")
    return stripped


def _url(value: object, what: str, *, allow_plaintext: bool) -> str:
    raw = _text(value, what, _MAX_URL)
    parts = urlsplit(raw)
    if parts.scheme == "http" and not allow_plaintext:
        raise _refuse(
            f"{what} is http. Use https, or set allow_plaintext together with plaintext_hosts"
        )
    if parts.scheme not in _DEFAULT_PORTS:
        raise _refuse(f"{what} must be an https URL, got scheme {parts.scheme!r}")
    if not parts.hostname:
        raise _refuse(f"{what} has no host")
    return raw.rstrip("/")


def endpoint_of(url: str) -> tuple[str, str, int]:
    """`(scheme, host, port)`, with the default port made explicit.

    The allowlist compares these triples rather than URL strings, so that `https://h/a` and
    `https://h:443/b` are recognised as the same destination -- a string comparison would let a
    port-explicit spelling of an allowed host read as a different one.
    """
    parts = urlsplit(url)
    if parts.scheme not in _DEFAULT_PORTS:
        # Typed rather than the KeyError the dict lookup below would otherwise raise. At load
        # time _url has already refused anything but http/https, so this is unreachable from
        # config -- but check_allowed calls this on whatever URL it is handed, and the read
        # client will one day hand it a `next` link out of a Bundle. A KeyError there escapes
        # every except clause in fetch and surfaces as a crash rather than a refusal.
        raise _refuse(
            f"{url!r} has scheme {parts.scheme!r}; only http and https have a known default port"
        )
    host = (parts.hostname or "").lower()
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    return (parts.scheme, host, port)


@dataclass(frozen=True)
class ConnectorAuth:
    mode: str
    client_id: str
    private_key_file: Path
    key_id: str
    algorithm: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class ConnectorProfile:
    connector_id: str
    organization: str
    vendor: str
    fhir_base_url: str
    token_url: str
    fhir_version: tuple[str, ...]
    auth: ConnectorAuth
    authorities: frozenset[str]
    ca_file: Path | None

    def holds(self, authority: str) -> bool:
        return authority in self.authorities

    def accepts_version(self, version: str) -> bool:
        return version in self.fhir_version

    @property
    def metadata_url(self) -> str:
        return f"{self.fhir_base_url}/metadata"


def _auth_from(entry: object, *, what: str) -> ConnectorAuth:
    if not isinstance(entry, dict):
        raise _refuse(f"{what}: auth must be an object")

    mode = _text(_require(entry, "mode", what), f"{what} auth.mode", 64)
    if mode not in AUTH_MODES:
        raise _refuse(f"{what}: auth.mode {mode!r} is not one of {AUTH_MODES}")

    algorithm = _text(_require(entry, "algorithm", what), f"{what} auth.algorithm", 16)
    if algorithm not in ALGORITHMS:
        raise _refuse(f"{what}: auth.algorithm {algorithm!r} is not one of {ALGORITHMS}")

    key_ref = _text(_require(entry, "private_key_file", what), f"{what} auth.private_key_file", 512)
    if "BEGIN" in key_ref or "\n" in key_ref:
        raise _refuse(
            f"{what}: auth.private_key_file must be a path, not key material. A configuration "
            "file is committed, pasted into tickets and readable by everyone with repo access"
        )

    raw_scopes = _require(entry, "scopes", what)
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise _refuse(f"{what}: auth.scopes must be a non-empty list")
    scopes = tuple(_text(s, f"{what} auth.scopes entry", 128) for s in raw_scopes)

    return ConnectorAuth(
        mode=mode,
        client_id=_text(_require(entry, "client_id", what), f"{what} auth.client_id", 256),
        private_key_file=Path(key_ref),
        key_id=_text(_require(entry, "key_id", what), f"{what} auth.key_id", 128),
        algorithm=algorithm,
        scopes=scopes,
    )


def _profile_from(entry: object, *, allow_plaintext: bool) -> ConnectorProfile:
    if not isinstance(entry, dict):
        raise _refuse("each connectors entry must be an object")

    raw_id = _require(entry, "connector_id", "connector")
    if not isinstance(raw_id, str) or not _CONNECTOR_ID_RE.match(raw_id):
        raise _refuse(
            f"connector_id {raw_id!r} must match {_CONNECTOR_ID_RE.pattern} -- it is written "
            "into audit rows and log lines"
        )
    if raw_id in RESERVED_PEER_IDS:
        raise _refuse(
            f"connector_id {raw_id!r} is reserved. Those ids already carry meanings in audit "
            "rows and loop_events.assertion_source -- 'coordinator' in particular means no "
            "message asserted the transition at all, so a connector able to claim it could "
            "attribute its own action to a human"
        )
    what = f"connector {raw_id}"

    raw_versions = _require(entry, "fhir_version", what)
    if not isinstance(raw_versions, list) or not raw_versions:
        raise _refuse(f"{what}: fhir_version must be a non-empty list of accepted versions")
    versions = tuple(_text(v, f"{what} fhir_version entry", 16) for v in raw_versions)

    raw_authorities = entry.get("authorities")
    if raw_authorities is None or not isinstance(raw_authorities, list):
        raise _refuse(f"{what}: authorities is required and may be an empty list, but not absent")
    unknown = sorted(a for a in raw_authorities if a not in AUTHORITIES)
    if unknown:
        raise _refuse(f"{what}: unknown authorities {unknown}, known are {sorted(AUTHORITIES)}")

    tls = entry.get("tls") or {}
    if not isinstance(tls, dict):
        raise _refuse(f"{what}: tls must be an object")
    ca_raw = tls.get("ca_file")
    ca_file = Path(_text(ca_raw, f"{what} tls.ca_file", 512)) if ca_raw is not None else None

    return ConnectorProfile(
        connector_id=raw_id,
        organization=_text(_require(entry, "organization", what), f"{what} organization", _MAX_ORGANIZATION),
        # Required despite being inert in A. A site profile that cannot say what it is talking
        # to is missing the point, and one exempt row would falsify the rule the whole table
        # rests on -- that nothing here is assumed on the operator's behalf.
        vendor=_text(_require(entry, "vendor", what), f"{what} vendor", _MAX_VENDOR),
        fhir_base_url=_url(_require(entry, "fhir_base_url", what), f"{what} fhir_base_url", allow_plaintext=allow_plaintext),
        token_url=_url(_require(entry, "token_url", what), f"{what} token_url", allow_plaintext=allow_plaintext),
        fhir_version=versions,
        auth=_auth_from(_require(entry, "auth", what), what=what),
        authorities=frozenset(raw_authorities),
        ca_file=ca_file,
    )


@dataclass(frozen=True)
class ConnectorRegistry:
    connectors: tuple[ConnectorProfile, ...]
    allow_plaintext: bool
    plaintext_hosts: frozenset[str]

    @classmethod
    def from_mapping(cls, data: object) -> "ConnectorRegistry":
        if not isinstance(data, dict):
            raise _refuse("the connector file must contain a JSON object")

        allow_plaintext = bool(data.get("allow_plaintext", False))
        raw_hosts = data.get("plaintext_hosts")
        if allow_plaintext and (not isinstance(raw_hosts, list) or not raw_hosts):
            raise _refuse(
                "allow_plaintext is set but plaintext_hosts is missing or empty. Both are "
                "required, so neither is reachable by a typo in the other"
            )
        if raw_hosts and not allow_plaintext:
            raise _refuse("plaintext_hosts is set but allow_plaintext is not")
        hosts = frozenset(_text(h, "plaintext_hosts entry", 256).lower() for h in (raw_hosts or []))

        raw = data.get("connectors")
        if not isinstance(raw, list) or not raw:
            raise _refuse("connectors must be a non-empty list")

        profiles = tuple(_profile_from(e, allow_plaintext=allow_plaintext) for e in raw)
        seen: set[str] = set()
        for p in profiles:
            if p.connector_id in seen:
                raise _refuse(f"duplicate connector_id {p.connector_id!r}")
            seen.add(p.connector_id)

        return cls(connectors=profiles, allow_plaintext=allow_plaintext, plaintext_hosts=hosts)

    def get(self, connector_id: str) -> ConnectorProfile:
        for p in self.connectors:
            if p.connector_id == connector_id:
                return p
        raise _refuse(f"no connector named {connector_id!r}; configured: {self.connector_ids()}")

    def connector_ids(self) -> tuple[str, ...]:
        return tuple(p.connector_id for p in self.connectors)

    def warn_on_peer_collisions(self, peer_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Names used by both an inbound peer and an outbound connector.

        A warning rather than a refusal. One organization on both ends is the natural thing to
        configure, and the two files are read by different code paths, so nothing resolves
        wrongly -- but audit rows from the two directions sit next to each other, and a reader
        who has to work out which direction `example-ris` meant has been handed a puzzle we
        could have flagged at boot.
        """
        shared = tuple(sorted(set(self.connector_ids()) & set(peer_ids)))
        for name in shared:
            logger.warning(
                "connector_id %r is also a configured peer id. Inbound and outbound are "
                "resolved separately, so nothing is ambiguous to the code -- but audit rows "
                "from both directions will carry this name.",
                name,
            )
        return shared

    def endpoints(self) -> frozenset[tuple[str, str, int]]:
        """The egress allowlist. Every destination this process may reach, and nothing else."""
        found: set[tuple[str, str, int]] = set()
        for p in self.connectors:
            found.add(endpoint_of(p.fhir_base_url))
            found.add(endpoint_of(p.token_url))
        return frozenset(found)


def load_connector_registry(path: Path | str) -> ConnectorRegistry:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _refuse(f"connector file not found: {target}") from exc
    except OSError as exc:
        raise _refuse(f"connector file unreadable: {target}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _refuse(f"connector file is not valid JSON: {target}: {exc}") from exc
    return ConnectorRegistry.from_mapping(data)
