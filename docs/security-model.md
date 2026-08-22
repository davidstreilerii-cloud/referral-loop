# Five Trust Boundaries in a Clinical Message Pipeline

*A security writeup for `referral-loop` — an inbound referral loop closure system over HL7 v2.*

---

## The problem this system exists to prevent

A physician refers a patient to a specialist. The order goes out. Somewhere between the order and
the consult note coming back, the loop breaks — and nobody notices, because a referral that
silently fails looks exactly like a referral that is still in progress.

`referral-loop` tracks a referral from order to returned documentation and surfaces the ones that
never came back. It ingests HL7 v2 over MLLP, matches inbound results to open orders, and replays
loop state from an append-only event log. A coordinator sees three queues: awaiting a result,
awaiting acknowledgement, and orphans — results that matched no order.

It is deterministic. There are no model calls, no ML stack, and no model client. That is not a
design preference I am asking you to take on faith; `tests/test_install_closure.py` asserts it
against the **built container image**, not against the Dockerfile. A Dockerfile is a claim about
an image. The image is the artifact that runs.

What follows is the security model, organized as the five boundaries a message crosses.

---

## Boundary 1 — The wire

**mTLS is required.** `listen` will not start without it. But the interesting decision is not that
peers present certificates; it is how a certificate is turned into an identity.

A peer is identified by the **SHA-256 of its certificate's DER encoding**. Not by subject DN.

This matters more than it sounds. A subject DN is chosen by whoever asks the CA, and it survives
renewal. Pin the DN and you have not authenticated a peer — you have authenticated *any
certificate that CA can be talked into signing with that name*. In a health system, the CA is
frequently an internal PKI with a request process, which is to say: a social engineering surface.
The thumbprint pins the key, and rotation is an explicit registry edit rather than something that
happens to you.

TLS material and the peer map live in **one file**, because "which CA may sign a client
certificate" and "which certificate is which peer" are two halves of one decision. Splitting them
across two config files is how you end up with a trusted CA and a stale peer list.

`--allow-plaintext` exists for local development: loopback only, one identity, and a warning on
every start. A file-based registry can opt out the same way, but only by saying so explicitly
*and* naming the source. Opting out of transport security is never a default and never silent.

## Boundary 2 — The parser

The HL7 v2 parser works from a **segment allowlist**: `MSH, PID, MRG, PV1, ORC, OBR, OBX, SCH,
RF1`. Everything else is dropped at the door.

The MLLP layer refuses bodies containing an embedded `FS` — the frame terminator — because a
message that can smuggle its own terminator can desynchronize the stream and cause the next
message to be read as a continuation of this one. `MSH-10`, the control ID, is sanitized to
`[A-Za-z0-9._-]{1,20}` because it becomes a dedup key, and a dedup key that accepts arbitrary
bytes is a collision primitive.

The general principle: a clinical interface engine is an *inbound* surface fed by systems you do
not control and cannot patch. Parse defensively or do not parse at all.

## Boundary 3 — The matcher

Attaching a result to an order is the highest-consequence decision the system makes, and it is
the one people evaluate wrongly.

The matcher is tiered, with a confidence floor. But the important part is the **release gate
metric: false-match rate, not accuracy.**

Accuracy is the wrong instrument here because the two error directions have wildly asymmetric
cost. A missed match leaves a loop open — a coordinator sees it in a queue and works it. A false
match attaches *another patient's result* to this referral and marks the loop closed. The first
error is visible and self-correcting. The second is invisible and terminal: the loop leaves every
queue, and the wrong document is now in the record. An accuracy number averages these together
and will happily trade one for the other.

`eval.py` is an archive-replay harness that measures the rate that actually matters.

## Boundary 4 — Persistence

The state change and the event append are **one transaction**. Not two writes with hope in
between. The guarantee is that no intermediate state ever becomes visible — which is precisely
the kind of property no ordinary test looks for, because ordinary tests assert that something
exists rather than that something never existed. The way to test it is to make the second write
fail and assert the first went with it.

The audit log is append-only, enforced by a **SQLite authorizer** rather than by convention. The
distinction is whether "append-only" is a property of the storage layer or a property of every
future caller remembering.

The rule pack is **Ed25519-signed and verified before load**. Matching rules are exactly the sort
of thing that gets hot-edited at 2am during an integration issue, which is exactly why they are
signed.

PHI has a boot gate: under `PHI_MODE=full` — the documented operating mode — the process refuses
to start unless disk encryption is verified, either by OS detection or explicit attestation. Under
`deidentified` it warns and continues; under `disabled` the gate does not run. The refusal is
unconditional only in the mode that persists identifiers.

Detection is deliberately narrow. It asks which volume actually backs the database path, and
requires *that* device to be encrypted — a BitLocker-protected `C:` does not vouch for a database
on `D:`, and an encrypted swap partition elsewhere on the host vouches for nothing. Anything it
cannot positively identify is treated as unencrypted, so LVM-on-LUKS reports `lvm` and does not
auto-detect even though it is genuinely encrypted. Walking the device-mapper tree upward to infer
otherwise is exactly the reasoning that produces a confident wrong yes. Those sites attest with
`PHI_ENCRYPTION_VERIFIED=1`, which is also the practical path on Windows, where `manage-bde` is
often slower than the probe timeout.

## Boundary 5 — Egress

