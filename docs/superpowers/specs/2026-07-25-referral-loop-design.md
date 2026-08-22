# Referral Loop Closure — Design

**Date:** 2026-07-25
**Status:** Approved for planning
**Scope:** v1 — deterministic referral/order loop tracking and results matching, on-premise, no PHI egress, no model calls.

> **Read this against the monorepo, not against this repository.** This spec was written while the
> subsystem lived inside `healthcare_rag/`, and every path it names is a pre-extraction path. Where
> it says `guardrails/immutable_audit.py` and `encryption_check.py`, this repository has
> `src/referral_loop/immutable_audit.py` and `src/referral_loop/encryption_check.py` — both vendored
> during the extraction, which is what severed the last two couplings to the parent. Where it names
> `guardrails/phi_redactor.py`, `guardrails/step_up_auth.py`, `guardrails/tenant_isolation.py`,
> `db.py`, `audit_trail.py`, `api.py`, `Dockerfile.referral`, or Revenue Integrity, **those files do
> not exist here and never will** — they are parent-repo modules, and the arguments below for *not*
> importing most of them are why. The extraction is written up in
> `docs/superpowers/plans/2026-08-01-referral-loop-extraction.md`; the same caveat applies to
> `docs/superpowers/plans/2026-07-25-referral-loop.md`, which is this spec's implementation plan.
> Kept as written: rewriting a design document's paths after the fact would make it a worse record
> of the decision than it is a map of the tree.

---

## 1. Problem

A patient is referred or an order is placed, and nobody confirms they were seen or that the result came back. Failure to follow up on test results is a leading source of malpractice claims, and the Joint Commission carries an accreditation goal on communicating critical results. *(Naming it correctly as of 2026: effective 2026-01-01 the Joint Commission renamed the chapter from National Patient Safety Goals to **National Performance Goals** and renumbered this requirement to **NPG.01.02.01** for the Hospital and Critical Access Hospital programs. It remains **NPSG.02.03.01** in the Laboratory program. This spec was written under the old numbering; both citations are given because a reader checking one program's manual will not find the other's number in it.)*

Three capabilities were requested — track referral updates, auto-map arriving imaging results, identify high-risk care gaps. They are **one pipeline, not three subsystems**:

- *Track referral updates* → is the loop open?
- *Auto-map imaging results* → which open loop does this `ORU` close?
- *High-risk care gaps* → which still-open loops are dangerous?

A state machine with a risk overlay. v1 builds the state machine. Risk scoring is v2 with its own spec.

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| PHI boundary | Deterministic local; Claude only ever on de-identified text | v1 needs neither — tracking and matching are identifier arithmetic |
| Ingress | HL7 v2 from the hospital's interface engine | `REF`/`ORU`/`SIU` already flow through Mirth/Rhapsody/Cloverleaf. No vendor approval, no marketplace listing |
| v1 scope | Loop tracking + results matching | Fully deterministic. Fastest to something installable, easiest security review |
| User surface | Local Flask worklist for coordinators | Referral coordinators live in queues; a dedicated one fits their day. Clinicians would reject a second screen — coordinators do not |
| Rule distribution | Rules-as-data, signed and versioned | On-prem means shipping source. Keeping matching rules as a signed, separately-versioned artifact lets a rule change ship and be audited without a code release |
| Structure | Separate deployable, shared primitives | Reuses tested PHI-handling machinery without dragging unrelated components into this deployment's security review |

**Explicitly not integrating with Epic In Basket.** It is the hardest possible ingress and Epic-mediated. The messages that populate the inbox already flow through the interface engine; consuming that feed gives the same information without the dependency.

**This system holds patient data by design**, which places it in a different compliance posture than software that never touches PHI. The relevant path is SOC 2 → HITRUST → BAA, and that requirement shapes several decisions below — notably the on-prem deployment model and the decision to persist raw messages under an explicit retention policy rather than treat them as transient.

**Risk scoring (v2) may be regulated CDS.** The 21st Century Cures Act exemption requires a clinician be able to independently review the basis for a recommendation. The citation-and-confidence architecture satisfies that, but it must be designed in when risk scoring is specced — not retrofitted.

---

## 3. Architecture

```
healthcare_rag/referral_loop/
  listener.py        MLLP/file listener, HL7 v2 framing
  parse_hl7.py       segment parser — allowlist, same shape as ingest_835.py
  registry.py        open-loop store + state machine
  matcher.py         results -> open-order resolution, rule-pack driven
  rules/             signed, versioned rule pack (data, not code)
  worklist.py        Flask blueprint — coordinator queue
  cli.py             referral-loop entry point
Dockerfile.referral  installs healthcare_rag[referral]
```

