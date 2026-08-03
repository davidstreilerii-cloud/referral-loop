# FHIR Read Client — Design Spec

> **Slice 3, sub-project B.** Pull the documents that close open loops: a bounded, paginated,
> retrying read against a configured FHIR endpoint, returning validated resources with the query
> that produced them.

**Status:** design approved 2026-08-02. Implementation plan not yet written.
**Depends on:** sub-project A (`docs/superpowers/specs/2026-08-02-connector-registry-design.md`),
merged to `main` at `7827c2d`.
**Parent spec:** `2026-07-31-referral-kernel-design.md` — §1.3 (corrections 4 and 5), §2 (slice 3).

---

## 0. What this spec covers

One sub-project: a purpose-built query for candidate closing documents, and the machinery it needs
— pagination, retry, `OperationOutcome` handling, structural validation. It does not cover mapping
onto the canonical model, US Core profile validation, Synthea fixtures, TEFCA, or any write.

## 1. Context

### 1.1 What A left in place

A shipped `src/referral_loop/connect/`: a validated connector registry, a hard egress allowlist,
SMART Backend Services authentication with an in-memory token cache, and a `connectors` CLI mode
that preflights every endpoint. B is the first thing that reads an actual resource.

Three properties A established that B must not weaken:

1. **`egress.fetch` is the only call site that opens a connection**, because `check_allowed` runs
   inside it. `tests/test_import_closure.py` pins both halves — which file may import
   `urllib.request`, and that `fetch` is the sole `.open()` caller within it.
2. **Every outbound URL is normalized to `(scheme, host, port)` and checked against
   `ConnectorRegistry.endpoints()`.** `endpoint_of` refuses an unknown scheme with a typed error
   rather than a `KeyError`, added in A specifically because "the read client will one day hand it
   a `next` link out of a Bundle." That day is this spec.
3. **No default for any value whose default would be a decision**, and refusal in preference to
   assumption.

### 1.2 The use case, stated exactly

A referral loop is open. The order went out, the staleness threshold has passed, and no result has
come back over HL7. The question this sub-project answers is: *does a document exist on the other
side that nobody sent us?*

Parent spec §1.3 correction 5 is the argument for why this matters — query-based exchange is the
mechanism for pulling a consult note nobody pushed, and it is the only path that produces value
with zero cooperation from the receiving side. Everything else in the product waits for a message.

### 1.3 The identity problem, and how far this spec goes into it

Parent spec §1.3 correction 4 records that cross-organization patient identity is "the actual hard
problem" and scopes probabilistic resolution to slice 2. B does not attempt it.

B searches by identifier in a namespace the connector **declares** it accepts. Where that
declaration is absent, B refuses to search rather than searching badly. The whole design of §3
follows from one property:

> **Zero results must always mean "we asked and there was nothing." It must never mean "we could
> not ask."**

Those two are indistinguishable in a result set and opposite in meaning. A product whose entire
purpose is finding loops that never closed cannot afford to report the second as the first.

## 2. Architecture

```
src/referral_loop/connect/
├── connectors.py     # MODIFY -- identifier_systems, deferred from A section 4.4
├── retry.py          # NEW -- fetch_retrying: bounded attempts, Retry-After aware
├── resources.py      # NEW -- FetchedResource, structural validation
└── documents.py      # NEW -- find_candidate_documents, the pagination walk
```

Nothing outside `connect/` changes. `core/` remains unreachable from here. The five files A froze —
`registry.py`, `store.py`, `listener.py`, `matcher.py`, `peers.py` — stay frozen.

**`retry.py` sits beside `egress.py` rather than inside it.** They answer different questions:
`egress.fetch` is one bounded request to an allowlisted host; `fetch_retrying` is a policy over
repeated requests. Separating them also keeps A's single-call-site test trivially true — retry
wraps `fetch` and never opens anything itself.

## 3. Patient identity

### 3.1 The field

