# Referral Loop Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract `healthcare_rag/referral_loop/` from the healthcare-rag monorepo into a standalone private repo named `referral-loop`, preserving git history, with **zero behaviour change** and a provably identical test result.

**Architecture:** `git filter-repo` rewrites a fresh clone down to the subsystem's paths, preserving all 50 commits that touch it. The package moves from `healthcare_rag/referral_loop/` to `src/referral_loop/` and becomes top-level. Two couplings to the parent repo are severed by vendoring: `guardrails/immutable_audit.py` (currently loaded by file path, not import) and `encryption_check.py`. Test-side coupling — a cross-module `PACK` import, a `parents[2]` repo root, and closure tests naming parent modules — is repointed. Nothing else changes.

**Tech Stack:** Python 3.12.10, git 2.53.0, pytest, ruff, `git-filter-repo` (must be installed), Docker (optional, for the install-closure test), `cryptography` and `flask` as the only two runtime dependencies.

---

## Why extraction is its own plan

The kernel redesign in the spec (canonical model, eleven-state machine, `Transition` objects, provenance projection, ingest remapping) is **Plan 2**. It is deliberately not in this plan.

Moving 11,752 lines of production code and 17,356 lines of tests across a history rewrite is already a change with real failure modes. Redesigning the state machine in the same plan means that when the suite goes red you cannot tell whether the move broke it or the redesign did. This plan ends with a standalone repo whose suite passes with **the same test count as the parent repo**, which is the baseline Plan 2 needs.

## Baseline facts (verified 2026-08-01 at `be81776`)

- `python -m pytest tests/referral_loop -q` → **1046 passed, 11 skipped**; collected-test list is **1057** lines *(re-measured 2026-08-01 at `202cd4a`; the plan was drafted against `be81776`, since when four tests landed. Every count in this plan is the corrected one.)*
- 50 commits touch `healthcare_rag/referral_loop`; 456 commits in the repo total
- `git filter-repo --version` → **not installed** (Task 1 fixes this)
- Only **two** production escapes from the package:
  - `healthcare_rag/referral_loop/cli.py:57` — `from healthcare_rag.encryption_check import verify_encryption_at_rest`
  - `healthcare_rag/referral_loop/audit.py:288-289` — loads `healthcare_rag/guardrails/immutable_audit.py` via `importlib.util.spec_from_file_location`, registering it in `sys.modules` under `"healthcare_rag.guardrails.immutable_audit"`. **This is not an import statement**, so a grep for imports misses it.
- No relative import escapes the package (`from ..` appears only in a docstring at `audit.py:74`)

## File structure

**Source repo paths preserved by filter-repo:**

| path | becomes |
|---|---|
| `healthcare_rag/referral_loop/` | `src/referral_loop/` |
| `tests/referral_loop/` | `tests/` |
| `Dockerfile.referral` | `Dockerfile` |
| `scripts/sign_referral_pack.py` | `scripts/sign_pack.py` |
| `docs/superpowers/specs/2026-07-25-referral-loop-design.md` | same path |
| `docs/superpowers/specs/2026-07-31-referral-kernel-design.md` | same path |
| `docs/superpowers/plans/2026-07-25-referral-loop.md` | same path |

**Files created new in the extracted repo:**

| file | responsibility |
|---|---|
| `src/referral_loop/immutable_audit.py` | vendored append-only audit store (was `healthcare_rag/guardrails/immutable_audit.py`) |
| `src/referral_loop/encryption_check.py` | vendored at-rest encryption boot gate |
| `pyproject.toml` | new; two runtime deps, not the parent's fifteen |
| `tests/_pack.py` | the shared test `RulePack`, moved out of `test_matcher.py` |
| `.github/workflows/ci.yml` | test + lint + typecheck |
| `README.md` | what this is, how to run it |

**Not preserved** (parent-repo-only, or belongs to another subsystem): `INTEROP_SPEC.md` (unverified scope — see Task 3 note), `command_center/`, `tests/test_immutable_audit.py`, `healthcare_rag/interop_store.py`, everything under `scripts/` except the pack signer.

---

### Task 1: Install git-filter-repo

**Files:** none — tooling only.

- [ ] **Step 1: Confirm it is missing**

Run: `git filter-repo --version`
Expected: `git: 'filter-repo' is not a git command. See 'git --help'.`

- [ ] **Step 2: Install it**

```bash
python -m pip install git-filter-repo
```

- [ ] **Step 3: Verify it is now on PATH**

Run: `git filter-repo --version`
Expected: a version string, e.g. `2.47.0`. If it still reports "not a git command", the pip scripts directory is not on PATH — find it with `python -c "import sysconfig; print(sysconfig.get_path('scripts'))"` and add it.

- [ ] **Step 4: Verify git version supports it**

Run: `git --version`
Expected: `git version 2.53.0.windows.2` or later. filter-repo requires git >= 2.24.

No commit — this task changes no files.

---

### Task 2: Capture the parity baseline

**Files:**
- Create: `$SCRATCH/baseline.txt`

This is the number Task 12 must reproduce. Capture it before touching anything.

- [ ] **Step 1: Confirm the parent repo is clean and on the right commit**

```bash
git -C "$UPSTREAM_REPO" status --short
git -C "$UPSTREAM_REPO" rev-parse HEAD
```

Expected: only `?? INTEROP_SPEC.md` and `?? verify_reports/` untracked; HEAD is `be81776` or a later commit on `feature/referral-loop`.

- [ ] **Step 2: Run the suite and record the result**

```bash
cd "$UPSTREAM_REPO"
python -m pytest tests/referral_loop -q 2>&1 | tail -3 | tee "$SCRATCH/baseline.txt"
```

Expected: `1042 passed, 11 skipped in ~420s`

- [ ] **Step 3: Record the collected-test list, not just the count**

A matching count with different tests is not parity.

```bash
cd "$UPSTREAM_REPO"
python -m pytest tests/referral_loop --collect-only -q 2>&1 | grep "::" | sed 's|^tests/referral_loop/||' | sort > "$SCRATCH/baseline-tests.txt"
wc -l < "$SCRATCH/baseline-tests.txt"
```

Expected: `1053` (1042 passed + 11 skipped).

No commit — these files live in the scratchpad, not the repo.

---

### Task 3: Extract with filter-repo

**Files:** creates `$REPO/` as a new git repo.

`filter-repo` refuses to run on a repo with unstaged changes and, by default, on a non-fresh clone. Work from a fresh clone so the parent repo is never at risk.

- [ ] **Step 1: Make a fresh clone to filter**

```bash
cd "$HOME"
git clone --no-local --branch feature/referral-loop "$UPSTREAM_REPO" referral-loop-filtering
cd referral-loop-filtering
git log --oneline -1
```

Expected: the clone succeeds and HEAD matches the parent's `feature/referral-loop`.

`--no-local` forces a real object copy rather than hardlinks, so filtering cannot corrupt the source repo's object store.

