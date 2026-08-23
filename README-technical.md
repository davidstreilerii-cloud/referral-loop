# referral-loop

> Looking for the overview? See the [project README](README.md) and the [security model](docs/security-model.md).

Inbound referral loop closure over HL7 v2. Tracks a referral from order to
returned documentation, and surfaces the ones that never came back.

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

## What it does

Receives HL7 v2 over MLLP, matches inbound results to open referrals, and
maintains an append-only event log from which loop state is replayed. A
coordinator worklist shows three queues: awaiting a result (`open_loops()`),
awaiting acknowledgement (`resulted_unacknowledged()`), and orphans — results
that matched no order.

`ACKNOWLEDGED` is v1's terminal state. `CLOSED` is defined but **never entered**:
no event type maps to it and `LoopStore.append_event` refuses any attempt to
reach it. A coordinator can reliably confirm that *this result belongs to this
order*; whether a clinician competent to act on an abnormal finding has read it
is a different claim, and nothing in v1 observes it.

## Running it

    pip install ".[worklist]"          # omit [worklist] for a listener-only deployment
    export REFERRAL_PACK_PUBKEY=<32-byte ed25519 public key, hex>
    export REFERRAL_THRESHOLDS_ACCEPTED=1
    export PHI_ENCRYPTION_VERIFIED=1   # or run on an OS-detected encrypted volume
    referral-loop listen --db data/referral_loops.db --peers peers.json

Modes: `listen`, `filedrop`, `worklist`, `eval`, `purge`, `stats`, `connectors`, `health`,
`rebuild`.

`listen` requires mutual TLS. `--peers` names a **JSON** file that carries both
the TLS material and the peer map, because "which CA may sign a client
certificate" and "which certificate is which peer" are two halves of one
decision:

```json
{
  "transport": "mtls",
  "tls": {
    "certfile": "/etc/referral/server.crt",
    "keyfile": "/etc/referral/server.key",
    "client_ca_file": "/etc/referral/client-ca.crt"
  },
  "peers": [
    {
      "peer_id": "example-radiology",
      "organization": "Example Radiology Group",
      "certificate_sha256": ["<sha256 of the client cert's DER encoding>"],
      "sending_application": "EHR",
      "sending_facility": "HOSP",
      "authorities": ["merge", "cancel", "result"]
    }
  ]
}
```

A peer is identified by the SHA-256 of its certificate's DER encoding, not by its
subject — a subject DN is chosen by whoever asks the CA and survives renewal, so
pinning it would make any certificate that CA can be talked into signing a valid
peer.

Authorities are granted, never inferred, and only three exist: `merge`
(`ADT^A40`), `cancel` (`SIU^S15`) and `result` (`ORU^R01` with `OBX-11 = F`).
Each can end with a clinically open loop on nobody's queue. Orders and schedules
are strictly additive and carry no authority.

`--allow-plaintext` runs without TLS for local development: loopback only, one
identity, and a warning on every start. A file-based registry can opt out the
same way, but only by saying `"allow_plaintext": true` *and* listing the source
addresses it will accept — two independent statements, so neither is reachable by
a typo in the other.

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

The `scopes` above are written in **SMART v1** syntax. SMART App Launch v2.2.0 replaced
`.read` / `.write` with granular `.cruds` letters, so the v2 spelling of that line is
`["system/Patient.rs", "system/DocumentReference.rs"]`. v1 syntax remains widely accepted,
so the example is left in the form most deployed servers are registered for — but it is the
remote, not this software, that decides. `scopes` is joined verbatim into the token
request's `scope` parameter and is never rewritten here, deliberately: a client that
"helpfully" translated a scope would be choosing what to ask for on the site's behalf. If a
remote refuses the grant, the v2 spelling is the first thing to try. `connectors` preflight reports a scope
refusal as a failed credential proof rather than a reachability failure, which is the
distinction that makes this diagnosable.

`authorities` uses the same three names as `peers.json` — `merge`, `cancel`, `result` —
and for the same reason. A FHIR endpoint whose documents can close a loop is asserting
what an MLLP peer asserts, and a rule that survives only one transport was never a rule.
A read-only connector grants none, but the field is not optional.

Preflight makes **two** proofs and reports them apart, because `/metadata` is
unauthenticated on Epic: fetching it proves reachability, TLS and version, and proves
nothing at all about whether our credentials work.

## Required configuration

Each of these refuses the boot rather than assuming a value:

| variable | why it has no default |
|---|---|
| `REFERRAL_PACK_PUBKEY` | the rule pack is signed; a default key verifies nothing |
| `REFERRAL_THRESHOLDS_ACCEPTED` | staleness thresholds are a clinical decision per specialty |
| `PHI_ENCRYPTION_VERIFIED` | at-rest encryption is attested by the deployment, not detected |
| `REFERRAL_AUDIT_DB` | see below — required in practice for an installed deployment |

The fourth gate is on writability rather than on the variable, because the package-relative
default is correct in a source checkout and wrong once installed: from `site-packages` it
resolves beside `site-packages`, which is read-only in the container and is nowhere
PHI-adjacent state belongs. Boot resolves the path and refuses if it cannot be written, so a
checkout keeps working and an installed deployment that forgets the variable is told at boot
instead of discovering an empty audit trail later. Audit writes fail open by design once the
process is running — that trade is only defensible if the trail was writable to begin with.

