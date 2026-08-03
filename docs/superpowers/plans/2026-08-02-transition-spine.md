# Transition & Provenance Spine Implementation Plan (Plan 2b)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it structurally impossible to change a referral's state without recording who asserted the change — and project that record to FHIR `Provenance` in which the system is never the verifier.

**Architecture:** A `Transition` value object carrying a required `assertion_source` becomes the only way state moves. `machine.apply()` is pure and refuses any transition into `RECONCILED` asserted by the system rather than a human. The store applies the state change and the event append in **one transaction**, so the event chain is gapless by construction. `fhir/provenance.py` projects each event; the absence of a human verifier agent is the machine-readable signal that a transition was inferred.

**Tech Stack:** Python 3.12, sqlite3, stdlib only for `core/`.

---

## Phases, and the checkpoint between them

**Phase 1 (Tasks 1–3) is additive**, like Plan 2a: `core/transitions.py` and `core/machine.py` are new files in the domain layer. `registry.py` and `store.py` stay untouched and the suite cannot move.

**Phase 2 (Tasks 4–7) changes behaviour.** It adds tables, routes `Registry` through the machine, and projects Provenance. This is where the suite will move, and every movement must be explained.

Stop at the end of Phase 1 and confirm the four protected files are still untouched before starting Phase 2. That boundary is the last safe point to discover the domain model is wrong.

## Baseline (verified 2026-08-02)

- `./.venv/Scripts/python.exe -m pytest tests/ -q` → **1165 passed, 13 skipped**
- Branch **`main`** only. An `interop` branch exists and is someone else's work — never switch to it, never `git add -A`.
- **Always use the repo venv.** `healthcare-rag` is pip-installed globally and shadows this package.
- `core/` currently holds `states.py` (11 `ReferralState`, 3 `ArtifactState`, `Hold`) and `models.py` (`Referral`, `InboundArtifact`, distinct `NewType` ids).
- Existing state machine: nine-member `LoopState`, twelve event types, legality enforced in `store.append_event` (choke point, refuses reserved and unknown types) plus per-method from-state frozensets in `Registry`.

## What this plan does not do

`REF^I13`/`I14` and `MDM^T02` ingest. Those are new message types, which is different work from reshaping the ones already handled — Plan 2c. Also not here: deleting `migration.py`. It goes when the last `Loop` consumer does, which is after Plan 2c.

## File structure

| file | responsibility |
|---|---|
| `src/referral_loop/core/transitions.py` | `Transition`, `AssertionSource`, `Evidence`, `EvidenceKind`, `Span`, `ActorRef` |
| `src/referral_loop/core/machine.py` | `apply()`, `LEGAL_TRANSITIONS`, `TransitionRejected` |
| `src/referral_loop/fhir/provenance.py` | `to_provenance(event, referral) -> dict` |
| `src/referral_loop/store.py` | *(Phase 2)* `transition_events` table, single-transaction `apply()` |
| `src/referral_loop/registry.py` | *(Phase 2)* routes through `machine.apply()` |

---

### Task 1: The Transition value object

**Files:**
- Create: `src/referral_loop/core/transitions.py`
- Create: `tests/test_transitions.py`

Design spec §8.1 is authoritative for the field list. Read it.

- [ ] **Step 1: Write the failing test**