- [ ] **Step 2: Filter to the subsystem paths, renaming as we go**

```bash
cd "$FILTER_WORKSPACE"
git filter-repo --force \
  --path healthcare_rag/referral_loop/ \
  --path tests/referral_loop/ \
  --path Dockerfile.referral \
  --path scripts/sign_referral_pack.py \
  --path docs/superpowers/specs/2026-07-25-referral-loop-design.md \
  --path docs/superpowers/specs/2026-07-31-referral-kernel-design.md \
  --path docs/superpowers/plans/2026-07-25-referral-loop.md \
  --path-rename healthcare_rag/referral_loop/:src/referral_loop/ \
  --path-rename tests/referral_loop/:tests/ \
  --path-rename Dockerfile.referral:Dockerfile \
  --path-rename scripts/sign_referral_pack.py:scripts/sign_pack.py
```

Expected: filter-repo prints a progress bar and ends with `Completely finished after N seconds.`

Note on `INTEROP_SPEC.md`: it is untracked in the parent repo, so filter-repo cannot preserve it regardless. Copy it in manually in Task 11 if you want it, after confirming it is exclusively about this subsystem.

- [ ] **Step 3: Verify the tree and that history survived**

```bash
cd "$FILTER_WORKSPACE"
git log --oneline | wc -l
ls src/referral_loop/ | head -5
ls tests/ | head -5
ls Dockerfile scripts/sign_pack.py
```

Expected: a commit count well above 50 (filter-repo keeps every commit that touched a preserved path, across the whole 456-commit history), `src/referral_loop/` containing `store.py` etc., `tests/` containing `test_listener.py` etc.

- [ ] **Step 4: Verify blame still works — this is the whole point of using filter-repo**

```bash
cd "$FILTER_WORKSPACE"
git log --oneline -3 -- src/referral_loop/store.py
git blame -L 119,125 src/referral_loop/store.py | head -3
```

Expected: real commit SHAs and author names, not a single squashed initial commit.

- [ ] **Step 5: Move it into place and re-init the branch name**

```bash
cd "$HOME"
mv referral-loop-filtering referral-loop
cd referral-loop
git branch -m main
git log --oneline -1
```

Expected: branch is `main`, HEAD is the last commit that touched a preserved path.

- [ ] **Step 6: Commit nothing yet — verify the working tree is clean**

Run: `git -C "$REPO" status --short`
Expected: empty output. filter-repo commits its own rewrite; there is nothing to add.

---

### Task 4: Vendor immutable_audit