**Imported from the existing codebase, and nothing else:** `guardrails/immutable_audit.py`, `guardrails/phi_redactor.py`, `guardrails/step_up_auth.py`, `encryption_check.py`, and Flask scaffolding from `api.py`.

`db.py` and `audit_trail.py` are **not** imported, reversing an earlier assumption. `db.py` is hardwired to `rag_growth.db` and carries the RAG growth schema; importing it would drag an unrelated database into a build whose whole claim is a separate deployable. `audit_trail.log_access` delegates to `db.log_phi_access`, so importing it inherits that coupling, and its `AuditEvent` is shaped for an HTTP API (`endpoint`, `tokens_used`, `cost_usd`) with a free-text `query_summary` field — the same free-text channel shape that leaked through an unbounded free-text field in an earlier system. Referral audit therefore routes through `guardrails/immutable_audit.py`, which already owns its own append-only database and blocks `UPDATE`/`DELETE` at the SQLite authorizer — the property §6 requires of `loop_events` anyway.

Both `GuardrailAuditEvent.detail` and `.resource_id` are free-text and must carry only allowlisted, non-identifying values (loop id, tier, pack version). Test 14 asserts this.

`guardrails/tenant_isolation.py` is deliberately **not** imported. v1 is a single-site install; importing an unexercised isolation control would suggest a guarantee the build does not test. It comes in with multi-tenancy or not at all.

**Segment allowlist rationale:** `MRG` is required for `ADT^A40` merges (§4 rule 3) and `ORC` for order control on `ORM`/`OMG`. Omitting either would silently break a documented requirement — the parser can only act on segments it is permitted to read.

**Excluded:** ChromaDB, sentence-transformers, the corpus, the MCP servers, `denial_rca`, `revenue_integrity`. Declared as a `[project.optional-dependencies] referral` group and asserted by an import-closure test.

### Two defining properties, both testable

**No network egress in v1.** Listens on a local port, writes a local encrypted SQLite file, serves a worklist on localhost. No outbound call to anyone, including us. Asserted by blocking non-loopback `socket.connect` and running the full suite.

**No model calls in v1.** Every transition and match is reproducible from `(messages, rule pack version)`. Asserted by monkeypatching `anthropic` and `claude_cli` to raise.

### PHI posture

Operating mode is `PHI_MODE=full`. This triggers existing fail-closed behavior: `encryption_check.verify_encryption_at_rest` at startup, `PHI_API_KEY` required or refuse to boot, per-user audit on every read.

---

## 4. The loop state machine

A loop is an expectation of a result returning. Created by `REF^I12` or `ORM^O01`/`OMG^O19`; **matched** when a final result arrives and a coordinator confirms it belongs to this loop. Whether the finding was then acted on clinically is a separate question, and a separate state — see below.

| State | Entered by | Meaning |
|---|---|---|
| `OPEN` | REF/ORM received | Expectation created |
| `SCHEDULED` | `SIU^S12` | Appointment exists |
| `RESULTED` | matching final ORU | Result arrived, not yet reviewed |
| `ACKNOWLEDGED` | coordinator confirms the match | This result belongs to this loop. Clerical, terminal for v1 |
| `CLOSED` | clinically responsible actor dispositions the finding | **v2 — not implemented in v1** |
| `STALE` | age > per-modality threshold | The thing the product exists to surface |
| `CANCELLED` | `SIU^S15` / order cancel | Expectation withdrawn |
| `ORPHAN` | ORU with no match | Result nobody ordered — needs a human |
| `DISMISSED` | coordinator judges an orphan unattachable | Terminal for orphans that belong to no loop here |

### `ACKNOWLEDGED` and `CLOSED` are two different claims

The original design had one terminal state, `CLOSED`, entered by coordinator acknowledgement. That conflates two claims of very different strength, and the weaker one was wearing the stronger one's name.

A referral coordinator can reliably confirm that *this result belongs to this order*. That is identifier work, and it is what the matching metrics in §7 measure. Whether a clinician competent to act on an abnormal finding has actually read it is a different assertion, and nothing in v1 observes it.

Collapsing them is the product's own failure mode, one layer up: a tool could report every loop closed while no clinician had seen a single result — precisely the "nobody followed up" scenario §1 exists to prevent, now with a dashboard asserting it did not happen.