```python
"""The object that makes 'who asserted this' unforgettable rather than merely required."""

from datetime import datetime, timezone

import pytest

from referral_loop.core.states import ReferralState
from referral_loop.core.transitions import (
    ActorRef, AssertionSource, Evidence, EvidenceKind, Span, Transition,
)

_NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _t(**kw):
    base = dict(
        to_state=ReferralState.DOCUMENTED,
        assertion_source=AssertionSource.RECEIVING_ORG,
        actor=ActorRef(kind="organization", id="example-lab"),
        evidence=(),
        occurred_at=_NOW,
        recorded_at=_NOW,
        hold=None,
        rationale=None,
    )
    base.update(kw)
    return Transition(**base)


def test_assertion_source_has_no_default():
    """The whole design rests on there being no way to move state without saying who said
    so. A default -- any default -- turns that from a guarantee into a convention, and the
    convention would be 'whatever the first caller happened to pass'."""
    with pytest.raises(TypeError):
        Transition(  # type: ignore[call-arg]
            to_state=ReferralState.SENT,
            actor=ActorRef(kind="device", id="referral-loop"),
            evidence=(),
            occurred_at=_NOW,
            recorded_at=_NOW,
            hold=None,
            rationale=None,
        )


def test_there_are_exactly_three_assertion_sources():
    assert {s.name for s in AssertionSource} == {"HUMAN", "RECEIVING_ORG", "SYSTEM_INFERRED"}


def test_a_transition_is_immutable():
    with pytest.raises(Exception):
        _t().assertion_source = AssertionSource.HUMAN


def test_occurred_at_and_recorded_at_are_separate():
    """Out-of-order arrival is detected on occurred_at; recorded_at stays monotonic per
    store, so the audit trail reads correctly when reality arrives backwards. Collapsing
    them loses the MSH-7 ordering guard."""
    t = _t(occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc), recorded_at=_NOW)
    assert t.occurred_at < t.recorded_at


def test_only_an_inferred_transition_may_carry_a_confidence():
    """A confidence on a human assertion is meaningless -- the human either asserted it or
    did not. Allowing it invites a caller to launder a match score into an assertion."""
    inferred = Evidence(kind=EvidenceKind.MATCH, ref="sha256:aa", spans=(), confidence=0.93)
    assert inferred.confidence == 0.93
    with pytest.raises(ValueError):
        Evidence(kind=EvidenceKind.USER_ACTION, ref="worklist", spans=(), confidence=0.93)


def test_a_span_points_into_a_source_document():
    s = Span(start=10, end=42)
    assert s.end > s.start
    with pytest.raises(ValueError):
        Span(start=42, end=10)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_transitions.py -v`
Expected: FAIL — `No module named 'referral_loop.core.transitions'`

- [ ] **Step 3: Write the module**

Per spec §8.1. Requirements the tests pin, plus:

- `AssertionSource` is a three-member enum with **no default anywhere** — not on the dataclass, not as a function parameter default, not in a factory.
- `Evidence` validates in `__post_init__`: a `confidence` is permitted only for `MATCH` and `FHIR_RESOURCE` kinds; `spans` is a tuple, never a list, so the frozen dataclass is immutable through its fields.
- `Span` validates `end > start`.
- Nothing here touches a clock. `occurred_at` and `recorded_at` are supplied by the caller — a `datetime.now()` default would make every transition's timing untestable and would silently paper over a missing `MSH-7`.

- [ ] **Step 4: Run it and watch it pass**

Expected: PASS

- [ ] **Step 5: Confirm `core/` closure still holds**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -k domain_core -v`
Expected: PASS. Add `referral_loop.core.transitions` to `_CORE_PROBE` so it is actually covered — a module the probe does not import is a module the closure test does not guard.

- [ ] **Step 6: Commit**

```bash
git add src/referral_loop/core/transitions.py tests/test_transitions.py tests/test_import_closure.py
git commit -m "feat(core): the Transition, and no default for who asserted it

assertion_source is required with no default anywhere -- not on the dataclass, not as
a parameter default, not in a factory. That absence is the enforcement: a default would
turn the guarantee into a convention, and the convention would be whatever the first
caller happened to pass.

Evidence refuses a confidence on anything but a match, so a caller cannot launder a
score into a human assertion."
```

---

### Task 2: The machine, and the guarantee

**Files:**
- Create: `src/referral_loop/core/machine.py`
- Create: `tests/test_machine.py`

This task implements design spec §6.3 — the replacement for "CLOSED is unreachable".

- [ ] **Step 1: Write the failing test**

The load-bearing tests:

```python
def test_the_system_cannot_reconcile_a_referral_on_its_own():
    """Spec section 6.3. This is the auto-close prohibition, moved from an unreachable
    enum member onto the transition, where it survives someone making the member
    reachable. An inferred completion that is wrong is a patient-safety event."""
    referral = _referral(state=ReferralState.DOCUMENTED)
    inferred = _t(to_state=ReferralState.RECONCILED,
                  assertion_source=AssertionSource.SYSTEM_INFERRED)
    with pytest.raises(TransitionRejected, match="reconcil"):
        apply(referral, inferred)


def test_a_human_may_reconcile_the_same_referral():
    referral = _referral(state=ReferralState.DOCUMENTED)
    human = _t(to_state=ReferralState.RECONCILED, assertion_source=AssertionSource.HUMAN)
    assert apply(referral, human).state is ReferralState.RECONCILED


def test_the_receiving_organisation_cannot_reconcile_either():
    """RECEIVING_ORG is not a human on this side of the exchange. A specialist's office
    asserting 'done' is evidence, not a coordinator's confirmation -- and the whole
    referral product exists because that assertion frequently never arrives at all."""
    referral = _referral(state=ReferralState.DOCUMENTED)
    org = _t(to_state=ReferralState.RECONCILED,
             assertion_source=AssertionSource.RECEIVING_ORG)
    with pytest.raises(TransitionRejected):
        apply(referral, org)


