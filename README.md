# referral-loop

Inbound referral loop closure over HL7 v2. Tracks a referral from order to
returned documentation, and surfaces the ones that never came back.

Deterministic: no model calls, no network egress. The image contains no ML stack
and no model client, and `tests/test_install_closure.py` asserts that against the
built image rather than against the Dockerfile.

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

Modes: `listen`, `filedrop`, `worklist`, `eval`, `purge`, `stats`.

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
      "peer_id": "example-ris",
      "organization": "Example Radiology",
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

## Required configuration

Each of these refuses the boot rather than assuming a value:

| variable | why it has no default |
|---|---|
| `REFERRAL_PACK_PUBKEY` | the rule pack is signed; a default key verifies nothing |
| `REFERRAL_THRESHOLDS_ACCEPTED` | staleness thresholds are a clinical decision per specialty |
| `PHI_ENCRYPTION_VERIFIED` | at-rest encryption is attested by the deployment, not detected |

`purge` mode additionally requires both of these, which likewise have no defaults
— retention of PHI is a site policy, not a vendor default:

| variable | |
|---|---|
| `REFERRAL_RAW_RETENTION_DAYS` | ages out the raw HL7 archive |
| `REFERRAL_RESOLVED_RETENTION_DAYS` | ages out resolved loops |

### A known disagreement: the worklist port

`cli.py` defaults `--worklist-port` to **5055** and passes it through, so that is
what a command line or a container actually binds, and it is what the Dockerfile
`EXPOSE`s. `make_worklist_server`'s own signature defaults to **5057**, which the
worklist tests use throughout; that default is reached only by an in-process
caller that omits the argument.

The two have disagreed since both were written. This is recorded rather than
reconciled — picking one is a code change and belongs to the restructure, not to
the extraction that found it. Pass `--worklist-port` explicitly and the question
does not arise.

### Installed deployments

One more variable matters once the package is installed rather than run from a
checkout:

| variable | |
|---|---|
| `REFERRAL_AUDIT_DB` | the audit database path. Its default is package-relative, which resolves correctly in a source checkout and lands beside `site-packages` — read-only — when installed. Audit write failures are swallowed by design, so unset the symptom is an empty audit trail rather than an error. The Dockerfile sets it. |

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

## Provenance

Extracted from the healthcare-rag monorepo with `git filter-repo`, history
preserved. Design: `docs/superpowers/specs/2026-07-31-referral-kernel-design.md`.
