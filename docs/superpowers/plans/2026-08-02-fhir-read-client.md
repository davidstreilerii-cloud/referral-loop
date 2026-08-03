# FHIR Read Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull the documents that could close an open referral loop — a two-hop, paginated, retrying read against a configured FHIR endpoint, returning validated resources and refusing every case where "empty" would be a lie.

**Architecture:** Three new modules in `connect/` (`retry.py`, `resources.py`, `documents.py`) plus two small extensions to existing ones (`Response` gains headers; `ConnectorProfile` gains `identifier_systems`). Nothing outside `connect/` changes. `core/` stays unreachable; `egress.fetch` stays the only `.open()` call site.

**Tech Stack:** Python 3.12, stdlib only. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-08-02-fhir-read-client-design.md`. Section references below point at it. Read §1.3 and §3.3 before Task 1 — the whole plan follows from one property.

---

## Baseline

- **Work in the isolated worktree: `$WORKTREE`**, not `$REPO`. It has its own `.venv` — always `./.venv/Scripts/python.exe` from the worktree root, never bare `python` and never the main tree's venv.
- Branch `fhir-read-client`, forked from `main` after sub-project A merged.
- **Baseline, measured in the worktree 2026-08-02: `1549 passed, 2 skipped, 11 deselected`**, `ruff` and `mypy` clean.
- **The full suite takes ~8.5 minutes — run it only in Task 9.** Every other task runs its own files.

### Why a worktree, and why it needs its own venv

A second agent is executing Plan 2b (`core/`, `registry.py`, `store.py`, `migration.py`, `fhir/`) in `$REPO`. This plan touches none of those files, but a branch is an isolated *ref*, not an isolated *workspace* — there is one working directory per checkout. While both ran in the same tree, that agent committed `87fb806` onto this feature branch and left uncommitted `core/machine.py` edits sitting in the tree, which measured as a two-test difference in the baseline.

The separate venv is not optional. `referral-loop` is installed editable, and the main tree's `.pth` points at `$REPO/src` — running the main venv's python from the worktree would import the *other* tree's source and test the wrong code while appearing to work.

Verify isolation if anything looks strange:

```bash
cd "$WORKTREE"
./.venv/Scripts/python.exe -c "import referral_loop; print(referral_loop.__file__)"
```

Must print a path under `referral-loop-B`. Verify `git branch --show-current` reads `fhir-read-client` immediately before every commit and abort if it does not.

## The property everything here defends

> Zero results must always mean "we asked and there was nothing." Never "we could not ask."

Four outcomes, and **three of them raise** so that only the fourth can return empty:

| outcome | result |
|---|---|
| connector declares no identifier system | `ConnectorCannotResolvePatients` |
| patient unknown at this connector | `PatientNotFoundAtConnector` |
| identifier matches more than one patient | `PatientAmbiguousAtConnector` |
| patient resolved, no documents found | **empty `DocumentSearch`** — the only correct empty |

If a task makes one of the first three return empty instead of raising, it has broken the product, not just a test.

## File structure

| file | responsibility |
|---|---|
| `src/referral_loop/connect/egress.py` | **modify** — `Response` gains headers and a case-insensitive lookup |
| `src/referral_loop/connect/connectors.py` | **modify** — `identifier_systems`, `is_queryable` |
| `src/referral_loop/connect/retry.py` | **new** — `fetch_retrying`, bounded, `Retry-After` aware, injected sleep |
| `src/referral_loop/connect/resources.py` | **new** — `FetchedResource`, `DocumentSearch`, structural validation |
| `src/referral_loop/connect/documents.py` | **new** — patient resolution, the pagination walk, `find_candidate_documents` |
| `src/referral_loop/cli.py` | **modify** — report queryable vs preflight-only |
| `tests/_fhirserver.py` | **modify** — Bundles with `next`, `OperationOutcome`, 429/503 |

---

### Task 1: `Response` carries headers

**Files:** modify `src/referral_loop/connect/egress.py`, `tests/test_egress.py`

`Retry-After` is a header and A's `Response` is `(status, body)` only, so Task 4 cannot read it. HTTP header names are case-insensitive, so a plain dict lookup on `"Retry-After"` misses a server that sends `retry-after`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_egress.py`:

```python
def test_a_response_exposes_headers_case_insensitively():
    """HTTP header names are case-insensitive and servers disagree about casing. A plain dict
    lookup on "Retry-After" silently misses a server that sends "retry-after", and the symptom
    is not an error -- it is a retry that ignores the interval it was given."""
    from referral_loop.connect.egress import Response

    r = Response(status=429, body=b"", headers=(("Retry-After", "12"), ("Content-Type", "x")))
    assert r.header("retry-after") == "12"
    assert r.header("RETRY-AFTER") == "12"
    assert r.header("Retry-After") == "12"
    assert r.header("absent") is None


def test_a_response_defaults_to_no_headers():
    """Existing call sites construct Response(status=..., body=...) positionally and by keyword;
    adding a required field would break them."""
    from referral_loop.connect.egress import Response

    assert Response(status=200, body=b"{}").header("anything") is None
```

- [ ] **Step 2: Run and watch it fail**

`./.venv/Scripts/python.exe -m pytest tests/test_egress.py -k response -v` → FAIL, `Response.__init__() got an unexpected keyword argument 'headers'`

- [ ] **Step 3: Extend `Response`**

In `egress.py`, replace the `Response` dataclass with:

```python
@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    # A tuple of pairs rather than a dict, so the dataclass stays hashable and immutable in
    # fact as well as in decorator. Lookup goes through header() because HTTP header names are
    # case-insensitive and servers disagree about casing -- a direct dict lookup on
    # "Retry-After" misses a server sending "retry-after", and the symptom is not an error, it
    # is a retry that silently ignores the interval it was told to wait.
    headers: tuple[tuple[str, str], ...] = ()

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None
```

Then populate it at both construction sites in `fetch`:

```python
        with opener.open(request, timeout=timeout) as raw:
            return Response(
                status=raw.status,
                body=_read_capped(raw, url),
                headers=tuple(raw.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        with exc:
            return Response(
                status=exc.code,
                body=_read_capped(exc, url),
                headers=tuple(exc.headers.items()),
            )
```

The `HTTPError` branch matters most: a 429 arrives there, not on the success path.

- [ ] **Step 4: Run and watch it pass**

`./.venv/Scripts/python.exe -m pytest tests/test_egress.py tests/test_preflight.py -q` → all pass. Preflight is included because it constructs and consumes `Response`; a default that broke it would show here.

- [ ] **Step 5: Commit**