So v1 implements `ACKNOWLEDGED` and stops there. The worklist says *result matched and acknowledged*, which is exactly true and defensible to a risk officer. `CLOSED` is reserved, unimplemented, and added in v2 once a pilot site names who may clinically disposition a finding (§12 q2). Because the store is append-only, that arrives as a new event type against existing loops — no migration, and no state whose meaning quietly shifts under a name already in use.

**Precision and false-match rate in §7 therefore measure acknowledgement, not clinical closure.** No metric in v1 claims a clinician saw anything.

**`STALE` is derived, not stored.** Three reasons, the last decisive:

1. Writing it into the `state` column destroys the underlying state — a stale loop is still `OPEN` or `SCHEDULED`, and must return to plain `OPEN` the moment a result arrives, without a second transition to undo.
2. A stored value goes wrong the instant a pack revises a threshold: loops labeled stale under the old number stay labeled until something rewrites them.
3. **It would violate §10.5.** No message causes the `STALE` transition — it is entered by the passage of time. Since §10.5 requires every loop's state be reconstructible from `loop_events` alone, and there is no event to replay, a stored `STALE` makes that criterion unsatisfiable.

Staleness is therefore computed from `(state ∈ {OPEN, SCHEDULED}, age, per-modality threshold)` at read time and is the worklist's primary sort. It is listed as a state in the table above because that is how a coordinator experiences it, not because it is one.

**`ORPHAN` is a stored state on a loop-shaped record whose origin is a result rather than an order.** An unmatched `ORU` has no loop by definition, so the matcher creates one in `ORPHAN` to hold it. This keeps the coordinator queue reading a single table, and attaching an orphan is then a merge into the real loop rather than a separate workflow. Every attachment is a labeled example (§7).

### Four rules that are clinical safety decisions

1. **Never acknowledge on a preliminary read.** `OBX-11 = P` advances to `RESULTED` but must not permit `ACKNOWLEDGED`. A radiology prelim that later corrects to a finding is precisely the malpractice scenario; auto-resolving on it would make the tool the cause.
2. **Corrected results reopen review.** `OBX-11 = C` on an `ACKNOWLEDGED` loop returns it to `RESULTED` and re-queues.
3. **Patient merges must carry loops.** `ADT^A40` reassigns an MRN. If loops do not follow the surviving identifier they vanish from the worklist while remaining clinically open — the tool then reports all-clear on an open loop. This breaks most homegrown trackers; it is a first-class case.
4. **A coordinator can undo their own acknowledgement.** Rule 2 recovers the machine's error — a result that later corrects. Nothing recovered the human's: a coordinator who acknowledges the wrong loop had no way back, and the loop stayed resolved while the real one stayed open. `ACKNOWLEDGED` therefore returns to `RESULTED` on an explicit reversal, recorded as a `reversed` event carrying the actor. The append-only store makes this a new event, never a mutation, so the mistake and its correction both remain in the history. Every reversal is also a labeled false positive for §7's flywheel — the most valuable label the system produces, because a human is telling you the matcher was wrong on real site data.

### Merges are not atomic — the retired MRN keeps arriving

Rule 3 carries the loops that exist **at the moment `ADT^A40` is applied**. It does not address what happens next: registration merges and downstream feeds do not cut over together, so interface engines keep emitting the retired MRN for hours or days afterwards. An `ORM` arriving in that window creates a loop on an identifier no coordinator will ever search — the same invisible-open-loop failure rule 3 exists to prevent, arriving through a different door.

v1 therefore keeps a persisted **alias table**, `mrn_aliases(retired_mrn, surviving_mrn, established_at, established_by)`, and resolves every inbound MRN through it. Four decisions govern it.

**1. Aliases do not expire.** The record merge they reflect is permanent, so an expiring alias would silently re-strand loops at an arbitrary later moment — reintroducing the bug on a timer. The concern that motivates expiry is a mistyped `A40` redirecting a real patient's loops, and expiry is the wrong instrument for it: it makes correct aliases fail while doing nothing about incorrect ones, which do their damage immediately. The mitigation is instead an explicit **administrative reversal**, recorded as an event exactly like the acknowledgement reversal in rule 4, plus surfacing alias creation on the worklist so a coordinator sees a merge happen rather than inferring it from missing work.

**2. Aliases chain, and compress on write.** A→B followed by B→C must resolve A→C. Compression happens when B→C is recorded, not when A is looked up. Read-time chasing puts unbounded work on every inbound message and turns a cycle into a hang; write-time compression keeps resolution a single lookup forever, and concentrates the one place a cycle can be detected.