@pytest.mark.parametrize("state", list(ReferralState))
def test_reconciled_is_unreachable_from_every_state_without_a_human(state):
    """The state-space sweep, following the pattern of the existing
    test_registry_safety.py. A guarantee that holds from DOCUMENTED but not from
    SCHEDULED is not a guarantee."""
    referral = _referral(state=state)
    t = _t(to_state=ReferralState.RECONCILED,
           assertion_source=AssertionSource.SYSTEM_INFERRED)
    with pytest.raises(TransitionRejected):
        apply(referral, t)
```

Plus: every legal transition in `LEGAL_TRANSITIONS` is reachable; every illegal one raises; `apply` returns a new `Referral` and does not mutate the input; `seq` increments by exactly one.

- [ ] **Step 2: Run it and watch it fail**

Expected: FAIL — `No module named 'referral_loop.core.machine'`

- [ ] **Step 3: Write the module**

`LEGAL_TRANSITIONS: Mapping[ReferralState, frozenset[ReferralState]]` — the from-state table. Derive it from the lifecycle in spec §6.1; every state must appear as a key, including the three terminal ones mapping to an empty frozenset, so a reader can see terminality rather than infer it from absence.

`apply(referral, transition) -> Referral` is **pure**: no I/O, no clock, no logging. It raises `TransitionRejected` with a message naming the referral id and the attempted move — and **never** the patient identifier. A live finding in this codebase is that MRNs reach logs through exception messages; do not add another path.

- [ ] **Step 4: Run it and watch it pass**

Expected: PASS

- [ ] **Step 5: Prove the guarantee test discriminates**

Delete the `RECONCILED` guard, confirm the sweep goes red across every state, restore by writing the saved text back **with Python, not `git checkout`**. Paste both outputs.

- [ ] **Step 6: Commit**

---

### Task 3: Phase 1 checkpoint

**Files:** none.

- [ ] **Step 1: Full suite**

Expected: `1165 passed` plus Tasks 1–2 additions, `13 skipped`. No pre-existing test may have changed result.

- [ ] **Step 2: Prove Phase 1 was additive**

```bash
git diff --stat <phase-1-base-sha>..HEAD -- src/referral_loop/registry.py src/referral_loop/store.py src/referral_loop/listener.py src/referral_loop/matcher.py
```

Expected: **empty**. Confirm by comparing blob SHAs as well — an empty diff is also what a mistyped pathspec produces.

- [ ] **Step 3: Stop and report**

Report the domain model as built and wait for confirmation before Phase 2. This is the last point at which discovering the model is wrong costs only the domain layer.

---

### Task 4: The `transition_events` table

**Files:**
- Modify: `src/referral_loop/store.py`
- Create/modify: `tests/test_transition_store.py`

**Phase 2 begins. Behaviour changes from here.**

Design spec §9. The single transaction and `UNIQUE(referral_id, seq)` are the whole point.

- [ ] **Step 1: Write the failing tests**

The invariants from spec §9.3, as tests:

```python
def test_the_event_chain_is_gapless_for_every_referral():
    ...  # MAX(seq) == COUNT(*) per referral


def test_the_projected_state_equals_the_fold_of_its_own_events():
    ...  # replay each referral, compare to the projection row


def test_two_writers_racing_on_one_referral_do_not_both_win():
    """UNIQUE(referral_id, seq) is optimistic concurrency, not just an integrity
    constraint: two MLLP connections applying to the same referral, one loses the insert
    and retries. Without it the second silently overwrites the first's seq."""
    ...


def test_a_rejected_transition_leaves_no_event_and_no_state_change():
    """The state change and the append are one transaction. A machine rejection must
    roll back both, or the log grows entries for things that never happened."""
    ...
