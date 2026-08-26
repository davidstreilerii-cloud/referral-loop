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

Built alongside that, and scoped honestly: a FHIR read client aimed at a further question — *does a
document exist on the other side that nobody sent us?* It is the only path here that produces value
without waiting for someone to push a message. Not without cooperation, though: it reads only from
endpoints that have already registered this client for SMART Backend Services and granted it scopes.
The client and its connector preflight are built and tested against a test FHIR server. Two modes
reach them today: `connectors` runs the preflight, and `documents` asks the question above for one
patient and prints what it found. Both are read-only — `documents` constructs no registry and opens
no loop database, so a search cannot move a loop, which is asserted by counting events either side
of the call rather than by reading the code. The job that would consume the search *automatically*,
attaching what it finds, is v2.

### The boundary this sits inside

Both ends of a tracked loop have to be reachable from one hospital's interface engine. That is a
real limit, not a deployment detail. The US closed-loop referral standard — IHE PCC 360X, and its
US National Extension — specifies Direct Secure Messaging carrying an XDM package with C-CDA
content as the way a referral crosses an organizational boundary. This system speaks none of that.
It reads HL7 v2 from a local interface engine, which 360X does not list as a transport at any
conformance level.

Worth being precise about what that does and does not mean. 360X's payloads *are* HL7 v2.5.1
messages — the gap is transport and packaging, not the data model. And the loops most likely to
break are the ones inside a single organization, where the order and the result already share an
interface engine and nobody is watching the gap between them. That is the population this tracks.

Crossing organizations is a different system, and an honest reading of the standard says so.
[`docs/STANDARDS_WATCH.md`](docs/STANDARDS_WATCH.md) records the specifics and what would change it.

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
| Source | ~17,000 lines, Python |
| Tests | ~24,500 lines · **over 1,900 passing** |
| Coverage | 90% floor enforced in CI, mid-90s measured |
| Runtime dependencies | one (`cryptography`) |
| Standards | HL7 v2 · FHIR · SMART Backend Services · mutual TLS |

## Where to look first

Reviewing this in fifteen minutes? In order:

1. **[Security writeup](docs/security-model.md)** — the five trust boundaries, and a defect I
   published before I had fixed it, plus the second one that fixing it exposed.
2. **`src/referral_loop/core/states.py`** — the state model. Every state earns its place.
3. **`docs/superpowers/specs/`** — five design specs. Scope decomposed, rejected options recorded,
   defects logged with reasoning. If you want to know how I think, read these rather than the code.
4. **`tests/test_import_closure.py`** and **`tests/test_install_closure.py`** — the two tests that
   turn documentation into controls.

## Status

v1, not deployed to a live site. Built against synthetic HL7 traffic and a test FHIR server.

The defect this section used to advertise as open is closed, and how it closed is the part worth
reading. `SIU^S15` (appointment cancellation) was mapped to the referral's `CANCELLED` state, so a
routine reschedule dropped a clinically open referral off every worklist. It now routes to
`unschedule`, which the store maps to `OPEN` — the loop stays on the queue.

Fixing it then **exposed a second defect the first had been masking.** With `CANCELLED` terminal,
nothing after an `S15` could be wrong about anything, so nobody could see that content-key dedup
was swallowing the rebooking: a specialist's office re-books under the *same* order numbers, every
field the key hashed was identical, and the second `SIU^S12` was discarded as a duplicate. The loop
then sat on `OPEN` while the patient held an appointment — the inverse error, same class of harm.
That one is fixed too; the key now hashes the appointment identifier. Both are written up in
`docs/superpowers/specs/`, reasoning and all. Found it, fixed it, the fix uncovered a second one,
fixed that too.

## Running it

    pip install ".[worklist]"
    referral-loop listen --db data/referral_loops.db --peers peers.json

`listen` requires mutual TLS. `--allow-plaintext` runs locally without it: loopback only, one
identity, a warning on every start. See [full setup](README-technical.md).

---

*Built by David Streiler — medical scribe, ~3,600 hours across five EHRs.*
