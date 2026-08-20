# The appointment identifier, and the rebooking the content key eats

**Date:** 2026-08-05
**Status:** approved, not yet implemented
**Follows:** the `SIU^S15` un-schedule fix (`b702547`), which made this defect visible for the first time.

## The defect

`listener.content_key` hashes the rule-pack version, the message type, `ORC-1`, the placer
and filler order numbers, the MRN, the prior MRN, and the OBX tuples. It does not hash
anything naming the appointment.

A specialist's office rebooking a patient sends a second `SIU^S12` carrying the **same**
order numbers -- `ORC-2` and `ORC-3` identify the *order*, which has not changed -- and a
**new** `SCH`. Every field the content key reads is identical across the two messages, so
the rebooking hashes to the key the original already spent, `content_key_owner` returns the
first message's control id, and `handle()` returns at the duplicate check before `_apply`
is ever called.

The clinical consequence, on the sequence this is most likely to arrive as:

```
S12 books A55    ORC-2=P100 ORC-3=F200 SCH-1=A55   -> SCHEDULED
S15 cancels A55  ORC-2=P100 ORC-3=F200 SCH-1=A55   -> OPEN      (correct, since b702547)
S12 books A78    ORC-2=P100 ORC-3=F200 SCH-1=A78   -> swallowed
```

The loop finishes on `OPEN` while the patient holds an appointment. It reads as un-booked
and ages on a worklist as though nobody had scheduled it, which is the *inverse* of the
`S15` defect and the same class of harm: the referral's recorded state and the patient's
actual situation disagree, and no operator is told.

The gap is narrower than "reschedules are broken". A rebooking that mints a new filler
order number is already distinguished, because `filler_order_number` is in the key. This
is specifically the case where the order numbers are stable and only the slot moves --
which HL7 v2 scheduling practice suggests is the common one. This is an assumption about
realistic `SIU` traffic, not a measurement -- no production feed has been observed.

Pinned green today by `test_a_rebooking_that_names_no_new_appointment_is_eaten_by_content_dedup`,
which records the gap rather than endorsing it. That test inverts as part of this work.

## Why it was invisible until now

Before `b702547` an `SIU^S15` drove the loop to `CANCELLED`, which is terminal. No
subsequent `S12` could have moved it anywhere regardless of whether it was deduped, so the
collision had no observable consequence and no test could have caught it. Fixing the `S15`
routing is what created a state the swallowed rebooking could be wrong *about*.

Worth recording as a pattern: a defect can be masked by a second, worse defect downstream,
and fixing the worse one is what makes the first one reachable. Nothing about the dedup
code changed.

## The fix

`SCH` is already in `parse_hl7.ALLOWED_SEGMENTS` and is parsed on every scheduling message.
No `field_map` concept reads it, so the parsed segment is discarded. The single field that
distinguishes the two messages is the one field the system cannot see.

Five pieces:

1. **`scripts/sign_pack.py` -- fix `PACK_DIR`.** It points at
   `healthcare_rag/referral_loop/rules`, the pre-extraction path, which does not exist in
   this repository. `--sign` exits "No pack at ...". The pack has not needed re-signing
   since the extraction, so nothing surfaced it. This blocks every other piece and is
   fixed first, on its own commit.

2. **`pack.json` -- add `appointment_id: ["SCH-1", "SCH-2"]`,** bump the pack `version`,
   re-sign. Two candidates in preference order, matching how `placer_order_number` and
   `filler_order_number` already list theirs. `pack._load_pack` validates every field
   reference against `ALLOWED_SEGMENTS` before construction, so a typo fails at boot rather
   than silently degrading a match.

3. **`content_key` -- include the appointment identifier.** Read through
   `concept_value(message, pack, "appointment_id")`, alongside the placer and filler.
   `None` when absent, which is what a non-scheduling message and a sender that omits `SCH`
   both produce, and which leaves their keys structurally as they are today.

4. **`_CONTENT_KEY_VERSION` -> `rl-content-v2`.** The key's construction changed; a version
   that did not move would make two different hashing schemes indistinguishable in the
   stored rows.

5. **Invert the pinning test**, and add coverage for the sequence above end to end.

## Migration: dual-read, single-write

Bumping the version changes *every* content key, including those of messages already
applied. A redelivery under a fresh `MSH-10` of a message applied before the deploy would
hash to a `v2` key that matches no stored row, and would be applied a second time. `MSH-10`
dedup still catches exact redeliveries, so the exposure is bounded -- but it is not zero,
and for a result it means a duplicate `record_result`.

Reads consult both versions; writes only ever land in `v2`.

This is not a new pattern. `store._dedup_scopes` already does exactly this for the
peer-scope migration, and its comment argues the case in the same terms:

> Reads look at both; writes only ever land in the first. A row migrated from a database
> written before the transport had an identity still suppresses a redelivery of the message
> it recorded, so an upgrade cannot cause an already-applied message to be applied a second
> time.

The cost is one additional lookup per message for a bounded period. The `v1` read carries a
comment naming the condition under which it can be retired -- once no `v1` row remains
inside the redelivery window any peer is configured for -- so it does not become permanent
by default, which is the failure mode `migration.translate_to_legacy` is already being
watched for.

## Explicitly out of scope

**Verifying that an `S15` names the appointment the loop actually holds.** Reading `SCH`
makes this fixable for the first time: today `unschedule` un-books regardless of which
appointment the message names, so a late or duplicate `S15` for a superseded booking
un-books the *current* one -- the same shape as the defect `b702547` fixed, one level down.

Deferred deliberately. It needs the identifier recorded on the loop, which is a column and
a migration, and it is a behaviour change owed its own before/after rather than riding a
dedup commit. Recorded here so the decision is visible rather than forgotten.

## Testing

- The three-message sequence above: the rebooking applies, and the loop ends `SCHEDULED`.
- A genuine redelivery -- identical `SCH`, fresh `MSH-10` -- is still suppressed.
- A message carrying no `SCH` keys exactly as it does today, so non-scheduling traffic is
  provably unaffected.
- A `v1` row still suppresses a redelivery after the bump. This is the migration's whole
  claim and the one thing a version bump can silently get wrong.
- The pack loads and verifies after re-signing, and a malformed `appointment_id` reference
  is refused at boot.

Every new test is mutation-checked: break the line it targets, confirm *that* test fails,
and confirm it fails for its own reason rather than because a different mechanism refused
one step later. Six tests in the last plan passed for the wrong reason and review caught
none of them.

## Open, not blocking

`"the only anti-replay control in the system"`, asserted at `registry._refuse_if_stale` and
in two `test_registry_safety` docstrings, is arguably false -- content-key dedup is also an
anti-replay control, and this change strengthens it. The claim is defensible only if scoped
to *clinical ordering* rather than replay generally. It sits inside the H2 security
narrative and was flagged rather than rewritten unilaterally.
