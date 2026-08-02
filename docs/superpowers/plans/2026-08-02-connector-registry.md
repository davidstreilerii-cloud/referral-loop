# Connector Registry and Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `connect/` — a validated registry of outbound FHIR endpoints, a SMART Backend Services credential flow, a hard egress allowlist, and a `connectors --verify` CLI mode that proves reachability and credentials separately.

**Architecture:** A new `connect/` package plus one new CLI mode. `connectors.py` mirrors `peers.py` — same id constraint, same authority vocabulary, same refuse-rather-than-default posture — pointed outbound instead of inbound. `egress.py` is the only module in the repo permitted to import `urllib.request`, enforced by an AST test. Nothing in `registry.py`, `store.py`, `listener.py`, `matcher.py` or `peers.py` changes.

**Tech Stack:** Python 3.12, stdlib `urllib.request` (no new runtime dependency), `cryptography` for RS384/RS256 signing (already a dependency).

**Spec:** `docs/superpowers/specs/2026-08-02-connector-registry-design.md`. Section references below (§4.1, §5.3, …) point at it. Read it before Task 1.

---

## Baseline

- **Always use the repo venv:** `./.venv/Scripts/python.exe`. `healthcare-rag` is pip-installed globally and still contains a `healthcare_rag/referral_loop/` package; global python lets a stale import resolve against the parent's tree.
- Before starting, record the suite result: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -3`. Every task below adds tests and must not change the count of pre-existing ones.
- **This repo has a concurrent writer.** Plan 2a (`docs/superpowers/plans/2026-08-02-canonical-model.md`) is being executed by another agent; as of 2026-08-02 its Tasks 1–3 have landed (`core/states.py`, `core/models.py`) and Tasks 4–6 have not. Plan 2a touches `core/`, `fhir/`, `migration.py`, `tests/test_import_closure.py` and its own new test files. **`tests/test_import_closure.py` is the one file both plans modify** — run `git pull`/`git log` before Task 1 and again before Task 10, and expect to merge rather than overwrite there.
- This plan does **not** depend on Plan 2a. Nothing here imports `core/`.

## File structure

| file | responsibility | imports |
|---|---|---|
| `src/referral_loop/connect/__init__.py` | package marker; no re-exports | — |
| `src/referral_loop/connect/connectors.py` | `ConnectorProfile`, `ConnectorAuth`, `ConnectorRegistry`, `load_connector_registry`, `ConnectorConfigError` | `..errors`, `..peers` (two frozensets only), stdlib |
| `src/referral_loop/connect/egress.py` | the allowlist, the opener, `fetch()`, `EgressRefused`, `ConnectorUnreachable` | `.connectors`, `..errors`, `urllib.request`, `ssl` |
| `src/referral_loop/connect/auth.py` | `build_assertion`, `acquire_token`, `Token`, `TokenCache`, `AuthFailure` | `.connectors`, `.egress`, `..errors`, `cryptography` |
| `src/referral_loop/connect/preflight.py` | `check_reach`, `check_credential`, `preflight`, `format_report`, `VersionMismatch` | `.connectors`, `.egress`, `.auth`, `..errors` |
| `src/referral_loop/cli.py` | **modify** — `connectors` mode, narrowed `--help` text | |
| `tests/_certs.py` | x509 generation extracted from `test_peer_identity.py` | |
| `tests/_fhirserver.py` | threaded TLS server serving `/metadata` and a token endpoint | `._certs` |
| `tests/test_connectors.py` · `tests/test_egress.py` · `tests/test_connector_auth.py` · `tests/test_preflight.py` | | |
| `tests/test_import_closure.py` | **modify** — `CORE_FORBIDDEN` entry, AST egress test | |
| `tests/test_peer_identity.py` | **modify** — import extracted cert helper; no logic change | |
| `README.md` | **modify** — narrow the egress claim | |

---

### Task 1: The package, and the closure rule that keeps it a layer

**Files:**
- Create: `src/referral_loop/connect/__init__.py`
- Modify: `tests/test_import_closure.py`

- [ ] **Step 1: Read the existing closure test**

Run: `cd "$REPO" && cat tests/test_import_closure.py`

Note the `CORE_FORBIDDEN` tuple and the clean-subprocess helper. Plan 2a added both; if `CORE_FORBIDDEN` is not present yet, that plan's Task 1 has not landed — add `referral_loop.connect` to the existing `FORBIDDEN` list instead and note it in the commit message.

- [ ] **Step 2: Add the forbidden entry**

In `tests/test_import_closure.py`, add to the `CORE_FORBIDDEN` tuple, immediately after the `"referral_loop.migration",` entry:

```python
    # The domain layer must not reach the network any more than it reaches the store.
    # core/ has to stay callable from a batch job with no connector configured at all.
    "referral_loop.connect",
    "urllib.request",
    "ssl",
```

- [ ] **Step 3: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -v`
Expected: PASS. It passes vacuously today — `connect/` does not exist — which is the point: the rule is in place before the code it constrains.

- [ ] **Step 4: Create the package**

`src/referral_loop/connect/__init__.py`:

```python
"""Who we call, how we prove ourselves, and what we may believe back.

peers.py settled the same question for inbound traffic -- whose message is this, and what is
that peer allowed to assert. It answered that a self-asserted origin is not an origin: identity
comes from a credential the sender cannot choose, and authority is granted per peer rather than
inferred from the message type.

This package is that decision pointed outbound. A FHIR endpoint we read from is asserting things
exactly as an MLLP peer does -- if a fetched DocumentReference can close a loop, that endpoint
just exercised `result` -- so it draws from the same authority vocabulary and refuses the boot
on the same kind of missing value.

egress.py is the only module in the repository permitted to import urllib.request. That is
enforced by an AST test rather than by convention, because it is the property the narrowed
README claim rests on: no model calls, and egress only to configured connectors.
"""
```

No re-exports. Same reason as `core/__init__.py`: a `from .connectors import *` here makes `connect` and `connect.connectors` two names for one thing, and that is how packages start growing sideways.

- [ ] **Step 5: Confirm the closure test still passes with the package present**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd "$REPO"
git add src/referral_loop/connect/__init__.py tests/test_import_closure.py
git commit -m "feat(connect): the outbound package, and the rule that keeps core off the network

