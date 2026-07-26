# Referral Loop Closure — Design

**Date:** 2026-07-25
**Status:** Approved for planning
**Scope:** v1 — deterministic referral/order loop tracking and results matching, on-premise, no PHI egress, no model calls.

---

## 1. Problem

A patient is referred or an order is placed, and nobody confirms they were seen or that the result came back. Failure to follow up on test results is a leading source of malpractice claims, and Joint Commission carries a National Patient Safety Goal on communicating critical results.

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
| IP protection | Rules-as-data, signed and versioned | On-prem means shipping source. Keeping matching rules as a signed, separately-versioned artifact lets a rule change ship and be audited without a code release |
| Structure | Separate deployable, shared primitives | Reuses tested PHI-handling machinery without dragging unrelated components into this deployment's security review |

**Explicitly not integrating with Epic In Basket.** It is the hardest possible ingress and Epic-mediated. The messages that populate the inbox already flow through the interface engine; consuming that feed gives the same information without the dependency.

**This is a different regulatory stage** than software that never touches PHI. This product holds patient data by design. It belongs to the SOC 2 → HITRUST → BAA path, not the pre-SOC-2 path.

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

Both `GuardrailAuditEvent.detail` and `.resource_id` are free-text and must carry only allowlisted, non-identifying values (loop id, tier, pack version). Test 7 asserts this.

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

A loop is an expectation of a result returning. Created by `REF^I12` or `ORM^O01`/`OMG^O19`; satisfied when a matching **final** result arrives and a coordinator acknowledges it.

| State | Entered by | Meaning |
|---|---|---|
| `OPEN` | REF/ORM received | Expectation created |
| `SCHEDULED` | `SIU^S12` | Appointment exists |
| `RESULTED` | matching final ORU | Result arrived, not yet reviewed |
| `CLOSED` | coordinator acknowledges | Loop complete |
| `STALE` | age > per-modality threshold | The thing the product exists to surface |
| `CANCELLED` | `SIU^S15` / order cancel | Expectation withdrawn |
| `ORPHAN` | ORU with no match | Result nobody ordered — needs a human |

**`STALE` is derived, not stored.** Three reasons, the last decisive:

1. Writing it into the `state` column destroys the underlying state — a stale loop is still `OPEN` or `SCHEDULED`, and must return to plain `OPEN` the moment a result arrives, without a second transition to undo.
2. A stored value goes wrong the instant a pack revises a threshold: loops labeled stale under the old number stay labeled until something rewrites them.
3. **It would violate §10.5.** No message causes the `STALE` transition — it is entered by the passage of time. Since §10.5 requires every loop's state be reconstructible from `loop_events` alone, and there is no event to replay, a stored `STALE` makes that criterion unsatisfiable.

Staleness is therefore computed from `(state ∈ {OPEN, SCHEDULED}, age, per-modality threshold)` at read time and is the worklist's primary sort. It is listed as a state in the table above because that is how a coordinator experiences it, not because it is one.

**`ORPHAN` is a stored state on a loop-shaped record whose origin is a result rather than an order.** An unmatched `ORU` has no loop by definition, so the matcher creates one in `ORPHAN` to hold it. This keeps the coordinator queue reading a single table, and attaching an orphan is then a merge into the real loop rather than a separate workflow. Every attachment is a labeled example (§7).

### Three rules that are clinical safety decisions

1. **Never close on a preliminary read.** `OBX-11 = P` advances to `RESULTED` but must not permit `CLOSED`. A radiology prelim that later corrects to a finding is precisely the malpractice scenario; auto-closing on it would make the tool the cause.
2. **Corrected results reopen review.** `OBX-11 = C` on a `CLOSED` loop returns it to `RESULTED` and re-queues.
3. **Patient merges must carry loops.** `ADT^A40` reassigns an MRN. If loops do not follow the surviving identifier they vanish from the worklist while remaining clinically open — the tool then reports all-clear on an open loop. This breaks most homegrown trackers; it is a first-class case.

### Staleness is per-modality

A stat CT unresulted at 4 hours and a screening mammogram unresulted at 30 days are both stale. One threshold for both is useless. Thresholds live in the rule pack.

---

## 5. Matching

| Tier | Predicate | Confidence |
|---|---|---|
| 1 | `OBR-2` placer order number exact | Highest |
| 2 | `OBR-3` filler order number / accession exact | High |
| 3 | MRN + `OBR-4` service code + date window | Medium |
| 4 | MRN + modality equivalence + date window | Low — needs tie-break |
| 5 | none | `ORPHAN` |

Tie-breakers at tiers 3–4, in order: nearest order date, same ordering provider, most specific modality. All pack-configured, none hardcoded.

The **date window** at tiers 3–4 is pack-configured per modality, not a single global value — a same-day window is right for a stat study and wrong for a screening study ordered weeks ahead.

**Orphans are workflow, not failure.** Outside imaging arrives with no order constantly. An orphan queue where a coordinator attaches the result is real value, and every attachment is a labeled example.

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

**Idempotency via `MSH-10`.** Duplicate control ID is a no-op, logged, never a second transition.

**Storage:** one encrypted SQLite file, three tables — `raw_messages`, `loops`, `loop_events` (append-only). Any loop's state is reconstructible by replaying its events.

**Retention is configured, not assumed.** Raw messages and closed loops both need a purge policy the hospital sets.

---

## 7. Rule pack and eval harness