```json
"identifier_systems": { "mrn": "urn:oid:1.2.840.114350.1.13.xxx.1.7.5.737384.14" }
```

Optional on the profile. When present it is validated at load like every other field: a non-empty
string, bounded length, refusing on malformed.

### 3.2 The search is two hops, not one

The obvious query is a chained search:

```
DocumentReference?patient.identifier=<system>|<mrn>&date=ge<ordered_at>
```

**It is not used.** Chained search parameter support varies widely between servers and is
inconsistent even across Epic deployments, and a server that does not support the chain does not
usually say so — it ignores the parameter and returns an unfiltered Bundle, or returns nothing.
Both are indistinguishable from a correct empty answer, which is the failure mode §1.3 forbids.

So the search resolves the patient first and then queries by reference, which is universally
supported:

```
1.  Patient?identifier=<system>|<mrn>
2.  DocumentReference?patient=Patient/<id>&date=ge<ordered_at>
    DiagnosticReport?patient=Patient/<id>&date=ge<ordered_at>
```

### 3.3 The three outcomes of hop 1, which must stay distinct

| hop 1 result | meaning | behaviour |
|---|---|---|
| exactly one Patient | resolved | proceed to hop 2 |
| **zero Patients** | **this connector does not know this patient** | `PatientNotFoundAtConnector` — **not** an empty document set |
| more than one Patient | the identifier is not unique here | `PatientAmbiguousAtConnector` — refuse; do not pick one |

The zero case is the whole reason this spec is shaped the way it is. "We asked and this patient is
unknown here" and "we asked and there are no documents" are opposite facts about a referral loop,
and a caller handed an empty list cannot tell them apart. One says *go look somewhere else*; the
other says *the specialist genuinely never documented anything*.

The ambiguous case refuses rather than guessing because picking a patient is how another patient's
consult note gets attached to this referral — a wrong match is worse than no match, and choosing
between candidates is slice 2's problem, not this one's.

### 3.4 Two resource types

Both are queried, because both close loops. A specialist consult note arrives as a
`DocumentReference`; a lab or imaging result arrives as a `DiagnosticReport`, which is the FHIR
analogue of the `ORU^R01` the existing HL7 path already closes on. Querying only the first would
miss every diagnostic referral, which is most of them.

Each type is a separate search with its own pagination walk. The caps in §5 apply to the call as a
whole, not per type, so two resource types cannot quietly double the budget.

### 3.5 Why optional, when the decision was "refuse at config time"

The decision taken during design was config-time refusal. Read literally that breaks A: every
connector configured today is preflight-only, declares no authorities, reads nothing, and has no
`identifier_systems`. Requiring the field at load would refuse each of them for not doing something
B has not shipped.

So the intent is preserved and the mechanism moved:

| condition | behaviour |
|---|---|
| `identifier_systems` present | validated at load; connector is **queryable** |
| absent | connector is **preflight-only**; `find_candidate_documents` raises `ConnectorCannotResolvePatients` naming the missing field |
| absent, and someone queries anyway | a typed refusal — **never an empty result set** |

The distinction is carried by a type rather than by a caller remembering to check a flag, which is
what makes §1.3's property hold under maintenance rather than under discipline.

`connectors --connectors <file>` additionally reports queryable versus preflight-only per
connector, so the incapability is visible at configuration time — which is what config-time
refusal was reaching for — instead of being discovered by the first query that needed it.

## 4. The query

```python
def find_candidate_documents(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    *,
    mrn: str,
    since: datetime,
    until: datetime | None = None,
) -> DocumentSearch
```

One entry point, deliberately. A general `search(resource_type, **params)` is a URL-construction
surface pointed at a network, and A spent its entire budget establishing that every outbound URL is
normalized and allowlisted. One purpose-built query keeps that boundary as small as the use case
allows, and there is exactly one consumer today. When a second query type appears, the machinery
behind this function is already factored to serve it; a general client built now would be
speculative surface.