```bash
cd "$WORKTREE"
git add src/referral_loop/connect/egress.py tests/test_egress.py
git commit -m "feat(connect): Response carries headers, looked up case-insensitively

Retry-After is a header and Response was (status, body), so the retry policy in the read
client had nothing to read. Stored as a tuple of pairs so the frozen dataclass stays genuinely
immutable, and read through header() because header names are case-insensitive and servers
disagree -- a dict lookup on Retry-After misses a server sending retry-after, and the symptom
is not an error but a retry that ignores the interval it was handed.

Populated on the HTTPError branch as well as the success branch, which is the one that matters:
a 429 arrives as an HTTPError."
```

---

### Task 2: `identifier_systems`, and what makes a connector queryable

**Files:** modify `src/referral_loop/connect/connectors.py`, `tests/test_connectors.py`

Spec §3.1 and §3.5.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connectors.py`:

```python
def test_a_connector_without_identifier_systems_is_not_queryable():
    """Absent is allowed -- every connector A shipped is preflight-only -- but it must be
    visible as an incapability rather than discovered by a query that returns nothing."""
    assert not _registry().get("example-med").is_queryable


def test_a_declared_mrn_system_makes_a_connector_queryable():
    reg = _registry(_profile(identifier_systems={"mrn": "urn:oid:1.2.840.114350.1.13.99"}))
    ku = reg.get("example-med")
    assert ku.is_queryable
    assert ku.mrn_system == "urn:oid:1.2.840.114350.1.13.99"


def test_a_malformed_identifier_systems_block_refuses():
    with pytest.raises(ConnectorConfigError, match="identifier_systems"):
        _registry(_profile(identifier_systems="urn:oid:1.2.3"))


def test_an_empty_mrn_system_refuses_rather_than_reading_as_absent():
    """An empty string is a typo, not a declaration. Treating it as absent would turn a broken
    config into a silently preflight-only connector."""
    with pytest.raises(ConnectorConfigError, match="mrn"):
        _registry(_profile(identifier_systems={"mrn": "  "}))


def test_an_unknown_identifier_kind_refuses():
    """Only mrn is consumed today. An unrecognised key is a typo that would otherwise sit in
    the file looking like it did something."""
    with pytest.raises(ConnectorConfigError, match="ssn|unknown|identifier"):
        _registry(_profile(identifier_systems={"ssn": "urn:oid:2.16.840.1.113883.4.1"}))
```

- [ ] **Step 2: Run and watch it fail**

`./.venv/Scripts/python.exe -m pytest tests/test_connectors.py -k identifier -v` → FAIL, `AttributeError: 'ConnectorProfile' object has no attribute 'is_queryable'`

- [ ] **Step 3: Implement**

In `connectors.py`, add near the other constants:

```python
# Only mrn is consumed. A second kind gets added here when something reads it -- an
# unrecognised key refuses rather than sitting in the file looking like it did something.
IDENTIFIER_KINDS = ("mrn",)
```

Add the field to `ConnectorProfile` after `ca_file`:

```python
    identifier_systems: Mapping[str, str]
```

with `from collections.abc import Mapping` at the top, and these two accessors on the dataclass:

```python
    @property
    def mrn_system(self) -> str | None:
        return self.identifier_systems.get("mrn")

    @property
    def is_queryable(self) -> bool:
        """Whether this connector can be asked about one of our patients at all.

        False is a legitimate configuration -- a preflight-only connector proves reachability
        and credentials and reads nothing. What must not happen is a query against one of these
        quietly returning an empty result set, which is why documents.py raises instead. See
        spec 1.3.
        """
        return self.mrn_system is not None
```

In `_profile_from`, before constructing the profile:

```python
    raw_identifiers = entry.get("identifier_systems", {})
    if not isinstance(raw_identifiers, dict):
        raise _refuse(f"{what}: identifier_systems must be an object")
    unknown_kinds = sorted(k for k in raw_identifiers if k not in IDENTIFIER_KINDS)
    if unknown_kinds:
        raise _refuse(
            f"{what}: unknown identifier_systems keys {unknown_kinds}; known are "
            f"{list(IDENTIFIER_KINDS)}"
        )
    identifier_systems = {
        kind: _text(value, f"{what} identifier_systems.{kind}", _MAX_URL)
        for kind, value in raw_identifiers.items()
    }
```

and pass `identifier_systems=identifier_systems` to the constructor. `_text` already refuses an empty or whitespace-only string, which is what makes the `"  "` test pass.

- [ ] **Step 4: Run and watch it pass**

`./.venv/Scripts/python.exe -m pytest tests/test_connectors.py -v` → all pass.

- [ ] **Step 5: Commit**

```bash
cd "$WORKTREE"
git add src/referral_loop/connect/connectors.py tests/test_connectors.py
git commit -m "feat(connect): identifier_systems, and is_queryable

Deferred out of sub-project A because nothing read it and validating a field with no reader
means the rules are guesses. Now something reads it.

Absent is legitimate -- a preflight-only connector proves reachability and credentials and
reads nothing -- but an empty string is a typo, and treating it as absent would turn a broken
config into a silently unqueryable connector. An unrecognised key refuses rather than sitting
in the file looking like it configured something."
```

---

### Task 3: Teach the test server to be a FHIR server

**Files:** modify `tests/_fhirserver.py`

No production code. Tasks 4–7 all need this; it comes first so they are not blocked.

- [ ] **Step 1: Extend `ServerBehaviour`**

Read `tests/_fhirserver.py` first. Add these fields to the dataclass, keeping the existing ones:

```python
    # Bundles keyed by path+query, so one server can answer a Patient search and two resource
    # searches differently within a single test.
    bundles: dict = field(default_factory=dict)
    # Status codes to return before behaving normally, popped one per request. [429, 503] means
    # fail twice then succeed -- which is what a retry test needs to assert on.
    transient_failures: list = field(default_factory=list)
    retry_after: str | None = None
    operation_outcome: dict | None = None
```

- [ ] **Step 2: Add the handler behaviour**

In `do_GET`, before the `/metadata` branch, add search handling:

```python
            if behaviour.transient_failures:
                status = behaviour.transient_failures.pop(0)
                if behaviour.retry_after is not None:
                    self._send_with_headers(
                        status,
                        behaviour.operation_outcome or {"resourceType": "OperationOutcome"},
                        {"Retry-After": behaviour.retry_after},
                    )
                else:
                    self._send(status, behaviour.operation_outcome or {"resourceType": "OperationOutcome"})
                return

            key = self.path
            if key in behaviour.bundles:
                self._send(200, behaviour.bundles[key])
                return
            if behaviour.operation_outcome is not None:
                self._send(400, behaviour.operation_outcome)
                return
```

and add the header-capable sender beside `_send`:

```python
        def _send_with_headers(self, status: int, payload: dict, extra: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
```

- [ ] **Step 3: Add a Bundle builder**

At module level:

```python
def bundle(*resources, next_url: str | None = None) -> dict:
    """A searchset Bundle. `next_url` is what the pagination walk will be handed -- tests point
    it off-allowlist, at itself, and at a real next page, because those are three different
    failures and only one of them is legitimate."""
    payload = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
        "link": [],
    }
    if next_url is not None:
        payload["link"].append({"relation": "next", "url": next_url})
    return payload


