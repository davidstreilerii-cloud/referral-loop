# Standards watch

Places where this system emits something non-standard **because the standard has no slot
for it**, not because a standard slot was inconvenient. Each entry names what we emit, so
that replacing it when a standard lands is a mapping exercise rather than an excavation.

An entry belongs here only if an external body could plausibly obsolete it. A local design
decision we could change unilaterally does not go here — it goes in the design spec.

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

## Not yet moved here

Design spec §14 holds other externally-dependent open questions — Epic's supported-hook
list and the MIPS 374 measure status among them — which belong in this file on the same
argument: they are bets on somebody else's roadmap, not decisions we own. Moving them is a
documentation task and was explicitly not part of Plan 2b Task 6, so §14 remains
authoritative for those until someone does it deliberately.