```
rules/
  pack.json          matching tiers, tie-breakers, modality equivalences,
                     per-modality staleness thresholds, confidence floor
  pack.sig           Ed25519 signature
  CHANGELOG.md       what changed, and the eval delta that justified it
```

The engine verifies the signature before loading and refuses an unsigned or altered pack. This is IP protection and a safety control — a tampered pack could cause false closes.

### The metric is false-close rate, not accuracy

| Metric | Definition | Role |
|---|---|---|
| Precision | of auto-closed loops, share correctly matched | Primary |
| **False-close rate** | results attached to the wrong open loop | **Safety** |
| Recall | of results with a correct open loop, share matched | Secondary |
| Orphan rate | results with no match | Workload, not failure |

A false close is strictly worse than an orphan. An orphan gets human attention. A false close attributes a result to the wrong order, marks that loop satisfied, and leaves the real loop open *while reporting it closed* — the tool conceals the thing it exists to surface.

The pack therefore carries a **confidence floor**; below it the matcher refuses to auto-match and routes to review. This is the product's equivalent of `INSUFFICIENT_REGULATORY_EVIDENCE` — decline rather than guess.

### Flywheel

Every orphan a coordinator attaches is a labeled example. Every auto-match they undo is a labeled false positive, and more valuable than a synthetic case because it is a real interface quirk from a real site.

**Pack release gate:** a new pack ships only if, replayed against the archived corpus, false-close rate does not increase **and** precision improves. Regression on the safety metric blocks release regardless of recall.

**Eval corpus, three sources:** synthetic pairs generated from the HL7 v2 spec (ships with the repo, no PHI); de-identified data from any future pilot; site-local labels that remain on the hospital's machine and are contributed back only on opt-in. The architecture must improve locally even when nothing is contributed back.

---

## 8. Failure matrix

| Failure | Behavior |
|---|---|
| Malformed framing | `AR`, archive raw, alert |
| Unparseable segment | Skip segment, keep message, flag |
| Unknown message type | Ignore, count, never error |
| Duplicate `MSH-10` | No-op, logged |
| DB unwritable or full | **`AE` so the engine queues.** Never ACK what cannot be stored |
| Pack signature invalid | Refuse to boot |
| Encryption-at-rest check fails | Refuse to boot |
| `ADT^A40` with unknown surviving MRN | Create surviving record, carry loops, log |
| Result for a `CANCELLED` loop | Orphan + flag |
| Future-dated observation | Accept, clamp for staleness math, flag |

---

## 9. Tests

The first four are safety, not correctness.

1. **Preliminary never closes.** `CLOSED` unreachable from `OBX-11 = P`.
2. **Corrected result reopens.** `OBX-11 = C` on `CLOSED` returns to `RESULTED`.
3. **Merge carries loops.** `ADT^A40` moves every open loop to the surviving MRN; none orphaned.
4. **False-close gate.** Replay labeled corpus; false-close rate zero at the confidence floor. Blocks pack release.
5. **No egress.** Block non-loopback `socket.connect`; full suite passes.
6. **No model calls.** Monkeypatch `anthropic` and `claude_cli` to raise; full suite passes.
7. **No PHI in artifacts.** Sentinels planted across `PID`, `NK1`, `GT1`, and note segments appear zero times in worklist HTML, logs, exports, and audit entries. Assert on what leaves the building, not on the parser.
8. **Persist before ACK.** Kill between durable write and parse; replay reconstructs state.
9. **Pack tamper.** Mutate one byte; assert refusal to load.
10. **State reconstruction.** Any loop's state derivable by replaying `loop_events`.

**Fixtures are synthetic,** generated from the HL7 v2 specification. No real message enters version control regardless of claimed de-identification.

---

## 10. Success criteria

1. A synthetic HL7 stream runs end-to-end to a populated worklist with zero model calls and zero non-loopback connections.
2. All four safety tests pass.
3. The PHI-sentinel proof passes on every downstream artifact.
4. False-close rate is zero against the labeled corpus at the configured floor.
5. A loop's state is reconstructible from `loop_events` alone.
6. The referral install requires neither ChromaDB nor the corpus, asserted by import-closure test.

---

## 11. Out of scope for v1

| Excluded | Reason |
|---|---|
| Care-gap risk scoring | v2. Introduces the de-identification path, LLM dependency, and CDS regulatory question at once |
| Writing back to the EHR | Needs interface-engine write access; a bad message pollutes the legal record |
| Epic In Basket integration | Epic-mediated, hardest possible ingress, unnecessary given the feed |
| Multi-tenancy | Single-site install. `tenant_isolation` is deliberately **not** imported (§3) |
| FHIR ingress | HL7 v2 first. FHIR is the better long-term model but adds vendor approval |

---

## 12. Open questions

1. **MLLP listener vs file drop for the pilot.** MLLP is the real integration and the design assumes it. A file-drop mode reading messages from a watched directory would let a site pilot before IT schedules an interface build. Decide when a pilot site is identified — the parser and registry are identical either way, so this is a listener-only concern.

2. **Who acknowledges a loop.** `CLOSED` requires coordinator acknowledgement, but if the coordinator is not the clinically responsible party then acknowledgement is a workflow record, not a clinical one. This affects what the worklist claims. Resolve with the first pilot site's actual workflow rather than by assumption.

3. **Whether staleness thresholds are defensible defaults or site-configured.** Shipping a default implies a clinical standard. Per-modality defaults should probably ship as a starting point the site must explicitly accept, so the threshold is their clinical decision rather than ours.