def document_reference(doc_id: str, patient_id: str = "p1") -> dict:
    return {
        "resourceType": "DocumentReference",
        "id": doc_id,
        "status": "current",
        "subject": {"reference": f"Patient/{patient_id}"},
        "content": [{"attachment": {"contentType": "application/pdf", "url": f"Binary/{doc_id}"}}],
    }


def diagnostic_report(report_id: str, patient_id: str = "p1") -> dict:
    return {
        "resourceType": "DiagnosticReport",
        "id": report_id,
        "status": "final",
        "subject": {"reference": f"Patient/{patient_id}"},
    }


def patient(patient_id: str = "p1") -> dict:
    return {"resourceType": "Patient", "id": patient_id}
```

- [ ] **Step 4: Smoke-test it**

```bash
cd "$WORKTREE"
./.venv/Scripts/python.exe - <<'PY'
import sys, ssl, json, urllib.request, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from _certs import localhost_cert
from _fhirserver import ServerBehaviour, bundle, document_reference, fhir_server

with tempfile.TemporaryDirectory() as d:
    certfile, keyfile, ca = localhost_cert(Path(d))
    b = ServerBehaviour(bundles={"/DocumentReference?patient=Patient/p1": bundle(document_reference("d1"))})
    with fhir_server(certfile, keyfile, b) as (base, behaviour):
        ctx = ssl.create_default_context(cafile=str(ca))
        with urllib.request.urlopen(f"{base}/DocumentReference?patient=Patient/p1", context=ctx, timeout=10) as r:
            got = json.loads(r.read())
        print("entries:", len(got["entry"]), "type:", got["entry"][0]["resource"]["resourceType"])
PY
```

Expected: `entries: 1 type: DocumentReference`

- [ ] **Step 5: Confirm nothing regressed and commit**

`./.venv/Scripts/python.exe -m pytest tests/test_preflight.py -q` → 8 passed (this file already uses the fixture).

```bash
cd "$WORKTREE"
git add tests/_fhirserver.py
git commit -m "test: the fixture learns to be a FHIR server, not just a metadata endpoint

Bundles keyed by path so one server answers a Patient search and two resource searches
differently in one test; transient_failures popped per request so a retry test can assert fail-
fail-succeed rather than mocking a clock; a next link the test chooses, because pointing it
off-allowlist, at itself, and at a real page are three different failures and only one is
legitimate."
```

---

### Task 4: `retry.py`

**Files:** create `src/referral_loop/connect/retry.py`, `tests/test_retry.py`

Spec §6.

- [ ] **Step 1: Write the failing test**

`tests/test_retry.py`:

```python
"""The retry policy, and the three things it must not do."""

from __future__ import annotations

import pytest

from referral_loop.connect.egress import Response
from referral_loop.connect.retry import (
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    RETRYABLE_STATUSES,
    fetch_retrying,
)


class _Calls:
    """Records what fetch was asked for and what the policy slept."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = 0
        self.slept: list[float] = []

    def fetch(self, *_args, **_kwargs) -> Response:
        self.requests += 1
        return self.responses.pop(0)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _ok() -> Response:
    return Response(status=200, body=b"{}")


def _retryable(status: int, retry_after: str | None = None) -> Response:
    headers = (("Retry-After", retry_after),) if retry_after else ()
    return Response(status=status, body=b"{}", headers=headers)


def test_a_success_is_returned_without_sleeping():
    calls = _Calls([_ok()])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == 200
    assert calls.requests == 1
    assert calls.slept == []


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_a_retryable_status_is_retried(status):
    calls = _Calls([_retryable(status), _ok()])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == 200
    assert calls.requests == 2
    assert len(calls.slept) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_fault_is_never_retried(status):
    """Same rubric as _OUR_FAULT in auth.py: a malformed query does not become well-formed on a
    second attempt, and retrying it spends the budget a genuine transient needs."""
    calls = _Calls([Response(status=status, body=b"{}")])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == status
    assert calls.requests == 1, "a client fault must not be retried"
    assert calls.slept == []


def test_retry_after_is_honoured():
    calls = _Calls([_retryable(429, retry_after="7"), _ok()])
    fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert calls.slept == [7.0]


def test_a_retry_after_beyond_the_ceiling_is_capped_not_obeyed():
    """A server answering Retry-After: 86400 must not park a coordinator's preflight for a day.
    Capped rather than ignored, because ignoring it would hammer a server that just asked us to
    stop."""
    calls = _Calls([_retryable(503, retry_after="86400"), _ok()])
    fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert calls.slept == [float(MAX_RETRY_AFTER_SECONDS)]


def test_a_malformed_retry_after_falls_back_to_backoff_rather_than_raising():
    """Retry-After may be an HTTP-date, and some servers send nonsense. Neither is worth failing
    a request over -- but neither may be read as 'retry immediately'."""
    calls = _Calls([_retryable(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), _ok()])
    fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert len(calls.slept) == 1
    assert calls.slept[0] > 0


def test_attempts_are_bounded_and_the_last_response_is_returned():
    calls = _Calls([_retryable(503) for _ in range(MAX_ATTEMPTS)])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == 503
    assert calls.requests == MAX_ATTEMPTS, "must stop at the cap rather than retrying forever"
```

- [ ] **Step 2: Run and watch it fail**

`./.venv/Scripts/python.exe -m pytest tests/test_retry.py -v` → FAIL, `No module named 'referral_loop.connect.retry'`

- [ ] **Step 3: Write the module**

`src/referral_loop/connect/retry.py`:

```python
"""A policy over repeated requests, kept apart from the request itself.

egress.fetch answers "one bounded request to an allowlisted host". This answers "how many times,
and how long between". Separating them is not tidiness: fetch is the only place in the package
that opens a connection, and tests/test_import_closure.py pins that. Retry wraps fetch and opens
nothing, so the pin stays true for free.

The sleep function is injected for the same reason every timestamp in auth.py is: a policy whose
intervals can only be observed by waiting for them is a policy nobody tests precisely.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from time import sleep as _real_sleep

from .connectors import ConnectorProfile, ConnectorRegistry
from .egress import ConnectorUnreachable, Response, fetch

logger = logging.getLogger(__name__)

# 429 is a rate limit and 503 is a server saying "not now"; both become a different answer if
# asked again. Everything else in the 4xx range is a fault in what we sent -- the same rubric
# auth._OUR_FAULT applies to token errors, for the same reason.
RETRYABLE_STATUSES = frozenset({429, 503})

MAX_ATTEMPTS = 3

