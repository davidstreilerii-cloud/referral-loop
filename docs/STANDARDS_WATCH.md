# Standards watch

Places where this system emits something non-standard **because the standard has no slot
for it**, not because a standard slot was inconvenient. Each entry names what we emit, so
that replacing it when a standard lands is a mapping exercise rather than an excavation.

Also here: standards this system **does not implement**, where not implementing them bounds
what the system may claim. Same test either way — an entry belongs here only if an external
body could plausibly obsolete it. A local design decision we could change unilaterally does
not go here — it goes in the design spec.

---

## Span citations on `Provenance.entity`

**Added:** 2026-08-03, Plan 2b Task 6.

**The gap.** `Provenance.entity` identifies *what* a transition drew on, but has no slot
for *where in it*. A referral matched from a document that named no order is supported by
a specific passage in that document, and the character offsets are the difference between
a citation a reviewer can check in one click and one they have to hunt for.

**What we emit.** `Evidence.spans` is a tuple of half-open `Span(start, end)` character
offsets into the artifact named by `Evidence.ref`. `Evidence.ref` is a content hash, a
resource reference or a rule id — never the content — so an offset is meaningless without
separately resolving the artifact, which is deliberate: it keeps clinical narrative out of
the Provenance resource and out of every downstream copy of the audit trail.

`fhir/provenance.py` currently emits `entity[].what.identifier.value = Evidence.ref` with
`role: source`, and **does not yet emit the spans at all**. When it does, it needs an
extension, because there is no conformant place to put them.

**What would replace it.** The HL7 AI Transparency work. If it ballots a way to cite a
region of a source artifact from a `Provenance.entity`, this extension is retired and the
mapping is `Span(start, end)` → whatever offset pair that work defines. Nothing else in
the system reads the spans, so the blast radius is this one projection.

**Watch:** HL7 AI Transparency / provenance-for-AI work items.

---

## IHE PCC 360X — cross-organization transport we do not speak

**Added:** 2026-08-26.

**The specification.** *IHE Patient Care Coordination Technical Framework Supplement — 360
Exchange Closed Loop Referral (360X)*, Rev. 1.2, published 2021-04-14, **Trial
Implementation** status. It is the US closed-loop referral profile: the thing a reader who
knows this domain will measure this project against.

**The gap.** 360X constrains the envelope, not just the content. §X.2.2: "The 360X Profile
requires the use of the ITI XDM Profile as the base transport mechanism", with XDR
(§X.2.2.1) and MHD (§X.2.2.2) as the only permitted substitutions. The US National
Extension is narrower still — §4.I.2.1: "The US National Extension requires the use of
Direct as the transport protocol for the XDM package for the messages in each
transaction... the Referral Initiator shall include the C-CDA content as an additional
S-MIME part."

A regex over the full extracted specification text returns **zero hits for `MLLP`**. MLLP
is what this system listens on. It is not a transport 360X names at any conformance level,
so a deployment of this software is not on the path a conformant referral takes between
organizations — which is the argument behind the boundary section in the README.

**What we do instead.** We read HL7 v2 from a local interface engine and match results to
orders inside one organization. 360X defines transactions **PCC-55 through PCC-61**; none
of them is implemented here. The message sets differ too: 360X carries `OMG^O19`,
`OSU^O51` and `SIU^S12/S13/S15/S26`, while this system ingests `REF`, `ORU`, `SIU` and
`ADT^A40`.

Note what is *not* the gap. 360X's payloads **are** HL7 v2.5.1 messages, wrapped in an XDM
package inside a Direct S/MIME message. The data model is the same one this project already
parses. What is missing is packaging and transport, and a reader who compresses that into
"HL7 v2 was the wrong choice" has read it backwards.

**Open question — UNCONFIRMED, do not cite this as fact.** 360X appears to assign a durable
cross-organization referral identifier carried across its transactions. *If* it does, a
conformant implementation closes a loop by **carrying that identifier** rather than by
matching a result to an order after the fact — which is exactly what `matcher.py` exists to
do. That would reframe deterministic matching as a workaround for environments where 360X
is not deployed, rather than as the general solution. This claim was tested and **did not
survive verification** (split 1-2); it is recorded here as a thing to settle against the
transaction definitions themselves, not as a finding. Settling it either way changes how
`matcher.py` should be described, not what it does.

**What would replace it.** ONC has in-progress work that could move the transport picture:
"360X on TEFCA" (November 2024) and "Package Structure and Transport" (February 2025). If
TEFCA-based exchange displaces the Direct-plus-XDM requirement, the transport this system
would need to speak changes, and this entry gets rewritten rather than retired — the
boundary stays until something on this list lands and is implemented.

**Watch:** IHE PCC 360X ballot status past Trial Implementation; ONC "360X on TEFCA" and
"Package Structure and Transport" work items.

---

## Not yet moved here

Design spec §14 holds other externally-dependent open questions — Epic's supported-hook
list and the MIPS 374 measure status among them — which belong in this file on the same
argument: they are bets on somebody else's roadmap, not decisions we own. Moving them is a
documentation task and was explicitly not part of Plan 2b Task 6, so §14 remains
authoritative for those until someone does it deliberately.