```

- [ ] **Step 2: Run and record RED**

- [ ] **Step 3: Implement**

Add the table with its append-only triggers, following the existing `_SCHEMA` structure exactly — this store already has ten such triggers and a per-connection authorizer denying UPDATE/DELETE on five tables; the new table joins both.

The migration must be **atomic**. `_widen_key` in this file was found emptying the PHI archive on a crash because its DDL ran outside a transaction; `_migrate` now wraps rebuilds in `BEGIN IMMEDIATE` with a rollback on `BaseException` and refuses a stranded `*__legacy` table at boot. Follow that pattern, and add a crash test at three kill points as `test_store.py` does.

- [ ] **Step 4: Green, then Step 5: Commit**

---

### Task 5: Route `Registry` through the machine

**Files:**
- Modify: `src/referral_loop/registry.py`
- Modify: `tests/test_registry_safety.py`

This is the largest and riskiest task in the plan. `registry.py` is 1,476 lines and its per-method from-state guards are what `test_registry_safety.py` currently proves.

- [ ] **Step 1: Establish what must not change**

Before writing anything, list the guarantees `test_registry_safety.py` currently proves — `CLOSED` unreachable, preliminary never acknowledgeable, corrected demotes, the `MSH-7` watermark, the `attached_from` exemption — and confirm each has a test. Any guarantee without one gets a test **first**, against the current implementation. You cannot preserve what you have not pinned.

- [ ] **Step 2: Route one method, prove nothing moved, commit. Then the next.**

Take `schedule` first — the simplest, and the one whose fail-open on an unstamped message was deliberately kept. One method per commit. After each: full suite, and the count must not move.

If any method's existing guard cannot be expressed as a `Transition`, **stop and report** — that is a finding about the domain model, not an obstacle.

- [ ] **Step 3: Delete the per-method from-state frozensets only once every method routes through the machine**

Two enforcement points that disagree is worse than either alone.

---

### Task 6: The Provenance projection

**Files:**
- Create: `src/referral_loop/fhir/provenance.py`
- Create: `tests/test_provenance.py`

Design spec §8.2.

- [ ] **Step 1: Write the failing test**

The load-bearing one:

```python
def test_an_inferred_transition_has_no_human_verifier_agent():
    """Spec section 8.2. The AI is never `verifier`, so the absence of a human verifier
    agent IS the machine-readable signal that a transition was inferred. This satisfies
    the requirement through conformant modelling rather than an invented extension --
    which is why it must be tested as a property of the output, not of our intent."""
    p = to_provenance(_event(source=AssertionSource.SYSTEM_INFERRED), _referral())
    agents = p["agent"]
    assert len(agents) == 1
    assert agents[0]["who"]["reference"].startswith("Device/")
    assert not any(a.get("type", {}).get("coding", [{}])[0].get("code") == "verifier"
                   for a in agents)


def test_a_human_transition_carries_a_practitioner_agent():
    ...


def test_a_receiving_org_transition_carries_an_informant_not_a_verifier():
    ...


def test_every_evidence_becomes_an_entity_with_role_source():
    ...
```

- [ ] **Step 2–4: RED, implement per §8.2, green**

The `Device` agent is always present and carries the system version, the rule-pack version, and the model identifier when a model was involved. Span citations need an extension — `Provenance.entity` has no native slot for character offsets. Record that in `docs/STANDARDS_WATCH.md` (create it) as a candidate to replace when the HL7 AI Transparency work ballots something.

- [ ] **Step 5: Commit**

---

### Task 7: Verify and report

- [ ] **Step 1: Full suite, and explain every movement**

Phase 2 changes behaviour, so the count will move. Every changed test must be explained as intended, not merely observed. A test that changed result without an explanation is a defect.

- [ ] **Step 2: Confirm the invariants hold on a populated database**

Build a store with a few hundred referrals via the wire, then assert the gapless chain and state-equals-fold over all of them. Invariants proven only on hand-built fixtures are proven only on hand-built fixtures.

- [ ] **Step 3: Confirm no MRN reaches a log or an exception message**

`test_spec_proofs.py` already greps four artifacts for identifiers. Extend it to the new exception and log paths.

- [ ] **Step 4: Report**

---

## Definition of done

- [ ] `assertion_source` is required with no default anywhere; every `Transition` construction site supplies one explicitly
- [ ] `apply()` refuses `RECONCILED` from `SYSTEM_INFERRED` **and** from `RECEIVING_ORG`, proven by a sweep over every state, and the sweep proven to fail when the guard is removed
- [ ] State change and event append are one transaction; a rejected transition leaves neither
- [ ] Gapless chain and state-equals-fold hold on a populated database, not just fixtures
- [ ] `Provenance` for an inferred transition has exactly one agent, a `Device`, with no `verifier`
- [ ] `registry.py`'s per-method from-state frozensets are gone, replaced by one enforcement point
- [ ] No MRN in any new log line or exception message
- [ ] Every test whose result changed is explained

## Coordination

`interop` is someone else's branch. Never switch to it, never `git add -A`, stage by name. `migration.py` stays until Plan 2c — the interop branch may be reading it, and deleting it here would break their build without warning.