**3. A circular merge is refused, not resolved.** If compressing would leave any identifier pointing at itself, the merge is rejected: the alias table is left unmodified, the event is logged, and the case is flagged for a human. Last-writer-wins would silently pick an arbitrary survivor and strand every loop on the losing side — precisely this section's failure mode, chosen deliberately. A circular merge is an upstream registration error; it needs a person, not a tiebreak. This is the same posture as the confidence floor in §7: decline rather than guess.

**4. Resolution applies to matching as well as loop creation, because it happens before either.** A result carrying the retired MRN has the same problem and a sharper edge: tiers 3 and 4 both key on MRN (§5), so an unresolved alias silently demotes a matchable result to an orphan. Rather than call resolution from the registry and again from the matcher — two call sites that will eventually disagree — **the MRN is resolved once, at message ingest, before the registry or matcher sees it.** The raw archive keeps the message verbatim, as always; everything downstream sees the surviving identifier.

Together, rule 3 handles loops that already existed and the alias table handles messages that arrive afterwards. Neither covers the other, and the gap between them is exactly where a loop goes invisible.

**Storage.** Aliases are an append-only `mrn_alias_events` log under the same tamper triggers as `loop_events`, with `mrn_aliases` as the write-compressed projection over it. The log is the record and the table is the index, exactly as for loops — so §10.5 reconstruction holds for identity as well as state, and an alias reversal is a new event rather than a deletion.

**One refusal inside `open_loop`, and it is not a second resolution point.** Resolving at ingest is not atomic with committing a merge. A listener can resolve an MRN, a merge can commit, and the loop is then written onto an identifier retired microseconds earlier — invisible to the surviving patient *and* missed by that merge's straggler scan, which already ran. Loop creation therefore re-checks that the MRN it was handed is still current, and **refuses** if it is not.

This does not violate decision 4. The guard never *chooses* an identity — it cannot, or it would be the second call site that eventually disagrees with the first. It only rejects one that has stopped being current, converting a silent invisibility into a loud, retryable error the engine will redeliver and the next resolution will get right. Choosing is one place; refusing a stale choice is a precondition, and preconditions belong where the write happens.

### Staleness is per-modality

A stat CT unresulted at 4 hours and a screening mammogram unresulted at 30 days are both stale. One threshold for both is useless. Thresholds live in the rule pack.

---

## 5. Matching

| Tier | Predicate | Confidence |
|---|---|---|
| 1 | placer order number exact, **and MRN agrees** | Highest |
| 2 | filler order number / accession exact, **and MRN agrees** | High |
| 3 | MRN + service code + date window | Medium |
| 4 | MRN + modality equivalence + date window | Low — needs tie-break |
| 5 | none | `ORPHAN` |

### Why the exact tiers still check the MRN

An order number is not a site-wide identifier. Placer numbers are unique per *placing application*, and two feeds that both number from 1000000 collide on the digits alone — so an exact tier-1 match on the digits can attach a result to **a different patient's loop, at full confidence**. That is the worst outcome this system can produce, and it arrives through the tier the design trusts most.

Measured before the guard existed:

```
bare number, different MRN : loop_id='L_other_patient', tier=1, confidence=1.0
```

The assigning authority in the field (`1000001^EPIC` vs `1000001^ATHENA`) already distinguishes them, and the default field map reads the whole field rather than a component, so this was not exploitable as shipped. But that protection is **pack data**: a revision narrowing `placer_order_number` to `OBR-2.1` strips the namespace and silently reopens the vector — through a signed pack edit, which does not pass code review the way a code change would.

So the exact tiers also require the MRN to agree. This cannot false-negative on merges, because resolution happens at ingest (§4) — both identifiers are already the surviving one by the time matching sees them.

**An absent MRN is not agreement, and the two sides are not symmetric.** The predicate above read "and MRN agrees *where both are known*" until the 2026-07-31 audit (finding H3), on the reasoning that a result carrying no `PID-3` must still match an exact accession or a real class of ORU is demoted to an orphan for carrying less data. That trade only holds if the two absences are comparable:

* A **loop** with no MRN cannot occur through ingest — `open_loop` refuses one, because it would be invisible to every patient-scoped query. That branch is unreachable, and it stays open as a defensive one. It is also consulted **second**: the result's half is tested first, so two absent MRNs decline rather than agreeing. Ordered the other way they agree, which is `loop.mrn == key.mrn` reached by another route, and it reopens the finding for any empty-MRN loop that enters an exact-tier state by a path `open_loop` does not guard — a restore from a foreign log, a direct `append_event`, a state added to the set later.
* A **result** with no MRN is ordinary, and it is the half the sender chooses. Order numbers are printed on requisitions and worklists and are frequently sequential, so omitting the PID segment and naming a guessed accession attached a result to a named patient's loop at confidence 1.0 — a loop then reported "awaiting acknowledgement" on evidence that named nobody.

