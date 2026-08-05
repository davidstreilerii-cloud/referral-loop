<!-- src/referral_loop/rules/CHANGELOG.md -->
# Rule pack changelog

Every entry records what changed and the eval delta that justified it. Replayed
against the archived corpus, a pack ships only if (spec section 7):

1. **False-match rate does not regress** — absolute veto, whatever else improved.
2. **Precision does not regress.**
3. **At least one target metric improves** — precision, recall, auto-match rate,
   orphan rate, or dismissal rate.

Condition 3 was formerly "precision improves", which blocked a pack that only
halved the orphan rate: a pure workload win with no safety cost, vetoed by the
one metric it did not move.

*The safety metric was renamed from **false-close rate** in spec `78abc7c`.
Nothing closes in v1 — `CLOSED` is v2 — so a metric named for closure described
something the system does not do. Same definition, same veto.*

## 1.2.0 — 2026-08-05

Added `appointment_id: ["SCH-1", "SCH-2"]` to `field_map`.

`SCH` is parsed on every scheduling message and then discarded, because no
concept read it. A specialist's office rebooking a patient sends a second
`SIU^S12` carrying the same `ORC-2`/`ORC-3` — the *order* has not changed — and a
new `SCH`. What tells the two messages apart is the appointment identifier, and
until now nothing could read it. Two candidates in preference order for the same
reason `placer_order_number` lists two: which field a site's scheduler populates
depends on its interface engine, not on HL7.

**This revision improves no metric the eval harness measures, and the release
gate blocks it.** Recorded rather than worked around. Replayed against the
shipped synthetic corpus with 1.1.0 as `--baseline-pack-dir`, both packs score
identically and `gate_pack_release` returns *"BLOCKED: no target metric
improved"* — correctly, on the numbers it has. Nothing reads `appointment_id`
yet, so a field map that gains it cannot move a match metric; and what it will
move once read is a content-key collision that swallows a rebooking, which is
not a false match, an orphan or a dismissal and so is not in the harness's
vocabulary at all. Section 7's conditions 1 and 2 — the two vetoes — hold: false-
match rate and precision both unchanged at 0.0000 and 1.0000.

Reproduce with `referral-loop eval --pack-dir src/referral_loop/rules
--baseline-pack-dir <1.1.0> --synthetic-only`:

```
  cases                13   matchable 7
  false-match rate 0.0000   (0)
  precision        1.0000   (6/6)
  recall           0.8571   (6/7)
  auto-match rate  0.8571   (floor is pack data)
  orphan rate      0.5385   (7)
  dismissal rate   0.0000   (no site labels)
```

Those are the numbers for *both* packs, and they are not the ones recorded below
for 1.1.0 on 2026-07-26: the synthetic corpus has grown since, so matchable went
6 -> 7 and orphans 8 -> 7 with no pack change at all. The section below is a
measurement of a corpus that no longer exists, which is worth knowing before
comparing a future revision against its numbers rather than re-measuring the
pack it replaces.

## Baseline measurement for 1.1.0 — 2026-07-26

No pack change. The eval harness the gate depends on now exists, so 1.1.0's
numbers against the shipped synthetic corpus are recorded here — every later
revision is measured against these, and a revision with no delta to show against
them does not ship.

Reproduce with `referral-loop eval --pack-dir src/referral_loop/rules
--synthetic-only` (repathed and renamed after the extraction; it no longer
reproduces the numbers below, see 1.2.0):

```
  cases                13   matchable 6
  false-match rate 0.0000   (0)
  precision        1.0000   (5/5)
  recall           0.8333   (5/6)
  auto-match rate  0.8333   (pack floor 0.5000)
  orphan rate      0.6154   (8)
  dismissal rate   0.0000   (no site labels)
```

Criterion 4 is met: false-match rate zero **and** auto-match rate above the
pack's minimum. The one matchable case 1.1.0 does not resolve is the tier-4
modality-equivalence pair, declined because tier 4's confidence (0.70) sits below
the floor (0.90). That is deliberate headroom, not a defect — a corpus every pack
already satisfies leaves condition 3 unsatisfiable and the gate becomes a
formality.

The dismissal rate is site-derived and reads 0.0000 here only because the shipped
corpus carries no coordinator labels. Within one gate run it is identical on both
sides of a comparison and cannot on its own justify a release; it moves between
releases, not during one.

## 1.1.0 — 2026-07-26

Added `field_map` and `min_auto_match_rate`.

`field_map` moves the HL7 field placement for each matched concept
(`placer_order_number`, `filler_order_number`, `service_code`, `modality`,
`ordering_provider`, `mrn`) out of code and into the signed pack. Tier
*logic* (an exact accession match is strong evidence everywhere) is stable
across sites; field *placement* (accession in `OBR-3` at one site,
`OBR-18`/`OBR-19` or `ORC-3` at another) is not — it depends on the site's
RIS and how its interface engine was built, sometimes a decade ago. Onboarding
a new site's field layout is now a signed pack revision evaluated by
replaying that site's own archive, not a code change requiring a release and
a security review.

`min_auto_match_rate` exists because a zero false-match rate is trivially
satisfied by a matcher that attaches nothing — every result orphans, the
safety number reads 0.000, and the gate goes green while doing no work.
Starting it at 0.5 is deliberate and low; raising it is a decision backed by
replay evidence against the eval harness, never an aspiration set at the
outset.

No eval delta for this revision: the eval harness that section 7's release
gate depends on does not exist yet (Task 14), so the gate cannot be applied
to a pack that predates it. Recording that honestly here rather than
implying a replay happened.

## 1.0.0 — 2026-07-25

Initial pack. No eval delta: this is the baseline every later pack is measured
against. Staleness thresholds are defaults requiring explicit site acceptance
(see `REFERRAL_THRESHOLDS_ACCEPTED`, a later task) — shipping a threshold
silently would imply a clinical standard that is the site's call, not ours.