`purge` mode additionally requires both of these, which likewise have no defaults
— retention of PHI is a site policy, not a vendor default:

| variable | |
|---|---|
| `REFERRAL_RAW_RETENTION_DAYS` | ages out the raw HL7 archive |
| `REFERRAL_RESOLVED_RETENTION_DAYS` | ages out resolved loops |

### The worklist port

**5055**, everywhere: `cli.py`'s `--worklist-port`, `make_worklist_server`'s signature
default, and the Dockerfile's `EXPOSE`.

It was not always one number. `make_worklist_server` defaulted to **5057** for as long as
both files existed, so an in-process caller that omitted the argument bound a port nothing
else in the project used. The extraction that discovered this recorded the disagreement
rather than reconciling it, reasoning that picking one was a code change outside its scope.
Reconciled before publication instead: two defaults that disagree read as an accident no
matter how carefully the comment explains them.

Some worklist tests still spell `5057` in request URLs. Those exercise `Host` and `Origin`
handling, where the port is incidental and any value serves.

### Installed deployments

One more variable matters once the package is installed rather than run from a
checkout:

| variable | |
|---|---|
| `REFERRAL_AUDIT_DB` | the audit database path. Its default is package-relative, which resolves correctly in a source checkout and lands beside `site-packages` — read-only — when installed. Audit write failures are swallowed by design, so unset the symptom is an empty audit trail rather than an error. The Dockerfile sets it. |

### Optional, and best left alone: the published CodeSystem canonical

| variable | |
|---|---|
| `REFERRAL_BUSINESS_STATUS_URL` | the canonical url of the `Task.businessStatus` CodeSystem this software publishes. Defaults to `https://referral-loop.health/fhir/CodeSystem/referral-business-status`. **Overriding it costs you interoperability:** a canonical url identifies the vocabulary, not the site, so two hospitals that each publish these eleven codes under their own url hand receiving organisations two code systems that cannot be recognised as the same one — the exact ambiguity publishing a CodeSystem is meant to remove. Leave it unset unless a policy forbids asserting a vendor-owned identifier, and set it *before* any external system stores its first Coding, because after that a change is a rename of something already in someone else's database. A site with no domain of its own can use a `urn:uuid:` form. An empty or malformed value refuses the boot rather than publishing a broken canonical. |

## Tests

    python -m pytest tests/ -q                 # everything
    python -m pytest tests/ -q -m "not docker" # skip the image closure tests

The image closure tests skip cleanly when no Docker daemon is reachable, and what
they verify is then **unverified** — not covered by anything else in the suite.

The release gate for a candidate rule pack is **false-match rate**, not accuracy:
`referral-loop eval`. It has two halves and `check_release_criteria` requires
both — false-match rate zero *and* auto-match rate at or above the pack's
`min_auto_match_rate`. A floor without the coverage half is passed perfectly by a
matcher that attaches nothing.

## Known architectural state

A model migration is **half landed**, and this is the honest summary of it. The detail lives
1,400 lines into `store.py` and in `migration.py`'s own docstring; a reader should not have to
find it by accident.

**Two state vocabularies coexist.** `events.LoopState` has nine members and drives replay and
everything that reads state. `core.states.ReferralState` has eleven and is what the state
machine adjudicates. `migration.py` bridges them, and its second line says *"Temporary. Plan 2b
deletes this module."*

**Two event logs are dual-written.** `loop_events` drives replay; `transition_events` is the
provenance log. One write in two places inside one transaction, so they cannot diverge on the
paths that write both — but `reverse_acknowledgement` and `undo_match` move the projection while
passing no `transition=`, so on those paths the chain folds to whatever the last recorded
transition said. `store.py` documents this as a gap in the write path rather than a vocabulary
cost, which is correct: it closes when something writes those transitions, not when the
vocabularies merge.

**What this costs, precisely.** `core/machine.py` is a real state machine — pure, table-driven,
one `apply()`, three ordered guards, and invalid transitions are structurally impossible *within
its own inputs*. But it is not the system's source of truth. `registry.py` asks it for a verdict
on a `Referral` reconstructed by translating the legacy projection, and six canonical states have
no legacy source, so that round-trip is lossy by construction. After an `unschedule` the
transition chain holds `ACCEPTED` while the projection holds `OPEN`, which reads back as `SENT` —
and `SENT` admits an edge that `ACCEPTED` does not. So "invalid transitions are structurally
impossible" is true of `machine.apply()` and is **not** yet true of the product.

**Why it is shipping this way.** Finishing the migration is weeks of work, and doing it badly
under publication pressure would be worse than the current state, which is understood, tested,
and bounded. The `CLOSED`-unreachability property that matters most clinically is held
independently of any of this, by `test_no_event_type_maps_to_closed` and a depth-3 sweep over
every public mutating call.

## Provenance

Extracted from a monorepo with `git filter-repo`, history preserved. Design:
`docs/superpowers/specs/2026-07-31-referral-kernel-design.md`.