So a result naming no patient no longer matches at tiers 1–2. It **declines with the tier intact**: `loop_id` is None, so it reaches the orphan queue rather than a patient's loop, while `match_tier` and `match_reason` record which tier fired and why it was not acted on. A coordinator can then attach it deliberately through `attach_orphan`, which is also a labeled example (§7). Ingest counts and logs the decline (`unattributable_result_count`), by control id and never by MRN.

Requiring a corroborating field (service code or modality) instead was considered and rejected: it travels in the same OBR, chosen by the same sender, and is printed on the same requisition, so it constrains a typo and not a forgery.

The same decline reaches `SIU` scheduling messages, which match on their order number through these tiers: an appointment message with a valid `ORC-2` and no readable `PID-3` now leaves the loop `OPEN` and counted, where it previously went `SCHEDULED`. That is a real recall loss on a benign shape, accepted rather than special-cased — `S12` and `S15` carry the same evidence, and on that evidence `S15` retires a clinically open loop into a state no worklist shows. A missed schedule leaves the loop where it already was, visible and still aging.

Tie-breakers at tiers 3–4, in order: nearest order date, same ordering provider, most specific modality. All pack-configured, none hardcoded.

The **date window** at tiers 3–4 is pack-configured per modality, not a single global value — a same-day window is right for a stat study and wrong for a screening study ordered weeks ahead.

### The candidate set is scoped before matching begins

Ingest asks the store for the loops that could match — this patient's, plus any loop naming an order number the message carries — rather than for every loop in the site. Every row that narrowing drops is one no tier could have returned: tiers 3–4 are keyed on the patient, tiers 1–2 on an order number the message names. The order-number half deliberately crosses patients, so that a loop carrying this result's accession under a *different* MRN is still seen and reported as a collision instead of quietly missing from the query.

### Which HL7 field feeds each tier is pack data, not code

The tier table above names concepts, not field numbers. **The mapping from concept to segment-field lives in the rule pack**, alongside the tiers, windows, and thresholds:

```json
"field_map": {
  "placer_order_number": ["OBR-2", "ORC-2"],
  "filler_order_number": ["OBR-3", "ORC-3", "OBR-18", "OBR-19"],
  "service_code":        ["OBR-4.1"],
  "modality":            ["OBR-24", "OBR-4.2"],
  "ordering_provider":   ["OBR-16.1"],
  "mrn":                 ["PID-3.1"]
}
```

Each concept lists candidate locations in priority order; the first populated one wins.

This corrects an inconsistency in the original design, which made tiers, tie-breakers, windows, thresholds, and the confidence floor all pack-configured — and then hardcoded the one thing that actually varies between hospitals. Tier *logic* is stable everywhere: an exact accession match is strong evidence at every site. Field *placement* is not. Accession lands in `OBR-3` at some sites, `OBR-18`/`OBR-19` or `ORC-3` at others, depending on the RIS and how the interface engine was built a decade ago.

With the mapping in code, onboarding a site whose accession sits in the wrong field needs a code change, a release, and a security review — which defeats "rules as data, code as commodity" (§2) exactly where it matters most. With it in the pack, that site is a signed pack revision evaluated by replaying its own archive.

The field map is covered by the pack signature, so a mapping change is as tamper-evident as a threshold change — and it needs to be, since pointing `placer_order_number` at the wrong field would silently degrade every tier-1 match into a false positive.

**Orphans are workflow, not failure.** Outside imaging arrives with no order constantly. An orphan queue where a coordinator attaches the result is real value, and every attachment is a labeled example.

**But some orphans belong to no loop here at all** — misrouted from another facility, a patient this site never ordered for, a feed misconfiguration. Without a terminal state these accumulate forever, and a queue that only grows is one coordinators stop opening. That silently disables the surface both the safety story and the flywheel depend on. A coordinator may therefore mark an orphan `DISMISSED`, with a required reason recorded on the event. `DISMISSED` is terminal, is never entered automatically, and its rate is monitored: a rising dismissal rate is a feed problem to investigate upstream, not a coordinator working faster.

---

## 6. Data flow