**Files:**
- Create: `$REPO/src/referral_loop/immutable_audit.py` (copy of the parent's, with the DB path made package-relative)
- Modify: `$REPO/src/referral_loop/audit.py` (the `_MODULE_NAME`/`_MODULE_PATH` block near line 288)
- Test: `$REPO/tests/test_audit.py`

`audit.py` currently loads the parent's module **by file path**, not by import:

```python
_MODULE_NAME = "healthcare_rag.guardrails.immutable_audit"
_MODULE_PATH = Path(__file__).resolve().parent.parent / "guardrails" / "immutable_audit.py"
```

That path does not exist in the extracted repo. It uses exactly four names from the module: `AUDIT_DB`, `init_audit_db()`, `log_guardrail_event(GuardrailAuditEvent(...))`, and `export_audit_trail(tenant_id=...)`.

- [ ] **Step 1: Copy the module in**

```bash
cp "$UPSTREAM_REPO/healthcare_rag/guardrails/immutable_audit.py" \
   "$REPO/src/referral_loop/immutable_audit.py"
```

- [ ] **Step 2: Write the failing test for the vendored module's DB path**

The parent computed `AUDIT_DB` as repo-root-relative (`dirname(dirname(abspath(__file__)))/../data/audit_trail.db`), which resolved correctly only because of where the file sat. In the new layout that resolves outside the package.

Add to `tests/test_audit.py`:

```python
def test_the_audit_database_path_is_not_computed_from_a_parent_repo_layout():
    """The vendored module inherited a path built by walking up two directories from
    healthcare_rag/guardrails/. In this repo that walk lands outside the package, so the
    path must come from configuration or a package-relative default, never from ``..``."""
    from referral_loop import immutable_audit

    source = Path(immutable_audit.__file__).read_text(encoding="utf-8")
    assert '".."' not in source, "the audit DB path still walks up out of the package"
    assert os.path.isabs(immutable_audit.AUDIT_DB), immutable_audit.AUDIT_DB
```

- [ ] **Step 3: Run it and watch it fail**

Run: `cd "$REPO" && python -m pytest tests/test_audit.py::test_the_audit_database_path_is_not_computed_from_a_parent_repo_layout -v`
Expected: FAIL — `AssertionError: the audit DB path still walks up out of the package`

- [ ] **Step 4: Replace the path computation**

In `src/referral_loop/immutable_audit.py`, replace the `AUDIT_DB = os.path.join(...)` line (was `immutable_audit.py:77`) with:

```python
# The audit database lives beside the loop database, not inside the package. A package
# directory is read-only in a container and may be on a different volume from the one the
# encryption gate attests; putting PHI-adjacent state there would put it outside the
# boundary that gate checks. REFERRAL_AUDIT_DB is the deployment's override.
_DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "data"
AUDIT_DB = os.environ.get(
    "REFERRAL_AUDIT_DB",
    str(_DEFAULT_AUDIT_DIR / "audit_trail.db"),
)
```

Add `from pathlib import Path` to the imports if it is not already there.

- [ ] **Step 5: Run it and watch it pass**

Run: `cd "$REPO" && python -m pytest tests/test_audit.py::test_the_audit_database_path_is_not_computed_from_a_parent_repo_layout -v`
Expected: PASS

- [ ] **Step 6: Repoint audit.py at the vendored module**

In `src/referral_loop/audit.py`, replace the `_MODULE_NAME` / `_MODULE_PATH` block and the `importlib.util.spec_from_file_location` loader with a plain import. Find `_module()` and make it:

```python
def _module():
    """The append-only audit store.

    This was loaded by file path in the monorepo, to import one module out of a package
    whose ``__init__`` pulled in the whole guardrails stack. Vendored here, it is an
    ordinary sibling and the indirection is gone -- but ``_module()`` is kept as the seam
    because the tests patch ``_module().AUDIT_DB`` to redirect the database.
    """
    from . import immutable_audit

    return immutable_audit
```

Delete `_MODULE_NAME`, `_MODULE_PATH`, and the `importlib` import if nothing else uses it.

- [ ] **Step 7: Run the audit tests**

Run: `cd "$REPO" && python -m pytest tests/test_audit.py -q`
Expected: FAIL — several tests assert on `"healthcare_rag.guardrails.immutable_audit"` being in `sys.modules` (was `test_audit.py:813-831`). Task 8 fixes those. Note which fail and move on.

- [ ] **Step 8: Commit**

```bash
cd "$REPO"
git add src/referral_loop/immutable_audit.py src/referral_loop/audit.py tests/test_audit.py
git commit -m "feat: vendor the append-only audit store

Loaded by file path in the monorepo, to reach one module inside a package whose
__init__ imported the whole guardrails stack. Here it is a sibling, so the
importlib indirection is gone. The DB path no longer walks up out of the package;
it defaults beside the loop database and honours REFERRAL_AUDIT_DB."
```

---

### Task 5: Vendor encryption_check

**Files:**
- Create: `$REPO/src/referral_loop/encryption_check.py`
- Modify: `$REPO/src/referral_loop/cli.py:57`
- Test: `$REPO/tests/test_boot_gates.py`

- [ ] **Step 1: Copy the module in**

```bash
cp "$UPSTREAM_REPO/healthcare_rag/encryption_check.py" \
   "$REPO/src/referral_loop/encryption_check.py"
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_boot_gates.py`:

```python
def test_the_boot_gate_does_not_reach_outside_this_package():
    """cli.py imported the encryption gate from the monorepo. A standalone repo that
    imports a package it does not ship fails at runtime, not at test time, and only on
    the machine that lacks it."""
    source = (Path(__file__).resolve().parents[1] / "src" / "referral_loop" / "cli.py").read_text(encoding="utf-8")
    assert "healthcare_rag" not in source, "cli.py still imports from the monorepo"
```

- [ ] **Step 3: Run it and watch it fail**

Run: `cd "$REPO" && python -m pytest tests/test_boot_gates.py::test_the_boot_gate_does_not_reach_outside_this_package -v`
Expected: FAIL — `AssertionError: cli.py still imports from the monorepo`

- [ ] **Step 4: Repoint the import**

In `src/referral_loop/cli.py`, change line 57 from:

```python
from healthcare_rag.encryption_check import verify_encryption_at_rest
```

to:

```python
from .encryption_check import verify_encryption_at_rest
```

- [ ] **Step 5: Run it and watch it pass**

Run: `cd "$REPO" && python -m pytest tests/test_boot_gates.py::test_the_boot_gate_does_not_reach_outside_this_package -v`
Expected: PASS

- [ ] **Step 6: Repoint the monkeypatch targets in the boot-gate tests**

`tests/test_boot_gates.py` patches `"healthcare_rag.encryption_check._detect_os_encryption"` at lines 168, 198, 246, 330, 377, and 454. Replace every occurrence:

```bash
cd "$REPO"
python - <<'PY'
from pathlib import Path
p = Path("tests/test_boot_gates.py")
s = p.read_text(encoding="utf-8")
n = s.count("healthcare_rag.encryption_check._detect_os_encryption")
s = s.replace("healthcare_rag.encryption_check._detect_os_encryption",
              "referral_loop.encryption_check._detect_os_encryption")
p.write_text(s, encoding="utf-8")
print(f"replaced {n} occurrences")
PY
```

Expected: `replaced 6 occurrences`

- [ ] **Step 7: Run the boot-gate tests**

Run: `cd "$REPO" && python -m pytest tests/test_boot_gates.py -q`
Expected: PASS (all of them) — these tests have no other parent-repo coupling.

- [ ] **Step 8: Commit**

```bash
cd "$REPO"
git add src/referral_loop/encryption_check.py src/referral_loop/cli.py tests/test_boot_gates.py
git commit -m "feat: vendor the at-rest encryption boot gate

cli.py imported it from the monorepo. A standalone repo that imports a package it
does not ship fails at runtime on the machine that lacks it, not in CI."
```

---

### Task 6: Write the new pyproject.toml

**Files:**
- Create: `$REPO/pyproject.toml`

The parent declares fifteen runtime dependencies including `chromadb`, `sentence-transformers` and `torch` transitively. This package needs **two**. That reduction is the point of the extraction and `Dockerfile.referral` already documents it as a trap.

- [ ] **Step 1: Write the file**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "referral-loop"
version = "0.1.0"
description = "Inbound referral loop closure over HL7 v2"
requires-python = ">=3.10"
dependencies = [
    "cryptography>=42.0,<47",
]

[project.optional-dependencies]
# The coordinator worklist is an HTTP surface, not part of the kernel. Keeping it
# optional is what lets a listener-only deployment ship without a web framework.
worklist = ["flask>=3.1,<4.0"]
dev = [
    "pytest>=7.0",
    "pytest-timeout>=2.0",
    "pytest-cov>=4.0",
    "mypy>=1.8",
    "ruff>=0.4",
    "flask>=3.1,<4.0",
]

[project.scripts]
referral-loop = "referral_loop.cli:main"

[tool.setuptools.packages.find]
where = ["src"]
include = ["referral_loop*"]

[tool.setuptools.package-data]
referral_loop = ["rules/*.json", "rules/*.sig", "rules/*.md"]

[tool.pytest.ini_options]
pythonpath = ["src", "."]
markers = [
  "docker: shells out to `docker build`/`docker run`. Runs by default; deselected with -m 'not docker' by the spec 12/13 meta-test, whose in-process guards cannot reach a container anyway.",
]

[tool.coverage.run]
source = ["referral_loop"]
omit = ["tests/*", "scripts/*"]

[tool.coverage.report]
# Placeholder -- Step 3 below measures the real number and sets this just under it.
# Do not ship the parent's 40; that floor is sized for a repo of loosely-tested
# analysis modules and this package would meet it by accident.
fail_under = 40
show_missing = true

[tool.ruff]
target-version = "py312"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "I"]
ignore = ["E501"]

[tool.mypy]
python_version = "3.12"
exclude = ["tests/", "scripts/"]
```

One deliberate difference from the parent: `asyncio_mode` and `pytest-asyncio` are **dropped** — nothing in this package is async.

- [ ] **Step 2: Install in editable mode and confirm the package resolves**

```bash
cd "$REPO"
python -m pip install -e ".[dev]"
python -c "import referral_loop; print(referral_loop.__file__)"
```

Expected: a path ending `src/referral_loop/__init__.py`

- [ ] **Step 3: Confirm the entry point works**

Run: `cd "$REPO" && referral-loop --help`
Expected: usage text listing modes `listen`, `filedrop`, `worklist`, `eval`, `purge`, `stats`

- [ ] **Step 3a: Measure coverage and set the floor from the measurement**

```bash
cd "$REPO"
python -m pytest tests/ -q -m "not docker" --cov=referral_loop --cov-report=term 2>&1 | tail -3
```

Read the TOTAL percentage. Set `fail_under` in `pyproject.toml` to **five points below it**, rounded down — high enough to catch a regression, with enough slack that an unrelated refactor does not fail CI. If the measurement is below 60, stop and report it: the parent's suite covers this package heavily, so a low number means something did not get extracted.

- [ ] **Step 4: Confirm the dependency reduction is real**

```bash
cd "$REPO"
python -m pip download --no-deps --dest /tmp/rl-deps . 2>&1 | tail -2
python -c "
import tomllib, pathlib
d = tomllib.loads(pathlib.Path('pyproject.toml').read_text())
deps = d['project']['dependencies']
print('runtime deps:', deps)
assert len(deps) == 1, deps
for banned in ('chromadb', 'sentence-transformers', 'torch', 'anthropic', 'mcp'):
    assert not any(banned in x for x in deps), banned
