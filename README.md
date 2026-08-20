# referral-loop

**Closes the inbound referral loop.** Tracks a referral from order to returned documentation, and
surfaces the ones that never came back.

---

## The problem

A physician refers a patient to a specialist. The order goes out. Somewhere between the order and
the consult note coming back, the loop breaks — and nobody notices, because a referral that
silently failed looks exactly like a referral still in progress.

I spent ~3,600 hours as a medical scribe watching physicians absorb this manually: chasing results,
re-faxing orders, discovering months later that a patient was never seen. The tracking mechanism was
a physician's memory.

This system replaces that with a worklist.

## What it does

Ingests HL7 v2 over MLLP, matches inbound results to open referrals, and replays loop state from an
append-only event log. A coordinator sees three queues:

| Queue | Meaning |
|---|---|
| **Open loops** | Referred, no result back yet |
| **Resulted, unacknowledged** | Result arrived, nobody has confirmed it |
| **Orphans** | A result arrived that matched no order |

Plus a FHIR read client that answers a further question: *does a document exist on the other side
that nobody sent us?* This is the only path that produces value with zero cooperation from the
receiving side — everything else waits for someone to send a message.

## Why it's built this way

**It contains no AI.** No model calls, no ML stack, no model client. In a market where every
clinical tool is adding a language model, this one does a safety-critical job deterministically —
and proves it: a test asserts the absence against the **built container image**, not the Dockerfile.
A Dockerfile is a claim about an image; the image is what runs.

**The release gate is false-match rate, not accuracy.** The two errors are not symmetric. A missed
match leaves a loop open and a coordinator works it. A false match attaches another patient's result
to this referral and marks it closed — invisible, and terminal. An accuracy metric averages these
together and will happily trade one for the other.

**Security properties are tests, not prose.** The README used to say "no network egress." That was
prose — nothing in the suite forbade a socket. It's now an enforced allowlist with a single
permitted importer of `urllib.request` and a test that fails the build if a second appears. A
property in a README is documentation. A property with a failing build is a control.

**It refuses to claim what it cannot observe.** A `CLOSED` state exists in the model and is
*unreachable* — no event maps to it and the store refuses it. The system can confirm that this
result belongs to this order. Whether a clinician competent to act on an abnormal finding has read
it is a different claim, and nothing here observes it. Unreachable-by-construction beats
reachable-but-we-agreed-not-to.

## By the numbers

| | |
|---|---|
| Source | ~16,000 lines, Python |
| Tests | ~22,500 lines · **1,815 passing** |
| Coverage | 90% floor, 95% measured |
| Runtime dependencies | one (`cryptography`) |
| Standards | HL7 v2 · FHIR · SMART Backend Services · mutual TLS |

## Where to look first

Reviewing this in fifteen minutes? In order:

1. **[Security writeup](docs/security-model.md)** — the five trust boundaries, and a defect I
   found and published rather than quietly fixed.
2. **`src/referral_loop/core/states.py`** — the state model. Every state earns its place.
3. **`docs/superpowers/specs/`** — five design specs. Scope decomposed, rejected options recorded,
   defects logged with reasoning. If you want to know how I think, read these rather than the code.
4. **`tests/test_import_closure.py`** and **`tests/test_install_closure.py`** — the two tests that
   turn documentation into controls.

## Status

v1, not deployed to a live site. Built against synthetic HL7 traffic and a test FHIR server.
Known open defect, tracked in the spec rather than hidden: `SIU^S15` (appointment cancellation) is
currently mapped to the referral's `CANCELLED` state, which means a routine reschedule can drop a
clinically open referral off every worklist. Fix in progress.

## Running it

    pip install ".[worklist]"
    referral-loop listen --db data/referral_loops.db --peers peers.json

`listen` requires mutual TLS. `--allow-plaintext` runs locally without it: loopback only, one
identity, a warning on every start. See [full setup](README-technical.md).

---

*Built by David Streiler — medical scribe, ~3,600 hours across five EHRs.*
