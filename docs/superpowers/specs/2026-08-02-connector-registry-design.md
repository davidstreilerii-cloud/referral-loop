# Connector Registry and Preflight — Design Spec

> **Slice 3, sub-project A.** The first interoperability unit: a validated description of an
> outbound FHIR endpoint, the credential flow that reaches it, the allowlist that bounds where
> this process may talk, and a command that proves all three.

**Status:** design approved 2026-08-02. Implementation plan not yet written.
**Branch:** `interop`, forked from tag `canonical-model-v1`.
**Parent spec:** `2026-07-31-referral-kernel-design.md` — §2 (decomposition), §4 (architecture),
§5.2 (fork point), §11.5 (egress boundary).

---

## 0. What this spec covers

One sub-project: the connector registry, the SMART Backend Services credential flow, the egress
allowlist, and a `connectors --verify` CLI mode. It does not cover reading FHIR resources, mapping
them onto the canonical model, or any invocation surface. Those are B, C and D in §1.2.

## 1. Context

### 1.1 Where this came from

The parent spec decomposes interoperability into slice 3 (FHIR data layer, L1) and slice 4
(invocation surfaces, L3). Both are too large to design as one document — slice 3 alone spans a
version-negotiating client, US Core adapters, profile validation in CI, Synthea fixtures and TEFCA
query exchange. This spec is the first unit of slice 3.

### 1.2 The decomposition this spec belongs to

| | sub-project | depends on |
|---|---|---|
| **A** | **Connector registry and preflight** *(this spec)* | — |
| B | FHIR R4 read client — typed reads, pagination, retry, `OperationOutcome` taxonomy | A |
| C | FHIR ↔ canonical adapters — `ServiceRequest`/`Task` → `Referral`, `DocumentReference` → `InboundArtifact` | A, B, Plan 2a |
| D | MCP server — read-only worklist tools over the store | — |
| E | CDS Hooks + JWT · SMART launch · A2A card · TEFCA · profile validation in CI | C, D |

**D depends on neither B nor C.** The parent spec's §2 records slice 4 as depending on slice 3,
and for CDS Hooks and SMART that is true — both need FHIR context. A read-only MCP server over the
existing SQLite store needs no FHIR client at all. That dependency is over-stated in the parent and
should not be allowed to serialise work that is genuinely parallel.

### 1.3 Relationship to the fork point

A does **not** depend on the canonical model. It is configuration, credentials and transport; no
`Referral`, no `InboundArtifact`, no `ReferralState`. It lives on the `interop` branch because C
needs `core/` and one branch is cheaper than two merges — not because A is blocked on Plan 2a.

The practical consequence: A's implementation plan can be written and executed in parallel with
Plan 2a. Only C must wait.

### 1.4 Scheduling note on the parent spec's open questions

§14 question 2 — whether Epic fires a CDS hook that can carry the note-quality service — is a
slice-4 question. A neither depends on it nor closes it. It is recorded here only so that a reader
of this spec does not assume the Epic surface is fully understood.

## 2. The problem this unit solves

`peers.py` settled a question for inbound traffic: *whose message is this, and what may they
assert*. Its answer was that a self-asserted origin is not an origin — identity comes from a
credential the sender cannot choose, and authority is granted per peer rather than inferred from
the message type.

Outbound traffic raises the mirror question and the codebase currently has no answer to it, because
it currently has no outbound traffic. Before anything can read from Epic, three things must be
decided and written down:

1. **Who are we calling.** A base URL, a token endpoint, an organization behind them, and which
   FHIR versions we are willing to accept from that endpoint.
2. **How do we prove ourselves.** A client identity and a signing key, and the flow that turns them
   into a bearer token.
3. **What may we believe back.** An endpoint that can move a referral's state is asserting exactly
   what an MLLP peer asserts, and the rule that authority is granted rather than inferred has to
   survive the change of transport or it was never a rule.

And one thing has to be *bounded*: where this process may open a socket at all.

### 2.1 The claim this breaks, and how it is narrowed

The claim is in **two** places, and both have to move together:

- `README.md`, first line of the description: *"Deterministic: no model calls, no network egress."*
- `cli.py`'s `ArgumentParser(description=...)`: *"Deterministic HL7 v2 referral-loop tracker.
  On-premise, no model calls, no network egress."* — which is what `--help` prints.