This one changed, and how it changed is the point.

Until an interoperability layer existed, the README said "no network egress." That was **prose**.
Nothing in the test suite forbade a socket. It was true on the day it was written and would have
quietly stopped being true the first time anyone needed to fetch something.

What replaced it is narrower and actually enforced:

1. The only outbound destinations are the ones named in the connector file. Every URL is
   normalized to `(scheme, host, port)` and checked against the registry.
2. `connect/egress.py` is the **one module permitted to import `urllib.request`**, and it is the
   sole call site that opens a connection — because the allowlist check runs *inside* the fetch,
   where it cannot be bypassed by a second caller.
3. `tests/test_import_closure.py` fails the build if a second importer appears.

A deployment that configures no connectors makes no outbound connections at all, and the `listen`
and `filedrop` modes never import the package.

The lesson generalizes: **a security property stated in a README is documentation; a security
property with a test that fails the build is a control.** The difference is what happens six
months later when someone reasonable needs to add a feature.

---

## What v1 deliberately does not claim

`CLOSED` is defined in the state model but is **never entered**. No event type maps to it, and
`LoopStore.append_event` refuses any attempt to reach it. `ACKNOWLEDGED` is the terminal state.

The reasoning: a coordinator can reliably confirm that *this result belongs to this order*. That
is an observable fact and the system observes it. Whether a clinician competent to act on an
abnormal finding has actually read it is a **different claim**, and nothing in v1 observes it. A
system that marked such loops `CLOSED` would be asserting something it cannot see, and the
assertion would be load-bearing for patient safety.

Unreachable-by-construction is better than reachable-but-we-agreed-not-to.

## A defect worth publishing, and the one it was hiding

During routing work, a defect surfaced that illustrates why this domain rewards paranoia.

`SIU^S15` is an **appointment** cancellation. The system mapped it to `CANCELLED`, the referral's
withdrawn state. But a cancelled appointment is not a withdrawn referral — the patient still needs
to be seen and the referral still needs rebooking.

`CANCELLED` appears in neither the awaiting-result queue nor the awaiting-acknowledgement queue.
So on the entirely benign happy path — a specialist's office cancels and reschedules, which
happens daily — **a clinically open referral silently left every coordinator queue.** That is
the precise failure the product exists to prevent, occurring by design rather than by attack.

The part I find most instructive: a security audit had already reached this same fact from the
other direction. It listed `SIU^S15` mass-cancellation as an exploit *because* `CANCELLED` is on
no worklist — framing it as something an adversary does. A routine scheduling message did it too,
every day, with no adversary at all.

Threat modeling found the mechanism and mislabeled the trigger. The most likely cause of a
security-relevant outcome is usually not an attacker. It is Tuesday.

**It is fixed.** `listener._apply_unschedule` drives `registry.unschedule` from `S15`, never
`registry.cancel`; the store maps `unscheduled` to `OPEN`, so the loop stays on the awaiting-result
queue and a reschedule is a visible pending referral rather than a terminal one.
`tests/test_listener.py::test_an_s15_then_an_s12_reschedules_through_the_listener` holds that
end to end. `CANCELLED` is now reachable only from an actual withdrawal by the referring side.

**And fixing it uncovered a second defect that the first one had been masking.** With `CANCELLED`
terminal, nothing arriving after an `S15` could be wrong about anything — so nothing could reveal
that the content-key dedup was eating the rebooking. A specialist's office re-books under the
*same* placer and filler order numbers, because the order did not change; only the slot did. Every
field `content_key` hashed was byte-identical across the two `SIU^S12`s, so the second one hashed
to a key the first had already spent and was discarded at the duplicate check before it was ever
applied. The loop finished on `OPEN` while the patient held an appointment: the inverse of the
`S15` error and the same class of harm, the recorded state and the patient's actual situation
disagreeing with nobody told.

That is fixed too — `content_key` now hashes an `appointment_id` concept read from `SCH`, under a
bumped key version with a dual-read migration so a pre-bump row still suppresses a redelivery.
The design is in `docs/superpowers/specs/2026-08-05-appointment-identifier-dedup-design.md`.

The generalizable lesson is the sequencing, not either defect: **a defect can be masked by a worse
defect downstream, and fixing the worse one is what makes the first one reachable.** Nothing about
the dedup code changed. It became wrong because something else stopped being wrong.

---

## Summary

| Boundary | Control | Enforced by |
|---|---|---|
| Wire | mTLS; peer = certificate SHA-256, not subject DN | Startup refusal; registry |
| Parser | Segment allowlist; `FS` rejection; `MSH-10` sanitization | Parser, at the door |
| Matcher | Confidence floor; gate on false-match rate | `eval.py` replay harness |
| Persistence | Single transaction; SQLite-authorizer append-only; signed rules | Storage layer |
| Egress | Destination allowlist; single permitted importer | `test_import_closure.py` (build fails) |

Authorities are granted, never inferred. Exactly three exist — `merge` (`ADT^A40`), `cancel`
(`SIU^S15`), and `result` (`ORU^R01` with `OBX-11 = F`) — selected by one criterion: **each can
end with a clinically open loop on nobody's queue.** Orders and schedules are strictly additive
and carry no authority, so they need none.

~17,000 lines of source, ~24,500 lines of tests, 1,889 passing, 90% coverage floor against a
measured 96%. One runtime dependency.