print('OK')
"
```

Expected: `runtime deps: ['cryptography>=42.0,<47']` then `OK`

- [ ] **Step 5: Commit**

```bash
cd "$REPO"
git add pyproject.toml
git commit -m "feat: package as referral-loop with two runtime dependencies

The monorepo declared fifteen, including chromadb and sentence-transformers, so
depending on it dragged the whole ML stack in -- the trap Dockerfile.referral
documents and works around. This package needs cryptography, and flask only for
the worklist surface.

Coverage floor is 80, not the parent's 40: that floor is sized for a repo of
loosely-tested analysis modules and this package would meet it by accident."
```

---

### Task 6a: Repoint the package namespace, and prove the tests exercise `src/`

**Files:**
- Modify: every file under `$REPO/tests/` (21 modules, including `conftest.py`)
- Create: `$REPO/.gitignore`

**Added 2026-08-01 during execution.** The original plan had no task for this and it is the largest single piece of the extraction. Discovered by the Task 4/5 implementer, who could not produce a pytest summary because 22 of 23 test modules fail at import — a condition true at `99fd543`, before any of their changes.

Two independent causes:

| reference | count | where |
|---|---|---|
| `healthcare_rag.referral_loop.*` | 155 | 21 modules incl. `conftest.py` |
| `tests.referral_loop.*` | 19 | 9 modules |

**And a trap that would have silently invalidated Task 12.** `healthcare-rag 0.2.0` is pip-installed on this machine. Once `referral_loop` is importable, a module that still says `import healthcare_rag.referral_loop.store` **resolves successfully** — against the *parent's* code, not `src/`. The suite would go green while testing the wrong tree, and the parity proof would be a tautology. The uninstall in Step 1 is what makes a stale reference fail loudly instead of passing quietly.

- [ ] **Step 1: Make the parent package unimportable, so a stale reference cannot resolve**

```bash
cd "$REPO"
python -m pip uninstall -y healthcare-rag
python -c "import healthcare_rag" 2>&1 | tail -1
```

Expected: `ModuleNotFoundError: No module named 'healthcare_rag'`

If you would rather not touch the machine's global environment, create a venv instead and use it for every remaining step — but you must do one or the other. Skipping this step means Task 12 proves nothing.

- [ ] **Step 2: Count the references before changing them**

```bash
cd "$REPO"
grep -ro "healthcare_rag\.referral_loop" tests/ src/ | wc -l
grep -ro "tests\.referral_loop" tests/ | wc -l
```

Expected: `155` and `19` (approximately — record the actual numbers).

- [ ] **Step 3: Rewrite the namespace across the tests**

```bash
cd "$REPO"
python - <<'PY'
from pathlib import Path

changed = {}
for p in sorted(Path("tests").rglob("*.py")):
    s = orig = p.read_text(encoding="utf-8")
    s = s.replace("healthcare_rag.referral_loop", "referral_loop")
    s = s.replace("tests.referral_loop.", "tests.")
    s = s.replace("from tests.referral_loop import", "from tests import")
    if s != orig:
        p.write_text(s, encoding="utf-8")
        changed[str(p)] = sum(1 for a, b in zip(orig.splitlines(), s.splitlines()) if a != b)
for k, v in changed.items():
    print(f"{v:4d}  {k}")
print(f"\n{len(changed)} files changed")
PY
```

- [ ] **Step 4: Verify no reference survives**

```bash
cd "$REPO"
grep -rn "healthcare_rag" tests/ src/ --include="*.py" | grep -v "^src/referral_loop/audit.py" ; echo "exit: $?"
```

Expected: `exit: 1`, or only prose matches inside `audit.py`'s historical docstring. Any *import* or *string used as a module name* is a failure.

- [ ] **Step 5: Add a .gitignore**

Running Python here creates `__pycache__` under `src/` and `tests/`, which show up in `git status` and make it hard to tell real changes from noise.

```
__pycache__/
*.py[cod]
.pytest_cache/
.coverage
.coverage.*
htmlcov/
*.egg-info/
build/
dist/
data/
.venv/
```

`data/` is listed because `cli.py` defaults its database there and a PHI-bearing SQLite file must never be committable by accident.

- [ ] **Step 6: Confirm collection now works**

```bash
cd "$REPO"
python -m pytest tests/ --collect-only -q 2>&1 | tail -3
```

Expected: around `1057 tests collected`. Collection errors here name the remaining coupling — Tasks 7 and 8 fix those, so record the errors and continue if they are only the `PACK` import and `parents[2]` issues.

- [ ] **Step 7: Prove the tests are exercising `src/`, not a stale install**

This is the assertion that makes Task 12 meaningful. Add it to `tests/test_import_closure.py`:

```python
def test_the_suite_is_exercising_this_checkout_and_not_an_installed_copy():
    """A pip-installed copy of the monorepo satisfies `import referral_loop...` just as
    well as src/ does, so a green suite proves nothing about which tree ran. This is the
    only test that can tell the difference, and without it the parity check in the
    extraction plan is a tautology."""
    import referral_loop

    here = Path(__file__).resolve().parents[1] / "src" / "referral_loop"
    assert Path(referral_loop.__file__).resolve().parent == here, (
        f"referral_loop resolved to {referral_loop.__file__}, not {here}"
    )
```

- [ ] **Step 8: Run it and confirm it passes**

Run: `cd "$REPO" && python -m pytest tests/test_import_closure.py -k exercising -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
cd "$REPO"
git add -A
git commit -m "refactor: repoint the package namespace onto the extracted tree

155 references to healthcare_rag.referral_loop and 19 to tests.referral_loop
survived the move; 22 of 23 test modules failed at import.

The uninstall matters more than the rename. healthcare-rag was pip-installed, so
once referral_loop became importable a stale reference would have resolved -- against
the parent's code, not src/. The suite would have gone green while testing the wrong
tree. test_the_suite_is_exercising_this_checkout_and_not_an_installed_copy is what
makes the parity check mean something."
```

---

### Task 7: Move the shared test RulePack out of test_matcher

**Files:**
- Create: `$REPO/tests/_pack.py`
- Modify: `$REPO/tests/test_matcher.py` (remove the `PACK` definition, import it)
- Modify: every test file doing `from tests.referral_loop.test_matcher import PACK`

`PACK` is defined in `test_matcher.py:41-59` and imported by other test modules via the absolute path `tests.referral_loop.test_matcher`. That path no longer exists, and importing a fixture out of a sibling *test* module is fragile regardless — collecting `test_matcher.py` becomes a prerequisite for collecting others.

- [ ] **Step 1: Find every importer**

```bash
cd "$REPO"
grep -rn "test_matcher import PACK" tests/
```

Expected: a list of files, one of which is `tests/test_listener.py:72`.

- [ ] **Step 2: Create the shared module**

Copy the `PACK = RulePack(...)` literal out of `tests/test_matcher.py` into a new `tests/_pack.py`:

```python
"""The RulePack the tests run against.

This lived in test_matcher.py and was imported across test modules, which made
collecting one module a prerequisite for collecting others. It is fixture data, not a
test, so it belongs in a module pytest does not collect -- hence the leading underscore.

Deliberately not the shipped pack: the shipped pack is signature-verified and its
thresholds are a product decision. test_pack.py and test_spec_proofs.py exercise the
shipped one; everything else exercises this.
"""