The closure entry lands before the code it constrains. core/ must not reach the network any
more than it reaches the store -- it has to stay callable from a batch job with no connector
configured at all, and a single convenience import is all it takes to lose that."
```

---

### Task 2: The connector profile and its field rules

**Files:**
- Create: `src/referral_loop/connect/connectors.py`
- Create: `tests/test_connectors.py`

Field rules are spec §4.1. Read it; this task implements that table exactly.

- [ ] **Step 1: Write the failing test**

`tests/test_connectors.py`:

```python
"""The outbound registry, and the values it refuses to guess."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from referral_loop.connect.connectors import (
    ConnectorConfigError,
    ConnectorRegistry,
    load_connector_registry,
)


def _profile(**overrides) -> dict:
    base = {
        "connector_id": "example-med",
        "organization": "Example Medical Center",
        "vendor": "epic",
        "fhir_base_url": "https://fhir.example-med.example/api/FHIR/R4",
        "token_url": "https://fhir.example-med.example/oauth2/token",
        "fhir_version": ["4.0.1"],
        "auth": {
            "mode": "smart-backend-services",
            "client_id": "abc-123",
            "private_key_file": "/etc/referral/example-med-signing.pem",
            "key_id": "example-med-2026",
            "algorithm": "RS384",
            "scopes": ["system/Patient.read"],
        },
        "authorities": [],
    }
    base.update(overrides)
    return base


def _registry(*profiles, **top) -> ConnectorRegistry:
    data = {"connectors": list(profiles) or [_profile()]}
    data.update(top)
    return ConnectorRegistry.from_mapping(data)


def test_a_well_formed_profile_loads():
    reg = _registry()
    assert reg.connector_ids() == ("example-med",)
    ku = reg.get("example-med")
    assert ku.organization == "Example Medical Center"
    assert ku.auth.algorithm == "RS384"
    assert ku.accepts_version("4.0.1")
    assert not ku.accepts_version("3.0.2")


@pytest.mark.parametrize(
    "field",
    [
        "connector_id",
        "organization",
        "vendor",
        "fhir_base_url",
        "token_url",
        "fhir_version",
        "auth",
    ],
)
def test_a_missing_required_field_refuses_rather_than_defaulting(field):
    """Every one of these is a security or clinical decision. A default would be us making it."""
    broken = _profile()
    del broken[field]
    with pytest.raises(ConnectorConfigError, match=field):
        _registry(broken)


@pytest.mark.parametrize("key", ["client_id", "private_key_file", "key_id", "algorithm", "scopes"])
def test_a_missing_auth_field_refuses(key):
    auth = dict(_profile()["auth"])
    del auth[key]
    with pytest.raises(ConnectorConfigError, match=key):
        _registry(_profile(auth=auth))


@pytest.mark.parametrize("bad", ["EXAMPLE-MED", "-ku", "ku med", "k" * 65, "", "ku/med"])
def test_a_malformed_connector_id_refuses(bad):
    """The id lands in audit rows and log lines, so it is constrained once here rather than
    sanitized at each site -- the same argument peers.py makes for a peer id."""
    with pytest.raises(ConnectorConfigError, match="connector_id"):
        _registry(_profile(connector_id=bad))


@pytest.mark.parametrize("url_field", ["fhir_base_url", "token_url"])
def test_a_plaintext_url_refuses_without_the_opt_out(url_field):
    with pytest.raises(ConnectorConfigError, match="https"):
        _registry(_profile(**{url_field: "http://fhir.example-med.example/api/FHIR/R4"}))


def test_an_unknown_authority_refuses():
    """Drawn from peers.AUTHORITIES so there is one vocabulary rather than two that drift."""
    with pytest.raises(ConnectorConfigError, match="authorit"):
        _registry(_profile(authorities=["admit"]))


def test_a_granted_authority_is_readable():
    reg = _registry(_profile(authorities=["result"]))
    assert reg.get("example-med").holds("result")
    assert not reg.get("example-med").holds("cancel")


def test_an_empty_fhir_version_list_refuses():
    with pytest.raises(ConnectorConfigError, match="fhir_version"):
        _registry(_profile(fhir_version=[]))


def test_an_empty_scope_list_refuses():
    with pytest.raises(ConnectorConfigError, match="scopes"):
        auth = dict(_profile()["auth"])
        auth["scopes"] = []
        _registry(_profile(auth=auth))


def test_an_unknown_auth_mode_refuses():
    auth = dict(_profile()["auth"])
    auth["mode"] = "client-secret"
    with pytest.raises(ConnectorConfigError, match="mode"):
        _registry(_profile(auth=auth))


def test_an_unknown_algorithm_refuses():
    auth = dict(_profile()["auth"])
    auth["algorithm"] = "HS256"
    with pytest.raises(ConnectorConfigError, match="algorithm"):
        _registry(_profile(auth=auth))


def test_pem_content_pasted_where_a_path_belongs_refuses():
    """A configuration file gets committed, pasted into tickets, and read by everyone with repo
    access. The signing key is the whole proof of our identity to the remote."""
    auth = dict(_profile()["auth"])
    auth["private_key_file"] = "-----BEGIN PRIVATE KEY-----\nMIIEvQ...\n-----END PRIVATE KEY-----"
    with pytest.raises(ConnectorConfigError, match="path"):
        _registry(_profile(auth=auth))


def test_two_connectors_may_not_share_an_id():
    with pytest.raises(ConnectorConfigError, match="duplicate"):
        _registry(_profile(), _profile())


def test_an_empty_connector_list_refuses():
    with pytest.raises(ConnectorConfigError, match="connectors"):
        ConnectorRegistry.from_mapping({"connectors": []})


def test_allow_plaintext_without_hosts_refuses():
    """Two independent statements, so neither is reachable by a typo in the other -- the same
    construction peers.py uses for its plaintext listener."""
    with pytest.raises(ConnectorConfigError, match="plaintext_hosts"):
        _registry(_profile(), allow_plaintext=True)


def test_plaintext_hosts_without_the_flag_refuses():
    with pytest.raises(ConnectorConfigError, match="allow_plaintext"):
        _registry(_profile(), plaintext_hosts=["fhir.local"])


def test_allow_plaintext_with_hosts_permits_an_http_url():
    reg = _registry(
        _profile(
            fhir_base_url="http://fhir.local/api/FHIR/R4",
            token_url="http://fhir.local/oauth2/token",
        ),
        allow_plaintext=True,
        plaintext_hosts=["fhir.local"],
    )
    assert reg.allow_plaintext
    assert reg.plaintext_hosts == frozenset({"fhir.local"})


def test_plaintext_hosts_are_lowercased():
    """A host is compared against a URL's parsed hostname, which urlsplit lowercases."""
    reg = _registry(
        _profile(
            fhir_base_url="http://fhir.local/api/FHIR/R4",
            token_url="http://fhir.local/oauth2/token",
        ),
        allow_plaintext=True,
        plaintext_hosts=["FHIR.LOCAL"],
    )
    assert reg.plaintext_hosts == frozenset({"fhir.local"})


def test_load_reads_a_file(tmp_path: Path):
    path = tmp_path / "connectors.json"
    path.write_text(json.dumps({"connectors": [_profile()]}), encoding="utf-8")
    assert load_connector_registry(path).connector_ids() == ("example-med",)


def test_load_refuses_a_missing_file(tmp_path: Path):
    with pytest.raises(ConnectorConfigError, match="not found"):
        load_connector_registry(tmp_path / "absent.json")


def test_load_refuses_malformed_json(tmp_path: Path):
    path = tmp_path / "connectors.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConnectorConfigError, match="JSON"):
        load_connector_registry(path)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connectors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'referral_loop.connect.connectors'`

- [ ] **Step 3: Write the module**

`src/referral_loop/connect/connectors.py`:

```python
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
        raise _refuse(f"no connector named {connector_id!r}")

    def connector_ids(self) -> tuple[str, ...]:
        return tuple(p.connector_id for p in self.connectors)

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
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connectors.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add src/referral_loop/connect/connectors.py tests/test_connectors.py
git commit -m "feat(connect): the outbound registry, and the values it refuses to guess

Shaped like peers.py because it is the same decision pointed the other way. Authorities come
from peers.AUTHORITIES rather than a second list, so a FHIR endpoint that can close a loop is
governed by the rule that already governs an MLLP peer that can.

private_key_file refuses PEM content pasted where a path belongs. A config file is committed,
pasted into tickets and readable by everyone with repo access; the signing key is the whole
proof of our identity to the remote."
```

---

### Task 3: Reserved and colliding ids

**Files:**
- Modify: `src/referral_loop/connect/connectors.py`
- Modify: `tests/test_connectors.py`

Task 2 already refuses `RESERVED_PEER_IDS`. This task adds the *warning* half of spec §4.1.1 — a connector id that duplicates a configured peer id — which is separate because it is not a refusal.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connectors.py`:

```python
from referral_loop.peers import RESERVED_PEER_IDS


@pytest.mark.parametrize("reserved", sorted(RESERVED_PEER_IDS))
def test_a_reserved_peer_id_is_refused_as_a_connector_id(reserved):
    """The two namespaces are separate files, which does not mean they may overlap. These five
    already carry meanings in audit rows; 'coordinator' means no message asserted the transition
    at all, so a connector able to claim it could attribute its own action to a human."""
    with pytest.raises(ConnectorConfigError, match="reserved"):
        _registry(_profile(connector_id=reserved))


def test_an_id_shared_with_a_configured_peer_warns_but_loads(caplog):
    """One organization on both ends of the relationship is the natural case, and the two files
    are read by different code paths -- so the ambiguity is only in the reader's head. But audit
    rows from the two directions will sit next to each other."""
    reg = _registry(_profile(connector_id="example-ris"))
    with caplog.at_level("WARNING"):
        collisions = reg.warn_on_peer_collisions(("example-ris", "example-lab"))
    assert collisions == ("example-ris",)
    assert "example-ris" in caplog.text


def test_no_collision_is_silent(caplog):
    reg = _registry()
    with caplog.at_level("WARNING"):
        assert reg.warn_on_peer_collisions(("example-ris",)) == ()
    assert caplog.text == ""
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connectors.py -k collision -v`
Expected: FAIL — `AttributeError: 'ConnectorRegistry' object has no attribute 'warn_on_peer_collisions'`

- [ ] **Step 3: Add the method**

In `src/referral_loop/connect/connectors.py`, add the logging import at the top:

```python
import logging
```

and after the other module-level constants:

```python
logger = logging.getLogger(__name__)
```

Then add to `ConnectorRegistry`, after `connector_ids`:

```python
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
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connectors.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add src/referral_loop/connect/connectors.py tests/test_connectors.py
git commit -m "feat(connect): refuse reserved ids, warn on ids shared with a peer

Reserved is a refusal because 'coordinator' means no message asserted the transition at all --
a connector able to claim it could attribute its own action to a human. A collision with a
configured peer is only a warning: the two files are read by different code paths, so nothing
resolves wrongly, but the audit rows sit next to each other."
```

---

### Task 4: Egress — the allowlist and four wrong defaults

**Files:**
- Create: `src/referral_loop/connect/egress.py`
- Create: `tests/test_egress.py`
- Modify: `tests/test_import_closure.py`

Spec §5.2 and §5.3. The four overridden defaults are the substance of this module.

- [ ] **Step 1: Write the failing test**

`tests/test_egress.py`:

```python
"""The allowlist, and the four urllib defaults that are wrong for this client."""

from __future__ import annotations

import urllib.request

import pytest

from referral_loop.connect.connectors import ConnectorRegistry
from referral_loop.connect.egress import (
    MAX_RESPONSE_BYTES,
    EgressRefused,
    check_allowed,
    build_opener,
)


def _registry(**top) -> ConnectorRegistry:
    data = {
        "connectors": [
            {
                "connector_id": "example-med",
                "organization": "Example Medical Center",
                "vendor": "epic",
                "fhir_base_url": "https://fhir.example-med.example/api/FHIR/R4",
                "token_url": "https://auth.example-med.example/oauth2/token",
                "fhir_version": ["4.0.1"],
                "auth": {
                    "mode": "smart-backend-services",
                    "client_id": "abc",
                    "private_key_file": "/etc/k.pem",
                    "key_id": "k1",
                    "algorithm": "RS384",
                    "scopes": ["system/Patient.read"],
                },
                "authorities": [],
            }
        ]
    }
    data.update(top)
    return ConnectorRegistry.from_mapping(data)


def test_a_configured_host_is_allowed():
    check_allowed(_registry(), "https://fhir.example-med.example/api/FHIR/R4/metadata")


def test_the_token_host_is_allowed_too():
    check_allowed(_registry(), "https://auth.example-med.example/oauth2/token")


def test_an_unconfigured_host_is_refused():
    with pytest.raises(EgressRefused, match="not a configured connector endpoint"):
        check_allowed(_registry(), "https://evil.example/api")


def test_a_configured_host_on_another_port_is_refused():
    """The allowlist is (scheme, host, port). A host that is allowed on 443 is not thereby
    allowed on 8443 -- that is a different service."""
    with pytest.raises(EgressRefused):
        check_allowed(_registry(), "https://fhir.example-med.example:8443/api")


def test_an_explicit_default_port_is_the_same_destination():
    """https://h/a and https://h:443/b must not read as two different hosts."""
    check_allowed(_registry(), "https://fhir.example-med.example:443/api/FHIR/R4/metadata")


def test_plaintext_is_refused_even_to_a_configured_host():
    with pytest.raises(EgressRefused, match="https"):
        check_allowed(_registry(), "http://fhir.example-med.example/api")


def _plaintext_registry() -> ConnectorRegistry:
    data = {
        "allow_plaintext": True,
        "plaintext_hosts": ["fhir.local"],
        "connectors": [
            {
                "connector_id": "dev",
                "organization": "Local development",
                "vendor": "epic",
                "fhir_base_url": "http://fhir.local/api/FHIR/R4",
                "token_url": "http://fhir.local/oauth2/token",
                "fhir_version": ["4.0.1"],
                "auth": {
                    "mode": "smart-backend-services",
                    "client_id": "abc",
                    "private_key_file": "/etc/k.pem",
                    "key_id": "k1",
                    "algorithm": "RS384",
                    "scopes": ["system/Patient.read"],
                },
                "authorities": [],
            }
        ],
    }
    return ConnectorRegistry.from_mapping(data)


def test_plaintext_is_allowed_when_both_statements_are_present():
    check_allowed(_plaintext_registry(), "http://fhir.local/api/FHIR/R4/metadata")


def test_plaintext_to_a_host_not_named_in_plaintext_hosts_is_refused():
    """allow_plaintext is not a global switch. It permits the named hosts and nothing else, so
    turning it on for a dev server does not open every configured connector to plaintext."""
    registry = _plaintext_registry()
    with pytest.raises(EgressRefused, match="https"):
        check_allowed(registry, "http://other.local/api")


def test_the_opener_ignores_proxy_environment_variables(monkeypatch):
    """urllib reads http_proxy/https_proxy from the environment by default. On a hospital
    network that is frequently set, and honouring it routes PHI and credentials through a host
    nobody put in the registry.

    The assertion is that **no** ProxyHandler survives in the chain, which is subtler than it
    looks and is worth stating. Passing `ProxyHandler({})` to build_opener does two things:
    build_opener sees an instance of ProxyHandler among the handlers and therefore skips
    installing its own environment-reading default, and then add_handler discards the empty one
    because a ProxyHandler built from an empty mapping registers no *_open methods and
    add_handler only keeps handlers that register at least one. Both steps have to happen for
    the environment to be ignored.

    Which is exactly why this test asserts zero rather than one: with the environment set, if
    someone deletes the `ProxyHandler({})` argument as apparently useless, build_opener installs
    its default, that default reads http_proxy, it registers http_open/https_open, add_handler
    keeps it -- and this test goes from zero to one and fails. The empty handler looks inert and
    is load-bearing."""
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
    opener = build_opener(_registry().get("example-med"))
    proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies == [], (
        "a ProxyHandler in the chain means the environment was consulted: "
        f"{[p.proxies for p in proxies]}"
    )


def test_the_opener_refuses_redirects():
    """A 302 from the token endpoint hands our client assertion -- or a live bearer token -- to
    whoever answered."""
    from referral_loop.connect.egress import _RefuseRedirects

    handler = _RefuseRedirects()
    with pytest.raises(EgressRefused, match="redirect"):
        handler.redirect_request(
            req=None, fp=None, code=302, msg="Found",
            headers={}, newurl="https://fhir.example-med.example/elsewhere",
        )


def test_a_redirect_to_an_allowed_host_is_still_refused():
    """Not re-resolved against the allowlist: a redirect to a listed host is still a server we
    did not intend to talk to for this request."""
    from referral_loop.connect.egress import _RefuseRedirects

    with pytest.raises(EgressRefused, match="redirect"):
        _RefuseRedirects().redirect_request(
            req=None, fp=None, code=301, msg="Moved",
            headers={}, newurl="https://auth.example-med.example/oauth2/token",
        )


def test_the_response_cap_is_declared_and_bounded():
    assert 0 < MAX_RESPONSE_BYTES <= 64 * 1024 * 1024
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_egress.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'referral_loop.connect.egress'`

- [ ] **Step 3: Write the module**

`src/referral_loop/connect/egress.py`:

```python
"""The only module in this repository that opens an outbound socket.

That is enforced by an AST test in tests/test_import_closure.py rather than by convention,
because it is the property the narrowed README claim rests on: no model calls, and egress only
to configured connectors. A second `import urllib.request` anywhere under src/ fails the suite.

`urllib` rather than httpx or requests: the package has two runtime dependencies and
test_install_closure asserts the surface stays small. Nothing here needs pooling, HTTP/2 or a
retry policy. If the read client's pagination and backoff genuinely outgrow the stdlib, adding a
dependency then is a decision made with evidence rather than in advance.

Four urllib defaults are reasonable for a general client and unsafe for this one. Each is
overridden below and each override has a test.
"""
from __future__ import annotations

import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..errors import ReferralLoopError
from .connectors import ConnectorProfile, ConnectorRegistry, endpoint_of

logger = logging.getLogger(__name__)

# Generous and explicit rather than absent. An Epic CapabilityStatement is legitimately large --
# megabytes -- but a response is still something a hostile or broken server chooses the size of.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# An unbounded wait is a resource the other end controls. Same argument peers.py makes with
# TLS_HANDSHAKE_SECONDS, one layer up.
DEFAULT_TIMEOUT_SECONDS = 30.0

# TLS 1.2 floor, matching peers._MINIMUM_TLS. Older versions are not a compatibility question
# for a link being configured from scratch on both ends.
_MINIMUM_TLS = ssl.TLSVersion.TLSv1_2


class EgressRefused(ReferralLoopError):
    """A request would have left for somewhere the registry does not name.

    Never retried. This is a configuration bug or an attempted redirect, and neither becomes
    acceptable on a second attempt.
    """


class ConnectorUnreachable(ReferralLoopError):
    """Network or TLS failure reaching a configured endpoint."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, including one to an allowlisted host.

    urllib follows redirects by default. A 302 from the token endpoint sends our signed client
    assertion -- or a live bearer token -- to whoever answered, and the assertion is replayable
    until its exp.

    Not re-resolved against the allowlist, deliberately: a redirect to a *listed* host is still
    a server we did not intend to talk to for this request, and a FHIR base URL that redirects
    is a misconfiguration worth surfacing rather than absorbing.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise EgressRefused(
            f"refused a {code} redirect to {newurl!r}. Redirects are never followed: the "
            "destination of a signed credential is a local decision, not the remote's"
        )


def check_allowed(registry: ConnectorRegistry, url: str) -> None:
    """Raise unless `url` names a destination the registry configured."""
    scheme, host, port = endpoint_of(url)
    if scheme != "https" and not (registry.allow_plaintext and host in registry.plaintext_hosts):
        raise EgressRefused(
            f"refused {url!r}: https is required. Plaintext needs allow_plaintext together "
            "with the host named in plaintext_hosts"
        )
    if (scheme, host, port) not in registry.endpoints():
        raise EgressRefused(
            f"refused {url!r}: {host}:{port} is not a configured connector endpoint"
        )


def _tls_context(profile: ConnectorProfile) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(profile.ca_file) if profile.ca_file else None)
    context.minimum_version = _MINIMUM_TLS
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def build_opener(profile: ConnectorProfile) -> urllib.request.OpenerDirector:
    """An opener with the four defaults corrected.

    Built per request rather than cached. There is no pooling to preserve, and a per-connector
    TLS context means a shared opener would need keying anyway.
    """
    return urllib.request.build_opener(
        # DO NOT DELETE THIS AS DEAD WEIGHT. It looks inert and is load-bearing, by a two-step
        # mechanism worth spelling out because the obvious reading is wrong.
        #
        # build_opener installs its own ProxyHandler -- which reads http_proxy/https_proxy from
        # the environment -- unless an instance of ProxyHandler is among the handlers passed in.
        # Passing this one suppresses that default. Then add_handler drops this one too, because
        # a ProxyHandler built from an empty mapping registers no *_open methods and add_handler
        # keeps only handlers that register at least one.
        #
        # So the opener ends up with no ProxyHandler whatsoever, which is the goal: on a hospital
        # network https_proxy is frequently set, and honouring it would route PHI and credentials
        # through a host nobody put in the registry. Remove this argument and the default comes
        # back. tests/test_egress.py asserts the chain is proxy-free with the environment set,
        # which is what fails if someone tidies this away.
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_tls_context(profile)),
        _RefuseRedirects(),
    )


def fetch(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Response:
    """The one call. Refuses before opening a socket if the destination is not configured."""
    check_allowed(registry, url)

    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)

    opener = build_opener(profile)
    try:
        with opener.open(request, timeout=timeout) as raw:
            return Response(status=raw.status, body=_read_capped(raw, url))
    except urllib.error.HTTPError as exc:
        # A 4xx is a response, not a transport failure, and its body carries the reason -- the
        # token endpoint returns invalid_client as a 400 with JSON. Callers need to read it.
        with exc:
            return Response(status=exc.code, body=_read_capped(exc, url))
    except EgressRefused:
        raise
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        raise ConnectorUnreachable(f"{profile.connector_id}: could not reach {url}: {exc}") from exc


def _read_capped(stream: object, url: str) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if len(body) > MAX_RESPONSE_BYTES:
        raise ConnectorUnreachable(
            f"response from {url} exceeds {MAX_RESPONSE_BYTES} bytes and was not read"
        )
    return body
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_egress.py -v`
Expected: PASS