### 4.1 What comes back

```python
@dataclass(frozen=True)
class FetchedResource:
    resource_type: str                # "DocumentReference" | "DiagnosticReport"
    resource: Mapping[str, object]    # validated, still FHIR
    connector_id: str
    query_url: str
    page: int


@dataclass(frozen=True)
class DocumentSearch:
    resources: tuple[FetchedResource, ...]
    patient_id: str            # what hop 1 resolved to, for provenance
    pages_walked: int          # across both resource types
    skipped_malformed: int
    query_urls: tuple[str, ...]   # hop 1 and both hop-2 searches
```

Still FHIR, deliberately. Mapping onto `Referral` and `InboundArtifact` is sub-project C, and
stopping at this boundary is what lets B be tested without importing `core/` — the layering claim
the whole architecture rests on.

`skipped_malformed` is on the result rather than in a log, so "we found three documents" can never
quietly mean "we found three and discarded two."

## 5. Pagination

Every `Bundle.link[relation="next"]` is a URL chosen by the remote and is treated as such.

| rule | why |
|---|---|
| each `next` passes `check_allowed` before it is fetched | it is attacker-influenceable if the remote is compromised; this is the case `endpoint_of`'s scheme guard was hardened for in A |
| off-allowlist **refuses loudly** | a silent stop is a truncated result set wearing the costume of a complete one |
| `MAX_PAGES` and `MAX_RESOURCES`, exceeding either **raises** | a truncated search reporting "nothing further" is the same false negative as never searching |
| a `next` equal to the URL just fetched refuses immediately | a loop, caught directly rather than by burning the page budget to discover it |

Truncate-and-flag was considered and rejected for the same reason `skipped_malformed` is on the
result: a caller who forgets to check the flag reports a clean "nothing found" over a partial
search, and that is the failure this product exists to prevent.

## 6. Retry

`fetch_retrying` wraps `egress.fetch` with bounded attempts.

- **Retryable:** `429`, `503`, and transport-level `ConnectorUnreachable`. Nothing else.
- **A 400 is never retried.** Same rubric as `_OUR_FAULT` in `auth.py` — a malformed query does not
  become well-formed on a second attempt, and retrying it wastes the budget that a genuine
  transient needs.
- **`Retry-After` is honoured and capped.** A server answering `Retry-After: 86400` must not park a
  coordinator's preflight for a day. We take the smaller of what was asked and our ceiling, and log
  that we shortened it — silently ignoring the header would be worse than capping it visibly.
- **The sleep function is injected**, defaulting to `time.sleep`. Tests pass a recording no-op, so
  backoff intervals are asserted precisely and the suite stays fast. Same reasoning that put
  `now: datetime | None = None` throughout `auth.py`.

## 7. Errors, and one PHI trap

| error | when |
|---|---|
| `ConnectorCannotResolvePatients` | no `identifier_systems.mrn`; refuses rather than returning zero |
| `PatientNotFoundAtConnector` | hop 1 matched no Patient — this connector does not know them; **never** an empty document set |
| `PatientAmbiguousAtConnector` | hop 1 matched more than one Patient; refuse rather than pick |
| `FhirRequestFailed` | non-retryable HTTP status, or an `OperationOutcome` with severity `error` |
| `PaginationRefused` | `next` off-allowlist, self-referential, or a cap exceeded |
| `ResourceMalformed` | a resource missing a field C will need |

All subclass `ReferralLoopError`.

### 7.1 `OperationOutcome.diagnostics` is not safe to log

FHIR servers routinely echo the failing request in `diagnostics`. Our request contains an MRN.

So `FhirRequestFailed` carries the issue's `severity` and `code` — enumerated FHIR values, safe by
construction — and **never `diagnostics`**. A's review established that this codebase has no
logging scrubber installed (`cli.py` calls plain `logging.basicConfig` with no filter), so there is
nothing downstream to catch this if it is got wrong here. §9 requires a test asserting the MRN does
not appear in captured logs.