The second half stops being true when `connect/` exists. Updating only the README would leave the
binary itself asserting the old property to every operator who runs `--help`, which is the more
authoritative of the two. It is also, today, only half-defended:
`tests/test_install_closure.py` asserts against the built image that no ML distribution and no
model client is installed, but **nothing anywhere forbids a network library or an outbound
socket.** "No network egress" is prose.

This spec narrows the claim rather than deleting it, and makes the narrowed version testable:

> No model calls, and egress only to configured connectors — enforced by an allowlist and proven
> by a closure test.

Updating that sentence is a deliverable of A, not a follow-up. A README asserting a property the
code stopped having is worse than the property's absence, because it is the sentence a reviewer
trusts instead of reading `connect/`.

## 3. Architecture

```
src/referral_loop/connect/
├── __init__.py        # package marker; no re-exports
├── connectors.py      # ConnectorProfile, ConnectorRegistry, load_connector_registry
├── auth.py            # SMART Backend Services: assertion, token acquisition, in-memory cache
├── egress.py          # the allowlist, and the only opener in the package
└── preflight.py       # the two proofs, and the report
```

`connectors.py` mirrors `peers.py` in name and structure deliberately. They are one decision
pointed in two directions, and a reader who has understood one should recognise the other:
constrained id format, reserved ids refused, authorities drawn from the same frozenset, transport
policy loaded from the same file as the identity it belongs to.

**No re-exports in `__init__.py`.** Same reasoning as `core/__init__.py` in Plan 2a: a
`from .connectors import *` makes `connect` and `connect.connectors` two names for one thing, and
that is how packages start growing sideways.

### 3.1 Files modified outside the new package

Three, of which exactly one is production code:

| file | change |
|---|---|
| `src/referral_loop/cli.py` | one new mode, `connectors`; narrow the `--help` egress claim per §2.1. The only `src/` file outside the new package that changes |
| `tests/test_import_closure.py` | add `referral_loop.connect` to `CORE_FORBIDDEN`; add the egress closure test (§8.1) |
| `tests/test_peer_identity.py` | extract its inline x509 generation to `tests/_certs.py` and import it back; no test logic changes |
| `README.md` | narrow the egress claim per §2.1; document the new mode and its config file |

The `test_peer_identity.py` change is an extraction, not a rewrite. §8.4 needs certificates for the
local FHIR server and that file already generates them; Plan 2a's Task 1 sets the rule this follows
— *if the helper is inline rather than named, extract it; do not write a second one*. `tests/_pack.py`
is the existing precedent for an underscore-prefixed shared test helper.

`connectors` joins `purge` and `stats` in the early-return group in `main()`, ahead of the pack key
lookup and `boot()`. The comment already there gives the reason and it applies unchanged: an
operator whose connector file is malformed needs to hear *that*, not a message about a signing key.
Preflight touches no database, no pack and no PHI, so none of the three boot gates is relevant to
it.

Nothing else in `src/` is touched. `registry.py`, `store.py`, `listener.py`, `matcher.py` and
`peers.py` are not modified by this unit.

### 3.2 Layering

`core/` must not reach the network any more than it reaches the store. `referral_loop.connect`
joins `referral_loop.store` and the rest in `CORE_FORBIDDEN`, enforced in a clean subprocess by the
existing probe.

`connect/` may import `errors.py`, `clock.py`, and from `peers.py` the two frozensets `AUTHORITIES`
and `RESERVED_PEER_IDS` — names only, no behaviour. It must not import `PeerRegistry` itself, nor
`store.py`, `registry.py`, or `core/`: a connector is a description of a remote endpoint and knows
nothing about referrals.

The `peers.py` import is worth stating rather than leaving implicit, because it is the one seam
where the outbound half touches the inbound half, and the reason it is safe is that it takes
vocabulary and not machinery. `AUTHORITIES` is imported so there is one authority vocabulary rather
than two that drift; `RESERVED_PEER_IDS` so §4.1.1's check has a single source.

## 4. The configuration file

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