# Backoff between attempts when the server did not say. Deliberately not jittered: jitter needs
# randomness, randomness makes the interval untestable, and there is one client here rather than
# a thundering herd of them.
BACKOFF_SECONDS = (1.0, 4.0)

# A server may ask for any delay it likes. We are not obliged to grant it -- a preflight parked
# for a day is a failed preflight that looks like a hung one.
MAX_RETRY_AFTER_SECONDS = 30


def _wait_for(response: Response, attempt: int) -> float:
    raw = response.header("retry-after")
    if raw is not None:
        try:
            asked = float(raw)
        except ValueError:
            # An HTTP-date, or nonsense. Neither is worth failing over, and neither may be read
            # as "retry immediately" -- so fall through to our own backoff.
            logger.debug("unparseable Retry-After %r; using backoff instead", raw)
        else:
            capped = min(asked, float(MAX_RETRY_AFTER_SECONDS))
            if capped < asked:
                logger.warning(
                    "server asked us to wait %ss; waiting %ss instead", asked, capped
                )
            return capped
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]


def fetch_retrying(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    url: str,
    *,
    _fetch: Callable[..., Response] = fetch,
    _sleep: Callable[[float], None] = _real_sleep,
    **kwargs: object,
) -> Response:
    """fetch, repeated only for the failures that repeating can fix.

    Returns the last response rather than raising when the attempts run out: the caller has the
    status and the body and is better placed to say what a persistent 503 means for it than a
    transport helper is.
    """
    last: Response | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            last = _fetch(registry, profile, url, **kwargs)
        except ConnectorUnreachable:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            _sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
            continue

        if last.status not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS - 1:
            return last

        _sleep(_wait_for(last, attempt))

    assert last is not None  # unreachable: the loop runs at least once
    return last
```

- [ ] **Step 4: Run and watch it pass**

`./.venv/Scripts/python.exe -m pytest tests/test_retry.py -v` → all pass.

- [ ] **Step 5: Confirm the closure pin still holds**

`./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -v` → 6 passed. `retry.py` must not appear in the egress test's findings; it calls `fetch`, never `.open()`.

- [ ] **Step 6: Commit**

```bash
cd "$WORKTREE"
git add src/referral_loop/connect/retry.py tests/test_retry.py
git commit -m "feat(connect): a retry policy, beside the request rather than inside it

429 and 503 become a different answer if asked again. A 400 does not -- same rubric as
auth._OUR_FAULT -- and retrying it spends the budget a genuine transient needs.

Retry-After is honoured and capped: a server asking for 86400 seconds must not park a
coordinator's preflight for a day, and ignoring the header outright would hammer a server that
just asked us to stop. An unparseable value falls back to backoff rather than being read as
'retry immediately'.

The sleep function is injected. A policy whose intervals can only be observed by waiting for
them is a policy nobody tests precisely."
```

---

### Task 5: `resources.py` — the return shapes and structural validation

**Files:** create `src/referral_loop/connect/resources.py`, `tests/test_resources.py`

Spec §4.1 and §8.

- [ ] **Step 1: Write the failing test**

`tests/test_resources.py`:

```python
"""What a fetched resource has to have before anything downstream may rely on it."""

from __future__ import annotations

import pytest

from referral_loop.connect.resources import (
    READABLE_TYPES,
    DocumentSearch,
    FetchedResource,
    ResourceMalformed,
    validate,
)


def _doc(**overrides) -> dict:
    base = {
        "resourceType": "DocumentReference",
        "id": "d1",
        "status": "current",
        "subject": {"reference": "Patient/p1"},
        "content": [{"attachment": {"contentType": "application/pdf"}}],
    }
    base.update(overrides)
    return base


def _report(**overrides) -> dict:
    base = {
        "resourceType": "DiagnosticReport",
        "id": "r1",
        "status": "final",
        "subject": {"reference": "Patient/p1"},
    }
    base.update(overrides)
    return base


def test_a_well_formed_document_validates():
    assert validate(_doc())["id"] == "d1"


def test_a_well_formed_report_validates():
    assert validate(_report())["id"] == "r1"


def test_both_readable_types_are_declared():
    assert READABLE_TYPES == ("DocumentReference", "DiagnosticReport")


def test_a_resource_of_another_type_is_refused():
    """A server that ignores a search parameter returns whatever it likes. Accepting it would
    put an Observation into a set the caller believes is documents."""
    with pytest.raises(ResourceMalformed, match="resourceType"):
        validate({"resourceType": "Observation", "id": "o1", "status": "final"})


@pytest.mark.parametrize("missing", ["id", "status", "subject"])
def test_a_document_missing_a_field_the_mapper_needs_is_refused(missing):
    broken = _doc()
    del broken[missing]
    with pytest.raises(ResourceMalformed, match=missing):
        validate(broken)


def test_a_document_with_no_attachment_is_refused():
    """A DocumentReference whose content carries no attachment references no document. It would
    map to an artifact with nothing in it."""
    with pytest.raises(ResourceMalformed, match="attachment"):
        validate(_doc(content=[{}]))


def test_a_report_needs_no_attachment():
    """DiagnosticReport carries its result in presentedForm or result, not content -- requiring
    an attachment here would refuse every valid report."""
    assert validate(_report())


def test_a_search_reports_what_it_discarded():
    """skipped_malformed lives on the result, not in a log: 'we found three documents' must
    never quietly mean 'we found three and threw two away'."""
    search = DocumentSearch(
        resources=(), patient_id="p1", pages_walked=1, skipped_malformed=2, query_urls=("u",)
    )
    assert search.skipped_malformed == 2


def test_a_fetched_resource_records_where_it_came_from():
    r = FetchedResource(
        resource_type="DocumentReference",
        resource=_doc(),
        connector_id="example-med",
        query_url="https://x/DocumentReference?patient=Patient/p1",
        page=2,
    )
    assert r.connector_id == "example-med"
    assert r.page == 2
