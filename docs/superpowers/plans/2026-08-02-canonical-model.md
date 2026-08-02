# Canonical Model Implementation Plan (Plan 2a)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the canonical domain model — eleven referral states, a separate `InboundArtifact` aggregate, and a total projection to FHIR R4 `Task.status` — as a stable interface an interoperability branch can fork from.

**Architecture:** Purely **additive**. A new `core/` package holds the model and states and imports nothing from `store/`, `fhir/`, or `ingest/`. A new `fhir/` package holds the projection. A temporary top-level `migration.py` maps the existing `Loop`/`LoopState` vocabulary onto the canonical one and proves the two equivalent. `registry.py` and `store.py` are **not touched** — the existing nine-state machine keeps running exactly as it does today.

**Tech Stack:** Python 3.12, stdlib only for `core/` (dataclasses, enum, datetime). No new dependencies.

---

## Why this plan is additive, and why that matters

Plan 2b migrates the internals onto this model: `Transition` objects, `machine.apply()`, the single-transaction store write, `Provenance` projection, and the ingest remapping. All of that touches `registry.py` (1,476 lines) and `store.py` (2,163 lines) — the two largest files in the repo.

An interoperability branch (slices 3 and 4: FHIR data layer, CDS Hooks, MCP, SMART) forks **between 2a and 2b** and builds only in new directories. If it forked before 2a it would write adapters against `LoopState` and rewrite them all at merge; if it waited for 2b it would be blocked for the duration of the larger plan.

So the constraint on this plan is unusual and strict: **it must not change behaviour at all.** The suite must read exactly `1048 passed` plus the tests added here. If a single existing test changes result, something is wrong — there is no legitimate reason for adding a module to alter a running state machine.

## Baseline (verified 2026-08-01)

- `./.venv/Scripts/python.exe -m pytest tests/ -q` → **1048 passed, 13 skipped**
- **Always use the repo venv.** `healthcare-rag` is pip-installed globally and still contains a `healthcare_rag/referral_loop/` package; global python lets a stale import resolve against the parent's tree.
- Existing `LoopState` (`src/referral_loop/events.py:9-61`), nine members: `OPEN`, `SCHEDULED`, `RESULTED`, `ACKNOWLEDGED`, `CLOSED`, `CANCELLED`, `ORPHAN`, `DISMISSED`, `ATTACHED`
- Twelve event types written to `loop_events`: `created`, `scheduled`, `resulted`, `acknowledged`, `cancelled`, `orphaned`, `reopened`, `reversed`, `dismissed`, `attached`, `unmatched`, `merged_in`

## File structure

| file | responsibility | imports |
|---|---|---|
| `src/referral_loop/core/__init__.py` | package marker; no re-exports | — |
| `src/referral_loop/core/states.py` | `ReferralState`, `ArtifactState`, `Hold` | stdlib only |
| `src/referral_loop/core/models.py` | `Referral`, `InboundArtifact`, `PartyRef`, `PatientRef` | `.states`, stdlib |
| `src/referral_loop/fhir/__init__.py` | package marker | — |
| `src/referral_loop/fhir/codesystems.py` | the `businessStatus` CodeSystem we publish | stdlib only |
| `src/referral_loop/fhir/task_status.py` | total fn `ReferralState → (status, businessStatus)` | `..core.states`, `.codesystems` |
| `src/referral_loop/migration.py` | **temporary.** `Loop ↔ Referral`, `LoopState → ReferralState \| ArtifactState`. Deleted by Plan 2b. | `.core`, `.events` |
| `tests/test_canonical_states.py` | state enums, hold, exhaustiveness | |
| `tests/test_fhir_task_status.py` | projection totality and the collapse cases | |
| `tests/test_migration.py` | every `LoopState` maps; round-trip equivalence | |

`core/` importing nothing from the rest of the package is the architectural claim of the whole spec (§4). Task 1 makes it a test.

---

### Task 1: Create `core/` and prove its import closure

**Files:**
- Create: `src/referral_loop/core/__init__.py`
- Modify: `tests/test_import_closure.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_import_closure.py`:

```python
_CORE_PROBE = (
    "import referral_loop.core, referral_loop.core.states, referral_loop.core.models;"
    "import json,sys; print(json.dumps(sorted(sys.modules)))"
)

# core/ is the domain layer. The whole layering argument in the design spec rests on it
# taking plain objects and returning plain objects, so that the same rubric is callable
# from an MLLP listener, a CDS Hooks service and a batch job without duplication. A single
# convenience import of the store is all it takes to lose that, and it would be invisible
# in review.
CORE_FORBIDDEN = (
    "referral_loop.store",
    "referral_loop.registry",
    "referral_loop.listener",
    "referral_loop.mllp",
    "referral_loop.mllp_server",
    "referral_loop.matcher",
    "referral_loop.worklist",
    "referral_loop.audit",
    "referral_loop.pack",
    "referral_loop.fhir",
    "referral_loop.migration",
    "sqlite3",
    "flask",
    "jinja2",
    "cryptography",
)


def test_the_domain_core_imports_no_protocol_persistence_or_projection_code():
    loaded = _modules_in_a_clean_interpreter(_CORE_PROBE)
    leaked = [m for m in loaded if any(m == f or m.startswith(f + ".") for f in CORE_FORBIDDEN)]
    assert not leaked, f"core/ reached outside the domain layer: {leaked}"
```

Reuse whatever clean-subprocess helper the file already has (it runs `sys.executable -c` and parses JSON — an in-process `sys.modules` snapshot passes vacuously and the existing module docstring says so). If the helper is inline rather than named, extract it; do not write a second one.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -k domain_core -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'referral_loop.core'`

- [ ] **Step 3: Create the package**

`src/referral_loop/core/__init__.py`:

```python
"""The domain layer.

Nothing in here imports the store, the transport, the matcher, or the FHIR projection.
That is the property the whole layering rests on: the same model has to be reachable from
an MLLP listener, a CDS Hooks service and a batch job, and it stops being reachable the
moment it needs a database connection to be constructed.

Enforced by tests/test_import_closure.py, in a clean subprocess -- an in-process check
would pass vacuously once anything else in the suite had already imported the store.
"""
```

Leave it at that. No re-exports: a `from .models import *` here would make `core` and `core.models` two names for one thing and the closure test would still pass, which is how these packages start growing sideways.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -k domain_core -v`
Expected: FAIL still — `No module named 'referral_loop.core.states'`. That is correct; Task 2 creates it. Note the failure and proceed. Do not stub the modules to make the test pass early.

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add src/referral_loop/core/__init__.py tests/test_import_closure.py
git commit -m "feat(core): the domain layer, and the test that keeps it a domain layer

The closure test is written before the modules it guards, so it fails until Task 2
lands them -- deliberately. A layering rule added after the code it constrains is a
rule the code has already had a chance to break."
```

---

### Task 2: The state vocabularies

**Files:**
- Create: `src/referral_loop/core/states.py`
- Create: `tests/test_canonical_states.py`

- [ ] **Step 1: Write the failing test**

`tests/test_canonical_states.py`:

```python
"""The two state vocabularies, and the properties that make them two rather than one."""

import pytest

from referral_loop.core.states import ArtifactState, Hold, ReferralState


def test_the_referral_lifecycle_has_eleven_states():
    assert len(ReferralState) == 11


def test_the_artifact_lifecycle_has_three():
    assert len(ArtifactState) == 3


def test_no_state_name_appears_in_both_vocabularies():
    """The split exists because an inbound artifact that matched nothing is not a referral
    in a funny state. A name in both would let a future reader treat them as one enum
    again, which is exactly the conflation this removes."""
    assert not ({s.name for s in ReferralState} & {s.name for s in ArtifactState})


@pytest.mark.parametrize("state", list(ReferralState))
def test_every_referral_state_is_its_own_value(state):
    assert state.value == state.name.lower().replace("_", "-")


def test_hold_is_not_a_state():
    """A referral held from ACCEPTED and one held from SCHEDULED are operationally
    different, and the aging thresholds escalate on that difference. Collapsing both into
    an ON_HOLD member would destroy it."""
    assert not hasattr(ReferralState, "ON_HOLD")
    assert not hasattr(ReferralState, "HOLD")


