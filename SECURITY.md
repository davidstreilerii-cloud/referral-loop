# Security Policy

## What this project is

`referral-loop` tracks whether healthcare referral loops close. It ingests HL7 v2 over MLLP and
reads FHIR, and **it persists PHI by design** — `raw_messages.payload` holds verbatim HL7, because
a rule-pack revision cannot be evaluated against traffic that was not retained.

It is **not deployed to a live site.** It has been built and tested against synthetic HL7 traffic
and a test FHIR server — so no finding here has ever been exercised against real patient data, and
no control here has been proven under production load. Anyone considering it against real patient
data should read [docs/security-model.md](docs/security-model.md) first and treat the open items
below as blocking until they have been assessed for that deployment.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository: the *Security* tab →
*Report a vulnerability*. That opens a private advisory visible only to the maintainer.

Please do not open a public issue for a security problem.

**Include:** what you did, what happened, what you expected, and the affected file and version or
commit. A failing test or a `curl`/message that reproduces it is the fastest possible report.

**What to expect:** this is a single-maintainer project, not a funded product. Acknowledgement is
best-effort and typically within a week. There is no bounty. Credit in the advisory if you want it.

## Scope

**In scope** — anything reachable through the surfaces this project actually exposes:

- The MLLP listener and HL7 parser, including malformed or hostile message handling
- Peer identity and mutual TLS (`peers.py`, `mllp_server.py`)
- The rule-pack signature check (`pack.py`) — in particular any path that loads an unsigned,
  wrongly-signed, or substituted pack
- The egress allowlist (`connect/egress.py`) and the SMART Backend Services credential flow
- The append-only audit store and its tamper detection
- PHI reaching anywhere it should not: logs, temp files, error messages, world-readable files
- The coordinator worklist

**Out of scope:**

- The worklist has **no authentication** and refuses any non-loopback bind. That is a documented
  design position for a single-site v1, not an oversight — reports that it is unauthenticated will
  be closed as known. Reports that it can be made to bind non-loopback, that a rebound `Host` or a
  cross-origin request gets through, or that a reverse proxy configuration defeats those checks,
  are in scope and wanted.
- Findings that require an attacker who already has local filesystem access as the service user.
  That account can read the PHI database by design.
- Denial of service against a deployment that has published its MLLP port to an untrusted network.
  The documented deployment publishes 2575 only to the interface engine.

## Known open items

Disclosed rather than hidden. All are tracked in the design specs under `docs/superpowers/specs/`.

- **No logging scrubber.** `cli.py` configures logging with no filter attached, and there is no
  `logging.Filter` in `src/`. PHI is kept out of log records at the raise sites instead, which
  covers the enumerated refusal paths rather than every future call site. **Treat any new logging
  call site as unprotected by default.**
- **Audit writes fail open.** If the audit database cannot be written, the action still proceeds
  and an `ERROR` is logged naming the action and the exception *type* (never the message, which
  would carry the database path), plus a running count of drops. The reasoning is in `audit.py`'s
  module docstring: a wedged safety worklist is a worse outcome than a dropped audit row, and the
  action itself is still recorded in `loop_events`. Deployments with a hard audit-completeness
  requirement should treat this as a gap to close, not a setting to rely on.
- **Unbounded tables.** `applied_messages` and the MRN alias tables are excluded from retention by
  design — deleting either re-arms a real safety failure — so both grow for the life of the
  install. Run `referral-loop stats` for this deployment's actual figures.

## Security properties that are tested, not asserted

These have enforcing tests; if one regresses, the build fails:

| Property | Enforced by |
|---|---|
| No model client or ML stack in the built image | `tests/test_install_closure.py`, against the built image |
| Egress confined to one module | `tests/test_import_closure.py` |
| Audit store rejects `UPDATE`/`DELETE` | SQLite authorizer, `tests/test_audit.py` |
| PHI databases created owner-only (`0600`) | `tests/test_phi_file_modes.py` |
| MRNs absent from refusal log records | `tests/test_phi_in_logs.py` |
| Worklist refuses a rebound `Host`, or none | `tests/test_worklist.py` |
| State-changing worklist requests prove same origin | `Origin` + `Sec-Fetch-Site`, `tests/test_worklist.py` |

A property described in prose but not in this table should be read as design intent, not as an
implemented control.

**One limit on the audit row above, stated because the code states it:** the SQLite authorizer stops
accidental and external modification, not a deliberate caller inside this process —
`conn.set_authorizer(None)` is one line of Python. It is a guardrail against a mistake becoming
permanent, not a defense against code you have already chosen to run.