`example-med` is the worked example throughout. There is no live engagement with Example Medical Center and
no sandbox credential; it is a design persona standing in for "an Epic shop", and every value above
is illustrative. What the persona is for is forcing the schema to be complete enough that filling
it in for a real site is a configuration task rather than a code change.

### 4.1 Field rules

| field | rule |
|---|---|
| `connector_id` | `^[a-z0-9][a-z0-9._-]{0,63}$`, same constraint as a peer id and for the same reason: it lands in audit rows and log lines, so it is constrained once here rather than sanitized at each site. Must not collide with `RESERVED_PEER_IDS` — see §4.1.1 |
| `organization` | required, length-bounded, same treatment as `PeerIdentity.organization` |
| `vendor` | required, free text. Inert in A — see §9.1 |
| `fhir_base_url` | required, `https` (see §5.2 for the dev opt-out) |
| `token_url` | required, `https`. Not derived from the CapabilityStatement — see §4.3 |
| `fhir_version` | required, non-empty list of versions we accept from this endpoint |
| `auth.mode` | required, `smart-backend-services` is the only value in A |
| `auth.client_id` | required |
| `auth.private_key_file` | required, a **path**. See §4.2 |
| `auth.key_id` | required; becomes the JWT `kid` header |
| `auth.algorithm` | required, `RS384` or `RS256` |
| `auth.scopes` | required, non-empty |
| `authorities` | required, may be empty. Values drawn from the existing `AUTHORITIES` frozenset |
| `tls.ca_file` | optional; system trust store if absent |

**Every one of these refuses the boot rather than assuming a value.** There is no "assume R4", no
"derive the token endpoint", no "default to RS384". This is the posture the three existing boot
gates already establish: `REFERRAL_PACK_PUBKEY`, `REFERRAL_THRESHOLDS_ACCEPTED` and
`PHI_ENCRYPTION_VERIFIED` each refuse the boot because a default would be a clinical or security
decision made by a vendor.

#### 4.1.1 Reserved ids, and why the two namespaces are checked against each other

The user-facing decision was that inbound and outbound live in separate files with separate
identifier spaces. That does not mean the two spaces may overlap.

A `connector_id` is refused if it appears in `peers.RESERVED_PEER_IDS` — `unattributed`, `local`,
`filedrop`, `plaintext-loopback`, `coordinator`. Those five already carry meanings in audit rows and
`loop_events.assertion_source`, and `coordinator` in particular is the value that means *no message
asserted this at all*. A connector able to write an audit row tagged `coordinator` could attribute
its own action to a human.

A `connector_id` that duplicates a **configured** peer id is a warning, not a refusal. It is the
natural thing to write when one organization is on both ends of the relationship, and the two are
read from different files by different code paths, so the ambiguity is only in the reader's head —
but it is in the reader's head, and audit rows from the two directions will sit next to each other.

This is the one place `connect/` reads a name from `peers.py`. It imports the frozenset only, not
`PeerRegistry`.

#### 4.1.2 Top-level fields

The two fields outside the `connectors` array, both governing the §5.2 development opt-out:

| field | rule |
|---|---|
| `allow_plaintext` | optional, defaults to absent-meaning-false. `true` alone is not sufficient |
| `plaintext_hosts` | required **if and only if** `allow_plaintext` is true; a non-empty list of hosts reachable over plaintext. Either field without the other refuses the boot |

### 4.2 No secret appears inline

`private_key_file` is a path, and the loader refuses a value that looks like PEM content. A
configuration file is committed, pasted into tickets, and readable by everyone with repo access;
the signing key is the entire proof of our identity to the remote. This matches
`REFERRAL_PACK_PUBKEY` coming from the environment rather than shipping beside the pack.

The key file's permissions are checked at load on POSIX — a **warning**, not a refusal. Refusing
would strand a deployment whose key is mode 0644 behind an error it cannot fix without a shell on
the box, which is a worse failure than the one being prevented. On Windows the check is skipped
with a logged notice rather than silently: the ACL equivalent is not a one-liner, and a check that
quietly does nothing on the platform someone develops on is worse than an honest absence.

### 4.3 Why the token endpoint is configured, not discovered

SMART defines discovery: `/.well-known/smart-configuration` advertises the token endpoint. Reading
it would be one fewer field to configure.