from referral_loop.pack import RulePack

PACK = RulePack(
    version="test",
    confidence_floor=0.90,
    # ... copy the remaining fields verbatim from test_matcher.py:41-59
)
```

Copy the field values exactly as they are; do not retype them from memory.

- [ ] **Step 3: Repoint every importer**

```bash
cd "$REPO"
python - <<'PY'
from pathlib import Path
n = 0
for p in Path("tests").glob("*.py"):
    s = p.read_text(encoding="utf-8")
    if "test_matcher import PACK" not in s:
        continue
    s = s.replace("from tests.referral_loop.test_matcher import PACK", "from tests._pack import PACK")
    s = s.replace("from tests.test_matcher import PACK", "from tests._pack import PACK")
    p.write_text(s, encoding="utf-8")
    n += 1
print(f"repointed {n} files")
PY
```

- [ ] **Step 4: Make test_matcher import it too**

In `tests/test_matcher.py`, delete the `PACK = RulePack(...)` literal and add near the other imports:

```python
from tests._pack import PACK
```

- [ ] **Step 5: Verify no importer is left behind**

```bash
cd "$REPO"
grep -rn "test_matcher import PACK" tests/ ; echo "exit: $?"
```

Expected: no output, `exit: 1` (grep found nothing).

- [ ] **Step 6: Run the two suites that use it most**

Run: `cd "$REPO" && python -m pytest tests/test_matcher.py tests/test_listener.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd "$REPO"
git add tests/_pack.py tests/test_matcher.py tests/
git commit -m "refactor(test): move the shared RulePack out of test_matcher

It was imported across test modules by absolute path, so collecting one module was
a prerequisite for collecting others. It is fixture data, not a test."
```

---

### Task 8: Repoint the closure and spec-guard tests

**Files:**
- Modify: `$REPO/tests/test_import_closure.py`
- Modify: `$REPO/tests/test_install_closure.py`
- Modify: `$REPO/tests/test_audit.py` (lines around 813-831)
- Modify: `$REPO/tests/spec_guards.py`
- Modify: `$REPO/tests/test_spec_proofs.py`
- Modify: `$REPO/tests/test_pack.py`

These are the tests that assert *about* the parent repo. They are the enforcement mechanism for the layering, so they must keep working — pointed at the new structure.

- [ ] **Step 1: Fix the repo-root computations**

`test_spec_proofs.py` and `test_install_closure.py` both use `REPO_ROOT = Path(__file__).resolve().parents[2]`. Tests moved from `tests/referral_loop/` to `tests/`, so that is one level too many now.

```bash
cd "$REPO"
grep -rn "parents\[2\]\|parent.parent.parent" tests/
```

Change each `parents[2]` to `parents[1]`, and in `test_pack.py:73` change
`Path(__file__).parent.parent.parent / "healthcare_rag" / "referral_loop" / "rules"` to
`Path(__file__).parent.parent / "src" / "referral_loop" / "rules"`.

Same for `SHIPPED_PACK_DIR` in `test_spec_proofs.py`.

- [ ] **Step 2: Run one of them to confirm the path resolves**

Run: `cd "$REPO" && python -m pytest tests/test_pack.py -q`
Expected: PASS

- [ ] **Step 3: Update the import-closure forbidden list**

In `tests/test_import_closure.py`, the `FORBIDDEN` tuple names parent modules. The ML-stack entries still matter — they are what stops someone adding a convenience import that drags `torch` in. The `healthcare_rag.*` entries are now unreachable by construction and should be replaced with the probe's own target:

```python
_PROBE = "import referral_loop, referral_loop.errors; import json,sys; print(json.dumps(sorted(sys.modules)))"

FORBIDDEN = (
    "chromadb",
    "sentence_transformers",
    "torch",
    "transformers",
    "lightrag",
    "raganything",
    "mcp",
    "ollama",
    "anthropic",
    "healthcare_rag",
)
```

Note `anthropic` **moves from required to forbidden**. In the monorepo it was in the closure because `healthcare_rag/__init__.py` ran `claude_cli.install_shim()`; there is no such parent `__init__` here, so its presence would now mean someone added a model client. Delete the test that asserted `anthropic` is present and replace it with the reverse. Add `healthcare_rag` itself, so a stray import of the old namespace fails loudly.

- [ ] **Step 4: Run the import closure and watch it pass**

Run: `cd "$REPO" && python -m pytest tests/test_import_closure.py -v`
Expected: PASS

- [ ] **Step 5: Verify the closure test can still fail**

Temporarily add `import json` — no, that proves nothing. Add a real one:

```bash
cd "$REPO"
python - <<'PY'
from pathlib import Path
p = Path("src/referral_loop/__init__.py")
orig = p.read_text(encoding="utf-8")
p.write_text(orig + "\nimport anthropic  # MUTATION\n", encoding="utf-8")
PY
python -m pytest tests/test_import_closure.py -q 2>&1 | tail -3
```

Expected: FAIL (or an ImportError if `anthropic` is not installed — in which case install it, confirm the failure, then uninstall). Then restore:

```bash
cd "$REPO"
git checkout src/referral_loop/__init__.py
python -m pytest tests/test_import_closure.py -q 2>&1 | tail -2
```

Expected: PASS again. (This is the only place in the plan where `git checkout --` is acceptable, and only because the file has no uncommitted work.)

- [ ] **Step 6: Update test_audit.py's guardrails assertions**

Lines around 813-831 assert `"healthcare_rag.guardrails.immutable_audit" in loaded` and that the guardrails *package* is not loaded. The vendored module makes the second assertion moot — there is no package to avoid. Replace with:

```python
def test_the_audit_module_is_a_sibling_not_a_package_import():
    """The monorepo loaded this by file path to avoid importing a package whose __init__
    pulled in the whole guardrails stack. Vendored, that hazard is gone -- but the test
    stays, pointed at the property that still matters: importing audit must not pull in
    anything but the audit store."""
    loaded = _modules_after_importing("referral_loop.audit")
    assert "referral_loop.immutable_audit" in loaded
    assert not any(m.startswith("healthcare_rag") for m in loaded), sorted(
        m for m in loaded if m.startswith("healthcare_rag")
    )