- [ ] **Step 5: Add the AST closure test**

Append to `tests/test_import_closure.py`:

```python
import ast

_SRC = Path(__file__).resolve().parents[1] / "src" / "referral_loop"

# The single permitted egress site. This is the property the README's narrowed claim rests on --
# "no model calls, and egress only to configured connectors" -- and it is a claim about which
# file contains the import, not about which modules a probe happened to load. So this reads
# source rather than sys.modules; an import-probe cannot express it.
EGRESS_MODULE = "connect/egress.py"
_NETWORK_MODULES = {"urllib.request", "urllib.error", "http.client", "socket", "ftplib"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_only_the_egress_module_imports_a_network_library():
    offenders = {}
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative == EGRESS_MODULE:
            continue
        leaked = sorted(_imported_modules(path) & _NETWORK_MODULES)
        if leaked:
            offenders[relative] = leaked
    assert not offenders, (
        f"egress must stay confined to {EGRESS_MODULE}; these also import a network "
        f"library: {offenders}"
    )
```

`socket` and `ssl` are deliberately treated differently: `peers.py` and `mllp_server.py` legitimately import both for the *inbound* listener, so `ssl` is not in `_NETWORK_MODULES`. `socket` is — check whether `peers.py` or `mllp_server.py` import it directly, and if either does, remove `socket` from the set and say so in a comment rather than exempting the file. An allowlist of exempt files is how this test stops meaning anything.

- [ ] **Step 6: Run it, and prove it can fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -v`
Expected: PASS

Now prove the test is not vacuous:

```bash
cd "$REPO"
./.venv/Scripts/python.exe - <<'PY'
from pathlib import Path
p = Path("src/referral_loop/retention.py")
orig = p.read_text(encoding="utf-8")
p.write_text("import urllib.request\n" + orig, encoding="utf-8")
PY
./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -q 2>&1 | tail -5
```

Expected: FAIL naming `retention.py`. Then restore by writing `orig` back **with Python, not `git checkout`** — this repo has a concurrent writer and a checkout could take more than you put in:

```bash
./.venv/Scripts/python.exe - <<'PY'
from pathlib import Path
p = Path("src/referral_loop/retention.py")
body = p.read_text(encoding="utf-8")
p.write_text(body.replace("import urllib.request\n", "", 1), encoding="utf-8")
PY
./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -q 2>&1 | tail -3
```

Expected: PASS again.

- [ ] **Step 7: Commit**

```bash
cd "$REPO"
git add src/referral_loop/connect/egress.py tests/test_egress.py tests/test_import_closure.py
git commit -m "feat(connect): the egress allowlist, and four urllib defaults corrected

Redirects refused outright, including to an allowlisted host -- a redirect to a listed host is
still a server we did not intend to talk to for that request, and following one hands a signed
client assertion to whoever answered.

Proxy environment ignored. http_proxy is frequently set on a hospital network and honouring it
routes PHI and credentials through a host nobody put in the registry. build_opener installs an
environment-reading ProxyHandler when none is passed, so an empty one is not the same as none.

Bounded timeout and response size. The allowlist compares (scheme, host, port) so an explicit
:443 is not a different destination from an implicit one.

The AST closure test reads source rather than sys.modules, because the claim is about which
file holds the import. Proven non-vacuous by adding one to retention.py and watching it fail."
```

---

### Task 5: The signed client assertion

**Files:**
- Create: `src/referral_loop/connect/auth.py`
- Create: `tests/_certs.py`
- Modify: `tests/test_peer_identity.py`
- Create: `tests/test_connector_auth.py`

Spec §6.2. This task does the JWT only; Task 6 exchanges it.

- [ ] **Step 1: Extract the cert helper**

Open `tests/test_peer_identity.py` and find its inline x509 generation (it imports `from cryptography import x509` and builds certificates with `CertificateBuilder`). Move those helper functions verbatim into a new `tests/_certs.py`, then import them back in `test_peer_identity.py`. **No test logic changes** — this is a move.

`tests/_certs.py` gets this docstring:

```python
"""Certificate and key generation for tests that need a real TLS handshake.

Extracted from test_peer_identity.py when the connector preflight tests needed the same thing.
One generator rather than two: two would drift, and a divergence between the certificates the
inbound tests use and the ones the outbound tests use is exactly the kind of difference that
makes a failure look environmental. tests/_pack.py is the existing precedent for this shape.
"""
```

Add an RSA keypair generator alongside the existing EC material, since SMART Backend Services signs with RSA:

```python
from cryptography.hazmat.primitives.asymmetric import rsa


def rsa_keypair(tmp_path, name: str = "signing"):
    """Returns (private_key_path, public_key_object). 2048 bits -- these are test keys and
    generation time shows up in every test that calls this."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / f"{name}.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path, key.public_key()
```

- [ ] **Step 2: Confirm the extraction changed nothing**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_peer_identity.py -q 2>&1 | tail -3`
Expected: identical pass count to before the move. If it differs, the extraction was not a move — revert and redo it.

- [ ] **Step 3: Write the failing test**

`tests/test_connector_auth.py`:

```python
"""The assertion we sign, and what a verifier gets when they check it."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from referral_loop.connect.auth import ASSERTION_LIFETIME, build_assertion
from referral_loop.connect.connectors import ConnectorRegistry

from ._certs import rsa_keypair

_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _b64u_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _profile(key_path, algorithm: str = "RS384"):
    return ConnectorRegistry.from_mapping(
        {
            "connectors": [
                {
                    "connector_id": "example-med",
                    "organization": "Example Medical Center",
                    "vendor": "epic",
                    "fhir_base_url": "https://fhir.example-med.example/api/FHIR/R4",
                    "token_url": "https://auth.example-med.example/oauth2/token",
                    "fhir_version": ["4.0.1"],
                    "auth": {
                        "mode": "smart-backend-services",
                        "client_id": "client-abc",
                        "private_key_file": str(key_path),
                        "key_id": "example-med-2026",
                        "algorithm": algorithm,
                        "scopes": ["system/Patient.read"],
                    },
                    "authorities": [],
                }
            ]
        }
    ).get("example-med")