```
Interface engine --MLLP--> listener.py
                            1. frame (VT ... FS CR)
                            2. persist RAW message, encrypted
                            3. ACK  <-- only after durable write
                                v
                       parse_hl7.py  (allowlist: MSH, PID, MRG, PV1, ORC, OBR, OBX, SCH, RF1)
                                v
                          typed event
                                v
                       registry.py -> state transition -> encrypted SQLite
                                v
                       worklist.py (Flask, localhost)
```

**ACK ordering is load-bearing.** Persist raw durably *before* acknowledging. Acknowledging then crashing during parse means the engine considers the message delivered and it is gone — a silently lost result in a system whose purpose is not losing results. `AE`/`AR` on framing failure so the engine queues and retries.

**Raw archive is separate from parsed state,** retained on its own schedule and replayable. Rule pack updates are evaluated by replaying the archive and measuring the delta. Without the archive a pack revision cannot be evaluated, so the archive is what makes matching quality measurable rather than asserted.

**Idempotency via `MSH-10`, plus a content key.** Duplicate control ID is a no-op, logged, never a second transition.

`MSH-10` alone is not sufficient, and the gap is one this design creates for itself. §6 requires `AE` whenever a message cannot be stored, precisely so the engine queues and retries — and a number of interface engines stamp a **fresh control ID on retry**. The same result then arrives with a new `MSH-10`, passes the duplicate check, and produces a second `resulted` transition or a second orphan. The backpressure mechanism manufactures the duplicates.

Each message therefore also carries a **content key**: a hash over the identifying tuple — filler order number / accession, placer order number, MRN, and the `OBX` set (identifier, value, `OBX-11`). A message whose content key is already present is a no-op, logged, and counted separately from `MSH-10` duplicates. Counted separately because the two mean different things: `MSH-10` duplicates are ordinary engine chatter, while content duplicates under new control IDs indicate a retry configuration worth knowing about.

A genuine amendment carries `OBX-11 = C` and a different `OBX` value, so it produces a different content key and correctly reopens under rule 2 rather than being swallowed as a duplicate.

**Storage:** one encrypted SQLite file, three tables — `raw_messages`, `loops`, `loop_events` (append-only). Any loop's state is reconstructible by replaying its events.

**Retention is configured, not assumed.** Raw messages and resolved loops both need a purge policy the hospital sets. The tooling that enforces it is out of scope for v1 (§11).

---

## 7. Rule pack and eval harness

```
rules/
  pack.json          field map (§5), matching tiers, tie-breakers,
                     modality equivalences, per-modality staleness
                     thresholds and date windows, confidence floor
  pack.sig           Ed25519 signature
  CHANGELOG.md       what changed, and the eval delta that justified it
```

The engine verifies the signature before loading and refuses an unsigned or altered pack. This is IP protection and a safety control — a tampered pack could cause false matches, or point a tier at the wrong field (§5).

### The metric is false-match rate, not accuracy

| Metric | Definition | Role |
|---|---|---|
| Precision | of auto-matched loops, share correctly matched | Primary |
| **False-match rate** | results attached to the wrong open loop | **Safety** |
| Recall | of results with a correct open loop, share matched | Secondary |
| Auto-match rate | of results with a correct open loop, share resolved without a human | Guard on the safety metric — see §10.4 |
| Orphan rate | results with no match | Workload, not failure |
| Dismissal rate | orphans marked `DISMISSED` | Feed health, watched for drift (§5) |

*Renamed from **false-close rate**, which appears in earlier commits and in code written against them. Nothing closes in v1 — `CLOSED` is v2 (§4) — so a metric named for closure described something the system does not do. Same definition, same role, accurate name.*

A false match is strictly worse than an orphan. An orphan gets human attention. A false match attributes a result to the wrong order, marks that loop resolved, and leaves the real loop open *while reporting it handled* — the tool conceals the thing it exists to surface.

The pack therefore carries a **confidence floor**; below it the matcher refuses to auto-match and routes to review. This is the product's equivalent of `INSUFFICIENT_REGULATORY_EVIDENCE` — decline rather than guess.

### Flywheel

Every orphan a coordinator attaches is a labeled example. Every auto-match they undo is a labeled false positive, and more valuable than a synthetic case because it is a real interface quirk from a real site.

**Pack release gate.** Replayed against the archived corpus, a new pack ships only if:

1. **False-match rate does not regress** — absolute veto, regardless of every other number.
2. **Precision does not regress.**
3. **At least one target metric improves** — precision, recall, auto-match rate, orphan rate, or dismissal rate.