```

- [ ] **Step 2: Run and watch it fail**

`./.venv/Scripts/python.exe -m pytest tests/test_resources.py -v` → FAIL, `No module named 'referral_loop.connect.resources'`

- [ ] **Step 3: Write the module**

`src/referral_loop/connect/resources.py`:

```python
"""What comes back, and the shape it has to have first.

Structural validation, not profile conformance. US Core would need a validator dependency and
profile packages, and test_install_closure asserts the dependency surface stays small -- that is
its own unit. What is checked here is exactly the set of fields the canonical mapper in the next
sub-project reads, so that a resource which passes here cannot fail there for a missing field.

These stay FHIR. Mapping onto Referral and InboundArtifact is the next sub-project's job, and
stopping at this boundary is what lets the read client be tested without importing core/.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import ReferralLoopError

READABLE_TYPES = ("DocumentReference", "DiagnosticReport")


class ResourceMalformed(ReferralLoopError):
    """A resource is missing something downstream needs.

    Not fatal to a search -- documents.py skips and counts these, the same posture
    UnparseableSegmentError already takes for a bad HL7 segment. One malformed resource must not
    hide the nine good ones.
    """


@dataclass(frozen=True)
class FetchedResource:
    resource_type: str
    resource: Mapping[str, object]
    connector_id: str
    query_url: str
    page: int


@dataclass(frozen=True)
class DocumentSearch:
    resources: tuple[FetchedResource, ...]
    patient_id: str
    pages_walked: int
    skipped_malformed: int
    query_urls: tuple[str, ...]


def _require(resource: Mapping[str, object], field: str) -> object:
    if field not in resource or resource[field] in (None, "", [], {}):
        raise ResourceMalformed(
            f"{resource.get('resourceType', 'resource')} {resource.get('id', '?')} "
            f"has no {field}"
        )
    return resource[field]


def validate(resource: Mapping[str, object]) -> Mapping[str, object]:
    """Refuse anything the mapper could not use. Returns the resource unchanged."""
    kind = resource.get("resourceType")
    if kind not in READABLE_TYPES:
        # A server that ignores a search parameter returns whatever it likes. Accepting it would
        # put an Observation into a set the caller believes is documents.
        raise ResourceMalformed(
            f"resourceType {kind!r} is not one of {READABLE_TYPES}"
        )

    for field in ("id", "status", "subject"):
        _require(resource, field)

    if kind == "DocumentReference":
        content = _require(resource, "content")
        if not isinstance(content, list) or not any(
            isinstance(item, dict) and item.get("attachment") for item in content
        ):
            # Content with no attachment references no document; it would map to an artifact
            # with nothing in it. DiagnosticReport is exempt: it carries its result in
            # presentedForm or result, so requiring an attachment would refuse every valid one.
            raise ResourceMalformed(
                f"DocumentReference {resource.get('id', '?')} has no content[].attachment"
            )

    return resource
```

- [ ] **Step 4: Run and watch it pass**

`./.venv/Scripts/python.exe -m pytest tests/test_resources.py -v` → all pass.

- [ ] **Step 5: Commit**

```bash
cd "$WORKTREE"
git add src/referral_loop/connect/resources.py tests/test_resources.py
git commit -m "feat(connect): the return shapes, and structural validation

Checked against exactly the fields the canonical mapper reads, so a resource that passes here
cannot fail there for a missing one. Not US Core conformance -- that needs a validator
dependency and profile packages, against an install-closure test that asserts the dependency
surface stays small.

A wrong resourceType is refused rather than carried: a server that ignores a search parameter
returns whatever it likes, and accepting it puts an Observation into a set the caller believes
is documents. DiagnosticReport is exempt from the attachment requirement because it carries its
result in presentedForm, and requiring one would refuse every valid report.

skipped_malformed is a field on the result rather than a log line, so 'we found three' can
never quietly mean 'we found three and threw two away'."
```

---

### Task 6: Patient resolution — the three refusals

**Files:** create `src/referral_loop/connect/documents.py`, `tests/test_documents.py`

Spec §3.2 and §3.3. **This is the load-bearing task of the plan.**

- [ ] **Step 1: Write the failing test**

`tests/test_documents.py`:

```python
"""Resolving our patient at a remote, and the three answers that are not 'no documents'."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from referral_loop.connect.connectors import ConnectorRegistry
from referral_loop.connect.documents import (
    ConnectorCannotResolvePatients,
    PatientAmbiguousAtConnector,
    PatientNotFoundAtConnector,
    resolve_patient,
)

from ._certs import localhost_cert, rsa_keypair
from ._fhirserver import ServerBehaviour, bundle, fhir_server, patient

_SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _registry(base_url, key_path, ca_file, *, mrn_system="urn:oid:1.2.3"):
    connector = {
        "connector_id": "example-med",
        "organization": "Example Medical Center",
        "vendor": "epic",
        "fhir_base_url": base_url,
        "token_url": f"{base_url}/oauth2/token",
        "fhir_version": ["4.0.1"],
        "auth": {
            "mode": "smart-backend-services",
            "client_id": "client-abc",
            "private_key_file": str(key_path),
            "key_id": "k1",
            "algorithm": "RS384",
            "scopes": ["system/Patient.read"],
        },
        "authorities": [],
        "tls": {"ca_file": str(ca_file)},
    }
    if mrn_system is not None:
        connector["identifier_systems"] = {"mrn": mrn_system}
    return ConnectorRegistry.from_mapping({"connectors": [connector]})


@pytest.fixture
def certs(tmp_path):
    return localhost_cert(tmp_path)


def test_a_connector_with_no_declared_system_refuses_rather_than_returning_nothing(certs, tmp_path):
    """The load-bearing case. Returning an empty result here would tell a coordinator the
    specialist never documented anything, when in fact we never asked."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        registry = _registry(base, key_path, ca, mrn_system=None)
        with pytest.raises(ConnectorCannotResolvePatients, match="identifier_systems"):
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")


def test_one_match_resolves(certs, tmp_path):
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle(patient("p1"))}
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        assert resolve_patient(registry, registry.get("example-med"), mrn="MRN1") == "p1"


def test_no_match_is_not_the_same_as_no_documents(certs, tmp_path):
    """A patient unknown here and a patient with nothing filed are opposite facts about a
    referral. One says look elsewhere; the other says the specialist never documented."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle()}
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PatientNotFoundAtConnector, match="MRN1"):
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")


def test_two_matches_refuse_rather_than_pick(certs, tmp_path):
    """Choosing between candidates is how another patient's consult note gets attached to this
    referral. A wrong match is worse than no match."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        bundles={
            "/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle(patient("p1"), patient("p2"))
        }
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PatientAmbiguousAtConnector, match="2"):
            resolve_patient(registry, registry.get("example-med"), mrn="MRN1")


def test_the_mrn_does_not_reach_the_logs_on_failure(certs, tmp_path, caplog):
    """OperationOutcome.diagnostics echoes the failing request and ours carries an MRN. There is
    no logging scrubber in this codebase to catch it downstream."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    behaviour = ServerBehaviour(
        operation_outcome={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "processing",
                    "diagnostics": "failed searching Patient?identifier=urn:oid:1.2.3|MRN1",
                }
            ],
        }
    )
    with fhir_server(certfile, keyfile, behaviour) as (base, _b):
        registry = _registry(base, key_path, ca)
        with caplog.at_level("DEBUG"):
            with pytest.raises(Exception):
                resolve_patient(registry, registry.get("example-med"), mrn="MRN1")
    assert "MRN1" not in caplog.text, "the MRN reached the logs via diagnostics"