```

Keep whatever subprocess helper `_modules_after_importing` corresponds to in the existing file; do not invent a new one.

- [ ] **Step 6a: Delete `test_one_module_object_whichever_import_happens_first`**

`tests/test_audit.py:830`. **This one is deleted, not repointed** — the only such case in the plan.

Its prelude does `from healthcare_rag.guardrails import immutable_audit as pkg`, then asserts `audit._module() is pkg`. The property it tested was that two different import routes — the file-path load and the package import — bound to *one* module object and therefore one `_db_lock`. Vendoring collapses those two routes into one. There is no second route left, so the test is not testing a weakened property; it is testing a property that no longer has a way to be false.

Deleting a test in an extraction whose whole discipline is zero behaviour change needs to be visible, not quiet. **This will be the single legitimate deletion in Task 12 Step 2's collected-test diff.** Note it there in advance so it reads as expected rather than as a loss.

- [ ] **Step 6b: Fix `test_boot_gates.py`'s remaining parent-repo coupling**

Task 5 Step 7 claimed this file had no coupling beyond its monkeypatch targets. That was wrong. It also has:

- `REPO_ROOT = Path(__file__).resolve().parents[2]` at line 49 → `parents[1]`
- `SHIPPED_PACK_DIR = REPO_ROOT / "healthcare_rag" / "referral_loop" / "rules"` at line 50 → `REPO_ROOT / "src" / "referral_loop" / "rules"`
- a subprocess invoking `python -m healthcare_rag.referral_loop.cli --help` at line 537 → `python -m referral_loop.cli --help`

Run `python -m pytest tests/test_boot_gates.py -q` afterwards and confirm it passes.

- [ ] **Step 7: Fix spec_guards.py's model-call guard**

`spec_guards.py:160,187` and `test_spec_proofs.py:1043` reference `healthcare_rag.claude_cli`. That module does not exist here. Spec 13 (no model calls) gets simpler — only `anthropic` needs poisoning:

```bash
cd "$REPO"
grep -n "claude_cli" tests/spec_guards.py tests/test_spec_proofs.py
```

Remove the `healthcare_rag.claude_cli` entry from the list of modules poisoned, keeping `anthropic`. Do not remove the guard itself — spec 13 is one of the properties this product sells.

- [ ] **Step 8: Update test_install_closure.py for the new Dockerfile**

`DOCKERFILE = REPO_ROOT / "Dockerfile.referral"` becomes `REPO_ROOT / "Dockerfile"`. `FORBIDDEN_DISTRIBUTIONS` contains `healthcare-rag`; that is now unreachable, but leave it — a stray dependency on the old package should still fail. `REQUIRED_PATHS` and `FORBIDDEN_PATHS` are absolute in-image paths that Task 9 changes; update them there, not here.

- [ ] **Step 9: Run everything that is not the install closure**

Run: `cd "$REPO" && python -m pytest tests/ -q -m "not docker" 2>&1 | tail -5`
Expected: mostly passing. Note the failures — `test_spec_proofs.py` may still fail because it boots through `cli.boot()` and the Dockerfile changes are not in yet. Record what fails; Task 9 and Task 10 address them.

- [ ] **Step 10: Commit**

```bash
cd "$REPO"
git add tests/
git commit -m "test: repoint the closure and spec-guard tests at the extracted layout

Tests moved up one directory, so parents[2] is now parents[1]. The forbidden-import
list keeps the ML-stack entries -- they are what stops a convenience import dragging
torch back in -- and gains healthcare_rag, so a stray reference to the old namespace
fails loudly.

anthropic moves from required to forbidden. It was in the monorepo closure because
the parent __init__ installed a CLI shim; here its presence would mean someone added
a model client."
```

---

### Task 9: Rewrite the Dockerfile for the standalone package

**Files:**
- Modify: `$REPO/Dockerfile`
- Modify: `$REPO/tests/test_install_closure.py` (path constants)

The old Dockerfile's one-COPY-per-file trick existed to pull three files out of a monorepo without installing it. That is no longer necessary — but the property it protected is, so `pip install .` must still not drag in an ML stack, and `test_install_closure.py` is what proves it.

- [ ] **Step 1: Write the new Dockerfile**

```dockerfile
# The image contains no ML stack and no model client. That is a product claim, and
# tests/test_install_closure.py asserts it against the built image rather than against
# this file -- a Dockerfile that looks right and an image that is right are different
# things.
FROM python:3.12-slim

WORKDIR /app

# Install from the package metadata, not by copying files. The monorepo could not do
# this: its `referral` extra sat inside a project whose base dependencies were chromadb
# and sentence-transformers, so `pip install .[referral]` pulled the whole stack. Here
# the base dependency list is one line long, which is the point of the extraction.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[worklist]"

RUN mkdir -p /app/data \
 && useradd --system --uid 10001 --home /app referral \
 && chown -R referral:referral /app
USER referral

# REFERRAL_AUDIT_DB is not optional here. Its default is package-relative, which in a
# source checkout resolves to <repo>/data beside the loop database -- but from
# site-packages it lands beside site-packages itself, which is read-only in this image
# and is nowhere PHI-adjacent state belongs. Unset, the container's first audit write
# fails, and audit.py swallows write failures by design.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHI_MODE=full \
    REFERRAL_AUDIT_DB=/app/data/audit_trail.db

# 2575 MLLP, 5057 coordinator worklist. The monorepo's Dockerfile said 5055, which was
# wrong -- worklist.py has defaulted to 5057 since it was written.
EXPOSE 2575 5057

# Required at runtime, no defaults: REFERRAL_PACK_PUBKEY, REFERRAL_THRESHOLDS_ACCEPTED,
# PHI_ENCRYPTION_VERIFIED. Each refuses the boot rather than assuming a value.
CMD ["referral-loop", "listen", "--host", "0.0.0.0", "--port", "2575", "--db", "/app/data/referral_loops.db"]
```

- [ ] **Step 2: Update the in-image paths the closure test asserts on**

In `tests/test_install_closure.py`, `REQUIRED_PATHS` and `FORBIDDEN_PATHS` reference `/app/healthcare_rag/...`. The package is now pip-installed into site-packages, not copied to `/app`. Change `REQUIRED_PATHS` to check the *importability* instead, which is what actually matters:

```python
# The package is installed, not copied, so asserting a path under /app no longer proves
# anything. Assert what the path check was standing in for: the module imports, and the
# rules the pack loader needs shipped with it.
REQUIRED_IMPORTS = ("referral_loop.cli", "referral_loop.store", "referral_loop.pack")
REQUIRED_PACKAGE_DATA = ("rules/pack.json", "rules/pack.sig")
```

and replace the path-existence loop with these two checks, following whatever `_run_in_image` helper the file already uses to shell into the container:

```python
def test_the_image_can_import_every_module_the_entry_point_needs():
    for module in REQUIRED_IMPORTS:
        out = _run_in_image(["python", "-c", f"import {module}; print('ok')"])
        assert out.strip() == "ok", f"{module}: {out}"