It is configured anyway, because discovery makes the remote server the authority on where our
client assertion gets sent. A compromised or misconfigured discovery document redirects a signed
credential to a host of the server's choosing, and the assertion is replayable until its `exp`.
Configuring the token URL means the destination of our credential is a local decision, checked
against the allowlist like everything else.

Preflight does **not** fetch the discovery document at all in A. Reporting a mismatch against the
configured value would be useful signal, but it is a second network round trip in service of a
warning, and the field it would check is one an operator copied from the same documentation the
client id came from. It moves to B if a real endpoint makes it earn its place — recorded here so
the omission reads as a decision rather than an oversight.

### 4.4 Deliberately not in A

`identifier_systems` — the MRN OID and the other identifier namespaces a site uses — is the field a
reader most expects in a site profile, and it is not here. Preflight does not consume it. Validating
a field nothing reads means the validation rules are guesses, and guesses in a schema are harder to
remove than to add. It arrives in B, with a reader that constrains it.

## 5. Egress

### 5.1 One opener

`egress.py` builds the only `urllib` opener in the package, and every request in `connect/` goes
through it. `urllib` is used rather than `httpx` or `requests` because the package has exactly two
runtime dependencies and `tests/test_install_closure.py` asserts the dependency surface stays
small. Nothing in A needs pooling, HTTP/2 or a retry policy. If B's pagination and backoff
genuinely outgrow the stdlib, adding a dependency then is a decision made with evidence rather than
in advance.

### 5.2 The allowlist

The set of `(scheme, host, port)` triples derived from every configured connector's
`fhir_base_url` and `token_url`. A request to any other destination is refused before a socket
opens, raising `EgressRefused`.

`https` is required. The development opt-out mirrors the `allow_plaintext` construction in
`peers.py`: the file must say `"allow_plaintext": true` **and** enumerate the hosts it will accept
over plaintext — two independent statements, so neither is reachable by a typo in the other — and
the mode logs a warning on every run.

### 5.3 Four `urllib` defaults that are wrong here

These are the substance of the module. Each is a default that is reasonable for a general HTTP
client and unsafe for this one.

1. **Redirects are followed by default.** A 302 from the token endpoint sends our client assertion
   — or a live bearer token — to whoever answered. Redirects are **refused outright**, not
   re-resolved against the allowlist: a redirect to a *listed* host is still a server we did not
   intend to talk to for that request, and a FHIR base URL that redirects is a misconfiguration
   worth surfacing rather than absorbing.
2. **Proxy environment variables are honoured by default.** `http_proxy` and `https_proxy` are
   frequently set on a hospital network, and honouring them routes PHI and credentials through a
   host nobody put in the registry. The opener is built with an empty `ProxyHandler`, and §7 has a
   test that sets `https_proxy` and asserts it was not used.
3. **Waits are unbounded by default.** Connect and read timeouts are set. This is the same argument
   `TLS_HANDSHAKE_SECONDS` already makes in `peers.py`: an unbounded wait is a resource the other
   end controls.
4. **Responses are read whole by default.** A response size cap, so a hostile or broken server
   cannot stream this process out of memory. Epic CapabilityStatements are legitimately large; the
   cap is generous and explicit rather than absent.

TLS 1.2 is the floor, reusing the posture already set in `peers.py`. Hostname verification on,
`ca_file` honoured when configured.

## 6. Preflight

### 6.1 Two proofs, reported separately

**`/metadata` is unauthenticated on Epic.** The CapabilityStatement is public. A preflight that
fetched only `/metadata` would report success for a connector whose signing key is wrong, whose
`client_id` was never registered, or whose scopes were refused — the exact failures preflight
exists to catch.

So preflight makes two independent proofs:

| proof | request | establishes |
|---|---|---|
| **Reach** | `GET {fhir_base_url}/metadata` | reachability, TLS trust, the allowlist path, and that `fhirVersion` is one we accept |
| **Credential** | token acquisition against `token_url` | the client id is registered, the private key matches the public key on file with the remote, the assertion is well-formed, and the scopes were granted |

Either can fail while the other passes. They are reported as two lines, never collapsed into one
"connected ✓" — that collapse is how a connector ships that can reach a server it cannot
authenticate to.