## 8. Validation

Structural, not profile-based: `resourceType`, plus the fields C actually reads — `status`,
`subject`, and for `DocumentReference` at least one `content[].attachment`.

Not US Core conformance. That needs a validator dependency and profile packages, which is a real
change to a two-runtime-dependency posture that `tests/test_install_closure.py` asserts against the
built image. It is its own unit.

**A resource failing validation is skipped and counted, not fatal.** This matches the posture
`UnparseableSegmentError` already takes in `errors.py` — *"Skip it, keep the message, flag for
review."* One malformed resource must not hide the nine good ones. The count returns in
`DocumentSearch.skipped_malformed`.

## 9. Testing

`tests/_fhirserver.py` gains Bundle responses with configurable `next` links, and injectable
`OperationOutcome`, `429` with `Retry-After`, and `503`.

Required cases:

- happy path: one page, resources validated and returned
- multi-page walk, `pages_walked` correct
- `next` pointing off-allowlist → `PaginationRefused`, and **nothing fetched from it**
- `MAX_PAGES` exceeded → raises, does not truncate
- self-referential `next` → refuses immediately
- `429` with `Retry-After` honoured, and a large value capped, with an injected sleep recording the
  intervals
- `400` not retried — assert exactly one request reached the server
- **the MRN does not appear in `caplog`** after a failure carrying `diagnostics`
- a malformed resource skipped, counted, and the good ones still returned
- a connector without `identifier_systems` raises `ConnectorCannotResolvePatients` rather than
  returning an empty `DocumentSearch`
- **hop 1 returning zero Patients raises `PatientNotFoundAtConnector`, not an empty result**
- hop 1 returning two Patients raises `PatientAmbiguousAtConnector` and fetches no documents
- hop 1 resolving cleanly while both hop-2 searches return nothing yields an **empty
  `DocumentSearch`** — the one case where empty is the correct answer
- both resource types are queried, and a `DiagnosticReport`-only result set still comes back

The last four are the load-bearing tests of this spec, and they are one test in four parts: they
pin the boundary between *could not ask*, *asked the wrong way*, *asked about someone unknown*, and
*asked and there was nothing*. If any of the first three ever starts returning empty instead of
raising, §1.3's property is gone and the product reports closed loops that are open.

## 10. Definition of done

- [ ] `identifier_systems` validated at load when present; absent means preflight-only
- [ ] `find_candidate_documents` refuses, with a type, for a connector that cannot resolve patients
- [ ] the search is two hops; no chained `patient.identifier` parameter is sent
- [ ] a patient unknown at the connector, and an ambiguous one, each raise rather than return empty
- [ ] both `DocumentReference` and `DiagnosticReport` are queried, sharing one page budget
- [ ] every `next` URL passes `check_allowed`; off-allowlist refuses loudly
- [ ] page and resource caps raise rather than truncate; a self-referential `next` refuses
- [ ] `429`/`503`/transport retried with bounded attempts and a capped `Retry-After`; `400` never
- [ ] `FhirRequestFailed` carries `severity` and `code` and never `diagnostics`; a test proves the
      MRN stays out of the logs
- [ ] malformed resources skipped and counted, not fatal
- [ ] `connectors` report distinguishes queryable from preflight-only
- [ ] `core/` still cannot import `connect/`; `fetch` still the only `.open()` call site
- [ ] full suite green with the pre-existing count unmoved; `ruff` and `mypy` clean
- [ ] `git diff` over `registry.py`, `store.py`, `listener.py`, `matcher.py`, `peers.py` empty

## 11. Explicitly out of scope

US Core profile validation · Synthea fixtures · TEFCA/QHIN query exchange · any write back to a
remote · cross-organization identity resolution · mapping onto the canonical model (sub-project C)
· CDS Hooks · MCP · SMART launch · the worklist UI · scheduling or triggering these queries from
the aging agent.
