# referral-loop — Build Log

## 2026-08-22 — pre-publication documentation correction

Documentation only. No file under `src/` or `tests/` was touched.

Ten false or stale claims, found by four independent reviewers, corrected at the source:

- **`SIU^S15` described as an open defect** in `README.md` and `docs/security-model.md`. It has been
  closed since `b702547` — `S15` routes to `unschedule`, the store maps that to `OPEN`, and
  `test_an_s15_then_an_s12_reschedules_through_the_listener` holds it. Both documents now tell the
  real sequence, including the second defect the fix exposed (content-key dedup swallowing a
  rebooking that kept its order numbers) and its fix. `2026-08-05-appointment-identifier-dedup-design.md`
  still read "approved, not yet implemented"; it is implemented. The kernel spec's `Plan 2c item`
  marker now carries a closure note.
- **§11.5's PHI scrubber.** "All logging routes through a scrubber that also strips PHI" — no such
  scrubber exists. The correction already existed downstream in the connector-registry spec and
  cited §11.5 by number, so the repo held a correction pointing at an uncorrected original. Applied
  at the source.
- **§11.4's "enforced by test."** The described AST check does not exist. Every query *is*
  parameterized — audited twice — so the property holds; the enforcement did not. Restated as
  audited, with the note that `store.py`'s code-derived `DROP TRIGGER` f-string is exactly what the
  described check would have flagged.
- **The BSeR / 360X claim was falsifiable.** The BSeR IG uses the word "harmonization" verbatim and
  ships `CodeSystem/TaskBusinessStatusCS` numbered to the 360X diagram. Restated as the defensible
  version — the harmonization is one-directional — which preserves the original point. BSeR's scope
  corrected to "preventive or therapeutic."
- **Dockerfile capacity figures deleted.** `~166 bytes/row … including their indexes` is not
  reachable from anything this codebase can measure (`stats`'s own docstring says why: `dbstat` is
  not compiled in, and the index-inclusive floor is nearer 209 B/row), and the six growth
  projections built on it under-projected by 1.3–1.8×. Structural point and the pointer to
  `referral-loop stats` kept; invented precision removed.
- **`Example Radiology`** in `README-technical.md`'s `peers.json` example — real practices trade
  under that name, the same class of problem already fixed once for the connector example. Now
  `example-radiology` / `Example Radiology Group`.
- **§11.6a's commit SHAs.** 17 of ~19 do not resolve; `git filter-repo` rewrote every pre-extraction
  hash. One sentence added saying so. Not remapped individually — a hand-remapped table is one
  nobody can check against the audit it came from. `729ffb6` postdates the extraction and resolves.
- **Standards drift.** Joint Commission's goal is **NPG.01.02.01** for Hospital/Critical Access as
  of 2026-01-01 and remains **NPSG.02.03.01** in the Laboratory program; both are now named.
  `README-technical.md`'s `system/Patient.read` gained the SMART v1-vs-v2.2.0 `.cruds` caveat the
  connector-registry spec already carried.
- **Line counts recounted:** src 16,557 and tests 23,857, against `~16,000` / `~22,500` claimed.
  Now `~16,500` / `~23,900` in both `README.md` and `docs/security-model.md`.
- **The FHIR read client was presented as a shipped capability.** `find_candidate_documents` and
  `resolve_patient` have no caller in `src/` — only tests. The only production wiring of `connect/`
  is `connectors` preflight. `README.md` now scopes it: built and tested against a test FHIR
  server, reconciliation job is v2.
- **Dangling parent-repo references.** `guardrails/*`, `db.py`, `audit_trail.py`,
  `AnthropicClientError`, Revenue Integrity. The two pre-extraction documents that name them now
  open with a note saying which were vendored, which do not exist here, and that their commit
  hashes will not resolve.

Verification: every claim was checked against the file it describes before editing —
`listener.py:1188` (`CANCEL_TYPE: self._apply_unschedule`), `store.py:596`
(`"unscheduled": LoopState.OPEN`), `listener.py:250` (`_CONTENT_KEY_VERSION = "rl-content-v2"`),
`rules/pack.json` (`"appointment_id":["SCH-1","SCH-2"]`), `cli.py:633` (`logging.basicConfig`, no
filter), `store.py:754` (the `DROP TRIGGER` f-string), `store.py:2422` (`dbstat` not present in this
build), `connect/auth.py:205` (scopes joined verbatim into `scope`), and
`find -name '*.py' | xargs wc -l` for both line counts. The suite was not re-run: nothing executable
changed.

## 2026-08-20 — release baseline

Measured suite state:

```
1805 passed, 2 skipped, 11 deselected in 826.78s (0:13:46)
TOTAL                     4455    207    95%
Required test coverage of 90.0% reached. Total coverage: 95.35%
```

Command (exit code 0):

```bash
REFERRAL_THRESHOLDS_ACCEPTED=1 python -m pytest tests/ -q --no-header \
  -m "not docker" --timeout=120 --cov=referral_loop --cov-report=term-missing
```

**Running the suite — three things worth knowing:**

- Use the invocation above, not bare `pytest`. `tests/test_install_closure.py` is
  `@pytest.mark.docker` and requires a Docker daemon; those are the 11 deselected tests. The
  `image` CI job covers them against a real built image.