### 6.2 The credential flow

SMART Backend Services. Build a JWT:

- header: `alg` from `auth.algorithm`, `typ: JWT`, `kid` from `auth.key_id`
- claims: `iss` and `sub` both `client_id`, `aud` the configured `token_url`, a unique `jti`, and
  `exp` no more than five minutes ahead

Sign with `cryptography` — already a dependency, which is why RS384 costs nothing here. POST to
`token_url` with `grant_type=client_credentials`,
`client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`, the assertion, and
the configured scopes.

**Every function here that needs the current time takes `now: datetime | None = None`, defaulting
to `datetime.now(timezone.utc)` when absent.** This is the convention `clock.py` already uses for
`is_future_dated` and `is_readable_clock`, and it is what makes `exp` and the cache refresh margin
deterministically testable.

`clock.py` itself is *not* used here, and the distinction matters: it is a validation module — it
answers "is this timestamp trustworthy" about attacker-supplied HL7 timestamps — not a time source.
It exposes no `now()`. Borrowing its parameter convention is right; routing a JWT `exp` through a
guard built for `MSH-7` skew would be a category error.

`jti` uniqueness comes from `secrets.token_hex`, not from the clock. A clock-derived `jti` collides
under a frozen clock in tests, which is exactly when the tests would stop catching replay.

### 6.3 Token cache

In memory, keyed by `connector_id`, refreshed ahead of expiry. **Never written to disk.** A bearer
token is a short-lived credential and a disk copy outlives its usefulness; there is no cache that
survives the process.

### 6.4 The report

`referral-loop connectors --verify --connectors connectors.json` checks **every** connector and
reports all of them. It does not abort on the first failure: one run should give the whole picture
across every configured site, because the common case during setup is several connectors wrong in
different ways.

Exit non-zero if any connector failed either proof.

## 7. Errors and logging

| error | meaning | disposition |
|---|---|---|
| `ConnectorConfigError` | the file is wrong | refuse the boot, exit 2, matching the existing gates |
| `EgressRefused` | the allowlist said no | never retried; it is a configuration bug, not a transient |
| `AuthFailure` | token acquisition failed | distinguishes `invalid_client` — our key or id is wrong — from 5xx, which is theirs |
| `ConnectorUnreachable` | network or TLS failure | reported; other connectors still checked |

All subclass the existing `ReferralLoopError`.

**A FHIR version disagreement is deliberately not an exception.** It is a result — one connector
failed one of its two proofs — and preflight's contract is to check every connector and report.
An exception would be a control-flow signal for something the caller has to render as data
anyway, and the first thing any handler would do is convert it back into a `ProofResult`.

**The assertion JWT and the bearer token are redacted at the logging boundary, not at the call
sites.** A call site that forgets is the entire failure mode, and there will eventually be a call
site that forgets.

⚠ **Correction (2026-08-20).** An earlier revision of this section claimed "all logging routes
through the existing scrubber per parent spec §11.5, which already strips PHI and escapes CR/LF."
**That control does not exist in this codebase.** `cli.py` calls plain `logging.basicConfig` with no
filter attached, and there is no `logging.Filter` anywhere in `src/`. §11.5 of the parent spec is
design intent that was never implemented here, and this section inherited it as though it had been.
(§11.5 itself carried the false sentence uncorrected until 2026-08-22, so for a while this repo
held a correction pointing at an uncorrected original. The source now carries the same ⚠ note.)

The boundary-redaction design above is therefore aspirational, not implemented. What *is* implemented
is the M4 fix (`729ffb6`): PHI is kept out of the exceptions that reach log records in the first
place, at the raise sites. That is narrower than a scrubber — it covers the enumerated refusal paths
rather than every call site — and the reasoning is deliberate: a scrubber post-filters a record that
already contains PHI and has to know every identifier format to work. Treat any future logging call
site as unprotected by default.

## 8. Testing

### 8.1 The closure test

AST-level, matching the parent spec §4's description of the pattern: scan every source file under
`src/referral_loop/`, assert the only module importing `urllib.request` is `connect/egress.py`.