def test_the_assertion_has_three_segments(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    assert build_assertion(_profile(key_path), now=_NOW).count(".") == 2


def test_the_header_names_the_algorithm_and_the_key(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    header = json.loads(_b64u_decode(build_assertion(_profile(key_path), now=_NOW).split(".")[0]))
    assert header == {"alg": "RS384", "typ": "JWT", "kid": "example-med-2026"}


def test_the_claims_are_what_smart_backend_services_requires(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    claims = json.loads(_b64u_decode(build_assertion(_profile(key_path), now=_NOW).split(".")[1]))
    assert claims["iss"] == "client-abc"
    assert claims["sub"] == "client-abc"
    assert claims["aud"] == "https://auth.example-med.example/oauth2/token"
    assert claims["exp"] == int((_NOW + ASSERTION_LIFETIME).timestamp())
    assert claims["jti"]


def test_the_assertion_expires_within_five_minutes(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    claims = json.loads(_b64u_decode(build_assertion(_profile(key_path), now=_NOW).split(".")[1]))
    assert claims["exp"] - int(_NOW.timestamp()) <= 300


def test_the_signature_verifies_against_the_public_key(tmp_path):
    key_path, public = rsa_keypair(tmp_path)
    token = build_assertion(_profile(key_path), now=_NOW)
    header_b64, claims_b64, sig_b64 = token.split(".")
    public.verify(
        _b64u_decode(sig_b64),
        f"{header_b64}.{claims_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA384(),
    )


def test_rs256_signs_with_sha256(tmp_path):
    key_path, public = rsa_keypair(tmp_path)
    token = build_assertion(_profile(key_path, algorithm="RS256"), now=_NOW)
    header_b64, claims_b64, sig_b64 = token.split(".")
    public.verify(
        _b64u_decode(sig_b64),
        f"{header_b64}.{claims_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_two_assertions_differ_under_a_frozen_clock(tmp_path):
    """jti comes from secrets, not from the clock. A clock-derived jti collides under a frozen
    clock -- which is exactly when a replay test would stop catching anything."""
    key_path, _ = rsa_keypair(tmp_path)
    profile = _profile(key_path)
    first = json.loads(_b64u_decode(build_assertion(profile, now=_NOW).split(".")[1]))
    second = json.loads(_b64u_decode(build_assertion(profile, now=_NOW).split(".")[1]))
    assert first["jti"] != second["jti"]


def test_a_missing_key_file_is_a_typed_failure(tmp_path):
    from referral_loop.connect.auth import AuthFailure

    with pytest.raises(AuthFailure, match="private key"):
        build_assertion(_profile(tmp_path / "absent.pem"), now=_NOW)
```

- [ ] **Step 4: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connector_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'referral_loop.connect.auth'`

- [ ] **Step 5: Write the module**

`src/referral_loop/connect/auth.py`:

```python
"""SMART Backend Services: the assertion we sign, and the token it buys.

There is no `now()` anywhere in this module. Every function that needs the current time takes
`now: datetime | None = None` and falls back to `datetime.now(timezone.utc)` -- the convention
clock.py already uses for is_future_dated and is_readable_clock, and what makes `exp` and the
cache refresh margin deterministically testable.

clock.py itself is not used here, and the distinction is worth keeping: it is a validation
module answering "is this attacker-supplied HL7 timestamp trustworthy", not a time source. It
exposes no now(). Routing a JWT exp through a guard built for MSH-7 skew would be a category
error.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import urllib.parse  # parse only -- urllib.request lives in egress.py and the AST test enforces it
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..errors import ReferralLoopError
from .connectors import ConnectorProfile, ConnectorRegistry
from .egress import fetch

logger = logging.getLogger(__name__)

# The SMART spec caps this at five minutes. Stated as a constant because the test asserts
# against it rather than against a literal.
ASSERTION_LIFETIME = timedelta(minutes=5)

# Re-acquire this far before expiry, so a token is never presented in the window between our
# clock saying it is valid and theirs saying it is not.
REFRESH_MARGIN = timedelta(seconds=60)

_HASHES = {"RS256": hashes.SHA256, "RS384": hashes.SHA384}

# RFC 6749 section 5.2, the codes that mean the fault is on our side. Every one of them is a
# problem with what we sent or how we are registered, and no retry fixes any of them.
# `invalid_scope` is the reason this list is not just the obvious three: a scope typo is a
# configuration error an operator has to go and correct, and reporting it as the remote's
# problem invites them to wait out something that will never clear.
_OUR_FAULT = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)


class AuthFailure(ReferralLoopError):
    """The credential flow failed.

    Distinguishes ours from theirs in the message: an invalid_client means our key or client id
    is wrong and no retry will help, a 5xx is the authorization server's problem.
    """


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(payload: dict[str, object]) -> str:
    # Compact and key-sorted so the bytes are reproducible; a signature over a dict whose
    # serialisation varies is not reproducible in a test.
    return _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _warn_if_world_readable(path: Path, connector_id: str) -> None:
    """A warning, not a refusal, and only on POSIX.

    Refusing would strand a deployment whose key is mode 0644 behind an error it cannot fix
    without a shell on the box, which is a worse failure than the one being prevented. The
    check is skipped on Windows with a notice rather than silently: the ACL equivalent is not a
    one-liner, and a check that quietly does nothing on the platform someone is developing on
    is worse than an honest absence.
    """
    if os.name != "posix":
        logger.debug(
            "%s: key file permissions not checked on this platform; verify by hand that %s "
            "is readable only by the service account",
            connector_id, path,
        )
        return
    mode = path.stat().st_mode
    if mode & 0o077:
        logger.warning(
            "%s: private key %s is mode %o -- readable beyond its owner. The signing key is "
            "the whole proof of our identity to the remote.",
            connector_id, path, mode & 0o777,
        )


def _load_private_key(profile: ConnectorProfile) -> rsa.RSAPrivateKey:
    path = profile.auth.private_key_file
    try:
        _warn_if_world_readable(path, profile.connector_id)
        material = path.read_bytes()
    except OSError as exc:
        raise AuthFailure(
            f"{profile.connector_id}: cannot read the private key at {path}: {exc}"
        ) from exc
    try:
        key = serialization.load_pem_private_key(material, password=None)
    except (ValueError, TypeError) as exc:
        raise AuthFailure(f"{profile.connector_id}: private key at {path} is not readable PEM") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthFailure(
            f"{profile.connector_id}: private key at {path} is not RSA, but "
            f"auth.algorithm is {profile.auth.algorithm}"
        )
    return key


def build_assertion(
    profile: ConnectorProfile,
    *,
    now: datetime | None = None,
    jti: str | None = None,
) -> str:
    """The signed JWT we present as a client_assertion."""
    moment = datetime.now(timezone.utc) if now is None else now
    key = _load_private_key(profile)

    header = {"alg": profile.auth.algorithm, "typ": "JWT", "kid": profile.auth.key_id}
    claims = {
        "iss": profile.auth.client_id,
        "sub": profile.auth.client_id,
        # The configured token_url, never one discovered from the remote. See spec 4.3: a
        # discovery document that chooses our audience chooses where a replayable credential
        # is valid.
        "aud": profile.token_url,
        # From secrets, not the clock. A clock-derived jti collides under a frozen clock, which
        # is exactly when a replay test would stop catching anything.
        "jti": jti or secrets.token_hex(32),
        "exp": int((moment + ASSERTION_LIFETIME).timestamp()),
    }

    signing_input = f"{_segment(header)}.{_segment(claims)}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), _HASHES[profile.auth.algorithm]())
    return f"{signing_input.decode('ascii')}.{_b64u(signature)}"
```

- [ ] **Step 6: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connector_auth.py -v`
Expected: PASS

**Known transient lint state.** The import block above is the *finished* module's, so five names — `urllib.parse`, `dataclass`, `ConnectorRegistry`, `fetch`, and `timedelta` in the test file — are unused until Task 6 adds `Token`, `TokenCache` and `acquire_token`. `ruff check` reports F401 on each until then.

This is a flaw in how the plan was split, recorded rather than hidden: a commit should stand on its own, and this one does not pass lint. It is left as-is because Task 6 immediately follows and resolves all five, and removing-then-re-adding the same imports one task later is churn in the history for no gain. **Task 6 must verify `ruff check src/referral_loop/connect/` is clean before it commits** — that is what converts this from an unnoticed defect into a bounded one. If Task 6 is not going to run next, fix the imports here instead.

- [ ] **Step 7: Commit**

```bash
cd "$REPO"
git add src/referral_loop/connect/auth.py tests/_certs.py tests/test_peer_identity.py tests/test_connector_auth.py
git commit -m "feat(connect): the signed client assertion

aud is the configured token_url and never a discovered one -- a discovery document that
chooses our audience chooses where a replayable credential is valid.

jti comes from secrets rather than the clock, because a clock-derived jti collides under the
frozen clock the tests use, which is precisely when a replay test stops catching anything.

No now() in the module. Every function taking now=None mirrors clock.py's own convention
without borrowing clock.py itself -- that is a validation module for attacker-supplied HL7
timestamps, not a time source, and it exposes no now().

Cert generation moves to tests/_certs.py rather than being written a second time."
```

---

### Task 6: Token acquisition and cache

**Files:**
- Modify: `src/referral_loop/connect/auth.py`
- Modify: `tests/test_connector_auth.py`

Spec §6.2 and §6.3.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connector_auth.py`:

```python
Merge `REFRESH_MARGIN`, `Token` and `TokenCache` into the **existing top-of-file import** from `referral_loop.connect.auth` — do not add a second import statement partway down, which is an `E402` and will fail the ruff gate. Then append:

```python
def test_a_token_knows_whether_it_is_still_usable():
    token = Token(value="abc", expires_at=_NOW + timedelta(seconds=300))
    assert token.usable_at(_NOW)
    assert not token.usable_at(_NOW + timedelta(seconds=300) - REFRESH_MARGIN + timedelta(seconds=1))


def test_the_cache_returns_the_same_token_until_the_refresh_margin(tmp_path):
    calls = []

    def acquire():
        calls.append(1)
        return Token(value=f"t{len(calls)}", expires_at=_NOW + timedelta(seconds=300))

    cache = TokenCache()
    first = cache.get("example-med", acquire, now=_NOW)
    second = cache.get("example-med", acquire, now=_NOW + timedelta(seconds=60))
    assert first.value == second.value == "t1"
    assert len(calls) == 1


def test_the_cache_reacquires_inside_the_refresh_margin(tmp_path):
    calls = []

    def acquire():
        calls.append(1)
        return Token(value=f"t{len(calls)}", expires_at=_NOW + timedelta(seconds=300))

    cache = TokenCache()
    cache.get("example-med", acquire, now=_NOW)
    later = cache.get("example-med", acquire, now=_NOW + timedelta(seconds=299))
    assert later.value == "t2"
    assert len(calls) == 2


def test_two_connectors_do_not_share_a_cache_entry():
    cache = TokenCache()
    a = cache.get("example-med", lambda: Token("a", _NOW + timedelta(seconds=300)), now=_NOW)
    b = cache.get("other", lambda: Token("b", _NOW + timedelta(seconds=300)), now=_NOW)
    assert a.value == "a" and b.value == "b"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connector_auth.py -k token -v`
Expected: FAIL — `ImportError: cannot import name 'Token'`

- [ ] **Step 3: Add `Token`, `TokenCache` and `acquire_token`**

Append to `src/referral_loop/connect/auth.py`:

```python
@dataclass(frozen=True)
class Token:
    value: str
    expires_at: datetime

    def usable_at(self, moment: datetime) -> bool:
        return moment < self.expires_at - REFRESH_MARGIN


class TokenCache:
    """In memory, keyed by connector, and never written to disk.

    A bearer token is a short-lived credential; a disk copy outlives its usefulness and turns a
    file-read into an authentication bypass. There is no cache that survives the process, and
    that is the whole design -- preflight acquires one token per connector per run.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, Token] = {}

    def get(self, connector_id: str, acquire, *, now: datetime | None = None) -> Token:
        moment = datetime.now(timezone.utc) if now is None else now
        held = self._tokens.get(connector_id)
        if held is not None and held.usable_at(moment):
            return held
        fresh = acquire()
        self._tokens[connector_id] = fresh
        return fresh

    def forget(self, connector_id: str) -> None:
        self._tokens.pop(connector_id, None)


def acquire_token(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    *,
    now: datetime | None = None,
) -> Token:
    """Exchange a signed assertion for a bearer token."""
    moment = datetime.now(timezone.utc) if now is None else now
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": build_assertion(profile, now=moment),
            "scope": " ".join(profile.auth.scopes),
        }
    ).encode("ascii")

    response = fetch(
        registry,
        profile,
        profile.token_url,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )

    try:
        payload = json.loads(response.text())
    except json.JSONDecodeError as exc:
        raise AuthFailure(
            f"{profile.connector_id}: token endpoint returned {response.status} with a "
            "body that is not JSON"
        ) from exc

    if response.status != 200:
        # Named separately because the two need different reactions from an operator: ours is a
        # registration or key problem and no retry helps, theirs may clear on its own.
        error = str(payload.get("error", "unspecified"))
        if error in _OUR_FAULT:
            whose = "our client id, signing key, or configured scopes"
        elif response.status >= 500:
            whose = "the authorization server"
        else:
            # Neither list matched. Say so rather than picking one: guessing "theirs" tells an
            # operator to wait out something that may never clear, and guessing "ours" sends
            # them to re-check a configuration that is fine.
            whose = f"an unrecognised error code at status {response.status}"
        raise AuthFailure(
            f"{profile.connector_id}: token request failed with {response.status} "
            f"{error!r} -- this points at {whose}"
        )

    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthFailure(f"{profile.connector_id}: token response carried no access_token")

    expires_in = payload.get("expires_in", 300)
    if not isinstance(expires_in, int) or expires_in <= 0:
        raise AuthFailure(f"{profile.connector_id}: token response expires_in is not a positive integer")

    # Never logged, never returned in a message. The value goes into the cache and nowhere else.
    logger.info(
        "acquired a bearer token for %s, valid %ss, scopes %s",
        profile.connector_id, expires_in, " ".join(profile.auth.scopes),
    )
    return Token(value=access, expires_at=moment + timedelta(seconds=expires_in))
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connector_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add src/referral_loop/connect/auth.py tests/test_connector_auth.py
git commit -m "feat(connect): token acquisition, and an in-memory cache that never touches disk

A bearer token is a short-lived credential -- a disk copy outlives its usefulness and turns a
file read into an authentication bypass. There is no cache that survives the process.

invalid_client and friends are named apart from a 5xx in the failure message, because the two
need different reactions: ours is a registration or key problem no retry helps, theirs may
clear on its own."
```

---

### Task 7: A local FHIR server to preflight against

**Files:**
- Create: `tests/_fhirserver.py`

No production code. This is the fixture Task 8 needs, and it is its own task because a fake server that lies convincingly is most of the work of testing a client.

- [ ] **Step 1: Add a `localhost` certificate generator**

Task 5 extracted `tests/_certs.py` from `test_peer_identity.py`. That file's certificates identify *peers* — the inbound tests match them by DER fingerprint and never do hostname verification. Preflight's client does verify the hostname, so it needs a certificate carrying `localhost` as a SAN, which the peer certificates do not have.

Append to `tests/_certs.py`:

```python
import datetime as dt
import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def localhost_cert(tmp_path):
    """A self-signed cert for `localhost`, returned as (certfile, keyfile, ca_file).

    Self-signed, so the certificate is its own CA and `ca_file` is the same bytes as
    `certfile` -- the connector's tls.ca_file then trusts exactly this one server and nothing
    else, which is closer to a pinned deployment than loading a test CA into the system store.

    A SAN for `localhost` and 127.0.0.1 both, because egress.py sets check_hostname and the URL
    the tests build uses the hostname spelling.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    certfile = tmp_path / "server.crt"
    keyfile = tmp_path / "server.key"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return certfile, keyfile, certfile
```

If the Task 5 extraction already produced imports for `x509`, `hashes`, `serialization`, `ec` or `NameOID`, do not add them a second time — merge into the existing import block.

- [ ] **Step 2: Write the server helper**

`tests/_fhirserver.py`:

```python
"""A TLS server that answers /metadata and a token endpoint, for testing preflight.

Real TLS on loopback rather than a mocked opener, because three of the four things egress.py
overrides -- the TLS floor, the timeout, the redirect refusal -- do not exist at all in a mocked
transport. A test that patches urlopen proves the code calls urlopen.

Configurable per test: the fhirVersion it advertises, whether the token endpoint succeeds, and
whether either endpoint redirects instead of answering.
"""
from __future__ import annotations

import http.server
import json
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class ServerBehaviour:
    fhir_version: str = "4.0.1"
    token_status: int = 200
    token_body: dict = field(default_factory=lambda: {"access_token": "test-token", "expires_in": 300})
    redirect_metadata_to: str | None = None
    redirect_token_to: str | None = None
    requests: list = field(default_factory=list)


def _handler_for(behaviour: ServerBehaviour):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # keep pytest output readable
            return

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, target: str) -> None:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
            behaviour.requests.append(("GET", self.path))
            if not self.path.endswith("/metadata"):
                self._send(404, {"resourceType": "OperationOutcome"})
                return
            if behaviour.redirect_metadata_to:
                self._redirect(behaviour.redirect_metadata_to)
                return
            self._send(
                200,
                {
                    "resourceType": "CapabilityStatement",
                    "status": "active",
                    "fhirVersion": behaviour.fhir_version,
                    "format": ["json"],
                },
            )

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            behaviour.requests.append(("POST", self.path, self.rfile.read(length).decode("ascii")))
            if behaviour.redirect_token_to:
                self._redirect(behaviour.redirect_token_to)
                return
            self._send(behaviour.token_status, behaviour.token_body)

    return Handler


@contextmanager
def fhir_server(certfile, keyfile, behaviour: ServerBehaviour | None = None):
    """Yields (base_url, behaviour). The certificate must carry `localhost` as a SAN."""
    behaviour = behaviour or ServerBehaviour()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(behaviour))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    server.socket = context.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_address[1]}", behaviour
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

- [ ] **Step 3: Confirm a server actually starts and serves over TLS**

Not a committed test — a smoke run proving the fixture works before Task 8 depends on it:

```bash
cd "$REPO"
./.venv/Scripts/python.exe - <<'PY'
import sys, ssl, json, urllib.request, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from _certs import localhost_cert
from _fhirserver import fhir_server

with tempfile.TemporaryDirectory() as d:
    certfile, keyfile, ca = localhost_cert(Path(d))
    with fhir_server(certfile, keyfile) as (base, behaviour):
        ctx = ssl.create_default_context(cafile=str(ca))
        with urllib.request.urlopen(f"{base}/metadata", context=ctx, timeout=10) as r:
            print("ok", json.loads(r.read())["fhirVersion"])
        print("requests seen:", behaviour.requests)
PY
```

Expected: `ok 4.0.1` and one recorded GET. If the TLS handshake fails on hostname verification, the SAN in `localhost_cert` is wrong — fix that here rather than in Task 8, where it would look like a preflight bug.

- [ ] **Step 4: Commit**

```bash
cd "$REPO"
git add tests/_certs.py tests/_fhirserver.py
git commit -m "test: a TLS FHIR server that can lie in the specific ways preflight must catch

Real TLS on loopback, not a mocked opener. Three of the four defaults egress.py overrides --
the TLS floor, the timeout, the redirect refusal -- do not exist in a mocked transport, so a
test that patches urlopen proves only that the code calls urlopen."
```

---

### Task 8: Preflight — two proofs, reported apart

**Files:**
- Create: `src/referral_loop/connect/preflight.py`
- Create: `tests/test_preflight.py`

Spec §6.1 and §6.4.

- [ ] **Step 1: Write the failing test**

`tests/test_preflight.py`:

```python
"""Preflight, and the reason it makes two proofs rather than one."""

from __future__ import annotations

import pytest

from referral_loop.connect.connectors import ConnectorRegistry
from referral_loop.connect.preflight import format_report, preflight

from ._certs import localhost_cert, rsa_keypair
from ._fhirserver import ServerBehaviour, fhir_server


def _registry(base_url: str, key_path, ca_file, versions=("4.0.1",)) -> ConnectorRegistry:
    return ConnectorRegistry.from_mapping(
        {
            "connectors": [
                {
                    "connector_id": "example-med",
                    "organization": "Example Medical Center",
                    "vendor": "epic",
                    "fhir_base_url": base_url,
                    "token_url": f"{base_url}/oauth2/token",
                    "fhir_version": list(versions),
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
            ]
        }
    )


@pytest.fixture
def certs(tmp_path):
    """(certfile, keyfile, ca_file) for a server answering to `localhost`."""
    return localhost_cert(tmp_path)


def test_both_proofs_pass_against_a_healthy_server(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _behaviour):
        reports = preflight(_registry(base, key_path, ca_file))
    assert len(reports) == 1
    assert reports[0].reach.ok
    assert reports[0].credential.ok
    assert reports[0].ok


def test_a_wrong_fhir_version_fails_reach_only(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile, ServerBehaviour(fhir_version="3.0.2")) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert not reports[0].reach.ok
    assert "3.0.2" in reports[0].reach.detail
    assert reports[0].credential.ok, "a version mismatch must not be reported as a credential failure"


def test_an_invalid_client_fails_the_credential_proof_but_not_reach(certs, tmp_path):
    """This is the case the two-proof design exists for. /metadata is unauthenticated on Epic,
    so a single-request preflight would report this connector healthy."""
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(token_status=400, token_body={"error": "invalid_client"})
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert reports[0].reach.ok, "the server is reachable and its version is fine"
    assert not reports[0].credential.ok
    assert "invalid_client" in reports[0].credential.detail
    assert not reports[0].ok


def test_a_redirecting_metadata_endpoint_fails_rather_than_being_followed(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(redirect_metadata_to="https://elsewhere.example/metadata")
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert not reports[0].reach.ok
    assert "redirect" in reports[0].reach.detail.lower()


def test_a_redirecting_token_endpoint_does_not_leak_the_assertion(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(redirect_token_to="https://elsewhere.example/token")
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        reports = preflight(_registry(base, key_path, ca_file))
    assert not reports[0].credential.ok
    assert "redirect" in reports[0].credential.detail.lower()


def test_every_connector_is_checked_even_after_one_fails(certs, tmp_path):
    """One run should give the whole picture. The common case during setup is several
    connectors wrong in different ways."""
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        data = _registry(base, key_path, ca_file).connectors[0]
        registry = ConnectorRegistry.from_mapping(
            {
                "connectors": [
                    _as_dict(data, "example-med", base, key_path, ca_file),
                    _as_dict(data, "second-site", "https://unreachable.invalid", key_path, ca_file),
                ]
            }
        )
        reports = preflight(registry)
    assert [r.connector_id for r in reports] == ["example-med", "second-site"]
    assert reports[0].ok
    assert not reports[1].ok


def _as_dict(profile, connector_id, base, key_path, ca_file) -> dict:
    return {
        "connector_id": connector_id,
        "organization": profile.organization,
        "vendor": profile.vendor,
        "fhir_base_url": base,
        "token_url": f"{base}/oauth2/token",
        "fhir_version": list(profile.fhir_version),
        "auth": {
            "mode": "smart-backend-services",
            "client_id": profile.auth.client_id,
            "private_key_file": str(key_path),
            "key_id": profile.auth.key_id,
            "algorithm": profile.auth.algorithm,
            "scopes": list(profile.auth.scopes),
        },
        "authorities": [],
        "tls": {"ca_file": str(ca_file)},
    }


def test_the_report_shows_the_two_proofs_on_separate_lines(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        text = format_report(preflight(_registry(base, key_path, ca_file)))
    assert "reach" in text
    assert "credential" in text
    assert "example-med" in text


def test_the_bearer_token_never_appears_in_the_report(certs, tmp_path):
    certfile, keyfile, ca_file = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        text = format_report(preflight(_registry(base, key_path, ca_file)))
    assert "test-token" not in text
```

`localhost_cert` and `rsa_keypair` both come from `tests/_certs.py`, written in Tasks 5 and 7. Nothing new is added to that file here.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'referral_loop.connect.preflight'`

- [ ] **Step 3: Write the module**

`src/referral_loop/connect/preflight.py`:

```python
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
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_preflight.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add src/referral_loop/connect/preflight.py tests/test_preflight.py tests/_certs.py
git commit -m "feat(connect): preflight, with reachability and credentials proven apart

/metadata is unauthenticated on Epic, so a one-request preflight reports success for a
connector whose signing key is wrong -- which is the failure it exists to catch. The two proofs
are independent and printed on separate lines.

Every connector is checked before exiting. During setup the common case is several wrong in
different ways, and aborting at the first turns that into one round trip each.

The bearer token never reaches the report string; that text gets printed, logged and pasted
into tickets."
```

---

### Task 9: The `connectors` CLI mode

**Files:**
- Modify: `src/referral_loop/cli.py`
- Create: `tests/test_connectors_cli.py`

Spec §3.1 and §6.4.

- [ ] **Step 1: Write the failing test**

`tests/test_connectors_cli.py`:

```python
"""The connectors mode, and where it sits relative to the boot gates."""

from __future__ import annotations

import json

import pytest

from referral_loop.cli import MODES, main


def test_connectors_is_a_mode():
    assert "connectors" in MODES


def test_it_runs_without_the_pack_key(tmp_path, monkeypatch, capsys):
    """It joins purge and stats ahead of the pack lookup. An operator whose connector file is
    malformed needs to hear that, not a message about a signing key -- preflight touches no
    database, no pack and no PHI, so none of the three boot gates applies."""
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = tmp_path / "connectors.json"
    path.write_text("{ not json", encoding="utf-8")

    code = main(["connectors", "--connectors", str(path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "JSON" in err
    assert "REFERRAL_PACK_PUBKEY" not in err


def test_a_missing_file_refuses_with_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    code = main(["connectors", "--connectors", str(tmp_path / "absent.json")])
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_a_valid_file_with_an_unreachable_host_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    key = tmp_path / "k.pem"
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n")
    path = tmp_path / "connectors.json"
    path.write_text(
        json.dumps(
            {
                "connectors": [
                    {
                        "connector_id": "example-med",
                        "organization": "Example Medical Center",
                        "vendor": "epic",
                        "fhir_base_url": "https://unreachable.invalid/api/FHIR/R4",
                        "token_url": "https://unreachable.invalid/oauth2/token",
                        "fhir_version": ["4.0.1"],
                        "auth": {
                            "mode": "smart-backend-services",
                            "client_id": "abc",
                            "private_key_file": str(key),
                            "key_id": "k1",
                            "algorithm": "RS384",
                            "scopes": ["system/Patient.read"],
                        },
                        "authorities": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    code = main(["connectors", "--connectors", str(path)])
    assert code == 1
    out = capsys.readouterr().out
    assert "example-med" in out
    assert "FAIL" in out


def test_the_peer_cross_check_says_when_it_did_not_run(tmp_path, monkeypatch, capsys):
    """A silent skip would read as a clean bill of health rather than an absence of evidence
    -- the same reasoning the README applies to the image tests that skip without Docker."""
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = _connector_file(tmp_path)
    main(["connectors", "--connectors", str(path)])
    assert "peer id cross-check: skipped" in capsys.readouterr().out


def test_a_connector_id_shared_with_a_configured_peer_warns(tmp_path, monkeypatch, capsys, caplog):
    """This is the only caller of warn_on_peer_collisions. Without it the whole warning path
    is unreachable and an operator with a real collision never hears about it."""
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = _connector_file(tmp_path, connector_id="example-ris")
    peers = tmp_path / "peers.json"
    peers.write_text(
        json.dumps(
            {
                "transport": "mtls",
                "tls": {
                    "certfile": str(tmp_path / "s.crt"),
                    "keyfile": str(tmp_path / "s.key"),
                    "client_ca_file": str(tmp_path / "ca.crt"),
                },
                "peers": [
                    {
                        "peer_id": "example-ris",
                        "organization": "Example Radiology",
                        "certificate_sha256": ["a" * 64],
                        "authorities": ["result"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        main(["connectors", "--connectors", str(path), "--peers", str(peers)])
    assert "example-ris" in caplog.text
    assert "peer id cross-check: skipped" not in capsys.readouterr().out
```

`_connector_file(tmp_path, connector_id="example-med")` is a helper you should extract from the body of `test_a_valid_file_with_an_unreachable_host_exits_nonzero` above — it writes the same JSON with a throwaway key file, parametrised by `connector_id`. Do not write the JSON literal a third time.

**If `load_peer_registry` refuses this peers file** — it validates TLS file paths that do not exist here — then instead of constructing a real peers file, monkeypatch `referral_loop.cli.load_peer_registry` is *not* acceptable (it is a function-local import). In that case, build the peer registry fixture the way `tests/test_peer_identity.py` already does it, reusing its helpers, and say in your report that you did so.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connectors_cli.py -v`
Expected: FAIL — `assert 'connectors' in MODES`

- [ ] **Step 3: Wire the mode**

In `src/referral_loop/cli.py`:

**3a.** Extend `MODES` (line ~83):

```python
MODES = ("listen", "filedrop", "worklist", "eval", "purge", "stats", "connectors")
```

**3b.** Narrow the parser description (line ~473) — spec §2.1. Replace:

```python
        description="Deterministic HL7 v2 referral-loop tracker. On-premise, no model "
                    "calls, no network egress.",
```

with:

```python
        # "no network egress" was true until connect/ existed. Narrowed rather than dropped:
        # the property that remains is enforced by an allowlist and an AST closure test, and
        # --help is the more authoritative of the two places this claim lives.
        description="Deterministic HL7 v2 referral-loop tracker. On-premise, no model "
                    "calls, and egress only to endpoints named in the connector file.",
```

**3c.** Add to the `mode` argument's help text, after the `stats:` clause:

```python
             "connectors: check every configured FHIR endpoint -- reachability and "
             "credentials are proven separately -- and exit nonzero if any failed.",
```

**3d.** Add the argument, beside `--db`:

```python
    parser.add_argument("--connectors", default="connectors.json",
                        help="connectors mode: JSON file of outbound FHIR endpoints "
                             "(default: %(default)s)")
```

**3e.** Add the runner, beside `_run_stats`:

```python
def _run_connectors(args: argparse.Namespace) -> int:
    """Preflight every configured connector.

    Deliberately prints the report to stdout and returns a code rather than raising: an
    operator setting up three sites wants all three verdicts, and the exit code is for the
    script that wrapped the command.

    Cross-checks connector ids against the peer registry when `--peers` names one. That check
    is the only caller of warn_on_peer_collisions, and it says so when it does *not* run --
    a silent skip would make the warning look like a clean bill of health when it is actually
    an absence of evidence, which is the same reasoning the README applies to the image tests
    that skip when no Docker daemon is reachable.
    """
    from .connect.connectors import load_connector_registry
    from .connect.preflight import format_report, preflight
    from .peers import load_peer_registry

    registry = load_connector_registry(args.connectors)

    if args.peers:
        registry.warn_on_peer_collisions(load_peer_registry(args.peers).peer_ids())
    else:
        print("peer id cross-check: skipped, no --peers given\n")

    reports = preflight(registry)
    print(format_report(reports))
    return 0 if all(r.ok for r in reports) else 1
```

The import is function-local so that `cli.py` does not pull `connect/` — and therefore
`urllib.request` — into every `listen` and `filedrop` process that will never use it.

**3f.** Join the early-return group (line ~569):

```python
    if args.mode in ("purge", "stats", "connectors"):
        try:
            if args.mode == "purge":
                return _run_purge(args)
            if args.mode == "stats":
                return _run_stats(args)
            return _run_connectors(args)
        except (ReferralLoopError, RuntimeError) as exc:
            return _refuse(str(exc))
```

Extend the comment immediately above it:

```python
    # Answered before the pack key is even looked for, deliberately. Neither purge nor stats
    # loads a pack, and an operator whose retention period is unset -- or who just wants to see
    # how big their database has gotten -- needs to hear that rather than a message about a
    # signing key. connectors joins them for the same reason and a stronger one: preflight
    # touches no database, no pack and no PHI, so not one of the three boot gates is relevant
    # to what it does. See _run_purge, _run_stats and _run_connectors for which gates each runs.
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_connectors_cli.py -v`
Expected: PASS

- [ ] **Step 5: Confirm the function-local import held**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -v`
Expected: PASS. If `test_only_the_egress_module_imports_a_network_library` fails, the import in 3e was written at module level.

- [ ] **Step 6: Commit**

```bash
cd "$REPO"
git add src/referral_loop/cli.py tests/test_connectors_cli.py
git commit -m "feat(cli): connectors mode, ahead of the boot gates

Joins purge and stats in the early return for the reason the comment there already gives, and
a stronger one: preflight touches no database, no pack and no PHI, so not one of the three
gates is relevant to it. An operator whose connector file is malformed hears that rather than
a message about a signing key.

--help stops claiming no network egress. That string is the more authoritative of the two
places the claim lives, and leaving it would have the binary asserting a property the code
gave up."
```

---

### Task 10: The README, and the whole suite

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Check what the concurrent writer has done**

```bash
cd "$REPO"
git log --oneline -15
git status --short
```

Plan 2a may have landed `fhir/`, `migration.py` and Tasks 4–6 since this plan started. Note what is present; it does not change anything below, but a suite count that moved for reasons outside this plan needs to be attributable.

- [ ] **Step 2: Narrow the README claim**

In `README.md`, replace lines 6–8:

```markdown
Deterministic: no model calls, no network egress. The image contains no ML stack
and no model client, and `tests/test_install_closure.py` asserts that against the
built image rather than against the Dockerfile.
```

with:

```markdown
Deterministic: no model calls. The image contains no ML stack and no model client,
and `tests/test_install_closure.py` asserts that against the built image rather than
against the Dockerfile.

Egress is bounded rather than absent. Until `connect/` existed this said "no network
egress", which was prose — nothing in the suite forbade a socket. What replaced it is
narrower and actually enforced: **the only outbound destinations are the ones named in
the connector file**, `connect/egress.py` is the one module permitted to import
`urllib.request`, and `tests/test_import_closure.py` fails the build if a second one
appears. A deployment that configures no connectors makes no outbound connections at
all, and `listen` and `filedrop` never import the package.
```

- [ ] **Step 3: Document the mode**

In `README.md`, update the modes line:

```markdown
Modes: `listen`, `filedrop`, `worklist`, `eval`, `purge`, `stats`, `connectors`.
```

and add this section immediately before `## Required configuration`:

````markdown
### Outbound FHIR connectors

`connectors` mode checks every endpoint in a connector file and exits nonzero if any
failed:

    referral-loop connectors --connectors connectors.json

```json
{
  "connectors": [
    {
      "connector_id": "example-med",
      "organization": "Example Medical Center",
      "vendor": "epic",
      "fhir_base_url": "https://fhir.example-med.example/api/FHIR/R4",
      "token_url": "https://fhir.example-med.example/oauth2/token",
      "fhir_version": ["4.0.1"],
      "auth": {
        "mode": "smart-backend-services",
        "client_id": "<registered client id>",
        "private_key_file": "/etc/referral/example-med-signing.pem",
        "key_id": "example-med-2026",
        "algorithm": "RS384",
        "scopes": ["system/Patient.read", "system/DocumentReference.read"]
      },
      "authorities": [],
      "tls": { "ca_file": "/etc/referral/example-med-ca.crt" }
    }
  ]
}
```

No field has a default. A missing `token_url`, `client_id` or `fhir_version` refuses the
run — there is no "assume R4", and no deriving the token endpoint from the server's own
discovery document, because that would let the remote choose where a signed credential is
valid.

`private_key_file` is a path and the loader refuses PEM content pasted into it. A config
file gets committed and pasted into tickets; the signing key is the whole proof of our
identity to the remote.

`authorities` uses the same three names as `peers.json` — `merge`, `cancel`, `result` —
and for the same reason. A FHIR endpoint whose documents can close a loop is asserting
what an MLLP peer asserts, and a rule that survives only one transport was never a rule.
A read-only connector grants none, but the field is not optional.

Preflight makes **two** proofs and reports them apart, because `/metadata` is
unauthenticated on Epic: fetching it proves reachability, TLS and version, and proves
nothing at all about whether our credentials work.
````

- [ ] **Step 4: Full suite**

```bash
cd "$REPO"
./.venv/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -5
```

Expected: every pre-existing test still passing, plus this plan's additions. Any pre-existing test that changed result is a defect in this plan's work — investigate before continuing, and do not attribute it to the concurrent writer without checking `git log` for a commit that touches the failing area.

- [ ] **Step 5: Lint and types**

```bash
cd "$REPO"
./.venv/Scripts/ruff.exe check src/ tests/
./.venv/Scripts/mypy.exe src/referral_loop/ --ignore-missing-imports --check-untyped-defs --warn-unused-ignores
```

Expected: `All checks passed!` and `Success: no issues found`.

- [ ] **Step 6: Confirm the additive claim**

```bash
cd "$REPO"
git diff --stat <sha-before-task-1>..HEAD -- src/referral_loop/registry.py src/referral_loop/store.py src/referral_loop/listener.py src/referral_loop/matcher.py src/referral_loop/peers.py
```

Expected: **empty output.** If any of those five changed, this plan stopped being additive.

- [ ] **Step 7: Run the mode for real**

This is the verification the definition of done names. Generate a throwaway key and a config pointing at a local server, then run the actual CLI and paste its output into the commit or the report:

```bash
cd "$REPO"
./.venv/Scripts/python.exe -m pytest tests/test_preflight.py -q 2>&1 | tail -3
./.venv/Scripts/python.exe -m referral_loop.cli connectors --connectors /nonexistent.json ; echo "exit=$?"
```

Expected: the preflight suite passes, and the second command prints a refusal naming the missing file with `exit=2`. A green suite is not the same claim as a working command; both go in the report.

- [ ] **Step 8: Commit**

```bash
cd "$REPO"
git add README.md
git commit -m "docs: the egress claim, narrowed to something the suite actually enforces

'No network egress' was prose. test_install_closure defends the no-ML-stack half and nothing
anywhere forbade a socket. What replaces it is narrower and enforced: the only destinations are
the ones in the connector file, one module may import urllib.request, and the suite fails if a
second one appears."
```

---

## Definition of done

- [ ] `connect/` exists with five modules; `core/` cannot import it, proven in a clean subprocess
- [ ] Every field rule in spec §4.1 refuses with exit 2 when violated, each with a test
- [ ] A `connector_id` in `RESERVED_PEER_IDS` is refused; one matching a configured peer id warns
- [ ] `allow_plaintext` and `plaintext_hosts` each refuse when present without the other
- [ ] All four `urllib` defaults from spec §5.3 are overridden, each with a test
- [ ] The AST closure test passes **and has been shown to fail** when a second module imports `urllib.request`
- [ ] The assertion's signature verifies against the public key, for both RS384 and RS256
- [ ] Two assertions under a frozen clock carry different `jti`
- [ ] Preflight reports both proofs separately; a wrong `fhirVersion` fails reach while credential still passes, and `invalid_client` fails credential while reach still passes
- [ ] The bearer token appears in no report string
- [ ] Every connector is checked before exit; exit nonzero if any failed
- [ ] `connectors` runs ahead of the pack-key lookup
- [ ] `warn_on_peer_collisions` has a real caller — `_run_connectors` cross-checks against `--peers`, and says so when it skips
- [ ] Full suite green with pre-existing count unmoved; `ruff` and `mypy` clean
- [ ] `git diff` over `registry.py`, `store.py`, `listener.py`, `matcher.py`, `peers.py` is empty
- [ ] The CLI has been run and its output shown
- [ ] README and `--help` both narrowed

## Out of scope — these are B and later

FHIR resource reads · pagination · retry and backoff · `OperationOutcome` taxonomy ·
`identifier_systems` · adapters onto the canonical model · TEFCA · CDS Hooks · MCP · SMART launch ·
A2A agent card · worklist UI · any write back to a remote · JWKS hosting · US Core profile
validation in CI · Synthea fixtures · the `@pytest.mark.sandbox` test against Epic's public sandbox
(spec §8.5 — it needs a credential nobody has yet, and a skipped test that has never once run is
not evidence of anything).
