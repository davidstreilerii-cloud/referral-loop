<!-- healthcare_rag/referral_loop/rules/CHANGELOG.md -->
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