This is what keeps the narrowed README claim true against the second HTTP call site someone adds in
six months. An import-probe test cannot express it — the question is not *which modules got
loaded*, it is *which file contains the import* — so this one reads source rather than
`sys.modules`.

### 8.2 Offline unit tests

- **Registry refusals:** malformed id, reserved id, missing required field, non-`https` URL,
  unknown authority, PEM content pasted where a path belongs, empty `fhir_version`, empty `scopes`.
- **Assertion construction:** claims are what §6.2 specifies; the signature verifies against the
  corresponding public key; `exp` is within five minutes; two consecutive assertions have different
  `jti` **under a frozen clock**.
- **Token cache:** returns the cached token before the refresh margin, re-acquires after, driven
  through `clock.py`.

### 8.3 Egress unit tests

Allowlist accept and refuse; redirect refusal; the `https_proxy` test from §5.3; timeout; response
size cap; plaintext refused unless both opt-out statements are present.

### 8.4 Local integration

`tests/test_peer_identity.py` already generates x509 certificates with `cryptography` and stands up
a threaded TLS server. Reuse that machinery for a fake FHIR server serving a CapabilityStatement
and a token endpoint, and run full preflight against it — both proofs, over real TLS, through the
real opener.

Injected failures: wrong `fhirVersion`; `invalid_client` from the token endpoint; a redirect on the
metadata endpoint; and a redirect on the token endpoint, which is the one that would leak a signed
assertion.

Two of these carry the design's main claims and should be read as the load-bearing tests: a wrong
`fhirVersion` must fail **reach while credential still passes**, and `invalid_client` must fail
**credential while reach still passes**. If either failure collapses both proofs, the two-proof
design has been implemented as one proof wearing two labels.

### 8.5 Sandbox

One `@pytest.mark.sandbox` test against Epic's public sandbox, skipped by default and skipping
cleanly when no credential is configured. Same discipline as the existing docker-marked tests —
and, as the README already does for those, it says plainly that what the test verifies is
**unverified** when it skips, and is not covered by anything else in the suite.

## 9. Open questions and known softness

### 9.1 `vendor` is inert

Nothing in A branches on it. It is kept because a site profile that cannot say what it is talking
to is missing the point, and because B is likely to need it the first time an Epic search parameter
differs from a Cerner one. But it is inert in A, and **if B finds no use for it, it should be
removed rather than left implying a capability.**

### 9.2 Epic specifics are unverified

There is no sandbox credential and no engagement. The following are taken from public
documentation and are unconfirmed against a live endpoint: that `/metadata` is unauthenticated;
that RS384 is accepted; that the `system/*.read` scope spellings are right. Each is isolated to a
configuration value or one function, so confirming them later is a config change rather than a
redesign — but none of them is verified today, and §8.5's sandbox test is the thing that will
verify them.

### 9.3 Key rotation

Registering a rotated public key with the remote — JWKS hosting or manual re-registration — is out
of scope. `auth.key_id` exists so a rotation is expressible when the mechanism arrives.

## 10. Definition of done

- `connect/` exists with the five modules of §3; `core/` cannot import it, proven in a clean
  subprocess
- Every field rule in §4.1 refuses the boot with exit 2 when violated, each with a test
- A `connector_id` colliding with `RESERVED_PEER_IDS` is refused; one colliding with a configured
  peer id warns (§4.1.1)
- `allow_plaintext` and `plaintext_hosts` each refuse the boot when present without the other
- The four `urllib` defaults in §5.3 are each overridden and each has a test proving the override
- The AST closure test passes and has been shown to fail when a second module imports
  `urllib.request`
- Preflight reports both proofs separately and checks every connector before exiting
- Full suite green, with the pre-existing test count unmoved
- `ruff check` and `mypy` clean
- `referral-loop connectors --verify` run against the §8.4 local server, with its output shown
- `README.md` egress claim narrowed per §2.1

## 11. Explicitly out of scope

FHIR resource reads · pagination · retry and backoff · `OperationOutcome` taxonomy ·
`identifier_systems` · adapters onto the canonical model · TEFCA and query-based exchange ·
CDS Hooks · MCP server · SMART launch · A2A agent card · coordinator worklist UI · any write back
to a remote · JWKS hosting · US Core profile validation in CI · Synthea fixtures.