```

- [ ] **Step 2: Run and watch it fail**

`./.venv/Scripts/python.exe -m pytest tests/test_documents.py -v` → FAIL, `No module named 'referral_loop.connect.documents'`

- [ ] **Step 3: Write the resolution half**

`src/referral_loop/connect/documents.py` — this task writes only the imports, the errors, `_authorized_get`, and `resolve_patient`. Task 7 appends the walk.

```python
"""Pull the documents that could close an open loop.

The search is two hops, and the first one is the whole design. A chained
`DocumentReference?patient.identifier=...` would be one request, but chained parameter support
varies between servers and a server that does not support it usually does not say so -- it
ignores the parameter and returns an unfiltered Bundle, or nothing. Both are indistinguishable
from a correct empty answer, and this is the one product that cannot afford that confusion.

So: resolve the patient by identifier, then query by reference. Four outcomes, three of which
raise, so that only the fourth can hand back an empty result:

  * the connector declares no identifier system -- we could not ask
  * the patient is unknown here             -- we asked, they have no such person
  * the identifier matches several          -- we asked, and the answer is not usable
  * the patient resolved and nothing filed  -- we asked, and there is nothing. Empty is correct.

A caller handed an empty list cannot tell those apart, and they are opposite facts about a
referral loop. Design spec section 1.3.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import quote

from ..errors import ReferralLoopError
from .auth import acquire_token
from .connectors import ConnectorProfile, ConnectorRegistry
from .egress import Response
from .retry import fetch_retrying

logger = logging.getLogger(__name__)


class ConnectorCannotResolvePatients(ReferralLoopError):
    """The connector declares no identifier system, so no query can be built.

    Raised rather than returning an empty result, because empty would say "the specialist filed
    nothing" when the truth is "we never asked".
    """


class PatientNotFoundAtConnector(ReferralLoopError):
    """The identifier matched nobody here. Not the same as having no documents."""


class PatientAmbiguousAtConnector(ReferralLoopError):
    """The identifier matched more than one patient. Refused rather than resolved.

    Picking one is how another patient's consult note gets attached to this referral. Choosing
    between candidates is the identity-resolution slice's problem, not this one's.
    """


class FhirRequestFailed(ReferralLoopError):
    """A request failed in a way retrying will not fix.

    Carries the OperationOutcome's severity and code, which are enumerated FHIR values, and
    never its diagnostics -- servers routinely echo the failing request there and ours contains
    an MRN. There is no logging scrubber in this codebase to catch that downstream.
    """


def _outcome_summary(response: Response) -> str:
    """severity/code only. See FhirRequestFailed: diagnostics is not safe to carry."""
    try:
        payload = json.loads(response.text())
    except json.JSONDecodeError:
        return "unparseable response body"
    issues = payload.get("issue") if isinstance(payload, dict) else None
    if not isinstance(issues, list) or not issues:
        return "no issue reported"
    parts = []
    for issue in issues:
        if isinstance(issue, dict):
            parts.append(f"{issue.get('severity', '?')}/{issue.get('code', '?')}")
    return ", ".join(parts) or "no issue reported"


def _authorized_get(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    url: str,
) -> Mapping[str, object]:
    token = acquire_token(registry, profile)
    response = fetch_retrying(
        registry,
        profile,
        url,
        headers={
            "Accept": "application/fhir+json",
            "Authorization": f"Bearer {token.value}",
        },
    )
    if response.status != 200:
        raise FhirRequestFailed(
            f"{profile.connector_id}: request failed with {response.status} "
            f"({_outcome_summary(response)})"
        )
    try:
        payload = json.loads(response.text())
    except json.JSONDecodeError as exc:
        raise FhirRequestFailed(f"{profile.connector_id}: response was not JSON") from exc
    if not isinstance(payload, dict):
        raise FhirRequestFailed(f"{profile.connector_id}: response was not a JSON object")
    return payload


def _entries(bundle: Mapping[str, object]) -> list[Mapping[str, object]]:
    entry = bundle.get("entry")
    if not isinstance(entry, list):
        return []
    return [e["resource"] for e in entry if isinstance(e, dict) and isinstance(e.get("resource"), dict)]


def patient_search_url(profile: ConnectorProfile, mrn: str) -> str:
    """Hop 1's URL, built in one place so the search can record what it actually asked.

    The `system|value` separator in a FHIR token search is percent-encoded, and both halves go
    through quote(safe='') so a system URI's colons and slashes survive intact.
    """
    system = profile.mrn_system
    if system is None:
        raise ConnectorCannotResolvePatients(
            f"{profile.connector_id}: no identifier_systems.mrn declared, so this connector "
            "cannot be asked about our patients. It is preflight-only."
        )
    return (
        f"{profile.fhir_base_url}/Patient"
        f"?identifier={quote(system, safe='')}%7C{quote(mrn, safe='')}"
    )


def resolve_patient(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    *,
    mrn: str,
) -> str:
    """Hop 1. Returns the remote's Patient id, or raises -- never returns nothing."""
    system = profile.mrn_system
    url = patient_search_url(profile, mrn)  # raises ConnectorCannotResolvePatients if undeclared
    found = _entries(_authorized_get(registry, profile, url))

    if not found:
        raise PatientNotFoundAtConnector(
            f"{profile.connector_id} does not know patient {mrn} in {system}. This is not the "
            "same as having no documents for them."
        )
    if len(found) > 1:
        raise PatientAmbiguousAtConnector(
            f"{profile.connector_id} matched {len(found)} patients for {mrn} in {system}; "
            "refusing to choose between them"
        )

    patient_id = found[0].get("id")
    if not isinstance(patient_id, str) or not patient_id:
        raise FhirRequestFailed(f"{profile.connector_id}: resolved Patient carries no id")
    return patient_id
```

**Note the `%7C`**: the `system|value` separator in a FHIR token search must be percent-encoded, and both halves are `quote`d with `safe=''` so a system URI's colons and slashes survive intact.

- [ ] **Step 4: Run and watch it pass**

`./.venv/Scripts/python.exe -m pytest tests/test_documents.py -v` → all pass.

If the Patient search URL in the tests does not match what the module builds, **fix the test's expected key to match the module, not the other way round** — then confirm by printing `behaviour.requests` that the server saw the URL you expect.

- [ ] **Step 5: Commit**

```bash
cd "$WORKTREE"
git add src/referral_loop/connect/documents.py tests/test_documents.py
git commit -m "feat(connect): resolve the patient first, and refuse three ways

A chained DocumentReference?patient.identifier= would be one request instead of two, and is not
used: chained parameter support varies between servers, and a server that does not support it
usually ignores the parameter and returns an unfiltered Bundle or nothing. Both look exactly
like a correct empty answer.

Three of the four outcomes raise, so only the fourth can return empty. No declared identifier
system means we could not ask. No match means this connector does not know the patient. Several
matches means the answer is not usable -- and picking one is how another patient's consult note
gets attached to this referral.