def test_the_signed_rule_pack_shipped_inside_the_image():
    """pip install ships package data only if pyproject declares it. A pack that is not
    in the image is a boot failure at a customer site, not in CI."""
    probe = (
        "import importlib.resources as r;"
        "print(sorted(p.name for p in r.files('referral_loop').joinpath('rules').iterdir()))"
    )
    out = _run_in_image(["python", "-c", probe])
    for required in REQUIRED_PACKAGE_DATA:
        assert Path(required).name in out, f"{required} missing from image: {out}"
```

Keep `FORBIDDEN_DISTRIBUTIONS` and `MAX_IMAGE_MEGABYTES` as they are.

- [ ] **Step 3: Build the image**

Run: `cd "$REPO" && docker build -f Dockerfile -t referral-loop:test .`
Expected: a successful build. If Docker is not running, skip to Step 6 and note it — the closure test skips cleanly without a daemon.

- [ ] **Step 4: Run the install closure**

Run: `cd "$REPO" && python -m pytest tests/test_install_closure.py -v`
Expected: PASS, including the size check under 300 MB.

- [ ] **Step 5: Verify the image really has no ML stack**

```bash
docker run --rm referral-loop:test pip list 2>/dev/null | sort
```

Expected: a short list containing `cryptography`, `flask`, `referral-loop`, and their transitive deps. **Not** `torch`, `numpy`, `chromadb`, `anthropic`.

- [ ] **Step 6: Commit**

```bash
cd "$REPO"
git add Dockerfile tests/test_install_closure.py
git commit -m "feat: build the image from package metadata, not by copying files

The one-COPY-per-file trick existed to pull three files out of a monorepo whose
base dependencies were chromadb and sentence-transformers, so pip install .[referral]
pulled the whole stack. With a one-line dependency list, pip install . is correct.

Fixes EXPOSE 5055 -> 5057; worklist.py has defaulted to 5057 since it was written."
```

---

### Task 10: Add CI

**Files:**
- Create: `$REPO/.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install -e ".[dev]"
      # -m "not docker" because the install-closure test needs a built image; the
      # separate `image` job below covers it. Running it here would skip silently and
      # look like coverage that is not there.
      - run: python -m pytest tests/ -q -m "not docker" --timeout=120 --cov=referral_loop --cov-report=term-missing --cov-fail-under=80
        env:
          REFERRAL_THRESHOLDS_ACCEPTED: "1"

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install ruff mypy
      - run: ruff check src/ tests/
      - run: mypy src/referral_loop/ --ignore-missing-imports --check-untyped-defs

  image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install -e ".[dev]"
      - run: docker build -t referral-loop:test .
      - run: python -m pytest tests/test_install_closure.py -v
```

- [ ] **Step 2: Verify the test command works locally exactly as CI runs it**

```bash
cd "$REPO"
REFERRAL_THRESHOLDS_ACCEPTED=1 python -m pytest tests/ -q -m "not docker" --timeout=120 --cov=referral_loop --cov-report=term-missing --cov-fail-under=80 2>&1 | tail -8
```

Expected: PASS with coverage at or above 80%. If coverage is below 80, **do not lower the floor** — report it, since the parent's suite covers this package heavily and a low number means something did not get extracted.

- [ ] **Step 3: Verify lint passes**

```bash
cd "$REPO"
ruff check src/ tests/
```

Expected: `All checks passed!` — or the two pre-existing `I001` import-order findings noted in the parent repo, which should be fixed here rather than carried over.

- [ ] **Step 4: Commit**

```bash
cd "$REPO"
git add .github/workflows/ci.yml
git commit -m "ci: test, lint, and image jobs

The image job is separate because test_install_closure.py needs a built image and
skips cleanly without a daemon -- running it in the test job would skip silently and
look like coverage that is not there."
```

---

### Task 11: Write the README

**Files:**
- Create: `$REPO/README.md`

- [ ] **Step 1: Write it**

```markdown
# referral-loop

Inbound referral loop closure over HL7 v2. Tracks a referral from order to returned
documentation, and surfaces the ones that never came back.

## What it does

Receives HL7 v2 over MLLP (mTLS required), matches inbound results to open referrals,
and maintains an append-only event log from which loop state is replayed. A coordinator
worklist shows three queues: awaiting a result, awaiting acknowledgement, and orphans.

`CLOSED` is unreachable by design in v1 — no loop closes without a human.

## Running it

    pip install -e ".[dev]"
    export REFERRAL_PACK_PUBKEY=<32-byte ed25519 public key, hex>
    export REFERRAL_THRESHOLDS_ACCEPTED=1
    referral-loop listen --db data/referral_loops.db --peers peers.yaml

Modes: `listen`, `filedrop`, `worklist`, `eval`, `purge`, `stats`.

## Required configuration

Each of these refuses the boot rather than assuming a value:

| variable | why it has no default |
|---|---|
| `REFERRAL_PACK_PUBKEY` | the rule pack is signed; a default key verifies nothing |
| `REFERRAL_THRESHOLDS_ACCEPTED` | staleness thresholds are a clinical decision per specialty |
| `REFERRAL_RAW_RETENTION_DAYS` | retention of PHI is a site policy, not a vendor default |
| `PHI_ENCRYPTION_VERIFIED` | at-rest encryption is attested by the deployment, not detected |

## Tests

    python -m pytest tests/ -q                 # everything
    python -m pytest tests/ -q -m "not docker" # skip the image closure test

The release gate is **false-match rate**, not accuracy: `referral-loop eval`.

## Provenance

