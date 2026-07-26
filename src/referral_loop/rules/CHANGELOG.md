<!-- healthcare_rag/referral_loop/rules/CHANGELOG.md -->
# Rule pack changelog

Every entry records what changed and the eval delta that justified it. A pack
ships only if, replayed against the archived corpus, false-close rate does not
increase **and** precision improves (spec section 7).

## 1.0.0 — 2026-07-25

Initial pack. No eval delta: this is the baseline every later pack is measured
against. Staleness thresholds are defaults requiring explicit site acceptance
(see `REFERRAL_THRESHOLDS_ACCEPTED`, a later task) — shipping a threshold
silently would imply a clinical standard that is the site's call, not ours.