The original gate required precision to *improve*, which blocked releases it should have allowed. A pack that leaves precision untouched but halves the orphan rate is a pure coordinator-workload win with no safety cost — and under the old rule it could never ship, because the one metric it did not move was the one being gated on. Requiring *no regression* on the two safety-bearing metrics and *improvement somewhere* keeps the veto exactly as strict while letting the pack improve along any axis that matters.

Regression on false-match rate blocks release regardless of recall. That asymmetry is deliberate and is the whole point: missing a match costs a coordinator a lookup, and a wrong match costs a patient a missed finding.

**Eval corpus, three sources:** synthetic pairs generated from the HL7 v2 spec (ships with the repo, no PHI); de-identified data from any future pilot; site-local labels that remain on the hospital's machine and are contributed back only on opt-in. The architecture must improve locally even when nothing is contributed back.

---

## 8. Failure matrix

| Failure | Behavior |
|---|---|
| Malformed framing | `AR`, archive raw, alert |
| Unparseable segment | Skip segment, keep message, flag |
| Unknown message type | Ignore, count, never error |
| Duplicate `MSH-10` | No-op, logged |
| Duplicate content key, new `MSH-10` | No-op, logged and counted **separately** from an `MSH-10` duplicate — it signals a retry configuration, not ordinary chatter (§6) |
| Field map names a segment-field absent from the message | Fall through to the next candidate for that concept; if all are absent the tier does not fire. Never an error, never a partial match |
| Field map names a segment outside the §3 allowlist | **Refuse to boot.** A pack must not be able to widen the parser's read surface |
| Attempted transition to `CLOSED` | Refuse and log. Reserved for v2 (§4) |
| DB unwritable or full | **`AE` so the engine queues.** Never ACK what cannot be stored |
| Pack signature invalid | Refuse to boot |
| Encryption-at-rest check fails | Refuse to boot |
| `ADT^A40` with unknown surviving MRN | Create surviving record, carry loops, log |
| `ADT^A40` that would create a cycle | **Refuse.** Alias table unmodified, no loop moved, flagged for human review (§4) |
| Message carrying a retired MRN | Resolve to the surviving MRN at ingest, before registry or matcher. Raw archive keeps it verbatim |
| Alias reversal requested | Recorded as an event with actor and reason; never a deletion from the alias table |
| MRN retired between ingest resolution and loop creation | **Refuse the write.** Engine retries; the next resolution is current. Never land a loop on a retired identifier (§4) |
| Result for a `CANCELLED` loop | Orphan + flag |
| Future-dated observation | Accept, clamp for staleness math, flag |

---

## 9. Tests

The first nine are safety, not correctness.

1. **Preliminary never resolves.** `ACKNOWLEDGED` unreachable from `OBX-11 = P`.
2. **Corrected result reopens.** `OBX-11 = C` on `ACKNOWLEDGED` returns to `RESULTED`.
3. **Merge carries loops.** `ADT^A40` moves every open loop to the surviving MRN; none orphaned.
4. **A loop cannot be stranded after a merge.** An `ORM` arriving with the retired MRN *after* the `A40` opens its loop on the surviving MRN. Paired with test 3, this closes both sides of the window in §4; either alone leaves a clinically open loop off the worklist.
5. **A result carrying the retired MRN still matches.** Asserts resolution happens before matching, not only before loop creation — an unresolved alias would demote a tier-3 match to an orphan silently.
6. **Circular merge is refused.** A→B then B→A leaves the alias table unmodified and flags for review. Assert no identifier resolves to itself and no loop moved.
7. **Loop creation refuses an MRN retired since ingest resolved it.** Commit a merge between resolution and loop creation; assert the write is refused rather than landing on the retired identifier. Closes the window that resolve-once-at-ingest opens (§4).
8. **False-match gate.** Replay labeled corpus; false-match rate zero at the confidence floor **while auto-match rate meets its configured minimum**. Blocks pack release. Both halves are required — see §10.4.
9. **`CLOSED` is unreachable in v1.** No message, coordinator action, or replay path reaches it. Asserts the state reserved in §4 cannot be entered by accident before v2 defines who may enter it.
10. **Acknowledgement is reversible.** A `reversed` event returns `ACKNOWLEDGED` to `RESULTED`, the actor is recorded, and no prior event is mutated.
11. **Alias chains compress.** A→B then B→C resolves A→C in a single lookup, and the compression is asserted at write time rather than by chasing at read time.
12. **No egress.** Block non-loopback `socket.connect`; full suite passes.
13. **No model calls.** Monkeypatch `anthropic` and `claude_cli` to raise; full suite passes.
14. **No PHI in artifacts.** Sentinels planted across `PID`, `NK1`, `GT1`, and note segments appear zero times in worklist HTML, logs, exports, and audit entries. Assert on what leaves the building, not on the parser.
15. **Persist before ACK.** Kill between durable write and parse; replay reconstructs state.
16. **Retry under a new control ID is not a second transition.** Redeliver an identical result with a fresh `MSH-10`; assert one transition, and that the content-key duplicate is counted separately from an `MSH-10` duplicate.
17. **Pack tamper.** Mutate one byte; assert refusal to load. Includes a byte inside `field_map` — a mapping change must be as tamper-evident as a threshold change.
18. **Field map drives matching.** Relocate the accession from `OBR-3` to `OBR-18` in both fixture and pack; assert tier 2 still matches with no code change. This is the claim that "rules as data" actually holds where sites differ.
19. **State reconstruction.** Any loop's state derivable by replaying `loop_events`.