OperationOutcome diagnostics never leaves the module: servers echo the failing request there and
ours carries an MRN."
```

---

### Task 7: The pagination walk

**Files:** modify `src/referral_loop/connect/documents.py`, `tests/test_documents.py`

Spec §5.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_documents.py`:

```python
from referral_loop.connect.documents import (
    MAX_PAGES,
    PaginationRefused,
    find_candidate_documents,
)
from ._fhirserver import diagnostic_report, document_reference


def _patient_bundle():
    return {"/Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1": bundle(patient("p1"))}


def test_both_resource_types_are_queried(certs, tmp_path):
    """A consult note arrives as a DocumentReference and a lab result as a DiagnosticReport,
    which is the FHIR analogue of the ORU the HL7 path already closes on. Querying one would
    miss every diagnostic referral."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(document_reference("d1"))
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(diagnostic_report("r1"))
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        got = find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
    assert {r.resource_type for r in got.resources} == {"DocumentReference", "DiagnosticReport"}
    assert got.patient_id == "p1"


def test_a_resolved_patient_with_nothing_filed_returns_empty(certs, tmp_path):
    """The one case where empty is the right answer, and the reason the other three raise."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        got = find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
    assert got.resources == ()
    assert got.patient_id == "p1"


def test_a_next_link_off_the_allowlist_refuses_loudly(certs, tmp_path):
    """A next link is chosen by the remote. Refusing quietly would hand back a truncated result
    set wearing the costume of a complete one."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        document_reference("d1"), next_url="https://evil.example/page2"
    )
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        with pytest.raises(PaginationRefused, match="evil.example"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)


def test_a_self_referential_next_link_refuses(certs, tmp_path):
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    with fhir_server(certfile, keyfile) as (base, _b):
        page = f"{base}/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"
        bundles = _patient_bundle()
        bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
            document_reference("d1"), next_url=page
        )
        bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
        _b.bundles.update(bundles)
        registry = _registry(base, key_path, ca)
        with pytest.raises(PaginationRefused, match="itself|loop"):
            find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)


def test_a_malformed_resource_is_skipped_and_counted(certs, tmp_path):
    """One bad resource must not hide the good ones -- the posture UnparseableSegmentError
    already takes for a bad HL7 segment -- but the count comes back, not a log line."""
    certfile, keyfile, ca = certs
    key_path, _ = rsa_keypair(tmp_path)
    bundles = _patient_bundle()
    bundles["/DocumentReference?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle(
        document_reference("d1"), {"resourceType": "DocumentReference", "id": "broken"}
    )
    bundles["/DiagnosticReport?patient=Patient%2Fp1&date=ge2026-01-01"] = bundle()
    with fhir_server(certfile, keyfile, ServerBehaviour(bundles=bundles)) as (base, _b):
        registry = _registry(base, key_path, ca)
        got = find_candidate_documents(registry, registry.get("example-med"), mrn="MRN1", since=_SINCE)
    assert len(got.resources) == 1
    assert got.skipped_malformed == 1
```

- [ ] **Step 2: Run and watch it fail**

`./.venv/Scripts/python.exe -m pytest tests/test_documents.py -k "next_link or both_resource" -v` → FAIL, `cannot import name 'find_candidate_documents'`

- [ ] **Step 3: Write the walk**

Append to `documents.py`:

```python
# A page budget for the whole call, not per resource type, so two types cannot quietly double
# it. Exceeding it raises: a truncated search that reports "nothing further" is the same false
# negative as never having searched.
MAX_PAGES = 20
MAX_RESOURCES = 500


class PaginationRefused(ReferralLoopError):
    """A next link led somewhere it should not, or the walk ran past its budget."""


def _next_url(bundle: Mapping[str, object]) -> str | None:
    links = bundle.get("link")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("relation") == "next":
            url = link.get("url")
            return url if isinstance(url, str) and url else None
    return None


def _walk(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    first_url: str,
    *,
    budget: list[int],
) -> tuple[list[Mapping[str, object]], list[str], int]:
    """Follow next links, returning (resources, urls_visited, pages).

    `budget` is a one-element list shared across both resource-type walks, so the cap applies to
    the call rather than to each type.
    """
    collected: list[tuple[Mapping[str, object], int]] = []
    visited: list[str] = []
    url: str | None = first_url
    pages = 0

    while url is not None:
        if budget[0] <= 0:
            raise PaginationRefused(
                f"{profile.connector_id}: exceeded {MAX_PAGES} pages. Refusing rather than "
                "returning a truncated result, which would read as 'nothing further'."
            )
        if url in visited:
            raise PaginationRefused(
                f"{profile.connector_id}: next link points at itself ({url}); pagination loop"
            )

        # check_allowed runs inside fetch, so a next link off the allowlist refuses here rather
        # than being followed. It is a URL the remote chose.
        try:
            payload = _authorized_get(registry, profile, url)
        except EgressRefused as exc:
            raise PaginationRefused(
                f"{profile.connector_id}: next link left the allowlist: {exc}"
            ) from exc

        visited.append(url)
        budget[0] -= 1
        pages += 1
        # Paired with the page it came from, so FetchedResource.page is the real page rather
        # than arithmetic over an index -- provenance that is guessed is not provenance.
        collected.extend((resource, pages) for resource in _entries(payload))
        if len(collected) > MAX_RESOURCES:
            raise PaginationRefused(
                f"{profile.connector_id}: more than {MAX_RESOURCES} resources; refusing rather "
                "than truncating"
            )
        url = _next_url(payload)

    return collected, visited, pages


def find_candidate_documents(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    *,
    mrn: str,
    since: datetime,
    until: datetime | None = None,
) -> DocumentSearch:
    """Hop 1 then hop 2, for both readable resource types."""
    patient_id = resolve_patient(registry, profile, mrn=mrn)

    window = f"&date=ge{since.date().isoformat()}"
    if until is not None:
        window += f"&date=le{until.date().isoformat()}"

    budget = [MAX_PAGES]
    resources: list[FetchedResource] = []
    # The real hop-1 URL, not a reconstruction: provenance that is approximated is not
    # provenance, and this is the string a coordinator reads to see what was actually asked.
    urls: list[str] = [patient_search_url(profile, mrn)]
    skipped = 0
    pages_walked = 0

    for kind in READABLE_TYPES:
        first = (
            f"{profile.fhir_base_url}/{kind}"
            f"?patient={quote('Patient/' + patient_id, safe='')}{window}"
        )
        found, visited, pages = _walk(registry, profile, first, budget=budget)
        urls.extend(visited)
        pages_walked += pages
        for raw, page in found:
            try:
                validate(raw)
            except ResourceMalformed as exc:
                # Skipped and counted, not fatal. One malformed resource must not hide the
                # others -- and the count returns on the result rather than only in a log.
                logger.warning("%s: discarding a malformed resource: %s", profile.connector_id, exc)
                skipped += 1
                continue
            resources.append(
                FetchedResource(
                    resource_type=kind,
                    resource=raw,
                    connector_id=profile.connector_id,
                    query_url=first,
                    page=page,
                )
            )

    return DocumentSearch(
        resources=tuple(resources),
        patient_id=patient_id,
        pages_walked=pages_walked,
        skipped_malformed=skipped,
        query_urls=tuple(urls),
    )
```