- Expect roughly 14 minutes. `test_spec_12_and_13_the_whole_suite_runs_under_both_guards`
  re-runs the whole suite in a guarded subprocess and dominates the wall clock. That is by design.
- Never run with a per-test timeout below 120s, or the guarded meta-test above fails spuriously.

**Coverage:** the floor is `fail_under = 90` in `pyproject.toml`, deliberately set five points
under the measured value as platform margin rather than slack. `encryption_check.py` has
per-platform OS-detection branches, so a Linux runner covers a different subset than win32. A CI
figure between 90 and 95 is that difference, not a regression — check which lines moved before
adjusting the floor.

## 2026-08-20 — pre-publication pass

Suite after the M3/M4 fixes:

```
1815 passed, 5 skipped, 11 deselected in 795.91s (0:13:15)
Required test coverage of 90.0% reached. Total coverage: 95.55%
```

- Closed audit findings M3 (owner-only PHI files) and M4 (MRNs out of logs), `729ffb6`.
- Corrected a false claim that logging routes through a PHI scrubber. No such control exists
  in this codebase; the M4 fix keeps PHI out of the records instead.
- Renamed the connector worked-example from a real named health system to `example-med`.
- Added Apache-2.0 LICENSE; hiring-facing README and `docs/security-model.md` are the landing
  docs, with the previous technical README preserved as `README-technical.md`.

## 2026-08-22 — independent-review pass

Four reviewers (security, architecture, claims-vs-code, fabrication) read the tree before
publication. Suite after their findings were fixed:

```
1889 passed, 5 skipped, 11 deselected in 951.53s (0:15:51)
Required test coverage of 90.0% reached. Total coverage: 95.86%
```

**Controls that were advertised but not fully enforced.** The egress AST check recorded
`urllib` rather than `urllib.request` for `from`-imports, so `from urllib import request` —
the most natural alternate spelling of the exact import being policed — passed it. Third-party
HTTP clients were in no denylist at all. The `CLOSED`-unreachability sweep claimed "every
public mutating call" and had stopped covering three of them. The whole-suite meta-test's
anti-vacuity floor sat at 700 against a suite of 1,800+. All three now enforce what they say,
and a completeness test fails the build if a new `Registry` mutator joins neither list.

**A boot gate that could pass on the wrong volume.** `encryption_check` derived the Windows
drive from `CHROMA_DB_PATH` — an environment variable belonging to a vector database this
project asserts it never imports, and the only occurrence of that name in the tree. The gate
never received the database path at all, so it inspected `C:` regardless of where PHI landed,
and the Linux branch substring-matched `crypt` across all of `lsblk`. It now takes the resolved
path, identifies the backing device, and treats anything it cannot identify as unencrypted.

**Claims that were true when written.** The `SIU^S15` defect was fixed in `b702547` and both
landing documents still called it open. A PHI-scrubber claim was corrected downstream while
its source at §11.5 stayed uncorrected. §11.4 claimed an AST test that does not exist. A
capacity figure claimed index-inclusive measurement this codebase cannot produce. The IHE
360X / BSeR harmonization was called nonexistent when the BSeR IG says verbatim that it is one.

**No fabricated data.** The audit that hunted it found two data artifacts totalling 807 bytes,
both synthetic or cryptographic, and `data/` correctly gitignored. 30+ external citations
verified against source.

## 2026-08-23 — due-diligence pass

```
1908 passed, 5 skipped, 11 deselected in 936.42s (0:15:36)
Required test coverage of 90.0% reached. Total coverage: 95.92%
ruff: All checks passed   mypy: no issues in 40 source files
```

**Retryability is a type property, not prose.** `ReferralLoopError.retryable` defaults False;
all 25 subclasses declare it in their own class body and a test fails the build if one inherits
the default silently. The listener dispatches on the attribute rather than a class ladder, so a
new error class can no longer join the fail-open side by being forgotten. The bare
`except Exception` stays fail-open on purpose: AE on a message that will fault identically on
every redelivery takes the whole feed off the air rather than costing one message.

**A fourth boot gate: a writable audit trail.** Gated on the resolved path, not on the variable,
so a source checkout still boots and an installed deployment is told at boot instead of finding
an empty trail later. It runs before the pack gate because `load_pack` is itself audited —
ordered last, the gate dirtied the counter it exists to protect on its way to refusing.

**The counters are reachable.** New `health` mode reports every handler counter plus the audit
trail's path and write-failure count; a listener logs the same on shutdown. `counters()` derives
from `vars(self)`, so one added later cannot be left out of the report.

**`eval` no longer writes site PHI to the OS temp directory**, and `replay` refuses a
site-derived corpus given no scratch location — enforced where the property lives rather than by
its one caller.

**Connection slots return on every path.** Release moved to `shutdown_request`, keyed on the
admission rather than the address, verified against this interpreter's socketserver for the
double-release case a BoundedSemaphore would raise on.

**`rebuild` is a CLI mode.** It recovers a worklist that a restore left empty. Encryption gate
yes, pack gate no: the moment a site needs it is after a disaster, which is not the moment to
discover where the signing key was.

Also: `clock.as_utc` replaces six identical private copies (and the layering claim in clock.py's
docstring now has a test, which it did not before); every dangling reference to the extracted-from
codebase is gone from `src/`; the HL7 v2.7 fifth encoding character is named as considered and
refused rather than implied not to exist; every organization in examples is now Example-prefixed.