**Fixtures are synthetic,** generated from the HL7 v2 specification. No real message enters version control regardless of claimed de-identification.

---

## 10. Success criteria

1. A synthetic HL7 stream runs end-to-end to a populated worklist with zero model calls and zero non-loopback connections.
2. All nine safety tests pass.
3. The PHI-sentinel proof passes on every downstream artifact.
4. **False-match rate is zero against the labeled corpus at the configured floor, *and* auto-match rate is at or above its configured minimum.**
5. A loop's state is reconstructible from `loop_events` alone.
6. The referral install requires neither ChromaDB nor the corpus, asserted by import-closure test.

**Why criterion 4 has two halves.** Stated as "false-match rate is zero" alone, it is passed perfectly by a matcher that auto-matches nothing: every result orphans, no result is attached to the wrong loop, the safety number reads 0.000 and the gate goes green. The degenerate implementation scores best. Worse, it fails invisibly — the metric everyone watches looks ideal precisely when the product has stopped working, and the only symptom is a coordinator queue quietly filling with work the tool was bought to remove.

Pairing it with a minimum auto-match rate closes that. The pair says *decline when uncertain, but you must still resolve most of what you see* — which is the actual product claim. The minimum is pack-configured and starts deliberately low; raising it is a decision backed by replay evidence, never an aspiration set at the start.

This is the same reasoning as the confidence floor in §7, applied to the criterion that audits it. A floor without a coverage requirement optimises toward silence.

---

## 11. Out of scope for v1

| Excluded | Reason |
|---|---|
| Care-gap risk scoring | v2. Introduces the de-identification path, LLM dependency, and CDS regulatory question at once |
| Writing back to the EHR | Needs interface-engine write access; a bad message pollutes the legal record |
| Epic In Basket integration | Epic-mediated, hardest possible ingress, unnecessary given the feed |
| Multi-tenancy | Single-site install. `tenant_isolation` is deliberately **not** imported (§3) |
| FHIR ingress | HL7 v2 first. FHIR is the better long-term model but adds vendor approval |
| Clinical closure (`CLOSED`) | The state is reserved in §4 and asserted unreachable by test 9. It ships when a pilot site names who may clinically disposition a finding (§12 q2) |
| Retention purge tooling | §6 requires a retention *policy*, which is a site decision. Shipping a delete path over PHI and an append-only audit archive before that policy exists is the wrong order. `referral-loop purge --older-than` lands once a site states a period |

---

## 12. Open questions

1. **MLLP listener vs file drop for the pilot.** MLLP is the real integration and the design assumes it. A file-drop mode reading messages from a watched directory would let a site pilot before IT schedules an interface build. Decide when a pilot site is identified — the parser and registry are identical either way, so this is a listener-only concern.

2. ~~**Who acknowledges a loop.**~~ **Resolved by construction — see §4.** The question was whether a coordinator's acknowledgement is a clinical record or a workflow one. Rather than guess, v1 stops at `ACKNOWLEDGED` — a claim a coordinator can actually support — and reserves `CLOSED` for a clinically responsible actor, unimplemented and asserted unreachable (test 9). No metric in v1 claims a clinician saw anything.

   What still needs a pilot site is the narrower question: **who may enter `CLOSED`, and does that person work from this worklist or from the EHR they already live in?** That is a v2 scoping question, and it no longer blocks v1 or shapes what v1's worklist claims.

3. **Whether staleness thresholds are defensible defaults or site-configured.** Shipping a default implies a clinical standard. Per-modality defaults should probably ship as a starting point the site must explicitly accept, so the threshold is their clinical decision rather than ours.