Extracted from the healthcare-rag monorepo with `git filter-repo`, history preserved.
Design: `docs/superpowers/specs/2026-07-31-referral-kernel-design.md`.
```

- [ ] **Step 2: Commit**

```bash
cd "$REPO"
git add README.md
git commit -m "docs: README"
```

---

### Task 12: Prove parity with the parent repo

**Files:** none — verification only.

This is the task that says whether the extraction worked. A green suite is not enough; it must be the **same** green suite.

- [ ] **Step 1: Run the full suite**

```bash
cd "$REPO"
REFERRAL_THRESHOLDS_ACCEPTED=1 python -m pytest tests/ -q 2>&1 | tail -3
```

Expected: `1042 passed, 11 skipped` — plus the small number of tests added by Tasks 4 and 5, so `1044 passed, 11 skipped`. Any *lower* number means tests were lost in the move.

- [ ] **Step 2: Diff the collected test list against the baseline**

```bash
cd "$REPO"
python -m pytest tests/ --collect-only -q 2>&1 | grep "::" | sed 's|^tests/||' | sort > /tmp/extracted-tests.txt
diff "$SCRATCH/baseline-tests.txt" /tmp/extracted-tests.txt
```

Expected: the only differences are **additions** — the tests written in Tasks 4 and 5, and any test renamed in Task 8. **Zero deletions.** A deleted line is a test that did not survive the move, and each one must be explained before this task is complete.

- [ ] **Step 3: Confirm the parent repo is untouched**

```bash
git -C "$UPSTREAM_REPO" status --short
git -C "$UPSTREAM_REPO" rev-parse HEAD
```

Expected: unchanged from Task 2 Step 1. The extraction worked from a clone; if the parent has moved, something went wrong.

- [ ] **Step 4: Confirm history is real, not squashed**

```bash
cd "$REPO"
git log --oneline | wc -l
git log --oneline --follow -- src/referral_loop/store.py | wc -l
git log --format="%an" | sort -u
```

Expected: a substantial commit count, `store.py` showing many commits, and real author names.

- [ ] **Step 5: Confirm the security fixes came across**

The eighteen hardening commits are the reason this repo starts clean. Spot-check three:

```bash
cd "$REPO"
grep -n "ENCODING_CHARACTERS" src/referral_loop/parse_hl7.py
grep -n "BEGIN IMMEDIATE" src/referral_loop/store.py
grep -n "CHECK (id > 0)" src/referral_loop/immutable_audit.py
```

Expected: a hit for each — MSH-2 offset pinning (C1), the atomic migration (the archive-emptying bug), and the positive-id invariant (H4).

- [ ] **Step 6: Confirm `gh` is authenticated before creating anything**

Run: `gh auth status`
Expected: `Logged in to github.com as <user>`. If not, run `gh auth login` — that is interactive, so it must be done by the human at the terminal, not by an agent.

- [ ] **Step 7: Create the private GitHub repo and push**

```bash
cd "$REPO"
gh repo create referral-loop --private --source=. --remote=origin --push
gh repo view --json name,visibility,defaultBranchRef
```

Expected: `{"name":"referral-loop","visibility":"PRIVATE",...}`. **Confirm `PRIVATE`** before doing anything else — this repo contains the eval harness and rule-pack design that §3.1 of the spec identifies as the parts most worth getting right.

- [ ] **Step 8: Confirm CI is green on the pushed branch**

```bash
cd "$REPO"
gh run list --limit 3
```

Expected: the `ci` workflow queued or running. Wait for it and confirm all three jobs pass before declaring this plan complete.

---

## Definition of done

- [ ] `referral-loop` exists as a private GitHub repo with preserved history; `git blame` works on `store.py`
- [ ] `python -m pytest tests/ -q` → 1044 passed, 11 skipped, with a collected-test diff showing zero deletions against the baseline
- [ ] No file in `src/` or `tests/` references `healthcare_rag`
- [ ] `pip install .` pulls one runtime dependency
- [ ] The built image contains no ML stack and no model client, proven by `test_install_closure.py`
- [ ] CI green on all three jobs
- [ ] The parent repo at `$UPSTREAM_REPO` is byte-identical to its state at Task 2

## Deliberately out of scope

Everything in Plan 2: the canonical model, the eleven-state machine, the `Task.status` projection, `Transition` objects, the single-transaction store apply, the FHIR `Provenance` projection, and the `REF^I13`/`I14`/`MDM^T02` ingest work. Also out of scope: removing `healthcare_rag/referral_loop/` from the parent repo — decide that after this repo has run standalone for a while.

## Known issues carried forward, not fixed here

These are real and documented; fixing them in an extraction plan would defeat its purpose.

- The eight MEDIUM and four LOW audit findings at §11.6 of the design spec.
- `EXPOSE` in the old Dockerfile said 5055 where the worklist defaults to 5057 — fixed in Task 9 because the file is rewritten anyway.
---

## Corrections found during execution (2026-08-01)

Recorded rather than silently patched, because a plan that was wrong in seven places is
evidence about how to write the next one. Every item below was found by an implementer or
reviewer running the plan, not by re-reading it.

| # | Plan said | Actually |
|---|---|---|
| 1 | `1042 passed, 11 skipped`; 1053 collected | **1046 / 11**, 1057 collected. Drafted at `be81776`; four tests landed before execution. |
| 2 | *(nothing)* | **155 references to `healthcare_rag.referral_loop` and 19 to `tests.referral_loop` survived the move**, breaking 22 of 23 test modules. No task covered it — Task 6a added. |
| 3 | *(nothing)* | **`healthcare-rag` is pip-installed**, so once `referral_loop` became importable a stale reference would have *resolved* against the parent's tree. The suite would have gone green testing the wrong code and Task 12 would have proven nothing. Task 6a Step 1 + `test_the_suite_is_exercising_this_checkout_and_not_an_installed_copy`. |
| 4 | Dockerfile `ENV` block | **Missing `REFERRAL_AUDIT_DB`.** Package-relative default lands beside site-packages, read-only in the image; first audit write fails, and `audit.py` swallows write failures by design. |
| 5 | `EXPOSE 5055` is a bug, worklist defaults to 5057 | **Wrong — 5055 is right.** `cli.py:518` defaults `--worklist-port` to 5055 and the CLI is the image entry point; 5057 is only `make_worklist_server`'s signature default. A genuine library-vs-CLI disagreement, now documented rather than silently reconciled. |
| 6 | `--peers peers.yaml` | **JSON.** `load_peer_registry` calls `json.loads`. The plan documented a file format nobody had checked. |
| 7 | "the two pre-existing `I001` findings" | **16 findings** in `tests/` (4 `I001`, 5 `E741`, 3 `F541`, 2 `F401`, 1 `F841`, 1 `E401`), plus **6 pre-existing mypy errors** the plan never checked. Both jobs would have failed on first push. |
| 8 | CI `--cov-fail-under=80` | Conflicts with `pyproject.toml`'s floor; a flag silently overrides the file. Measured 95.24%; floor set to 90. |
| 9 | `--timeout=120` in CI | Aborts the run — `test_spec_12_and_13_the_whole_suite_runs_under_both_guards` re-runs the entire suite in a subprocess with its own 3600s budget. |

Two things the extraction *revealed* rather than caused, both worth keeping:

- **A `parents[2]` bug turned the eval false-match release gate into a skip.** The gate this product sells on was silently not running, and a skip is indistinguishable from a pass in a summary line. Fixed; it runs and passes.
- **`.dockerignore` did not survive the move.** A test documents why it must exist (`**/__pycache__/` must not ship), but no such file was present, so nothing was excluding anything.

---

## Known issues carried forward, not fixed here (continued)

- **The design spec's §6.5 migration table is incomplete.** It maps four states, but the module has nine: `OPEN, SCHEDULED, RESULTED, ACKNOWLEDGED, CLOSED, CANCELLED, ORPHAN, DISMISSED, ATTACHED`, and twelve event types (`created, scheduled, resulted, acknowledged, cancelled, orphaned, reopened, reversed, dismissed, attached, unmatched, merged_in`). `ORPHAN`/`DISMISSED`/`ATTACHED` are the orphan-queue lifecycle and have no target in the eleven-state model. **Plan 2 cannot start until §6.5 is corrected.**