def test_a_hold_records_who_applied_it_and_why():
    h = Hold(reason="awaiting patient callback", actor="coordinator-b")
    assert h.reason and h.actor
    with pytest.raises(Exception):
        h.reason = "changed"  # frozen
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_canonical_states.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'referral_loop.core.states'`

- [ ] **Step 3: Write the module**

`src/referral_loop/core/states.py`:

```python
"""The referral lifecycle, and the separate lifecycle of an artifact that matched nothing.

The existing LoopState in events.py has nine members and conflates the two: ORPHAN,
DISMISSED and ATTACHED are not states a referral can be in, they are the life of an
inbound document that found no referral to attach to. Design spec section 6.5 records
why they are split here and what it buys.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReferralState(str, Enum):
    """Ordered by the path a referral normally walks, exits last.

    Deliberately absent: a CLOSED member. The existing machine reserves one and refuses
    it at the store; the guarantee that replaces it lives on the transition, not the
    state -- a move into RECONCILED whose assertion source is the system rather than a
    human is refused. That is a stronger rule than an unreachable enum member, because it
    survives someone making the member reachable.
    """

    DRAFT = "draft"
    SENT = "sent"
    RECEIVED = "received"
    ACCEPTED = "accepted"
    SCHEDULED = "scheduled"
    SEEN = "seen"
    DOCUMENTED = "documented"
    RECONCILED = "reconciled"

    DECLINED = "declined"
    CANCELLED = "cancelled"
    AGED_OUT = "aged-out"


class ArtifactState(str, Enum):
    """An inbound document, and whether anyone has decided what it belongs to.

    Both exits are terminal. There is no route back to UNMATCHED: re-opening a decision
    means a new artifact record referencing the same content hash, so the coordinator's
    original judgement stays in the log rather than being overwritten.
    """

    UNMATCHED = "unmatched"
    ATTACHED = "attached"
    DISMISSED = "dismissed"


@dataclass(frozen=True)
class Hold:
    """Suspension, carried alongside the state rather than replacing it.

    FHIR's Task.status has an on-hold value, which is where this projects -- but
    projecting to it discards which state the referral was held *from*, and that is the
    distinction the aging agent escalates on ("accepted but never scheduled" is not
    "scheduled but never seen"). So the underlying state is preserved here and the
    projection reconstructs both halves.
    """

    reason: str
    actor: str
```

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_canonical_states.py -v`
Expected: PASS, 15 tests (11 parametrised + 4)

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add src/referral_loop/core/states.py tests/test_canonical_states.py
git commit -m "feat(core): eleven referral states and three artifact states

Split per design spec 6.5. ORPHAN/DISMISSED/ATTACHED were never referral states --
they are the life of an inbound document that matched nothing, and keeping them in one
enum is why the matcher's exact-tier set had to include ATTACHED and why attach_orphan
needed an exemption from the ordering guard.

No CLOSED member. The guarantee it stood for moves onto the transition in Plan 2b,
where it survives someone making the member reachable."
```

---

### Task 3: The two aggregates

**Files:**
- Create: `src/referral_loop/core/models.py`
- Modify: `tests/test_canonical_states.py` (rename to cover models too, or add `tests/test_canonical_models.py`)

- [ ] **Step 1: Write the failing test**

`tests/test_canonical_models.py`:

```python
from datetime import datetime, timezone

import pytest

from referral_loop.core.models import ArtifactKind, InboundArtifact, PartyRef, PatientRef, Referral
from referral_loop.core.states import ArtifactState, Hold, ReferralState


def _ref() -> Referral:
    return Referral(
        id="R-0001",
        patient=PatientRef(mrn="MRN1"),
        sending_org=PartyRef(id="example-ris", name="Example RIS"),
        receiving_org=None,
        referring_provider=None,
        specialty="cardiology",
        reason=None,
        service_request_id=None,
        state=ReferralState.SENT,
        hold=None,
        state_occurred_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        seq=1,
    )


def test_a_referral_is_immutable():
    with pytest.raises(Exception):
        _ref().state = ReferralState.RECONCILED


def test_a_referral_may_be_held_without_losing_the_state_it_was_held_from():
    held = _ref().with_hold(Hold(reason="patient unreachable", actor="coordinator-b"))
    assert held.hold is not None
    assert held.state is ReferralState.SENT, "the underlying state must survive a hold"


def test_an_artifact_may_have_no_patient_because_that_is_the_whole_problem():
    """An inbound consult note from another EHR frequently carries no identifier this site
    can resolve. A model that requires one cannot represent the case the reconciliation
    engine exists to handle."""
    a = InboundArtifact(
        id="A-0001",
        received_from=PartyRef(id="example-lab", name="Example Lab"),
        patient=None,
        content_hash="a" * 64,
        kind=ArtifactKind.RESULT,
        state=ArtifactState.UNMATCHED,
        received_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        observed_at=None,
    )
    assert a.patient is None


def test_the_two_aggregates_do_not_share_an_identifier_space():
    """A referral id and an artifact id must never be interchangeable, or an attach could
    name the wrong thing and typecheck."""
    assert Referral.__annotations__["id"] is not InboundArtifact.__annotations__["id"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_canonical_models.py -v`
Expected: FAIL — `No module named 'referral_loop.core.models'`

- [ ] **Step 3: Write the module**

Follow design spec §5 and §5.1 for the field lists — they are authoritative and this plan does not restate them. Requirements the tests pin:

- Both aggregates are `@dataclass(frozen=True)`.
- `ReferralId` and `ArtifactId` are distinct `NewType`s over `str`, so the two identifier spaces cannot be crossed silently.
- `Referral.with_hold(hold)` and `.released()` return new instances; there is no setter.
- `ArtifactKind` is an enum: `RESULT`, `DOCUMENT`, `SCHEDULE_NOTICE`.
- No method touches a database, a clock, or a network. If you find yourself wanting `datetime.now()` in here, that is the signal the value belongs on a `Transition` in Plan 2b instead.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_canonical_models.py -v`
Expected: PASS

- [ ] **Step 5: Confirm the closure test from Task 1 now passes**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -k domain_core -v`
Expected: PASS — this is the first point at which it can.

- [ ] **Step 6: Commit**

```bash
cd "$REPO"
git add src/referral_loop/core/models.py tests/test_canonical_models.py
git commit -m "feat(core): Referral and InboundArtifact

Distinct NewType identifier spaces, so an attach cannot name the wrong aggregate and
still typecheck. An artifact's patient is optional because an inbound consult note from
another EHR routinely carries no identifier this site can resolve -- a model requiring
one cannot represent the case the reconciliation engine exists for."
```

---

### Task 4: The FHIR projection

**Files:**
- Create: `src/referral_loop/fhir/__init__.py`, `src/referral_loop/fhir/codesystems.py`, `src/referral_loop/fhir/task_status.py`
- Create: `tests/test_fhir_task_status.py`

The mapping table is design spec §7. Do not retype it from memory; read it.

- [ ] **Step 1: Write the failing test**

`tests/test_fhir_task_status.py`:

```python
import pytest

from referral_loop.core.states import Hold, ReferralState
from referral_loop.fhir.task_status import R4_TASK_STATUS, project

_HOLD = Hold(reason="x", actor="y")


@pytest.mark.parametrize("state", list(ReferralState))
def test_the_projection_is_total(state):
    status, business = project(state, hold=None)
    assert status in R4_TASK_STATUS, f"{state} -> {status} is not an R4 Task.status code"
    assert business is None or isinstance(business, str)


@pytest.mark.parametrize("state", list(ReferralState))
def test_the_projection_is_total_under_hold_as_well(state):
    status, business = project(state, hold=_HOLD)
    assert status == "on-hold"
    assert business is not None, "a hold must not discard which state it was held from"


def test_the_three_states_that_collapse_are_distinguished_by_business_status():
    """SCHEDULED, SEEN and DOCUMENTED all project to in-progress. Those are exactly the
    three distinctions the aging agent escalates on, which is the concrete reason the
    model is dual-layer rather than just adopting Task.status as the vocabulary."""
    collapsing = [ReferralState.SCHEDULED, ReferralState.SEEN, ReferralState.DOCUMENTED]
    projected = [project(s, hold=None) for s in collapsing]
    assert {p[0] for p in projected} == {"in-progress"}
    assert len({p[1] for p in projected}) == 3, "the three must stay distinguishable"


def test_a_held_referral_can_be_told_apart_from_a_referral_held_from_elsewhere():
    a = project(ReferralState.ACCEPTED, hold=_HOLD)
    b = project(ReferralState.SCHEDULED, hold=_HOLD)
    assert a[0] == b[0] == "on-hold"
    assert a[1] != b[1]


def test_every_business_status_code_is_declared_in_our_codesystem():
    from referral_loop.fhir.codesystems import BUSINESS_STATUS

    declared = {c["code"] for c in BUSINESS_STATUS["concept"]}
    for state in ReferralState:
        for hold in (None, _HOLD):
            _, business = project(state, hold=hold)
            if business is not None:
                assert business in declared, f"{business} is emitted but not declared"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_fhir_task_status.py -v`
Expected: FAIL — `No module named 'referral_loop.fhir'`

- [ ] **Step 3: Write the modules**

`R4_TASK_STATUS` is the frozen set of the eleven R4 codes: `draft`, `requested`, `received`, `accepted`, `rejected`, `ready`, `cancelled`, `in-progress`, `on-hold`, `failed`, `completed`, `entered-in-error`. Declare it as data, and say in a comment that it is the published value set rather than our choice — a reader who does not know FHIR will otherwise assume it is editable.

`BUSINESS_STATUS` is a `CodeSystem` resource dict with a `url` under a namespace you control, a `version`, and one concept per code the projection can emit.

`project(state, *, hold)` returns `tuple[str, str | None]`. It must be **total** — no fall-through returning `None`, no `KeyError`. Prefer an explicit `match` or dict with a final `raise AssertionError(f"unmapped state: {state}")` that the parametrised test proves unreachable.

`AGED_OUT → failed` is a judgment call the spec flags as revisitable (§7). Put that note at the mapping, not only in the spec.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_fhir_task_status.py -v`
Expected: PASS, 27 tests

- [ ] **Step 5: Prove the totality test can fail**

```bash
cd "$REPO"
./.venv/Scripts/python.exe - <<'PY'
from pathlib import Path
p = Path("src/referral_loop/core/states.py")
orig = p.read_text(encoding="utf-8")
p.write_text(orig.replace('AGED_OUT = "aged-out"', 'AGED_OUT = "aged-out"\n    ESCALATED = "escalated"'), encoding="utf-8")
PY
./.venv/Scripts/python.exe -m pytest tests/test_fhir_task_status.py -q 2>&1 | tail -3
```

Expected: FAIL — a new state with no mapping must break the projection. Then restore by writing `orig` back with Python (**not** `git checkout`).

- [ ] **Step 6: Commit**

```bash
cd "$REPO"
git add src/referral_loop/fhir/ tests/test_fhir_task_status.py
git commit -m "feat(fhir): total projection from referral state to R4 Task.status

SCHEDULED, SEEN and DOCUMENTED all collapse to in-progress, which is the concrete
justification for the dual layer: those three are exactly what the aging agent
escalates on, and Task.status alone cannot tell them apart. businessStatus carries the
distinction, and a hold preserves the state it was held from rather than discarding it.

Totality is proven by parametrising over the enum, and the proof was checked by adding
a twelfth state and watching it break."
```

---

### Task 5: The migration map

**Files:**
- Create: `src/referral_loop/migration.py`
- Create: `tests/test_migration.py`

This module is **temporary**. Plan 2b deletes it when `Loop` goes away. Say so in its docstring, because a temporary module with no expiry note becomes permanent.

- [ ] **Step 1: Write the failing test**

`tests/test_migration.py`:

```python
import pytest

from referral_loop.core.states import ArtifactState, ReferralState
from referral_loop.events import LoopState
from referral_loop.migration import CLOSED_IS_UNREACHABLE, canonical_state


@pytest.mark.parametrize("legacy", [s for s in LoopState if s is not LoopState.CLOSED])
def test_every_legacy_state_maps_to_exactly_one_canonical_state(legacy):
    mapped = canonical_state(legacy)
    assert isinstance(mapped, (ReferralState, ArtifactState))


def test_the_three_orphan_states_map_to_the_artifact_vocabulary_not_the_referral_one():
    """This is the split, expressed as a test. If any of these three lands in
    ReferralState the conflation has come back."""
    assert canonical_state(LoopState.ORPHAN) is ArtifactState.UNMATCHED
    assert canonical_state(LoopState.DISMISSED) is ArtifactState.DISMISSED
    assert canonical_state(LoopState.ATTACHED) is ArtifactState.ATTACHED


def test_the_six_referral_states_map_to_the_referral_vocabulary():
    assert canonical_state(LoopState.OPEN) is ReferralState.SENT
    assert canonical_state(LoopState.SCHEDULED) is ReferralState.SCHEDULED
    assert canonical_state(LoopState.RESULTED) is ReferralState.DOCUMENTED
    assert canonical_state(LoopState.ACKNOWLEDGED) is ReferralState.RECONCILED
    assert canonical_state(LoopState.CANCELLED) is ReferralState.CANCELLED


def test_closed_has_no_mapping_and_says_why():
    """CLOSED is reserved and refused at store.append_event; nothing can produce a loop
    in it. Mapping it would invent a meaning for a state that has never existed."""
    with pytest.raises(ValueError, match=CLOSED_IS_UNREACHABLE):
        canonical_state(LoopState.CLOSED)


def test_the_mapping_is_injective_within_each_vocabulary():
    """Two legacy states collapsing onto one canonical state would make the migration
    lossy, and Plan 2b replays the event log through this map."""
    referral_targets = [
        canonical_state(s)
        for s in LoopState
        if s is not LoopState.CLOSED and isinstance(canonical_state(s), ReferralState)
    ]
    assert len(referral_targets) == len(set(referral_targets))
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_migration.py -v`
Expected: FAIL — `No module named 'referral_loop.migration'`

- [ ] **Step 3: Write the module**

`canonical_state(legacy: LoopState) -> ReferralState | ArtifactState`, total over the eight reachable members, raising `ValueError(CLOSED_IS_UNREACHABLE)` for `CLOSED`.

Also provide `to_referral(loop: Loop) -> Referral` and `to_artifact(loop: Loop) -> InboundArtifact`, choosing by which vocabulary the state maps into. These are what Plan 2b's replay uses and what an interop branch uses to get a canonical view without waiting for 2b.

Five legacy referral states map to five canonical ones. The other six canonical states — `DRAFT`, `RECEIVED`, `ACCEPTED`, `DECLINED`, `SEEN`, `AGED_OUT` — have **no** legacy source, because the existing machine cannot represent them. State that in the docstring; it is the concrete measure of what the richer model buys.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd "$REPO" && ./.venv/Scripts/python.exe -m pytest tests/test_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add src/referral_loop/migration.py tests/test_migration.py
git commit -m "feat: map the legacy nine-state vocabulary onto the canonical model

Temporary; Plan 2b deletes it when Loop goes away. Six canonical states have no legacy
source -- DRAFT, RECEIVED, ACCEPTED, DECLINED, SEEN, AGED_OUT -- which is the concrete
measure of what the richer model buys. CLOSED raises rather than mapping: it is
reserved and refused at the store, so a mapping would invent a meaning for a state that
has never existed."
```

---

### Task 6: Verify nothing moved, and tag the fork point

**Files:** none — verification and a tag.

- [ ] **Step 1: Full suite**

```bash
cd "$REPO"
./.venv/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -3
```

Expected: `1048 passed` plus this plan's additions, `13 skipped`. **The passed count for pre-existing tests must not have changed by one.** This plan adds modules; it does not touch the running state machine. A changed result is a defect, not a surprise.

- [ ] **Step 2: Confirm the additive claim mechanically**

```bash
cd "$REPO"
git diff --stat <tag-or-sha-before-this-plan>..HEAD -- src/referral_loop/registry.py src/referral_loop/store.py src/referral_loop/listener.py src/referral_loop/matcher.py
```

Expected: **empty output.** If any of those four files changed, this plan stopped being additive and the interop branch's fork point is no longer safe.

- [ ] **Step 3: Confirm lint and types are clean**

```bash
cd "$REPO"
./.venv/Scripts/ruff.exe check src/ tests/
./.venv/Scripts/mypy.exe src/referral_loop/ --ignore-missing-imports --check-untyped-defs --warn-unused-ignores
```

Expected: `All checks passed!` and `Success: no issues found`.

- [ ] **Step 4: Tag the fork point**

```bash
cd "$REPO"
git tag -a canonical-model-v1 -m "Canonical model and FHIR projection.

The interoperability branch forks here. core/ and fhir/ are stable from this point;
Plan 2b changes registry.py and store.py but not these, so a branch that adds only new
directories cannot conflict with it."
git tag -n99 canonical-model-v1
```

- [ ] **Step 5: Tell the human the fork point exists**

Report the tag name and the one-line instruction: `git checkout -b interop canonical-model-v1`.

---

## Definition of done

- [ ] `core/` imports nothing from `store/`, `fhir/`, `ingest/`, or any third-party package — proven in a clean subprocess
- [ ] Eleven referral states, three artifact states, no name shared between them
- [ ] `project()` is total over every state and every state-under-hold, proven by parametrisation and by adding a twelfth state and watching it break
- [ ] The three collapsing states stay distinguishable via `businessStatus`
- [ ] Every legacy `LoopState` except `CLOSED` maps to exactly one canonical state; `CLOSED` raises with a reason
- [ ] `git diff` over `registry.py`, `store.py`, `listener.py`, `matcher.py` is **empty**
- [ ] Suite result for pre-existing tests unchanged
- [ ] Tag `canonical-model-v1` exists

## Deliberately out of scope — this is Plan 2b

`Transition` objects and `assertion_source`; `machine.apply()` and the `RECONCILED`-requires-a-human guarantee; the single-transaction store apply and the gapless-event-chain invariant; `fhir/provenance.py`; `REF^I13`/`I14`/`MDM^T02` ingest work; deleting `migration.py`; and any change to `registry.py` or `store.py` whatsoever.