Add to the imports at the top of `documents.py`:

```python
from .egress import EgressRefused, Response
from .resources import READABLE_TYPES, DocumentSearch, FetchedResource, ResourceMalformed, validate
```

- [ ] **Step 4: Run and watch it pass**

`./.venv/Scripts/python.exe -m pytest tests/test_documents.py -v` → all pass.

- [ ] **Step 5: Prove the allowlist refusal is real**

The off-allowlist test must fail for the right reason. Confirm the server never saw a request for the evil host:

```bash
cd "$WORKTREE"
./.venv/Scripts/python.exe -m pytest tests/test_documents.py -k off_the_allowlist -v
```

Then temporarily change `_walk` to catch and ignore `EgressRefused` instead of re-raising, re-run, and confirm the test **fails** — proving it pins the refusal rather than passing for an unrelated reason. Restore with a Python string-replace, not `git checkout`.

- [ ] **Step 6: Commit**

```bash
cd "$WORKTREE"
git add src/referral_loop/connect/documents.py tests/test_documents.py
git commit -m "feat(connect): the pagination walk, bounded and allowlisted

Every next link is a URL the remote chose, so each one goes through check_allowed like anything
else -- and off-allowlist refuses loudly rather than ending the walk, because a silent stop
hands back a truncated result set wearing the costume of a complete one.

The page budget is shared across both resource types rather than applied per type, so querying
two cannot quietly double it. Exceeding it raises: a truncated search reporting 'nothing
further' is the same false negative as never having searched.

A malformed resource is skipped and counted, the posture UnparseableSegmentError already takes
for a bad HL7 segment. The count returns on the result."
```

---

### Task 8: The `connectors` report says which endpoints are queryable

**Files:** modify `src/referral_loop/cli.py`, `tests/test_connectors_cli.py`

Spec §3.5 — the incapability has to be visible at configuration time, not discovered by the first query.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connectors_cli.py`:

```python
def test_the_report_says_which_connectors_can_be_queried(tmp_path, monkeypatch, capsys):
    """A preflight-only connector is a legitimate configuration. What must not happen is
    discovering it by a query that comes back empty."""
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = _connector_file(tmp_path)
    main(["connectors", "--connectors", str(path)])
    out = capsys.readouterr().out
    assert "preflight-only" in out
```

- [ ] **Step 2: Run and watch it fail**

`./.venv/Scripts/python.exe -m pytest tests/test_connectors_cli.py -k queried -v` → FAIL on the assertion.

- [ ] **Step 3: Add the line to the report**

In `cli.py`'s `_run_connectors`, after the peer cross-check block and before `preflight(registry)`:

```python
    unqueryable = [c.connector_id for c in registry.connectors if not c.is_queryable]
    if unqueryable:
        # Stated at configuration time rather than discovered by a query returning nothing.
        # These connectors can prove they are reachable and that our credentials work, and
        # cannot be asked about a patient at all.
        print(
            "preflight-only (no identifier_systems.mrn, cannot be queried for documents): "
            + ", ".join(unqueryable)
            + "\n"
        )
```

- [ ] **Step 4: Run and watch it pass**

`./.venv/Scripts/python.exe -m pytest tests/test_connectors_cli.py -v` → all pass.

- [ ] **Step 5: Commit**

```bash
cd "$WORKTREE"
git add src/referral_loop/cli.py tests/test_connectors_cli.py
git commit -m "feat(cli): the report names the connectors that cannot be queried

A preflight-only connector proves reachability and credentials and cannot be asked about a
patient. That is a legitimate configuration; discovering it from a document search that comes
back empty is not. Stated where the rest of the configuration is checked."
```

---

### Task 9: Verify

**Files:** none.

- [ ] **Step 1: Check the tree**

`git log --oneline -12` and `git status --short`. A second agent is executing Plan 2b on `main`; confirm you are still on `fhir-read-client` and that nothing unexpected landed here.

- [ ] **Step 2: Full suite**

```bash
cd "$WORKTREE"
./.venv/Scripts/python.exe -m pytest tests/ -q -m "not docker" 2>&1 | tail -5
```

Compare against the baseline recorded before Task 1. Every pre-existing test must still pass; report the arithmetic explicitly.

- [ ] **Step 3: Lint and types**

```bash
./.venv/Scripts/ruff.exe check src/ tests/
./.venv/Scripts/mypy.exe src/referral_loop/ --ignore-missing-imports --check-untyped-defs --warn-unused-ignores
```

Both clean.

- [ ] **Step 4: Confirm the layering still holds**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_import_closure.py -v
git diff --stat <sha-before-task-1>..HEAD -- src/referral_loop/registry.py src/referral_loop/store.py src/referral_loop/listener.py src/referral_loop/matcher.py src/referral_loop/peers.py src/referral_loop/core/
```

The diff must be **empty** — including `core/`, which this plan must not touch and which the other agent is actively changing.

- [ ] **Step 5: Walk the definition of done** below, item by item, with evidence for each.

## Definition of done

- [ ] `Response` carries headers; `header()` is case-insensitive; both `fetch` branches populate it
- [ ] `identifier_systems` validated when present; empty string refuses; unknown key refuses
- [ ] `is_queryable` false for a connector with no declared MRN system
- [ ] `find_candidate_documents` raises for: no declared system, patient unknown, patient ambiguous
- [ ] a resolved patient with nothing filed returns an **empty** `DocumentSearch` — the only correct empty
- [ ] both `DocumentReference` and `DiagnosticReport` are queried, sharing one page budget
- [ ] no chained `patient.identifier` parameter is ever sent
- [ ] every `next` link passes `check_allowed`; off-allowlist refuses loudly, proven non-vacuous
- [ ] a self-referential `next` refuses; the page and resource caps raise rather than truncate
- [ ] 429/503/transport retried with bounded attempts; `Retry-After` honoured and capped; 400 never retried
- [ ] the MRN does not appear in `caplog` after a failure carrying `diagnostics`
- [ ] malformed resources skipped and counted on the result
- [ ] `connectors` report names preflight-only connectors
- [ ] full suite green with the pre-existing count unmoved; `ruff` and `mypy` clean
- [ ] `git diff` over the five frozen files **and `core/`** is empty

## Out of scope

US Core profile validation · Synthea fixtures · TEFCA/QHIN exchange · any write to a remote ·
cross-organization identity resolution · mapping onto the canonical model · scheduling these
queries from the aging agent · `Binary` retrieval of the document content itself.
