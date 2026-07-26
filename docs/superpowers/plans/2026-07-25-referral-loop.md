# Referral Loop Closure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, on-premise HL7 v2 referral/order loop tracker that ingests messages from a hospital interface engine, tracks each expectation-of-a-result through a state machine, matches arriving results to open loops via a signed rule pack, and surfaces still-open loops on a localhost coordinator worklist — with zero model calls and zero network egress.

**Architecture:** A linear pipeline — listener frames MLLP and durably persists raw before ACK, an allowlist parser turns messages into typed events, a registry applies state transitions to an append-only event log, and a rule-pack-driven matcher resolves results to open loops. Every component is deterministic: state is reproducible from `(messages, pack version)` alone. Reads happen through `guardrails/immutable_audit.py`; nothing else from the parent codebase is imported.

**Tech Stack:** Python 3.12, stdlib `sqlite3` (on an OS-encrypted volume, attested by `encryption_check`), `cryptography` for Ed25519 pack signatures, Flask for the worklist blueprint, pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-referral-loop-design.md`

---

## Deviations from the spec, and why

The spec was corrected in three places while writing this plan. Read these before Task 1 — they change what you import.

1. **`db.py` and `audit_trail.py` are not imported.** `db.py` is hardwired to `rag_growth.db`; `audit_trail.log_access` delegates to it. All audit goes through `guardrails/immutable_audit.py`, which owns its own append-only DB and blocks `UPDATE`/`DELETE` at the SQLite authorizer.
2. **`STALE` is derived, never stored.** Computed at read time from `(state, age, per-modality threshold)`. Storing it would destroy the underlying `OPEN`/`SCHEDULED` state.
3. **`ORPHAN` is a stored state** on a loop-shaped record the matcher creates to hold an unmatched result.

**"Encrypted SQLite" means a plain SQLite file on an OS-encrypted volume**, attested by `encryption_check.verify_encryption_at_rest(phi_mode)`. This is how the parent codebase already defines encryption at rest. No SQLCipher dependency.

---

## RESOLVED: success criterion 6 was asserted against the wrong thing

**Found during Task 1 review. Decision taken 2026-07-26: slim the image (option 2 below). Task 13 implements it.**

`chromadb>=1.5,<2.0` and `sentence-transformers>=5.3,<6.0` are **unconditional core dependencies** (`pyproject.toml:10-11`), along with `lightrag-hku`, `raganything[all]`, `mcp`, `ollama`, `biopython` and three `tree-sitter` packages. So `pip install ".[referral]"` — exactly what `Dockerfile.referral` runs — installs the entire ML stack plus torch, transitively.

The import-closure test passes anyway, because those modules are never *imported*. They are still *installed*. Spec §10.6 says "The referral install requires neither ChromaDB nor the corpus, **asserted by import-closure test**" — but the claim is about the image a hospital's security team reviews, and it is being verified against the runtime import graph. That is the same proxy-assertion mistake as the vacuous snapshot test: measuring something adjacent to the property that matters.

Three ways out were considered:

1. **Move the RAG-only dependencies into an optional `rag` group** and leave core minimal. Correct long-term fix, but it changes what `pip install healthcare-rag` yields for every existing consumer — a parent-package decision with blast radius well beyond this subsystem.
2. **Build `Dockerfile.referral` without the parent's core dependencies** — copy the needed modules and install an explicit pinned set rather than `pip install .[referral]`. **← chosen.** Fixes the artifact a security team actually reviews, leaves the parent's install contract untouched, and keeps option 1 available later.
3. **Weaken the claim.** Rejected — it concedes the argument that motivated a separate deployable.

This works because `healthcare_rag/__init__.py:4-8` wraps `install_shim()` in `try/except Exception: pass`, so the package imports cleanly with `anthropic` absent.

**A useful consequence:** in the slim image `anthropic` is not installed at all, so "no model calls" becomes structurally true in production rather than only enforced by a test. Spec test 6 still earns its place — it proves the property in the dev environment where `anthropic` *is* importable.

**The assertion moves from import closure to install closure.** Task 13 adds a test over the image's actual site-packages, because that — not `sys.modules` — is what success criterion 6 is really about.

---

## File structure

| File | Responsibility |
|---|---|
| `healthcare_rag/referral_loop/__init__.py` | Package marker; exports nothing |
| `healthcare_rag/referral_loop/errors.py` | Typed exceptions — one place, so failure-matrix behavior is greppable |
| `healthcare_rag/referral_loop/pack.py` | Rule pack load + Ed25519 verification |
| `healthcare_rag/referral_loop/events.py` | Frozen dataclasses: `ParsedMessage`, `Loop`, `LoopEvent`, `MatchResult` |
| `healthcare_rag/referral_loop/parse_hl7.py` | Segment-allowlist parser |
| `healthcare_rag/referral_loop/mllp.py` | MLLP framing/deframing only |
| `healthcare_rag/referral_loop/store.py` | SQLite schema, raw archive, append-only events, replay |
| `healthcare_rag/referral_loop/registry.py` | Loop state machine + merge handling |
| `healthcare_rag/referral_loop/matcher.py` | Tiered result→loop resolution |
| `healthcare_rag/referral_loop/staleness.py` | Derived staleness |
| `healthcare_rag/referral_loop/listener.py` | MLLP server + file-drop source |
| `healthcare_rag/referral_loop/worklist.py` | Flask blueprint |
| `healthcare_rag/referral_loop/cli.py` | `referral-loop` entry point |
| `healthcare_rag/referral_loop/rules/` | `pack.json`, `pack.sig`, `CHANGELOG.md` |
| `healthcare_rag/referral_loop/eval.py` | Replay harness + pack release gate |
| `tests/referral_loop/` | Mirrors the above, plus `test_proofs.py` for cross-cutting assertions |

Framing is split from `listener.py` and events from `parse_hl7.py` so the safety tests can exercise them without opening a socket.

---

### Task 1: Package skeleton and dependency boundary

Establishes the import boundary first, so every later task is constrained by it.

**Files:**
- Create: `healthcare_rag/referral_loop/__init__.py`
- Create: `healthcare_rag/referral_loop/errors.py`
- Modify: `pyproject.toml:28-46`
- Test: `tests/referral_loop/test_import_closure.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_import_closure.py
"""Success criterion 6: the referral install needs neither ChromaDB nor the corpus.

This MUST run in a clean subprocess. An in-process sys.modules snapshot passes
vacuously: under `pytest tests/` another test has already imported
healthcare_rag.db long before this one runs, so db never appears as "newly
imported" and the assertion silently proves nothing. Verified -- the snapshot
form passes even with healthcare_rag.db loaded.

Same principle as spec test 7: assert on the real end state, not on a proxy.
"""
import json
import subprocess
import sys

import pytest

FORBIDDEN = [
    "chromadb", "sentence_transformers", "torch", "transformers",
    "lightrag", "raganything", "mcp", "ollama",
    "healthcare_rag.revenue_integrity", "healthcare_rag.denial_rca",
    "healthcare_rag.db", "healthcare_rag.audit_trail",
    "healthcare_rag.guardrails.tenant_isolation",
]

_PROBE = """
import json, sys
import healthcare_rag.referral_loop
import healthcare_rag.referral_loop.errors
print(json.dumps(sorted(sys.modules)))
"""


def _run_probe() -> set[str]:
    """Import referral_loop in a clean interpreter, return everything it loaded.

    Surfaces stderr on failure rather than using check=True: a real ImportError
    inside the probe would otherwise arrive as an opaque non-zero exit with the
    actual traceback swallowed.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        pytest.fail(f"probe failed (exit {proc.returncode}):\n{proc.stderr}")
    return set(json.loads(proc.stdout))


def test_referral_import_closure_in_a_clean_interpreter():
    loaded = _run_probe()
    leaked = sorted(m for m in loaded if any(m == f or m.startswith(f + ".") for f in FORBIDDEN))
    assert leaked == [], f"referral_loop pulled in forbidden modules: {leaked}"


def test_anthropic_is_in_the_closure_and_that_is_expected():
    """Documents a constraint so nobody 'fixes' it wrongly later.

    healthcare_rag/__init__.py:5-6 calls claude_cli.install_shim(), which imports
    anthropic. Every healthcare_rag.* import therefore pulls it in, and short of
    restructuring the parent package that cannot be avoided.

    This does not weaken the v1 claim. "No model calls" is proven by spec test 6
    (Task 12), which monkeypatches anthropic and claude_cli to raise and runs the
    whole suite -- an assertion about behavior, not about the import graph. A
    module being importable is not a model call.
    """
    assert "anthropic" in _run_probe(), (
        "If anthropic is no longer in the closure the parent package changed; "
        "re-check that spec test 6 still proves what it claims."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_import_closure.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop'`

- [ ] **Step 3: Create the packages**

`tests/` is already an importable package (`tests/__init__.py` exists, and `pyproject.toml` sets `pythonpath`). Later tasks import shared fixtures across test modules — e.g. `from tests.referral_loop.test_matcher import PACK` — so the test directory must be a package too. Create an empty `tests/referral_loop/__init__.py` and an empty `tests/referral_loop/fixtures/__init__.py`.

```python
# healthcare_rag/referral_loop/__init__.py
"""Referral loop closure — deterministic HL7 v2 loop tracking.

Deliberately imports nothing from the RAG side of the codebase. See
docs/superpowers/specs/2026-07-25-referral-loop-design.md section 3.
"""
```

```python
# healthcare_rag/referral_loop/errors.py
"""Typed failures for the referral loop subsystem.

Most map to a row of the spec failure matrix (section 8). ThresholdsNotAcceptedError
does not -- it encodes the resolution of open question 3 (section 12): staleness
thresholds ship as defaults but the site must accept them explicitly, so a
threshold stays the hospital's clinical decision rather than ours.

Every name carries the -Error suffix, matching the convention already used
across this codebase (AnthropicClientError, SpendLimitError, MissingColumnsError).
"""


class ReferralLoopError(Exception):
    """Base for every referral-loop failure."""


class FramingError(ReferralLoopError):
    """MLLP framing malformed. Respond AR; the engine retries."""


class UnparseableSegmentError(ReferralLoopError):
    """One segment failed to parse. Skip it, keep the message, flag for review."""


class PackVerificationError(ReferralLoopError):
    """Pack signature missing, invalid, or altered. Refuse to boot."""


class StoreUnavailableError(ReferralLoopError):
    """Durable write failed. Respond AE so the engine queues. Never ACK."""


class ThresholdsNotAcceptedError(ReferralLoopError):
    """Staleness thresholds shipped as defaults but not accepted by the site."""
```

- [ ] **Step 4: Add the optional-dependency group**

In `pyproject.toml`, after the `cdi` group (line 39), add:

```toml
referral = [
    "flask>=3.0",
    "cryptography>=42.0",
]
```

And in `[project.scripts]` (after line 45) add:

```toml
referral-loop = "healthcare_rag.referral_loop.cli:main"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/referral_loop/test_import_closure.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add healthcare_rag/referral_loop/ tests/referral_loop/ pyproject.toml
git commit -m "feat(referral): package skeleton and import boundary"
```

---

### Task 2: Rule pack loading and signature verification

Built before the parser because the pack gates boot — nothing else may load without it.

**Files:**
- Create: `healthcare_rag/referral_loop/pack.py`
- Create: `healthcare_rag/referral_loop/rules/pack.json`
- Create: `healthcare_rag/referral_loop/rules/CHANGELOG.md`
- Test: `tests/referral_loop/test_pack.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_pack.py
"""Spec test 9: pack tamper. Mutate one byte; assert refusal to load."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from healthcare_rag.referral_loop.errors import PackVerificationError
from healthcare_rag.referral_loop.pack import load_pack

PACK = {
    "version": "1.0.0",
    "confidence_floor": 0.90,
    "date_windows_hours": {"CT": 24, "MG": 720, "_default": 168},
    "staleness_hours": {"CT": 4, "MG": 720, "_default": 336},
    "modality_equivalence": {"CT": ["CT", "CAT"], "MG": ["MG", "MAM"]},
    "tie_breakers": ["nearest_order_date", "same_ordering_provider", "most_specific_modality"],
    "tier_confidence": {"1": 1.0, "2": 0.98, "3": 0.92, "4": 0.70},
}


def _write_pack(tmp_path, pack_dict, corrupt=False):
    key = Ed25519PrivateKey.generate()
    pack_bytes = json.dumps(pack_dict, sort_keys=True, separators=(",", ":")).encode()
    sig = key.sign(pack_bytes)
    if corrupt:
        mutable = bytearray(pack_bytes)
        mutable[10] = mutable[10] ^ 0x01
        pack_bytes = bytes(mutable)
    (tmp_path / "pack.json").write_bytes(pack_bytes)
    (tmp_path / "pack.sig").write_bytes(sig)
    return key.public_key().public_bytes_raw()


def test_valid_pack_loads(tmp_path):
    pubkey = _write_pack(tmp_path, PACK)
    pack = load_pack(tmp_path, pubkey)
    assert pack.version == "1.0.0"
    assert pack.confidence_floor == 0.90


def test_single_byte_mutation_refuses_to_load(tmp_path):
    pubkey = _write_pack(tmp_path, PACK, corrupt=True)
    with pytest.raises(PackVerificationError):
        load_pack(tmp_path, pubkey)


def test_missing_signature_refuses_to_load(tmp_path):
    pubkey = _write_pack(tmp_path, PACK)
    (tmp_path / "pack.sig").unlink()
    with pytest.raises(PackVerificationError):
        load_pack(tmp_path, pubkey)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_pack.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.pack'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/pack.py
"""Signed rule pack. Verified before load; an altered pack must never run.

This is IP protection and a safety control at once -- a tampered pack could
lower the confidence floor and cause false closes, the one failure the product
exists to prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import PackVerificationError


@dataclass(frozen=True)
class RulePack:
    version: str
    confidence_floor: float
    date_windows_hours: dict[str, int]
    staleness_hours: dict[str, int]
    modality_equivalence: dict[str, list[str]]
    tie_breakers: tuple[str, ...]
    tier_confidence: dict[int, float]

    def date_window_hours(self, modality: str) -> int:
        return self.date_windows_hours.get(modality, self.date_windows_hours["_default"])

    def staleness_threshold_hours(self, modality: str) -> int:
        return self.staleness_hours.get(modality, self.staleness_hours["_default"])

    def equivalent_modalities(self, modality: str) -> frozenset[str]:
        """Symmetric: querying from an alias returns the same class as the canonical.

        The pack keys equivalences canonically ("CT": ["CT", "CAT"]), but sending
        systems emit either spelling. A naive .get(modality) returns {"CAT"} for
        the alias, so a CAT result would never match a CT order -- failing safe
        (an orphan, not a false close) but silently costing recall on exactly the
        interface quirk this table exists to absorb.
        """
        return self._equivalence_index.get(modality, frozenset({modality}))


def load_pack(pack_dir: Path, public_key_raw: bytes) -> RulePack:
    """Load and verify the pack. Raises PackVerificationError on any doubt."""
    pack_path = Path(pack_dir) / "pack.json"
    sig_path = Path(pack_dir) / "pack.sig"

    if not pack_path.is_file():
        raise PackVerificationError(f"No pack at {pack_path}")
    if not sig_path.is_file():
        raise PackVerificationError(f"No signature at {sig_path}; refusing to load an unsigned pack")

    pack_bytes = pack_path.read_bytes()
    signature = sig_path.read_bytes()

    try:
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(signature, pack_bytes)
    except InvalidSignature as exc:
        raise PackVerificationError("Pack signature invalid; refusing to boot") from exc

    try:
        raw = json.loads(pack_bytes)
    except json.JSONDecodeError as exc:
        raise PackVerificationError("Pack is signed but not valid JSON") from exc

    # A signed body that parses as JSON but is not an object would otherwise
    # escape as AttributeError, and a caller catching PackVerificationError to
    # refuse boot would crash instead of refusing.
    if not isinstance(raw, dict):
        raise PackVerificationError(f"Pack must be a JSON object, got {type(raw).__name__}")

    if "_default" not in raw.get("date_windows_hours", {}):
        raise PackVerificationError("date_windows_hours missing '_default'")
    if "_default" not in raw.get("staleness_hours", {}):
        raise PackVerificationError("staleness_hours missing '_default'")

    return RulePack(
        version=raw["version"],
        confidence_floor=float(raw["confidence_floor"]),
        date_windows_hours=raw["date_windows_hours"],
        staleness_hours=raw["staleness_hours"],
        modality_equivalence=raw["modality_equivalence"],
        tie_breakers=tuple(raw["tie_breakers"]),
        tier_confidence={int(k): float(v) for k, v in raw["tier_confidence"].items()},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/referral_loop/test_pack.py -v`
Expected: PASS — 3 passed

- [ ] **Step 5: Write the shipped pack and changelog**

```json
{"confidence_floor":0.9,"date_windows_hours":{"CT":24,"MG":720,"MR":168,"US":72,"XR":24,"_default":168},"modality_equivalence":{"CT":["CT","CAT"],"MG":["MG","MAM"],"MR":["MR","MRI"],"US":["US","SONO"],"XR":["XR","CR","DX"]},"staleness_hours":{"CT":4,"MG":720,"MR":168,"US":72,"XR":24,"_default":336},"tie_breakers":["nearest_order_date","same_ordering_provider","most_specific_modality"],"tier_confidence":{"1":1.0,"2":0.98,"3":0.92,"4":0.7},"version":"1.0.0"}
```

Write that to `healthcare_rag/referral_loop/rules/pack.json` as a single line — the signature is over exact bytes, so reformatting invalidates it.

```markdown
<!-- healthcare_rag/referral_loop/rules/CHANGELOG.md -->
# Rule pack changelog

Every entry records what changed and the eval delta that justified it. A pack
ships only if, replayed against the archived corpus, false-close rate does not
increase **and** precision improves (spec section 7).

## 1.0.0 — 2026-07-25

Initial pack. No eval delta: this is the baseline every later pack is measured
against. Staleness thresholds are defaults requiring explicit site acceptance
(see `REFERRAL_THRESHOLDS_ACCEPTED`, Task 9) — shipping a threshold silently
would imply a clinical standard that is the site's call, not ours.
```

- [ ] **Step 6: Write the signing utility and produce `pack.sig`**

`load_pack` refuses an unsigned pack, so a shipped `pack.json` with no `pack.sig` cannot boot. The signature is over exact bytes, so it must be generated from the file rather than by hand.

**The private key never enters version control.** It is the thing that makes a tampered pack detectable; committing it would make the signature decorative. For development, generate a keypair under `~/.config/healthcare-rag/` (outside the repo) and commit only `pack.sig` and the public key.

```python
# scripts/sign_referral_pack.py
"""Sign a referral rule pack. Private key stays outside the repo, always.

Usage:
    python scripts/sign_referral_pack.py --keygen        # once, writes to ~/.config
    python scripts/sign_referral_pack.py --sign          # signs rules/pack.json
    python scripts/sign_referral_pack.py --pubkey        # prints hex for REFERRAL_PACK_PUBKEY
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEY_DIR = Path.home() / ".config" / "healthcare-rag"
PRIVATE_KEY = KEY_DIR / "referral_pack_ed25519.key"
PACK_DIR = Path(__file__).parent.parent / "healthcare_rag" / "referral_loop" / "rules"


def keygen() -> None:
    if PRIVATE_KEY.exists():
        sys.exit(f"Refusing to overwrite existing key at {PRIVATE_KEY}")
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    PRIVATE_KEY.write_bytes(key.private_bytes_raw())
    PRIVATE_KEY.chmod(0o600)
    print(f"Wrote {PRIVATE_KEY}")
    print(f"Public key (hex): {key.public_key().public_bytes_raw().hex()}")


def _load_private() -> Ed25519PrivateKey:
    if not PRIVATE_KEY.exists():
        sys.exit(f"No signing key at {PRIVATE_KEY}. Run --keygen first.")
    return Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY.read_bytes())


def sign() -> None:
    key = _load_private()
    pack_bytes = (PACK_DIR / "pack.json").read_bytes()
    (PACK_DIR / "pack.sig").write_bytes(key.sign(pack_bytes))
    print(f"Signed {len(pack_bytes)} bytes -> {PACK_DIR / 'pack.sig'}")


def pubkey() -> None:
    print(_load_private().public_key().public_bytes_raw().hex())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--keygen", action="store_true")
    group.add_argument("--sign", action="store_true")
    group.add_argument("--pubkey", action="store_true")
    args = parser.parse_args()
    if args.keygen:
        keygen()
    elif args.sign:
        sign()
    else:
        pubkey()
```

Run, and record the public key — Task 13's CLI needs it as `REFERRAL_PACK_PUBKEY`:

```bash
python scripts/sign_referral_pack.py --keygen
python scripts/sign_referral_pack.py --sign
python scripts/sign_referral_pack.py --pubkey
```

Add a test proving the shipped pack actually verifies against the shipped key:

```python
# append to tests/referral_loop/test_pack.py
import os
from pathlib import Path

SHIPPED_PACK = Path(__file__).parent.parent.parent / "healthcare_rag" / "referral_loop" / "rules"


@pytest.mark.skipif(
    not os.environ.get("REFERRAL_PACK_PUBKEY"), reason="REFERRAL_PACK_PUBKEY not set"
)
def test_shipped_pack_verifies_against_the_shipped_public_key():
    """A pack that cannot verify is a pack that cannot boot."""
    pack = load_pack(SHIPPED_PACK, bytes.fromhex(os.environ["REFERRAL_PACK_PUBKEY"]))
    assert pack.version == "1.0.0"
    assert pack.confidence_floor == 0.9


def test_shipped_pack_and_signature_both_exist():
    assert (SHIPPED_PACK / "pack.json").is_file()
    assert (SHIPPED_PACK / "pack.sig").is_file(), (
        "load_pack refuses an unsigned pack; shipping pack.json alone cannot boot"
    )
```

Confirm the private key is not tracked: `git check-ignore -v ~/.config/healthcare-rag/referral_pack_ed25519.key` should report the path is outside the repo, and `git status --short` must not list it.

- [ ] **Step 7: Protect the signed bytes from line-ending conversion**

This repo has `core.autocrlf=true` globally and no `.gitattributes`. The shipped pack survives a fresh Windows clone today only because `pack.json` happens to contain zero newline bytes — verified empirically by cloning with `--no-local --config core.autocrlf=true` and re-checking the signature.

That safety is incidental. A single trailing newline — what any editor adds on save — makes checkout produce a different byte count than was signed, and the pack then fails to verify on every Windows clone. That is a customer boot path protected by "nobody ever opens this file in an editor."

Create `.gitattributes` at the repo root:

```gitattributes
# The rule pack is signed over exact bytes. Any line-ending conversion breaks
# verification and the engine then refuses to boot. Never let git rewrite these.
healthcare_rag/referral_loop/rules/pack.json -text
healthcare_rag/referral_loop/rules/pack.sig  -text
```

Verify it takes effect: `git check-attr text healthcare_rag/referral_loop/rules/pack.json` must report `text: unset`.

- [ ] **Step 8: Cover the untested error paths**

Three of the five documented failure conditions had no test. The `_default` validation in particular is exactly the kind of check a later refactor silently drops.

```python
# append to tests/referral_loop/test_pack.py

def test_missing_pack_file_refuses(tmp_path):
    with pytest.raises(PackVerificationError, match="No pack"):
        load_pack(tmp_path, b"\x00" * 32)


def test_signed_but_non_json_body_refuses(tmp_path):
    key = Ed25519PrivateKey.generate()
    body = b"this is signed but is not json"
    (tmp_path / "pack.json").write_bytes(body)
    (tmp_path / "pack.sig").write_bytes(key.sign(body))
    with pytest.raises(PackVerificationError, match="not valid JSON"):
        load_pack(tmp_path, key.public_key().public_bytes_raw())


def test_signed_json_that_is_not_an_object_refuses(tmp_path):
    """Must raise PackVerificationError, not AttributeError -- a caller catching
    it to refuse boot would otherwise crash instead of refusing."""
    key = Ed25519PrivateKey.generate()
    body = b"[1, 2, 3]"
    (tmp_path / "pack.json").write_bytes(body)
    (tmp_path / "pack.sig").write_bytes(key.sign(body))
    with pytest.raises(PackVerificationError, match="must be a JSON object"):
        load_pack(tmp_path, key.public_key().public_bytes_raw())


@pytest.mark.parametrize("field", ["date_windows_hours", "staleness_hours"])
def test_missing_default_window_refuses(tmp_path, field):
    broken = {**PACK, field: {"CT": 24}}
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match=f"{field} missing"):
        load_pack(tmp_path, pubkey)
```

- [ ] **Step 9: Commit**

```bash
git add healthcare_rag/referral_loop/pack.py healthcare_rag/referral_loop/rules/ scripts/sign_referral_pack.py tests/referral_loop/test_pack.py .gitattributes
git commit -m "feat(referral): signed rule pack with Ed25519 verification"
```

Verify the private key is absent from the commit: `git show --stat HEAD | grep -i key` must return nothing but the script name.

---

### Task 3: Typed events and the segment-allowlist parser

**Files:**
- Create: `healthcare_rag/referral_loop/events.py`
- Create: `healthcare_rag/referral_loop/parse_hl7.py`
- Test: `tests/referral_loop/test_parse_hl7.py`

The allowlist is the PHI control. Mirror `healthcare_rag/revenue_integrity/ingest_835.py:23` — an allowlist, not a redactor, because a denylist fails silently the first time a sending system emits an unexpected segment carrying an identifier.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_parse_hl7.py
from healthcare_rag.referral_loop.parse_hl7 import ALLOWED_SEGMENTS, parse_hl7_text

ORU = (
    "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|CTRL0001|P|2.5.1\r"
    "PID|1||MRN123456^^^HOSP^MR||DOE^JANE||19800101|F\r"
    "NK1|1|DOE^JOHN|SPO|555 ELM ST^^SPRINGFIELD^IL^62701\r"
    "GT1|1||DOE^JOHN^^^^^L|||555 ELM ST^^SPRINGFIELD^IL^62701\r"
    "OBR|1|PLACER987|FILLER654|71260^CT CHEST W CONTRAST^C4|||20260724080000\r"
    "OBX|1|TX|71260^CT CHEST^C4||No acute finding||||||F\r"
)


def test_allowlist_is_exactly_the_documented_set():
    assert ALLOWED_SEGMENTS == frozenset(
        {"MSH", "PID", "MRG", "PV1", "ORC", "OBR", "OBX", "SCH", "RF1"}
    )


def test_nk1_and_gt1_are_never_parsed():
    """These carry next-of-kin and guarantor identifiers. We never read them."""
    msg = parse_hl7_text(ORU)
    assert "NK1" not in msg.segments
    assert "GT1" not in msg.segments


def test_parses_control_id_and_type():
    msg = parse_hl7_text(ORU)
    assert msg.control_id == "CTRL0001"
    assert msg.message_type == "ORU^R01"


def test_extracts_allowlisted_fields():
    msg = parse_hl7_text(ORU)
    obr = msg.segments["OBR"][0]
    assert obr[2] == "PLACER987"   # OBR-2 placer order number
    assert obr[3] == "FILLER654"   # OBR-3 filler order number
    assert msg.segments["OBX"][0][11] == "F"  # OBX-11 result status


def test_unparseable_segment_is_skipped_message_kept():
    """Failure matrix: skip the segment, keep the message, flag for review.

    The OBR is replaced with a bare segment id carrying no data fields -- that
    is what 'unparseable' means here. Truncating only part of it would leave a
    parseable segment and the assertion would be vacuous.
    """
    broken = ORU.replace(
        "OBR|1|PLACER987|FILLER654|71260^CT CHEST W CONTRAST^C4|||20260724080000",
        "OBR",
    )
    msg = parse_hl7_text(broken)
    assert msg.control_id == "CTRL0001", "the message survives a bad segment"
    assert "OBR" in msg.flags_for_review
    assert "OBX" in msg.segments, "other segments still parse"


def test_unknown_message_type_never_raises():
    """Failure matrix: ignore, count, never error."""
    unknown = ORU.replace("ORU^R01", "ZZZ^Z99")
    msg = parse_hl7_text(unknown)
    assert msg.message_type == "ZZZ^Z99"
    assert msg.is_known_type is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_parse_hl7.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.parse_hl7'`

- [ ] **Step 3: Write the event types**

```python
# healthcare_rag/referral_loop/events.py
"""Typed records. Every field here is one the allowlist permits."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LoopState(str, Enum):
    OPEN = "OPEN"
    SCHEDULED = "SCHEDULED"
    RESULTED = "RESULTED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    ORPHAN = "ORPHAN"
    # STALE is deliberately absent -- it is derived at read time. See staleness.py.


@dataclass(frozen=True)
class ParsedMessage:
    control_id: str                       # MSH-10
    message_type: str                     # "ORU^R01"
    is_known_type: bool
    segments: dict[str, list[list[str]]]  # allowlisted segments only
    flags_for_review: tuple[str, ...] = ()


@dataclass(frozen=True)
class Loop:
    loop_id: str
    mrn: str
    state: LoopState
    placer_order_number: str = ""
    filler_order_number: str = ""
    service_code: str = ""
    modality: str = ""
    ordering_provider: str = ""
    ordered_at: datetime | None = None
    ack_by: str = ""
    ack_role: str = ""
    ack_at: datetime | None = None


@dataclass(frozen=True)
class LoopEvent:
    loop_id: str
    event_type: str        # "created" | "scheduled" | "resulted" | "closed" | ...
    occurred_at: datetime
    control_id: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    loop_id: str | None
    tier: int              # 1-4 matched, 5 = no match
    confidence: float
    reason: str
```

- [ ] **Step 4: Write the parser**

```python
# healthcare_rag/referral_loop/parse_hl7.py
"""HL7 v2 parser built on a segment allowlist.

Only the segments enumerated in ALLOWED_SEGMENTS are ever turned into Python
objects. Everything else -- next of kin in NK1, guarantor in GT1, free-text
notes in NTE -- is dropped at the door, so there is nothing downstream to leak.

This is deliberately not a redaction pass. A redactor is a denylist, and a
denylist fails silently the first time a sending system emits an unexpected
segment carrying an identifier. Same shape as revenue_integrity/ingest_835.py.

MRG is allowlisted because ADT^A40 merges require it (spec section 4 rule 3).
ORC is allowlisted for order control on ORM/OMG. Omitting either would silently
break a documented requirement.
"""
from __future__ import annotations

from .events import ParsedMessage

ALLOWED_SEGMENTS = frozenset({"MSH", "PID", "MRG", "PV1", "ORC", "OBR", "OBX", "SCH", "RF1"})

KNOWN_MESSAGE_TYPES = frozenset(
    {"REF^I12", "ORM^O01", "OMG^O19", "ORU^R01", "SIU^S12", "SIU^S15", "ADT^A40"}
)

# Max fields we index per segment. OBX-11 is the deepest field we read.
_MAX_FIELDS = 32


def _split_fields(segment: str) -> list[str]:
    """Split a segment so that index n holds field n.

    For every segment except MSH, index 0 is the segment id and field n lands
    at index n naturally. MSH shifts by one because MSH-1 *is* the separator;
    parse_hl7_text handles that case separately.
    """
    fields = segment.split("|")
    if len(fields) < _MAX_FIELDS:
        fields.extend([""] * (_MAX_FIELDS - len(fields)))
    return fields[:_MAX_FIELDS]


def parse_hl7_text(text: str) -> ParsedMessage:
    """Parse a single HL7 v2 message. Never raises on content -- see failure matrix."""
    raw_segments = [s for s in text.replace("\n", "\r").split("\r") if s.strip()]

    segments: dict[str, list[list[str]]] = {}
    flags: list[str] = []
    control_id = ""
    message_type = ""

    for raw in raw_segments:
        # Exact segment-id match, not a prefix match. `raw[:3] in ALLOWED_SEGMENTS`
        # alone would ingest "OBXTRA|..." as an OBX, letting a non-allowlisted
        # segment's content reach Python objects -- the one thing the allowlist
        # exists to prevent. HL7 v2 ids are always exactly 3 characters, so a
        # conformant sender never trips this; that is precisely the reasoning this
        # module rejects for denylists, so it is enforced rather than assumed.
        seg_id = raw[:3]
        if seg_id not in ALLOWED_SEGMENTS or not (len(raw) == 3 or raw[3] == "|"):
            continue
        fields = _split_fields(raw)

        if seg_id == "MSH":
            # MSH-1 is the field separator itself, so MSH fields shift by one.
            msh = [raw[:3], "|"] + raw[4:].split("|")
            msh.extend([""] * (_MAX_FIELDS - len(msh)))
            fields = msh[:_MAX_FIELDS]
            control_id = fields[10]
            message_type = fields[9]

        # A segment with no data fields is malformed: skip it, flag it, keep going.
        if seg_id != "MSH" and not any(fields[1:]):
            flags.append(seg_id)
            continue

        segments.setdefault(seg_id, []).append(fields)

    return ParsedMessage(
        control_id=control_id,
        message_type=message_type,
        is_known_type=message_type in KNOWN_MESSAGE_TYPES,
        segments=segments,
        flags_for_review=tuple(flags),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_parse_hl7.py -v`
Expected: PASS — 6 passed

- [ ] **Step 6: Commit**

```bash
git add healthcare_rag/referral_loop/events.py healthcare_rag/referral_loop/parse_hl7.py tests/referral_loop/test_parse_hl7.py
git commit -m "feat(referral): typed events and allowlist HL7 parser"
```

---

### Task 4: MLLP framing

**Files:**
- Create: `healthcare_rag/referral_loop/mllp.py`
- Test: `tests/referral_loop/test_mllp.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_mllp.py
import pytest

from healthcare_rag.referral_loop.errors import FramingError
from healthcare_rag.referral_loop.mllp import VT, FS, CR, build_ack, deframe, frame


def test_frame_wraps_in_vt_fs_cr():
    assert frame("MSH|^~\\&|") == VT + b"MSH|^~\\&|" + FS + CR


def test_deframe_roundtrip():
    assert deframe(frame("HELLO")) == "HELLO"


def test_missing_start_block_raises():
    with pytest.raises(FramingError):
        deframe(b"MSH|no start block" + FS + CR)


def test_missing_end_block_raises():
    with pytest.raises(FramingError):
        deframe(VT + b"MSH|no end block")


def test_ack_codes():
    assert "|AA|" in build_ack("CTRL1", "AA")
    assert "|AE|" in build_ack("CTRL1", "AE")
    assert "|AR|" in build_ack("CTRL1", "AR")
    assert "CTRL1" in build_ack("CTRL1", "AA")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_mllp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.mllp'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/mllp.py
"""MLLP framing, kept separate from the listener so it is testable without a socket."""
from __future__ import annotations

from .errors import FramingError

VT = b"\x0b"   # start block
FS = b"\x1c"   # end block
CR = b"\x0d"   # carriage return


def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


def deframe(payload: bytes) -> str:
    if not payload.startswith(VT):
        raise FramingError("Missing MLLP start block (VT)")
    if not payload.endswith(FS + CR):
        raise FramingError("Missing MLLP end block (FS CR)")
    try:
        return payload[len(VT):-len(FS + CR)].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # The listener catches FramingError and answers AR. A bare
        # UnicodeDecodeError would escape that handler and crash the connection
        # on hostile bytes -- exactly the input this layer exists to reject.
        raise FramingError(f"Payload is not valid UTF-8: {exc}") from exc


# MSH-10 is at most 20 characters and carries no delimiters. Anything else is
# either a malformed sender or an injection attempt.
_SAFE_CONTROL_ID = re.compile(r"[^A-Za-z0-9._-]")
_MAX_CONTROL_ID = 20


def sanitize_control_id(control_id: str) -> str:
    """Make an untrusted MSH-10 safe to interpolate into an ACK.

    control_id comes from the inbound message, so it is attacker-controlled. A
    bare '|' silently shifts every later MSH field; an embedded '\\r' plus a
    fabricated 'MSH|...' forges a second segment, and a parser reading the
    result takes the forged header as authoritative.

    We always return a well-formed ACK -- refusing to answer is not an option,
    because the engine would just retry forever -- so this sanitizes rather
    than raises.
    """
    cleaned = _SAFE_CONTROL_ID.sub("", control_id or "")[:_MAX_CONTROL_ID]
    return cleaned or "UNKNOWN"


def build_ack(control_id: str, code: str) -> str:
    """AA = accepted, AE = error (engine queues and retries), AR = rejected.

    Never return AA for a message that was not durably stored.
    """
    if code not in {"AA", "AE", "AR"}:
        raise ValueError(f"Invalid ACK code: {code}")
    safe_id = sanitize_control_id(control_id)
    return (
        f"MSH|^~\\&|REFERRAL|LOCAL|SENDER|SENDER|||ACK|{safe_id}|P|2.5.1\r"
        f"MSA|{code}|{safe_id}\r"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_mllp.py -v`
Expected: PASS — 5 passed

- [ ] **Step 5: Commit**

```bash
git add healthcare_rag/referral_loop/mllp.py tests/referral_loop/test_mllp.py
git commit -m "feat(referral): MLLP framing and ACK construction"
```

---

### Task 5: Store — raw archive, append-only events, replay

> **AS-BUILT — the code block below is superseded in five ways. Read this first; implementing the snippet verbatim reintroduces four defects.** Authoritative source is `healthcare_rag/referral_loop/store.py`.
>
> 1. **Append-only is enforced by SQL triggers, not the authorizer.** A `sqlite3` authorizer binds per-connection, so it guards only connections `LoopStore` opens — any other process could delete the event log freely. `BEFORE DELETE`/`BEFORE UPDATE` triggers on `loop_events` and `raw_messages` travel with the file. The authorizer is retained as defence in depth. Verified blocked from an outside connection: `DELETE`, `UPDATE`, `UPDATE ... FROM`, `ALTER TABLE`, `DROP TRIGGER`.
> 2. **`replay` must NOT filter falsy detail values.** `event.detail` contains exactly the keys an event intends to change, so `attrs.update(event.detail)` already leaves other fields alone. The filter made it impossible for an event to *clear* a field, which made safety rule 2 — a corrected result clearing the prior acknowledgement — unimplementable through the event log.
> 3. **`record_raw` must not treat every `IntegrityError` as a duplicate.** It returned `False` for a message that was never stored, so the listener would answer `AA` and the message would be gone. It now confirms the row exists before claiming duplicate, and raises `StoreUnavailableError` otherwise. A falsy `control_id` is refused outright — SQLite permits repeated NULLs in a TEXT primary key, which silently defeats MSH-10 idempotency.
> 4. **Unknown `event_type` is rejected in `append_event`, before the insert** — not only in `replay`. Validating in `replay` alone raises *after* the row is committed, and because the log is append-only that event can never be removed: one typo makes a loop permanently unreplayable. The `replay` guard remains for a restored or foreign-written log.
> 5. **Durability is pinned, not defaulted.** `_connect()` sets `PRAGMA synchronous=FULL`, `recursive_triggers=ON` (so `REPLACE INTO` fires the delete trigger) and `foreign_keys=ON`, with a test asserting they read back. Measured by hard-killing the process after `record_raw` returned: the row survives. Without pinning, one stray `PRAGMA synchronous=OFF` in a later task would silently remove persist-before-ACK with no test failing.
>
> Also added: `rebuild_projection()` (a restore from the event log otherwise leaves an empty worklist — a loop vanishing silently), and timezone-aware UTC `received_at` to match `occurred_at`.
>
> **Residual, documented in the module docstring rather than papered over:** a foreign connection that does not set `recursive_triggers`, and `DROP TABLE`, both remain possible. These are bounded by filesystem permissions, not by this module, and belong in the audit narrative.
>
> **Carried to Task 6:** `replay` orders by arrival (`event_id`), deliberately — each event was validated by the registry when applied, so arrival order is the authoritative accepted sequence. **The registry must therefore reject a clinically-older message arriving late before appending it**; nothing below the registry guards this, and a `scheduled` arriving after a `resulted` would otherwise regress the state. Separately, `_materialize`'s lock is per-instance, so two processes on one file can race the `loops` projection.

**Files:**
- Create: `healthcare_rag/referral_loop/store.py`
- Test: `tests/referral_loop/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_store.py
"""Spec tests 8 and 10: persist-before-ACK and state reconstruction."""
from datetime import datetime, timezone

from healthcare_rag.referral_loop.events import LoopEvent, LoopState
from healthcare_rag.referral_loop.store import LoopStore

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def test_raw_message_persists_before_any_parse(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    store.record_raw("CTRL1", "MSH|raw payload")
    assert store.raw_count() == 1


def test_duplicate_control_id_is_a_noop(tmp_path):
    """Failure matrix: duplicate MSH-10 is a no-op, logged, never a second transition."""
    store = LoopStore(tmp_path / "loops.db")
    assert store.record_raw("CTRL1", "MSH|first") is True
    assert store.record_raw("CTRL1", "MSH|second") is False
    assert store.raw_count() == 1


def test_state_is_reconstructible_from_events_alone(tmp_path):
    """Spec test 10: the property an auditor will ask you to demonstrate."""
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1", "modality": "CT"}))
    store.append_event(LoopEvent("L1", "scheduled", NOW, "C2", {}))
    store.append_event(LoopEvent("L1", "resulted", NOW, "C3", {"obx11": "F"}))

    loop = store.replay("L1")
    assert loop.state is LoopState.RESULTED
    assert loop.mrn == "MRN1"
    assert loop.modality == "CT"


def test_events_are_append_only(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    store.append_event(LoopEvent("L1", "created", NOW, "C1", {"mrn": "MRN1"}))
    import sqlite3
    with sqlite3.connect(tmp_path / "loops.db") as conn:
        try:
            conn.execute("DELETE FROM loop_events")
            deleted = True
        except sqlite3.DatabaseError:
            deleted = False
    assert deleted is False, "loop_events must reject DELETE"


def test_replay_survives_process_restart(tmp_path):
    """Spec test 8: kill between durable write and parse; replay reconstructs state."""
    db = tmp_path / "loops.db"
    store = LoopStore(db)
    store.record_raw("CTRL1", "MSH|payload")
    store.append_event(LoopEvent("L1", "created", NOW, "CTRL1", {"mrn": "MRN1"}))
    del store  # simulate process death before parse completed

    reopened = LoopStore(db)
    assert reopened.raw_count() == 1
    assert reopened.replay("L1").state is LoopState.OPEN
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.store'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/store.py
"""Three tables: raw_messages, loops, loop_events.

loop_events is append-only and authoritative -- any loop's state is derivable by
replaying it. The loops table is a materialized convenience for the worklist
query and carries no information the event log does not.

"Encrypted" here means a plain SQLite file on an OS-encrypted volume, attested
at startup by encryption_check.verify_encryption_at_rest. Same definition the
rest of the codebase uses.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .errors import StoreUnavailableError
from .events import Loop, LoopEvent, LoopState

_SQLITE_DELETE = 9
_SQLITE_UPDATE = 23

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    control_id  TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id     TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    control_id  TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_loop ON loop_events(loop_id);
CREATE TABLE IF NOT EXISTS loops (
    loop_id           TEXT PRIMARY KEY,
    mrn               TEXT NOT NULL,
    state             TEXT NOT NULL,
    placer_order_number TEXT DEFAULT '',
    filler_order_number TEXT DEFAULT '',
    service_code      TEXT DEFAULT '',
    modality          TEXT DEFAULT '',
    ordering_provider TEXT DEFAULT '',
    ordered_at        TEXT,
    ack_by            TEXT DEFAULT '',
    ack_role          TEXT DEFAULT '',
    ack_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_loops_mrn ON loops(mrn);
CREATE INDEX IF NOT EXISTS idx_loops_state ON loops(state);
"""

# Event type -> resulting state. Replay applies these in order.
_EVENT_STATE = {
    "created": LoopState.OPEN,
    "scheduled": LoopState.SCHEDULED,
    "resulted": LoopState.RESULTED,
    "closed": LoopState.CLOSED,
    "cancelled": LoopState.CANCELLED,
    "orphaned": LoopState.ORPHAN,
    "reopened": LoopState.RESULTED,
    "merged_in": None,   # carries fields, does not change state
}


def _authorizer(action_code: int, arg1, arg2, *_args):
    """Block UPDATE and DELETE on loop_events. Raw archive stays immutable too."""
    if action_code in (_SQLITE_DELETE, _SQLITE_UPDATE) and arg1 in ("loop_events", "raw_messages"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class LoopStore:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        try:
            conn = self._connect()
            conn.executescript(_SCHEMA)
            conn.commit()
            conn.close()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"Cannot initialize store at {self.db_path}: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _guarded(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.set_authorizer(_authorizer)
        return conn

    def record_raw(self, control_id: str, payload: str) -> bool:
        """Durably persist the raw message. Returns False if already seen.

        This must complete before any ACK. Acknowledging then crashing during
        parse means the engine considers the message delivered and it is gone.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO raw_messages (control_id, payload, received_at) VALUES (?, ?, ?)",
                    (control_id, payload, datetime.now().isoformat()),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            except sqlite3.Error as exc:
                raise StoreUnavailableError(f"Durable write failed: {exc}") from exc
            finally:
                conn.close()

    def raw_count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
        finally:
            conn.close()

    def append_event(self, event: LoopEvent) -> None:
        with self._lock:
            conn = self._guarded()
            try:
                conn.execute(
                    "INSERT INTO loop_events (loop_id, event_type, occurred_at, control_id, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        event.loop_id,
                        event.event_type,
                        event.occurred_at.isoformat(),
                        event.control_id,
                        json.dumps(event.detail, sort_keys=True),
                    ),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise StoreUnavailableError(f"Event append failed: {exc}") from exc
            finally:
                conn.close()
            self._materialize(event.loop_id)

    def events_for(self, loop_id: str) -> list[LoopEvent]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM loop_events WHERE loop_id = ? ORDER BY event_id", (loop_id,)
            ).fetchall()
        finally:
            conn.close()
        return [
            LoopEvent(
                loop_id=r["loop_id"],
                event_type=r["event_type"],
                occurred_at=datetime.fromisoformat(r["occurred_at"]),
                control_id=r["control_id"],
                detail=json.loads(r["detail"]),
            )
            for r in rows
        ]

    def replay(self, loop_id: str) -> Loop:
        """Reconstruct a loop from its events alone. Spec test 10."""
        events = self.events_for(loop_id)
        if not events:
            raise KeyError(f"No events for loop {loop_id}")

        state = LoopState.OPEN
        attrs: dict = {}
        for event in events:
            attrs.update({k: v for k, v in event.detail.items() if v not in (None, "")})
            mapped = _EVENT_STATE.get(event.event_type)
            if mapped is not None:
                state = mapped

        ordered_at = attrs.get("ordered_at")
        ack_at = attrs.get("ack_at")
        return Loop(
            loop_id=loop_id,
            mrn=attrs.get("mrn", ""),
            state=state,
            placer_order_number=attrs.get("placer_order_number", ""),
            filler_order_number=attrs.get("filler_order_number", ""),
            service_code=attrs.get("service_code", ""),
            modality=attrs.get("modality", ""),
            ordering_provider=attrs.get("ordering_provider", ""),
            ordered_at=datetime.fromisoformat(ordered_at) if ordered_at else None,
            ack_by=attrs.get("ack_by", ""),
            ack_role=attrs.get("ack_role", ""),
            ack_at=datetime.fromisoformat(ack_at) if ack_at else None,
        )

    def _materialize(self, loop_id: str) -> None:
        """Refresh the loops row from the event log. Never the source of truth."""
        loop = self.replay(loop_id)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO loops (loop_id, mrn, state, placer_order_number, "
                "filler_order_number, service_code, modality, ordering_provider, ordered_at, "
                "ack_by, ack_role, ack_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    loop.loop_id, loop.mrn, loop.state.value, loop.placer_order_number,
                    loop.filler_order_number, loop.service_code, loop.modality,
                    loop.ordering_provider,
                    loop.ordered_at.isoformat() if loop.ordered_at else None,
                    loop.ack_by, loop.ack_role,
                    loop.ack_at.isoformat() if loop.ack_at else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def open_loops(self, mrn: str | None = None) -> list[Loop]:
        query = "SELECT loop_id FROM loops WHERE state IN ('OPEN', 'SCHEDULED')"
        params: tuple = ()
        if mrn:
            query += " AND mrn = ?"
            params = (mrn,)
        conn = self._connect()
        try:
            ids = [r["loop_id"] for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()
        return [self.replay(i) for i in ids]

    def all_loops(self) -> list[Loop]:
        conn = self._connect()
        try:
            ids = [r["loop_id"] for r in conn.execute("SELECT loop_id FROM loops").fetchall()]
        finally:
            conn.close()
        return [self.replay(i) for i in ids]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_store.py -v`
Expected: PASS — 5 passed

- [ ] **Step 5: Commit**

```bash
git add healthcare_rag/referral_loop/store.py tests/referral_loop/test_store.py
git commit -m "feat(referral): append-only store with event replay"
```

---

### Task 6: Registry state machine — the two safety rules

**Files:**
- Create: `healthcare_rag/referral_loop/registry.py`
- Test: `tests/referral_loop/test_registry_safety.py`

Safety tests 1 and 2. These are not correctness tests; they are the reason the product is defensible.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_registry_safety.py
"""Spec tests 1 and 2. Safety, not correctness."""
import pytest

from healthcare_rag.referral_loop.errors import ReferralLoopError
from healthcare_rag.referral_loop.events import LoopState
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore


@pytest.fixture()
def registry(tmp_path):
    return Registry(LoopStore(tmp_path / "loops.db"))


def test_preliminary_advances_to_resulted(registry):
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="P", control_id="C2")
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_closed_is_unreachable_from_a_preliminary_read(registry):
    """Spec test 1. A prelim that later corrects is the malpractice scenario;
    auto-closing on it would make the tool the cause."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="P", control_id="C2")

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")

    assert registry.get(loop_id).state is LoopState.RESULTED


def test_final_result_permits_close(registry):
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    assert registry.get(loop_id).state is LoopState.CLOSED


def test_corrected_result_reopens_a_closed_loop(registry):
    """Spec test 2."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    assert registry.get(loop_id).state is LoopState.CLOSED

    registry.record_result(loop_id, obx11="C", control_id="C4")
    reopened = registry.get(loop_id)
    assert reopened.state is LoopState.RESULTED
    assert reopened.ack_at is None, "reopening must clear the prior acknowledgement"


def test_result_for_a_cancelled_loop_is_orphaned(registry):
    """Failure matrix: someone cancelled an order that then produced a result."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.cancel(loop_id, control_id="C2")
    with pytest.raises(ReferralLoopError):
        registry.record_result(loop_id, obx11="F", control_id="C3")


def test_acknowledgement_records_actor_and_role(registry):
    """Open question 2: if the coordinator is not clinically responsible, CLOSED
    means 'someone looked at it'. Recording the role keeps that answerable."""
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    registry.acknowledge(loop_id, actor="coord1", role="coordinator", control_id="C3")
    loop = registry.get(loop_id)
    assert loop.ack_by == "coord1"
    assert loop.ack_role == "coordinator"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_registry_safety.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.registry'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/registry.py
"""The loop state machine.

A loop is an expectation that a result returns. Two transitions here are
clinical safety decisions rather than engineering choices, and both are
enforced structurally rather than by convention:

  1. A preliminary read (OBX-11 = P) may reach RESULTED but never CLOSED.
  2. A corrected read (OBX-11 = C) returns a CLOSED loop to RESULTED.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .errors import ReferralLoopError
from .events import Loop, LoopEvent, LoopState
from .store import LoopStore

# OBX-11 result status codes we act on.
PRELIMINARY = "P"
FINAL = "F"
CORRECTED = "C"

_CLOSEABLE_FROM = {LoopState.RESULTED}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Registry:
    def __init__(self, store: LoopStore):
        self.store = store

    def get(self, loop_id: str) -> Loop:
        return self.store.replay(loop_id)

    def open_loop(
        self,
        mrn: str,
        control_id: str,
        modality: str = "",
        placer_order_number: str = "",
        filler_order_number: str = "",
        service_code: str = "",
        ordering_provider: str = "",
        ordered_at: datetime | None = None,
        loop_id: str | None = None,
    ) -> str:
        loop_id = loop_id or f"L-{uuid.uuid4().hex[:12]}"
        self.store.append_event(
            LoopEvent(
                loop_id=loop_id,
                event_type="created",
                occurred_at=_now(),
                control_id=control_id,
                detail={
                    "mrn": mrn,
                    "modality": modality,
                    "placer_order_number": placer_order_number,
                    "filler_order_number": filler_order_number,
                    "service_code": service_code,
                    "ordering_provider": ordering_provider,
                    "ordered_at": (ordered_at or _now()).isoformat(),
                },
            )
        )
        return loop_id

    def schedule(self, loop_id: str, control_id: str) -> None:
        loop = self.get(loop_id)
        if loop.state not in (LoopState.OPEN, LoopState.SCHEDULED):
            raise ReferralLoopError(f"Cannot schedule a loop in state {loop.state}")
        self.store.append_event(LoopEvent(loop_id, "scheduled", _now(), control_id, {}))

    def cancel(self, loop_id: str, control_id: str) -> None:
        loop = self.get(loop_id)
        if loop.state is LoopState.CLOSED:
            raise ReferralLoopError("Cannot cancel a closed loop")
        self.store.append_event(LoopEvent(loop_id, "cancelled", _now(), control_id, {}))

    def record_result(self, loop_id: str, obx11: str, control_id: str) -> None:
        """Apply an arriving result. OBX-11 decides which transition is legal."""
        loop = self.get(loop_id)

        if loop.state is LoopState.CANCELLED:
            raise ReferralLoopError(
                f"Result arrived for CANCELLED loop {loop_id}; route to orphan queue and flag"
            )

        if obx11 == CORRECTED:
            # Rule 2: a correction reopens review and clears the prior ack.
            self.store.append_event(
                LoopEvent(
                    loop_id, "reopened", _now(), control_id,
                    {"obx11": obx11, "ack_by": "", "ack_role": "", "ack_at": ""},
                )
            )
            return

        if obx11 not in (PRELIMINARY, FINAL):
            raise ReferralLoopError(f"Unhandled OBX-11 status: {obx11!r}")

        self.store.append_event(
            LoopEvent(loop_id, "resulted", _now(), control_id, {"obx11": obx11})
        )

    def acknowledge(self, loop_id: str, actor: str, role: str, control_id: str) -> None:
        """Close the loop. Refuses on a preliminary read -- spec section 4 rule 1.

        `role` is recorded because if the acknowledging party is not clinically
        responsible then CLOSED means 'someone looked at it', and the worklist
        must not claim more than that. See open question 2.
        """
        loop = self.get(loop_id)
        if loop.state not in _CLOSEABLE_FROM:
            raise ReferralLoopError(f"Cannot close a loop in state {loop.state}")

        if self._latest_result_status(loop_id) == PRELIMINARY:
            raise ReferralLoopError(
                f"Loop {loop_id} has only a preliminary result; CLOSED is unreachable"
            )

        self.store.append_event(
            LoopEvent(
                loop_id, "closed", _now(), control_id,
                {"ack_by": actor, "ack_role": role, "ack_at": _now().isoformat()},
            )
        )

    def orphan(self, control_id: str, mrn: str, detail: dict) -> str:
        """Create a loop-shaped record to hold a result nobody ordered."""
        loop_id = f"O-{uuid.uuid4().hex[:12]}"
        self.store.append_event(
            LoopEvent(loop_id, "orphaned", _now(), control_id, {"mrn": mrn, **detail})
        )
        return loop_id

    def _latest_result_status(self, loop_id: str) -> str:
        status = ""
        for event in self.store.events_for(loop_id):
            if "obx11" in event.detail:
                status = event.detail["obx11"]
        return status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_registry_safety.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add healthcare_rag/referral_loop/registry.py tests/referral_loop/test_registry_safety.py
git commit -m "feat(referral): loop state machine with preliminary and correction rules"
```

---

### Task 7: Patient merge carries loops

**Files:**
- Modify: `healthcare_rag/referral_loop/registry.py` (add `merge_patient`)
- Test: `tests/referral_loop/test_merge.py`

Spec test 3. This is the failure that makes a tracker report all-clear while loops are open, and it breaks most homegrown trackers.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_merge.py
"""Spec test 3: ADT^A40 moves every open loop to the surviving MRN."""
from healthcare_rag.referral_loop.events import LoopState
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore


def _registry(tmp_path):
    return Registry(LoopStore(tmp_path / "loops.db"))


def test_merge_moves_every_open_loop_to_surviving_mrn(tmp_path):
    reg = _registry(tmp_path)
    a = reg.open_loop(mrn="MRN_OLD", modality="CT", control_id="C1")
    b = reg.open_loop(mrn="MRN_OLD", modality="MG", control_id="C2")
    reg.schedule(b, control_id="C3")

    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEW", control_id="C4")

    assert reg.get(a).mrn == "MRN_NEW"
    assert reg.get(b).mrn == "MRN_NEW"
    assert reg.get(b).state is LoopState.SCHEDULED, "merge must not alter loop state"


def test_no_loop_is_orphaned_by_a_merge(tmp_path):
    """The assertion that matters: nothing may be left behind on the prior MRN."""
    reg = _registry(tmp_path)
    for i in range(5):
        reg.open_loop(mrn="MRN_OLD", modality="CT", control_id=f"C{i}")

    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEW", control_id="CM")

    assert reg.store.open_loops(mrn="MRN_OLD") == []
    assert len(reg.store.open_loops(mrn="MRN_NEW")) == 5


def test_merge_to_unknown_surviving_mrn_still_carries_loops(tmp_path):
    """Failure matrix: create the surviving record, carry loops, log."""
    reg = _registry(tmp_path)
    a = reg.open_loop(mrn="MRN_OLD", modality="CT", control_id="C1")
    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEVER_SEEN", control_id="C2")
    assert reg.get(a).mrn == "MRN_NEVER_SEEN"


def test_closed_loops_also_follow_the_surviving_identifier(tmp_path):
    reg = _registry(tmp_path)
    a = reg.open_loop(mrn="MRN_OLD", modality="CT", control_id="C1")
    reg.record_result(a, obx11="F", control_id="C2")
    reg.acknowledge(a, actor="coord1", role="coordinator", control_id="C3")

    reg.merge_patient(prior_mrn="MRN_OLD", surviving_mrn="MRN_NEW", control_id="C4")

    merged = reg.get(a)
    assert merged.mrn == "MRN_NEW"
    assert merged.state is LoopState.CLOSED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_merge.py -v`
Expected: FAIL with `AttributeError: 'Registry' object has no attribute 'merge_patient'`

- [ ] **Step 3: Add `merge_patient` to `registry.py`**

Add this method to the `Registry` class, after `orphan`:

```python
    def merge_patient(self, prior_mrn: str, surviving_mrn: str, control_id: str) -> list[str]:
        """ADT^A40. Move every loop from the prior MRN to the surviving one.

        If loops do not follow the surviving identifier they vanish from the
        worklist while remaining clinically open, and the tool then reports
        all-clear on an open loop. Every loop moves, in any state -- a closed
        loop left on a retired MRN corrupts the audit trail just as badly.

        An unknown surviving MRN is not an error: create the record, carry the
        loops, log it (failure matrix).
        """
        moved: list[str] = []
        for loop in self.store.all_loops():
            if loop.mrn != prior_mrn:
                continue
            self.store.append_event(
                LoopEvent(
                    loop_id=loop.loop_id,
                    event_type="merged_in",
                    occurred_at=_now(),
                    control_id=control_id,
                    detail={"mrn": surviving_mrn, "merged_from_mrn": prior_mrn},
                )
            )
            moved.append(loop.loop_id)
        return moved
```

Note: `"merged_in"` maps to `None` in `store._EVENT_STATE`, so it carries the new MRN without altering state. That mapping already exists from Task 5.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_merge.py -v`
Expected: PASS — 4 passed

- [ ] **Step 5: Run the full referral suite for regressions**

Run: `python -m pytest tests/referral_loop/ -v`
Expected: PASS — all tests from Tasks 1-7

- [ ] **Step 6: Commit**

```bash
git add healthcare_rag/referral_loop/registry.py tests/referral_loop/test_merge.py
git commit -m "feat(referral): ADT^A40 merge carries all loops to surviving MRN"
```

---

### Task 8: Tiered matcher with confidence floor

**Files:**
- Create: `healthcare_rag/referral_loop/matcher.py`
- Test: `tests/referral_loop/test_matcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_matcher.py
from datetime import datetime, timedelta, timezone

from healthcare_rag.referral_loop.events import Loop, LoopState
from healthcare_rag.referral_loop.matcher import ResultKey, match_result
from healthcare_rag.referral_loop.pack import RulePack

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

PACK = RulePack(
    version="test",
    confidence_floor=0.90,
    date_windows_hours={"CT": 24, "MG": 720, "_default": 168},
    staleness_hours={"CT": 4, "_default": 336},
    modality_equivalence={"CT": ["CT", "CAT"]},
    tie_breakers=("nearest_order_date", "same_ordering_provider", "most_specific_modality"),
    tier_confidence={1: 1.0, 2: 0.98, 3: 0.92, 4: 0.70},
)


def _loop(loop_id, **kw):
    base = dict(mrn="MRN1", state=LoopState.OPEN, modality="CT", ordered_at=NOW - timedelta(hours=2))
    base.update(kw)
    return Loop(loop_id=loop_id, **base)


def test_tier1_placer_order_number_exact():
    loops = [_loop("L1", placer_order_number="PLACER1"), _loop("L2", placer_order_number="PLACER2")]
    key = ResultKey(mrn="MRN1", placer="PLACER2", filler="", service_code="", modality="CT", observed_at=NOW)
    result = match_result(key, loops, PACK)
    assert result.loop_id == "L2"
    assert result.tier == 1


def test_tier2_filler_order_number_exact():
    loops = [_loop("L1", filler_order_number="FILLER9")]
    key = ResultKey(mrn="MRN1", placer="", filler="FILLER9", service_code="", modality="CT", observed_at=NOW)
    result = match_result(key, loops, PACK)
    assert result.loop_id == "L1"
    assert result.tier == 2


def test_tier3_mrn_service_code_and_date_window():
    loops = [_loop("L1", service_code="71260")]
    key = ResultKey(mrn="MRN1", placer="", filler="", service_code="71260", modality="CT", observed_at=NOW)
    result = match_result(key, loops, PACK)
    assert result.loop_id == "L1"
    assert result.tier == 3


def test_outside_the_date_window_does_not_match_at_tier3():
    loops = [_loop("L1", service_code="71260", ordered_at=NOW - timedelta(hours=200))]
    key = ResultKey(mrn="MRN1", placer="", filler="", service_code="71260", modality="CT", observed_at=NOW)
    assert match_result(key, loops, PACK).tier == 5


def test_date_window_is_per_modality_not_global():
    """A 30-day-old screening mammogram is in window; a 30-day-old CT is not."""
    mg = [_loop("L1", modality="MG", service_code="77067", ordered_at=NOW - timedelta(days=25))]
    key = ResultKey(mrn="MRN1", placer="", filler="", service_code="77067", modality="MG", observed_at=NOW)
    assert match_result(key, mg, PACK).tier == 3


def test_tier4_falls_below_confidence_floor_and_refuses_to_auto_match():
    """The product's equivalent of INSUFFICIENT_REGULATORY_EVIDENCE: decline, do not guess."""
    loops = [_loop("L1", service_code="OTHER", modality="CAT")]
    key = ResultKey(mrn="MRN1", placer="", filler="", service_code="", modality="CT", observed_at=NOW)
    result = match_result(key, loops, PACK)
    assert result.tier == 4
    assert result.confidence < PACK.confidence_floor
    assert result.loop_id is None, "below the floor the matcher must route to review, not attach"
    assert "floor" in result.reason


def test_no_candidate_is_tier5_orphan():
    key = ResultKey(mrn="MRN_UNKNOWN", placer="", filler="", service_code="", modality="CT", observed_at=NOW)
    result = match_result(key, [], PACK)
    assert result.tier == 5
    assert result.loop_id is None


def test_tiebreak_prefers_nearest_order_date():
    loops = [
        _loop("L_far", service_code="71260", ordered_at=NOW - timedelta(hours=20)),
        _loop("L_near", service_code="71260", ordered_at=NOW - timedelta(hours=1)),
    ]
    key = ResultKey(mrn="MRN1", placer="", filler="", service_code="71260", modality="CT", observed_at=NOW)
    assert match_result(key, loops, PACK).loop_id == "L_near"


def test_closed_loops_are_never_match_candidates():
    loops = [_loop("L1", placer_order_number="PLACER1", state=LoopState.CLOSED)]
    key = ResultKey(mrn="MRN1", placer="PLACER1", filler="", service_code="", modality="CT", observed_at=NOW)
    assert match_result(key, loops, PACK).tier == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_matcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.matcher'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/matcher.py
"""Resolve an arriving result to an open loop.

A false close is strictly worse than an orphan. An orphan gets human attention;
a false close attributes a result to the wrong order, marks that loop satisfied,
and leaves the real loop open while reporting it closed -- the tool conceals the
thing it exists to surface. So below the pack's confidence floor the matcher
returns no loop at all and routes to review.

Every threshold, window and tie-breaker is pack-configured. Nothing here is
hardcoded clinical judgement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .events import Loop, LoopState, MatchResult
from .pack import RulePack

_CANDIDATE_STATES = {LoopState.OPEN, LoopState.SCHEDULED, LoopState.RESULTED}


@dataclass(frozen=True)
class ResultKey:
    """The allowlisted fields of an arriving ORU that matching is permitted to use."""
    mrn: str
    placer: str
    filler: str
    service_code: str
    modality: str
    observed_at: datetime
    ordering_provider: str = ""


def _in_window(loop: Loop, key: ResultKey, pack: RulePack) -> bool:
    if loop.ordered_at is None:
        return False
    window = timedelta(hours=pack.date_window_hours(loop.modality or key.modality))
    return abs(key.observed_at - loop.ordered_at) <= window


def _tiebreak(candidates: list[Loop], key: ResultKey, pack: RulePack) -> Loop:
    """Apply pack-ordered tie-breakers. Order is data, not code."""
    ranked = candidates
    for rule in pack.tie_breakers:
        if len(ranked) == 1:
            break
        if rule == "nearest_order_date":
            ranked = sorted(
                ranked,
                key=lambda loop: abs(key.observed_at - loop.ordered_at)
                if loop.ordered_at else timedelta.max,
            )
            best = abs(key.observed_at - ranked[0].ordered_at) if ranked[0].ordered_at else None
            ranked = [
                loop for loop in ranked
                if loop.ordered_at and abs(key.observed_at - loop.ordered_at) == best
            ] or ranked[:1]
        elif rule == "same_ordering_provider" and key.ordering_provider:
            same = [loop for loop in ranked if loop.ordering_provider == key.ordering_provider]
            ranked = same or ranked
        elif rule == "most_specific_modality":
            exact = [loop for loop in ranked if loop.modality == key.modality]
            ranked = exact or ranked
    return ranked[0]


def match_result(key: ResultKey, loops: list[Loop], pack: RulePack) -> MatchResult:
    """Return the matched loop, or tier 5 / sub-floor with loop_id None."""
    candidates = [loop for loop in loops if loop.state in _CANDIDATE_STATES]

    # Tier 1 -- placer order number exact.
    if key.placer:
        hits = [loop for loop in candidates if loop.placer_order_number == key.placer]
        if hits:
            return MatchResult(hits[0].loop_id, 1, pack.tier_confidence[1], "placer order number exact")

    # Tier 2 -- filler order number / accession exact.
    if key.filler:
        hits = [loop for loop in candidates if loop.filler_order_number == key.filler]
        if hits:
            return MatchResult(hits[0].loop_id, 2, pack.tier_confidence[2], "filler order number exact")

    same_patient = [loop for loop in candidates if loop.mrn == key.mrn]

    # Tier 3 -- MRN + service code + per-modality date window.
    if key.service_code:
        hits = [
            loop for loop in same_patient
            if loop.service_code == key.service_code and _in_window(loop, key, pack)
        ]
        if hits:
            chosen = _tiebreak(hits, key, pack)
            confidence = pack.tier_confidence[3]
            return _gate(chosen, 3, confidence, "MRN + service code + date window", pack)

    # Tier 4 -- MRN + modality equivalence + date window.
    equivalents = pack.equivalent_modalities(key.modality)
    hits = [
        loop for loop in same_patient
        if loop.modality in equivalents and _in_window(loop, key, pack)
    ]
    if hits:
        chosen = _tiebreak(hits, key, pack)
        confidence = pack.tier_confidence[4]
        return _gate(chosen, 4, confidence, "MRN + modality equivalence + date window", pack)

    # Tier 5 -- no match. Orphans are workflow, not failure.
    return MatchResult(None, 5, 0.0, "no candidate loop")


def _gate(loop: Loop, tier: int, confidence: float, reason: str, pack: RulePack) -> MatchResult:
    if confidence < pack.confidence_floor:
        return MatchResult(
            None, tier, confidence,
            f"{reason}; confidence {confidence:.2f} below floor {pack.confidence_floor:.2f} -- routed to review",
        )
    return MatchResult(loop.loop_id, tier, confidence, reason)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_matcher.py -v`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
git add healthcare_rag/referral_loop/matcher.py tests/referral_loop/test_matcher.py
git commit -m "feat(referral): tiered matcher with confidence floor gate"
```

---

### Task 9: Derived staleness, gated on explicit site acceptance

**Files:**
- Create: `healthcare_rag/referral_loop/staleness.py`
- Test: `tests/referral_loop/test_staleness.py`

Resolves open question 3 by construction: defaults ship, but the site must accept them before any loop is labeled stale.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_staleness.py
from datetime import datetime, timedelta, timezone

import pytest

from healthcare_rag.referral_loop.errors import ThresholdsNotAcceptedError
from healthcare_rag.referral_loop.events import Loop, LoopState
from healthcare_rag.referral_loop.staleness import is_stale, require_thresholds_accepted
from tests.referral_loop.test_matcher import PACK

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _loop(state=LoopState.OPEN, modality="CT", hours_ago=1):
    return Loop(
        loop_id="L1", mrn="MRN1", state=state, modality=modality,
        ordered_at=NOW - timedelta(hours=hours_ago),
    )


def test_stat_ct_is_stale_at_five_hours():
    assert is_stale(_loop(modality="CT", hours_ago=5), NOW, PACK) is True


def test_stat_ct_is_not_stale_at_three_hours():
    assert is_stale(_loop(modality="CT", hours_ago=3), NOW, PACK) is False


def test_threshold_is_per_modality():
    """A CT at 100h is stale; the same age on the default threshold is not."""
    assert is_stale(_loop(modality="CT", hours_ago=100), NOW, PACK) is True
    assert is_stale(_loop(modality="MG", hours_ago=100), NOW, PACK) is False


def test_closed_loops_are_never_stale():
    assert is_stale(_loop(state=LoopState.CLOSED, hours_ago=9999), NOW, PACK) is False


def test_cancelled_loops_are_never_stale():
    assert is_stale(_loop(state=LoopState.CANCELLED, hours_ago=9999), NOW, PACK) is False


def test_resulted_loops_are_not_stale():
    """The result arrived. It awaits review, which is a different queue."""
    assert is_stale(_loop(state=LoopState.RESULTED, hours_ago=9999), NOW, PACK) is False


def test_future_dated_observation_is_clamped_not_rejected():
    """Failure matrix: accept, clamp for staleness math, flag. Clock skew is endemic."""
    future = _loop(hours_ago=-48)
    assert is_stale(future, NOW, PACK) is False


def test_thresholds_must_be_explicitly_accepted(monkeypatch):
    """Open question 3: shipping a default implies a clinical standard."""
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    with pytest.raises(ThresholdsNotAcceptedError):
        require_thresholds_accepted()

    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    require_thresholds_accepted()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_staleness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.staleness'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/staleness.py
"""Staleness is derived, never stored.

Writing STALE into the state column would destroy the underlying state -- a
stale loop is still OPEN or SCHEDULED, and must return to plain OPEN the moment
a result arrives, without a second transition to undo. So it is computed at read
time and used as the worklist's primary sort.

Thresholds ship as defaults but the site must accept them explicitly. Shipping a
number silently would imply a clinical standard that is the hospital's call.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from .errors import ThresholdsNotAcceptedError
from .events import Loop, LoopState
from .pack import RulePack

_STALEABLE_STATES = {LoopState.OPEN, LoopState.SCHEDULED}


def require_thresholds_accepted() -> None:
    """Refuse to compute staleness until the site has accepted the thresholds."""
    if os.environ.get("REFERRAL_THRESHOLDS_ACCEPTED", "0") != "1":
        raise ThresholdsNotAcceptedError(
            "Per-modality staleness thresholds are shipped defaults, not a clinical "
            "standard. Set REFERRAL_THRESHOLDS_ACCEPTED=1 after the site has "
            "reviewed rules/pack.json staleness_hours."
        )


def age(loop: Loop, now: datetime) -> timedelta:
    """Age of the expectation. Future-dated orders clamp to zero, never negative."""
    if loop.ordered_at is None:
        return timedelta(0)
    delta = now - loop.ordered_at
    return delta if delta > timedelta(0) else timedelta(0)


def is_stale(loop: Loop, now: datetime, pack: RulePack) -> bool:
    if loop.state not in _STALEABLE_STATES:
        return False
    threshold = timedelta(hours=pack.staleness_threshold_hours(loop.modality))
    return age(loop, now) > threshold


def staleness_ratio(loop: Loop, now: datetime, pack: RulePack) -> float:
    """How far past threshold, for worklist sorting. 1.0 is exactly at threshold."""
    if loop.state not in _STALEABLE_STATES:
        return 0.0
    threshold = pack.staleness_threshold_hours(loop.modality)
    if threshold <= 0:
        return 0.0
    return age(loop, now).total_seconds() / (threshold * 3600)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_staleness.py -v`
Expected: PASS — 8 passed

- [ ] **Step 5: Commit**

```bash
git add healthcare_rag/referral_loop/staleness.py tests/referral_loop/test_staleness.py
git commit -m "feat(referral): derived per-modality staleness gated on site acceptance"
```

---

### Task 10: Listener — ACK ordering and the AE path

**Files:**
- Create: `healthcare_rag/referral_loop/listener.py`
- Test: `tests/referral_loop/test_listener.py`

File-drop is built alongside MLLP not as speculation but because §7 requires replaying the raw archive to evaluate a pack revision — that replay needs a non-socket source regardless of how the pilot ingests.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_listener.py
"""ACK ordering is load-bearing: never acknowledge what you cannot store."""
import pytest

from healthcare_rag.referral_loop.errors import StoreUnavailableError
from healthcare_rag.referral_loop.listener import MessageHandler
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore
from tests.referral_loop.test_matcher import PACK
from tests.referral_loop.test_parse_hl7 import ORU

ORM = (
    "MSH|^~\\&|EHR|HOSP|RIS|HOSP|20260724080000||ORM^O01|CTRL_ORM|P|2.5.1\r"
    "PID|1||MRN123456^^^HOSP^MR||DOE^JANE||19800101|F\r"
    "ORC|NW|PLACER987\r"
    "OBR|1|PLACER987||71260^CT CHEST W CONTRAST^C4|||20260724080000\r"
)


@pytest.fixture()
def handler(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    return MessageHandler(store=store, registry=Registry(store), pack=PACK)


def test_valid_message_returns_aa_after_durable_write(handler):
    ack = handler.handle(ORM)
    assert "|AA|" in ack
    assert handler.store.raw_count() == 1


def test_raw_is_persisted_before_parsing(handler, monkeypatch):
    """If parse explodes, the raw message must already be on disk."""
    import healthcare_rag.referral_loop.listener as listener_mod

    def exploding_parse(_text):
        raise RuntimeError("parser blew up")

    monkeypatch.setattr(listener_mod, "parse_hl7_text", exploding_parse)
    handler.handle(ORM)
    assert handler.store.raw_count() == 1, "raw must survive a parse failure"


def test_store_failure_returns_ae_never_aa(handler, monkeypatch):
    """Failure matrix: DB unwritable -> AE so the engine queues."""
    def failing_record(*_a, **_kw):
        raise StoreUnavailableError("disk full")

    monkeypatch.setattr(handler.store, "record_raw", failing_record)
    ack = handler.handle(ORM)
    assert "|AE|" in ack
    assert "|AA|" not in ack


def test_duplicate_control_id_is_a_noop_but_still_acked(handler):
    handler.handle(ORM)
    ack = handler.handle(ORM)
    assert "|AA|" in ack
    assert handler.store.raw_count() == 1
    assert len(handler.store.all_loops()) == 1, "duplicate must not create a second loop"


def test_unknown_message_type_is_counted_never_errors(handler):
    unknown = ORM.replace("ORM^O01", "ZZZ^Z99")
    ack = handler.handle(unknown)
    assert "|AA|" in ack
    assert handler.unknown_type_count == 1


def test_oru_with_no_matching_order_creates_an_orphan(handler):
    handler.handle(ORU.replace("PLACER987", "NOSUCHORDER"))
    orphans = [loop for loop in handler.store.all_loops() if loop.loop_id.startswith("O-")]
    assert len(orphans) == 1


def test_oru_matching_an_open_order_advances_it(handler):
    handler.handle(ORM)
    handler.handle(ORU)
    loops = [loop for loop in handler.store.all_loops() if loop.loop_id.startswith("L-")]
    assert len(loops) == 1
    assert loops[0].state.value == "RESULTED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_listener.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.listener'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/listener.py
"""Ingest sources and the message handler.

ACK ordering is the load-bearing property. Persist raw durably *before*
acknowledging: acknowledging and then crashing during parse means the interface
engine considers the message delivered and it is gone -- a silently lost result
in a system whose entire purpose is not losing results.
"""
from __future__ import annotations

import logging
import socketserver
from datetime import datetime, timezone
from pathlib import Path

from .errors import FramingError, StoreUnavailableError
from .events import LoopState
from .matcher import ResultKey, match_result
from .mllp import build_ack, deframe, frame
from .pack import RulePack
from .parse_hl7 import parse_hl7_text
from .registry import Registry
from .store import LoopStore

logger = logging.getLogger(__name__)


def _hl7_datetime(raw: str) -> datetime:
    """HL7 TS format YYYYMMDDHHMMSS, truncated at any length."""
    raw = (raw or "").strip()[:14]
    if len(raw) < 8:
        return datetime.now(timezone.utc)
    padded = raw.ljust(14, "0")
    return datetime(
        int(padded[0:4]), int(padded[4:6]), int(padded[6:8]),
        int(padded[8:10]), int(padded[10:12]), int(padded[12:14]),
        tzinfo=timezone.utc,
    )


def _component(field: str, index: int = 0) -> str:
    return field.split("^")[index] if field else ""


class MessageHandler:
    """Parses and applies one message. Returns the ACK string to send."""

    def __init__(self, store: LoopStore, registry: Registry, pack: RulePack):
        self.store = store
        self.registry = registry
        self.pack = pack
        self.unknown_type_count = 0

    def handle(self, text: str) -> str:
        control_id = self._peek_control_id(text)

        # 1. Durable write first. Never ACK what cannot be stored.
        try:
            is_new = self.store.record_raw(control_id, text)
        except StoreUnavailableError:
            logger.error("Durable write failed for %s; returning AE so the engine queues", control_id)
            return build_ack(control_id, "AE")

        if not is_new:
            logger.info("Duplicate MSH-10 %s: no-op", control_id)
            return build_ack(control_id, "AA")

        # 2. Parse and apply. A failure here is recoverable -- the raw is on disk.
        try:
            message = parse_hl7_text(text)
            self._apply(message)
        except Exception:
            logger.exception("Parse/apply failed for %s; raw archived for replay", control_id)

        return build_ack(control_id, "AA")

    def _peek_control_id(self, text: str) -> str:
        for line in text.replace("\n", "\r").split("\r"):
            if line.startswith("MSH"):
                parts = line[4:].split("|")
                if len(parts) >= 9:
                    return parts[8]
        return "UNKNOWN"

    def _apply(self, message) -> None:
        if not message.is_known_type:
            self.unknown_type_count += 1
            logger.info("Unknown message type %s: counted, ignored", message.message_type)
            return

        handlers = {
            "ORM^O01": self._apply_order,
            "OMG^O19": self._apply_order,
            "REF^I12": self._apply_order,
            "ORU^R01": self._apply_result,
            "SIU^S12": self._apply_schedule,
            "SIU^S15": self._apply_cancel,
            "ADT^A40": self._apply_merge,
        }
        handler = handlers.get(message.message_type)
        if handler:
            handler(message)

    def _mrn(self, message) -> str:
        pid = message.segments.get("PID", [[]])
        return _component(pid[0][3]) if pid and len(pid[0]) > 3 else ""

    def _apply_order(self, message) -> None:
        obr = message.segments.get("OBR", [[]])[0]
        if not obr:
            return
        self.registry.open_loop(
            mrn=self._mrn(message),
            control_id=message.control_id,
            placer_order_number=_component(obr[2]),
            filler_order_number=_component(obr[3]),
            service_code=_component(obr[4]),
            modality=_component(obr[4], 2),
            ordered_at=_hl7_datetime(obr[7]),
        )

    def _apply_result(self, message) -> None:
        obr = message.segments.get("OBR", [[]])[0]
        obx = message.segments.get("OBX", [[]])[0]
        if not obr:
            return
        obx11 = obx[11] if obx and len(obx) > 11 else "F"

        key = ResultKey(
            mrn=self._mrn(message),
            placer=_component(obr[2]),
            filler=_component(obr[3]),
            service_code=_component(obr[4]),
            modality=_component(obr[4], 2),
            observed_at=_hl7_datetime(obr[7]),
        )
        candidates = self.store.open_loops()
        candidates += [
            loop for loop in self.store.all_loops() if loop.state is LoopState.RESULTED
        ]
        result = match_result(key, candidates, self.pack)

        if result.loop_id is None:
            self.registry.orphan(
                control_id=message.control_id,
                mrn=key.mrn,
                detail={
                    "modality": key.modality,
                    "service_code": key.service_code,
                    "match_tier": result.tier,
                    "match_reason": result.reason,
                },
            )
            return

        self.registry.record_result(result.loop_id, obx11=obx11, control_id=message.control_id)

    def _apply_schedule(self, message) -> None:
        for loop in self.store.open_loops(mrn=self._mrn(message)):
            self.registry.schedule(loop.loop_id, control_id=message.control_id)

    def _apply_cancel(self, message) -> None:
        for loop in self.store.open_loops(mrn=self._mrn(message)):
            self.registry.cancel(loop.loop_id, control_id=message.control_id)

    def _apply_merge(self, message) -> None:
        mrg = message.segments.get("MRG", [[]])[0]
        if not mrg:
            return
        prior = _component(mrg[1])
        surviving = self._mrn(message)
        moved = self.registry.merge_patient(prior, surviving, control_id=message.control_id)
        logger.info("ADT^A40: carried %d loops from %s", len(moved), "prior MRN")


class FileDropSource:
    """Read messages from a watched directory.

    Also the replay mechanism for pack evaluation (spec section 7) -- a pack
    revision cannot be measured without replaying the archive through a
    non-socket source.
    """

    def __init__(self, handler: MessageHandler, directory: Path | str):
        self.handler = handler
        self.directory = Path(directory)

    def drain(self) -> int:
        count = 0
        for path in sorted(self.directory.glob("*.hl7")):
            self.handler.handle(path.read_text(encoding="utf-8"))
            path.unlink()
            count += 1
        return count


def make_mllp_server(handler: MessageHandler, host: str = "127.0.0.1", port: int = 2575):
    """Bind loopback only. v1 makes no outbound connection to anyone, us included."""

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):
            buffer = b""
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                if not buffer.endswith(b"\x1c\x0d"):
                    continue
                try:
                    text = deframe(buffer)
                except FramingError:
                    logger.error("Malformed framing; archiving raw and returning AR")
                    self.request.sendall(frame(build_ack("UNKNOWN", "AR")))
                    return
                self.request.sendall(frame(handler.handle(text)))
                buffer = b""

    return socketserver.ThreadingTCPServer((host, port), _Handler)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_listener.py -v`
Expected: PASS — 7 passed

- [ ] **Step 5: Commit**

```bash
git add healthcare_rag/referral_loop/listener.py tests/referral_loop/test_listener.py
git commit -m "feat(referral): MLLP and file-drop listener with persist-before-ACK"
```

---

### Task 11: Coordinator worklist

**Files:**
- Create: `healthcare_rag/referral_loop/worklist.py`
- Test: `tests/referral_loop/test_worklist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_worklist.py
from datetime import datetime, timedelta, timezone

import pytest

from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore
from healthcare_rag.referral_loop.worklist import create_app
from tests.referral_loop.test_matcher import PACK


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    store = LoopStore(tmp_path / "loops.db")
    registry = Registry(store)
    app = create_app(store=store, registry=registry, pack=PACK)
    app.config["TESTING"] = True
    return app.test_client(), registry


def test_worklist_lists_open_loops(client):
    http, registry = client
    registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    response = http.get("/worklist/")
    assert response.status_code == 200
    assert b"CT" in response.data


def test_worklist_never_renders_a_patient_name_or_mrn(client):
    """MRN is an identifier. The coordinator queue keys on loop id."""
    http, registry = client
    registry.open_loop(mrn="MRN_SENTINEL_9999", modality="CT", control_id="C1")
    response = http.get("/worklist/")
    assert b"MRN_SENTINEL_9999" not in response.data


def test_stale_loops_sort_first(client):
    http, registry = client
    old = datetime.now(timezone.utc) - timedelta(hours=100)
    registry.open_loop(mrn="MRN1", modality="CT", control_id="C1", ordered_at=old)
    registry.open_loop(mrn="MRN1", modality="CT", control_id="C2")
    response = http.get("/worklist/?format=json")
    rows = response.get_json()["loops"]
    assert rows[0]["is_stale"] is True


def test_acknowledging_a_preliminary_result_is_refused_with_409(client):
    """Spec test 1, enforced at the HTTP boundary too."""
    http, registry = client
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="P", control_id="C2")
    response = http.post(f"/worklist/{loop_id}/acknowledge", json={"actor": "c1", "role": "coordinator"})
    assert response.status_code == 409
    assert b"preliminary" in response.data.lower()


def test_acknowledging_a_final_result_closes_the_loop(client):
    http, registry = client
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    response = http.post(f"/worklist/{loop_id}/acknowledge", json={"actor": "c1", "role": "coordinator"})
    assert response.status_code == 200
    assert registry.get(loop_id).state.value == "CLOSED"


def test_acknowledge_requires_a_role(client):
    """Open question 2: an unattributed close cannot be interpreted later."""
    http, registry = client
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")
    response = http.post(f"/worklist/{loop_id}/acknowledge", json={"actor": "c1"})
    assert response.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_worklist.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.worklist'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/worklist.py
"""Coordinator queue. Localhost only.

The worklist renders loop identifiers, modality, age and state. It does not
render MRN, patient name, or any note text -- the parser never built those
objects, and this template must not reintroduce them. Test 7 plants sentinels
and asserts zero occurrences in rendered HTML.

What CLOSED means depends on who acknowledges. Until open question 2 is settled
with a pilot site, the UI says "Acknowledged by <role>" rather than "Closed",
because if the acknowledging party is not clinically responsible then the
stronger word claims more than the record supports.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, Flask, jsonify, render_template_string, request

from .errors import ReferralLoopError
from .events import LoopState
from .pack import RulePack
from .registry import Registry
from .staleness import is_stale, require_thresholds_accepted, staleness_ratio
from .store import LoopStore

_TEMPLATE = """<!doctype html>
<title>Referral worklist</title>
<style>
 body { font-family: system-ui, sans-serif; margin: 2rem; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #ddd; }
 tr.stale { background: #fff4f4; }
 .badge { font-size: .8rem; padding: .1rem .4rem; border-radius: 3px; background: #eee; }
</style>
<h1>Referral worklist</h1>
<p>{{ rows|length }} loops &middot; pack {{ pack_version }}</p>
<table>
 <tr><th>Loop</th><th>Modality</th><th>State</th><th>Age (h)</th><th></th></tr>
 {% for row in rows %}
 <tr class="{{ 'stale' if row.is_stale else '' }}">
   <td>{{ row.loop_id }}</td>
   <td>{{ row.modality }}</td>
   <td>{{ row.state }}</td>
   <td>{{ '%.1f'|format(row.age_hours) }}</td>
   <td>{% if row.is_stale %}<span class="badge">STALE</span>{% endif %}
       {% if row.ack_role %}<span class="badge">Acknowledged by {{ row.ack_role }}</span>{% endif %}</td>
 </tr>
 {% endfor %}
</table>
"""


def _row(loop, now: datetime, pack: RulePack) -> dict:
    """Only allowlisted, non-identifying fields reach the template."""
    age_hours = 0.0
    if loop.ordered_at:
        age_hours = max(0.0, (now - loop.ordered_at).total_seconds() / 3600)
    return {
        "loop_id": loop.loop_id,
        "modality": loop.modality,
        "state": loop.state.value,
        "age_hours": age_hours,
        "is_stale": is_stale(loop, now, pack),
        "staleness_ratio": staleness_ratio(loop, now, pack),
        "ack_role": loop.ack_role,
    }


def create_blueprint(store: LoopStore, registry: Registry, pack: RulePack) -> Blueprint:
    bp = Blueprint("worklist", __name__, url_prefix="/worklist")

    @bp.get("/")
    def index():
        require_thresholds_accepted()
        now = datetime.now(timezone.utc)
        loops = [
            loop for loop in store.all_loops()
            if loop.state is not LoopState.CANCELLED
        ]
        rows = sorted(
            (_row(loop, now, pack) for loop in loops),
            key=lambda r: (not r["is_stale"], -r["staleness_ratio"]),
        )
        if request.args.get("format") == "json":
            return jsonify({"loops": rows, "pack_version": pack.version})
        return render_template_string(_TEMPLATE, rows=rows, pack_version=pack.version)

    @bp.post("/<loop_id>/acknowledge")
    def acknowledge(loop_id: str):
        payload = request.get_json(silent=True) or {}
        actor = payload.get("actor")
        role = payload.get("role")
        if not actor or not role:
            return jsonify({"error": "actor and role are both required"}), 400
        try:
            registry.acknowledge(loop_id, actor=actor, role=role, control_id="WORKLIST")
        except ReferralLoopError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"loop_id": loop_id, "state": registry.get(loop_id).state.value})

    return bp


def create_app(store: LoopStore, registry: Registry, pack: RulePack) -> Flask:
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store, registry, pack))
    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_worklist.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add healthcare_rag/referral_loop/worklist.py tests/referral_loop/test_worklist.py
git commit -m "feat(referral): coordinator worklist with role-attributed acknowledgement"
```

---

### Task 12: The four cross-cutting proofs

**Files:**
- Create: `tests/referral_loop/test_proofs.py`
- Create: `tests/referral_loop/fixtures/generate.py`

These assert on what leaves the building, not on the parser. Same end-to-end proof shape that caught an unbounded free-text channel in an earlier system.

- [ ] **Step 1: Write the fixture generator**

```python
# tests/referral_loop/fixtures/generate.py
"""Synthetic HL7 v2 messages generated from the specification.

No real message enters version control regardless of how de-identified anyone
believes it to be -- the same rule an earlier system uses for 835s.
"""
from __future__ import annotations

SENTINELS = {
    "PID_NAME": "ZZSENTINELNAME",
    "PID_MRN": "ZZSENTINELMRN",
    "NK1_NAME": "ZZSENTINELKIN",
    "GT1_NAME": "ZZSENTINELGUARANTOR",
    "NTE_TEXT": "ZZSENTINELNOTE",
    # A segment id that prefix-collides with an allowlisted one. An allowlist
    # that matches on prefix would ingest this as an OBX; this sentinel makes
    # that bypass visible in the end-to-end proof rather than only in a unit test.
    "PREFIX_COLLISION": "ZZSENTINELPREFIX",
}


def oru_with_sentinels(control_id: str = "SENT001") -> str:
    """An ORU carrying a planted identifier in every segment we must never read."""
    return (
        f"MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|{control_id}|P|2.5.1\r"
        f"PID|1||{SENTINELS['PID_MRN']}^^^HOSP^MR||{SENTINELS['PID_NAME']}^JANE||19800101|F\r"
        f"NK1|1|{SENTINELS['NK1_NAME']}^JOHN|SPO|555 ELM ST\r"
        f"GT1|1||{SENTINELS['GT1_NAME']}^JOHN|||555 ELM ST\r"
        f"OBR|1|PLACER1|FILLER1|71260^CT CHEST^C4|||20260725100000\r"
        f"OBX|1|TX|71260^CT CHEST^C4||{SENTINELS['NTE_TEXT']}||||||F\r"
        f"NTE|1||{SENTINELS['NTE_TEXT']}\r"
        f"OBXTRA|1|TX|CODE||{SENTINELS['PREFIX_COLLISION']}||||||F\r"
    )


def order(control_id: str = "ORD001", placer: str = "PLACER1", modality: str = "CT") -> str:
    return (
        f"MSH|^~\\&|EHR|HOSP|RIS|HOSP|20260725080000||ORM^O01|{control_id}|P|2.5.1\r"
        f"PID|1||MRN0001^^^HOSP^MR||DOE^JANE||19800101|F\r"
        f"ORC|NW|{placer}\r"
        f"OBR|1|{placer}||71260^CT CHEST^{modality}|||20260725080000\r"
    )
```

- [ ] **Step 2: Write the failing proofs**

```python
# tests/referral_loop/test_proofs.py
"""Spec tests 4-7. The properties that make the product defensible."""
import json
import socket
from datetime import datetime, timezone

import pytest

from healthcare_rag.referral_loop.listener import MessageHandler
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore
from healthcare_rag.referral_loop.worklist import create_app
from tests.referral_loop.fixtures.generate import SENTINELS, order, oru_with_sentinels
from tests.referral_loop.test_matcher import PACK


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    store = LoopStore(tmp_path / "loops.db")
    registry = Registry(store)
    handler = MessageHandler(store=store, registry=registry, pack=PACK)
    app = create_app(store=store, registry=registry, pack=PACK)
    app.config["TESTING"] = True
    return handler, app.test_client(), tmp_path


# ── Spec test 5: no egress ────────────────────────────────────────────────────

def test_no_non_loopback_connection_is_ever_attempted(stack, monkeypatch):
    handler, http, _ = stack
    attempted: list = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempted.append(host)
            raise AssertionError(f"Non-loopback connection attempted to {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)

    handler.handle(order())
    handler.handle(oru_with_sentinels())
    http.get("/worklist/")
    assert attempted == []


# ── Spec test 6: no model calls ───────────────────────────────────────────────

def test_pipeline_runs_with_model_clients_disabled(stack, monkeypatch):
    """v1 is genuinely deterministic: every transition is reproducible from
    (messages, pack version) alone."""
    import healthcare_rag.claude_cli as claude_cli

    def explode(*_a, **_kw):
        raise AssertionError("v1 must make no model call")

    for attr in dir(claude_cli):
        if callable(getattr(claude_cli, attr, None)) and not attr.startswith("_"):
            monkeypatch.setattr(claude_cli, attr, explode, raising=False)

    anthropic = pytest.importorskip("anthropic", reason="anthropic not installed is also a pass")
    monkeypatch.setattr(anthropic, "Anthropic", explode, raising=False)

    handler, http, _ = stack
    handler.handle(order())
    handler.handle(oru_with_sentinels())
    assert http.get("/worklist/").status_code == 200


# ── Spec test 7: no PHI in artifacts ──────────────────────────────────────────

def test_sentinels_appear_zero_times_in_every_downstream_artifact(stack, caplog):
    """Assert on what leaves the building, not on the parser."""
    handler, http, tmp_path = stack
    with caplog.at_level("DEBUG"):
        handler.handle(order())
        handler.handle(oru_with_sentinels())

    html = http.get("/worklist/").data.decode()
    api = http.get("/worklist/?format=json").data.decode()
    logs = "\n".join(record.getMessage() for record in caplog.records)

    # NOTE: the loop store is deliberately NOT an artifact here. It legitimately
    # holds the MRN -- matching and ADT^A40 merges are identifier arithmetic and
    # cannot work without it, and the spec says plainly that this product holds
    # patient data by design. Asserting "no MRN in the store" would be asserting
    # the product does not work. The claim is about what LEAVES the building.
    artifacts = {"worklist HTML": html, "worklist JSON": api, "logs": logs}

    for sentinel_name, sentinel in SENTINELS.items():
        for artifact_name, content in artifacts.items():
            assert sentinel not in content, (
                f"{sentinel_name} leaked into {artifact_name}"
            )


def test_the_store_does_hold_the_mrn_and_that_is_correct(stack):
    """The companion to the test above, and the reason it is scoped as it is.

    If this ever fails, matching and merges are broken -- not fixed. It exists so
    nobody 'hardens' the sentinel test by scrubbing the store and silently
    breaking loop tracking.
    """
    handler, _, _ = stack
    handler.handle(order())
    loops = handler.store.all_loops()
    assert loops, "an order must create a loop"
    assert loops[0].mrn, "the store must retain the MRN or matching cannot work"


def test_segments_outside_the_allowlist_never_reach_the_store(stack):
    """PID is allowlisted, so PID sentinels legitimately land in the store.
    NK1, GT1, notes and prefix-collision segments must not, anywhere."""
    import json as _json

    handler, _, _ = stack
    handler.handle(oru_with_sentinels())
    stored = _json.dumps([loop.__dict__ for loop in handler.store.all_loops()], default=str)

    for name in ("NK1_NAME", "GT1_NAME", "NTE_TEXT", "PREFIX_COLLISION"):
        assert SENTINELS[name] not in stored, f"{name} reached the store"


def test_sentinels_do_not_reach_the_audit_trail(stack, tmp_path):
    handler, _, _ = stack
    handler.handle(oru_with_sentinels())
    from healthcare_rag.guardrails.immutable_audit import AUDIT_DB
    import os
    if not os.path.exists(AUDIT_DB):
        pytest.skip("no audit db written in this run")
    with open(AUDIT_DB, "rb") as fh:
        blob = fh.read()
    for sentinel in SENTINELS.values():
        assert sentinel.encode() not in blob


# ── Spec test 4: false-close gate ─────────────────────────────────────────────

def test_false_close_rate_is_zero_at_the_configured_floor(stack):
    """A false close attributes a result to the wrong order and reports the real
    loop closed. This test blocks a pack release."""
    handler, _, _ = stack

    # Two same-patient CT orders one hour apart; the result names neither
    # placer nor filler, so only tier 3/4 can fire.
    handler.handle(order(control_id="O1", placer="P1"))
    handler.handle(order(control_id="O2", placer="P2"))

    ambiguous = (
        "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|AMB1|P|2.5.1\r"
        "PID|1||MRN0001^^^HOSP^MR||DOE^JANE||19800101|F\r"
        "OBR|1|||99999^UNKNOWN STUDY^ZZ|||20260725100000\r"
        "OBX|1|TX|99999^UNKNOWN^ZZ||finding||||||F\r"
    )
    handler.handle(ambiguous)

    resulted = [loop for loop in handler.store.all_loops() if loop.state.value == "RESULTED"]
    orphans = [loop for loop in handler.store.all_loops() if loop.state.value == "ORPHAN"]

    assert resulted == [], "an ambiguous result must never attach below the floor"
    assert len(orphans) == 1, "it must land in the orphan queue for a human instead"
```

- [ ] **Step 3: Run the proofs**

Run: `python -m pytest tests/referral_loop/test_proofs.py -v`
Expected: PASS — 5 passed. If the PHI-sentinel test fails, do not weaken the assertion; find where the identifier entered the artifact and stop it at the source.

- [ ] **Step 4: Run the whole suite under the egress guard**

Run: `python -m pytest tests/referral_loop/ -v`
Expected: PASS — every test from Tasks 1-12

- [ ] **Step 5: Commit**

```bash
git add tests/referral_loop/test_proofs.py tests/referral_loop/fixtures/
git commit -m "test(referral): egress, model-call, PHI-sentinel and false-close proofs"
```

---

### Task 13: CLI, boot gates, and container

**Files:**
- Create: `healthcare_rag/referral_loop/cli.py`
- Create: `Dockerfile.referral`
- Test: `tests/referral_loop/test_boot_gates.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_boot_gates.py
"""Two conditions refuse to boot: an invalid pack, and unverified encryption."""
import pytest

from healthcare_rag.referral_loop.cli import boot
from healthcare_rag.referral_loop.errors import PackVerificationError


def test_refuses_to_boot_when_encryption_at_rest_is_unverified(tmp_path, monkeypatch):
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.delenv("PHI_ENCRYPTION_VERIFIED", raising=False)
    monkeypatch.setattr(
        "healthcare_rag.encryption_check._detect_os_encryption", lambda: None
    )
    with pytest.raises(RuntimeError, match="encryption at rest"):
        boot(db_path=tmp_path / "loops.db", pack_dir=tmp_path, public_key_hex="00" * 32)


def test_refuses_to_boot_on_an_invalid_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    with pytest.raises(PackVerificationError):
        boot(db_path=tmp_path / "loops.db", pack_dir=tmp_path, public_key_hex="00" * 32)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_boot_gates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.cli'`

- [ ] **Step 3: Write the CLI**

```python
# healthcare_rag/referral_loop/cli.py
"""referral-loop entry point.

Two gates run before anything else and both fail closed: encryption at rest must
be verified, and the rule pack signature must validate. A tampered pack could
lower the confidence floor and cause false closes, so an unverified pack is a
safety failure, not a licensing one.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from healthcare_rag.encryption_check import verify_encryption_at_rest

from .listener import FileDropSource, MessageHandler, make_mllp_server
from .pack import load_pack
from .registry import Registry
from .staleness import require_thresholds_accepted
from .store import LoopStore
from .worklist import create_app

logger = logging.getLogger(__name__)

DEFAULT_PACK_DIR = Path(__file__).parent / "rules"


def boot(db_path: Path | str, pack_dir: Path | str, public_key_hex: str):
    """Run both boot gates, then build the stack. Raises rather than degrading."""
    verify_encryption_at_rest(os.environ.get("PHI_MODE", "full"))
    pack = load_pack(Path(pack_dir), bytes.fromhex(public_key_hex))
    require_thresholds_accepted()

    store = LoopStore(db_path)
    registry = Registry(store)
    handler = MessageHandler(store=store, registry=registry, pack=pack)
    return store, registry, handler, pack


def main() -> int:
    parser = argparse.ArgumentParser(prog="referral-loop")
    parser.add_argument("mode", choices=["listen", "filedrop", "worklist"])
    parser.add_argument("--db", default="data/referral_loops.db")
    parser.add_argument("--pack-dir", default=str(DEFAULT_PACK_DIR))
    parser.add_argument("--drop-dir", default="data/dropbox")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2575)
    parser.add_argument("--worklist-port", type=int, default=5055)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    public_key_hex = os.environ.get("REFERRAL_PACK_PUBKEY")
    if not public_key_hex:
        parser.error("REFERRAL_PACK_PUBKEY must be set to the pack signing public key (hex)")

    store, registry, handler, pack = boot(args.db, args.pack_dir, public_key_hex)
    logger.info("Booted with pack %s", pack.version)

    if args.mode == "listen":
        server = make_mllp_server(handler, host=args.host, port=args.port)
        logger.info("MLLP listening on %s:%d", args.host, args.port)
        server.serve_forever()
    elif args.mode == "filedrop":
        count = FileDropSource(handler, args.drop_dir).drain()
        logger.info("Processed %d messages from %s", count, args.drop_dir)
    else:
        app = create_app(store=store, registry=registry, pack=pack)
        app.run(host="127.0.0.1", port=args.worklist_port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_boot_gates.py -v`
Expected: PASS — 2 passed

- [ ] **Step 5: Write the Dockerfile**

```dockerfile
# Dockerfile.referral
# Separate deployable. The image is the security-review surface, so anything not
# needed to track a loop must not be in it.
#
# Deliberately NOT `pip install .[referral]`. chromadb, sentence-transformers,
# lightrag-hku, raganything[all], mcp, ollama, biopython and three tree-sitter
# packages are unconditional core dependencies of healthcare-rag, so installing
# the extra would drag the entire ML stack (and torch, transitively) into an
# image whose whole claim is that it contains none of it.
#
# Instead: copy only the modules referral_loop imports, install only what they
# need, and put the package on PYTHONPATH. healthcare_rag/__init__.py wraps
# install_shim() in try/except, so it imports cleanly with anthropic absent --
# which also means no model client exists in this image at all.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "flask>=3.1,<4.0" "cryptography>=42.0,<47"

COPY healthcare_rag/__init__.py            healthcare_rag/__init__.py
COPY healthcare_rag/encryption_check.py    healthcare_rag/encryption_check.py
COPY healthcare_rag/guardrails/            healthcare_rag/guardrails/
COPY healthcare_rag/referral_loop/         healthcare_rag/referral_loop/

ENV PYTHONPATH=/app
ENV PHI_MODE=full
EXPOSE 2575 5055

# Loopback only by default. This container makes no outbound connection to anyone,
# including us. Binding 0.0.0.0 here is for the container's own network namespace;
# publish the port only to the interface engine.
CMD ["python", "-m", "healthcare_rag.referral_loop.cli", "listen", "--host", "0.0.0.0", "--port", "2575"]
```

- [ ] **Step 6: Write the install-closure test**

Import closure is the wrong assertion for success criterion 6 — it measures what gets imported, while the claim is about what gets installed. Assert on the image.

```python
# tests/referral_loop/test_install_closure.py
"""Success criterion 6, asserted against the image rather than sys.modules.

The referral container must not contain the ML stack. An import-closure test
cannot show this: chromadb can be installed and simply never imported, which is
exactly the situation `pip install .[referral]` produces, since chromadb is an
unconditional core dependency of the parent package.
"""
import shutil
import subprocess

import pytest

FORBIDDEN_DISTRIBUTIONS = [
    "chromadb", "sentence-transformers", "torch", "transformers",
    "lightrag-hku", "raganything", "mcp", "ollama", "biopython",
]

IMAGE = "referral-loop:test"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_referral_image_contains_no_ml_stack():
    subprocess.run(
        ["docker", "build", "-f", "Dockerfile.referral", "-t", IMAGE, "."],
        check=True, capture_output=True, text=True, timeout=1800,
    )
    listing = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "pip", "list", "--format=freeze"],
        check=True, capture_output=True, text=True, timeout=300,
    ).stdout.lower()

    installed = {line.split("==")[0] for line in listing.splitlines() if line}
    leaked = sorted(d for d in FORBIDDEN_DISTRIBUTIONS if d in installed)
    assert leaked == [], f"referral image ships forbidden distributions: {leaked}"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_referral_image_has_no_model_client_installed():
    """Structural, not behavioral. Spec test 6 proves the dev environment; this
    proves production cannot make a model call because no client is present."""
    listing = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "pip", "list", "--format=freeze"],
        check=True, capture_output=True, text=True, timeout=300,
    ).stdout.lower()
    assert "anthropic==" not in listing
```

- [ ] **Step 7: Verify the image builds, is slim, and the entry point works**

Run:
```bash
docker build -f Dockerfile.referral -t referral-loop:test .
docker run --rm referral-loop:test python -m healthcare_rag.referral_loop.cli --help
docker run --rm referral-loop:test pip list --format=freeze
docker images referral-loop:test --format "{{.Size}}"
python -m pytest tests/referral_loop/test_install_closure.py -v
```
Expected: `--help` prints `listen|filedrop|worklist|purge`; `pip list` shows only flask, cryptography and their transitive dependencies — no chromadb, no torch, no anthropic; install-closure tests pass.

Record the image size in the commit message. If Docker is unavailable, say so explicitly and mark success criterion 6 as **unverified** — do not claim it passes on the strength of the import-closure test alone.

- [ ] **Step 7: Commit**

```bash
git add healthcare_rag/referral_loop/cli.py Dockerfile.referral tests/referral_loop/test_boot_gates.py
git commit -m "feat(referral): CLI with fail-closed boot gates and container"
```

---

### Task 14: Eval harness and the pack release gate

**Files:**
- Create: `healthcare_rag/referral_loop/eval.py`
- Test: `tests/referral_loop/test_eval.py`

Without the archive replay a pack revision cannot be evaluated, so this is what makes matching quality measurable rather than asserted.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_eval.py
from healthcare_rag.referral_loop.eval import EvalResult, gate_pack_release


def test_release_is_allowed_when_false_close_holds_and_precision_improves():
    baseline = EvalResult(false_close_rate=0.0, precision=0.90, recall=0.80, orphan_rate=0.10)
    candidate = EvalResult(false_close_rate=0.0, precision=0.93, recall=0.78, orphan_rate=0.12)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is True, reason


def test_any_increase_in_false_close_blocks_release():
    """Regression on the safety metric blocks release regardless of recall."""
    baseline = EvalResult(false_close_rate=0.0, precision=0.90, recall=0.80, orphan_rate=0.10)
    candidate = EvalResult(false_close_rate=0.001, precision=0.99, recall=0.99, orphan_rate=0.01)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "false-close" in reason


def test_precision_must_improve_not_merely_hold():
    baseline = EvalResult(false_close_rate=0.0, precision=0.90, recall=0.80, orphan_rate=0.10)
    candidate = EvalResult(false_close_rate=0.0, precision=0.90, recall=0.95, orphan_rate=0.05)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "precision" in reason


def test_a_worse_orphan_rate_alone_does_not_block():
    """Orphan rate is workload, not failure."""
    baseline = EvalResult(false_close_rate=0.0, precision=0.90, recall=0.80, orphan_rate=0.05)
    candidate = EvalResult(false_close_rate=0.0, precision=0.92, recall=0.80, orphan_rate=0.30)
    allowed, _ = gate_pack_release(baseline, candidate)
    assert allowed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_eval.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.eval'`

- [ ] **Step 3: Write the implementation**

```python
# healthcare_rag/referral_loop/eval.py
"""Replay harness and the pack release gate.

The metric is false-close rate, not accuracy. A false close is strictly worse
than an orphan: an orphan gets human attention, while a false close attributes a
result to the wrong order, marks that loop satisfied, and leaves the real loop
open while reporting it closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .listener import MessageHandler
from .pack import RulePack
from .registry import Registry
from .store import LoopStore


@dataclass(frozen=True)
class EvalResult:
    false_close_rate: float
    precision: float
    recall: float
    orphan_rate: float


@dataclass(frozen=True)
class LabeledCase:
    """One synthetic or site-labeled message pair with a known correct answer."""
    messages: tuple[str, ...]
    expected_loop_placer: str | None   # None means the result is a true orphan


def replay(cases: list[LabeledCase], pack: RulePack, db_path: Path) -> EvalResult:
    """Run the labeled corpus through the real pipeline and score the outcome."""
    store = LoopStore(db_path)
    handler = MessageHandler(store=store, registry=Registry(store), pack=pack)

    attached = 0
    correct = 0
    false_closes = 0
    orphans = 0
    should_match = 0

    for index, case in enumerate(cases):
        before = {loop.loop_id for loop in store.all_loops()}
        for message in case.messages:
            handler.handle(message)
        after = store.all_loops()

        new_orphans = [
            loop for loop in after
            if loop.state.value == "ORPHAN" and loop.loop_id not in before
        ]
        resulted = [loop for loop in after if loop.state.value == "RESULTED"]

        if case.expected_loop_placer is None:
            orphans += 1 if new_orphans else 0
            # A result that should have orphaned but attached instead is a false close.
            false_closes += 0 if new_orphans else 1
            continue

        should_match += 1
        matched = [loop for loop in resulted if loop.placer_order_number == case.expected_loop_placer]
        if matched:
            attached += 1
            correct += 1
        elif resulted:
            attached += 1
            false_closes += 1
        elif new_orphans:
            orphans += 1

    total = max(len(cases), 1)
    return EvalResult(
        false_close_rate=false_closes / total,
        precision=correct / attached if attached else 0.0,
        recall=correct / should_match if should_match else 0.0,
        orphan_rate=orphans / total,
    )


def gate_pack_release(baseline: EvalResult, candidate: EvalResult) -> tuple[bool, str]:
    """A new pack ships only if false-close does not increase AND precision improves."""
    if candidate.false_close_rate > baseline.false_close_rate:
        return False, (
            f"BLOCKED: false-close rate rose {baseline.false_close_rate:.4f} -> "
            f"{candidate.false_close_rate:.4f}. Safety regression blocks release "
            f"regardless of recall."
        )
    if candidate.precision <= baseline.precision:
        return False, (
            f"BLOCKED: precision did not improve "
            f"({baseline.precision:.4f} -> {candidate.precision:.4f})."
        )
    return True, (
        f"OK: false-close {baseline.false_close_rate:.4f} -> {candidate.false_close_rate:.4f}, "
        f"precision {baseline.precision:.4f} -> {candidate.precision:.4f}."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_eval.py -v`
Expected: PASS — 4 passed

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/referral_loop/ -v --tb=short`
Expected: PASS — all tests from Tasks 1-14

- [ ] **Step 6: Confirm no regression in the existing suite**

Run: `python -m pytest tests/ -q`
Expected: the pre-existing baseline (1317 passed, 0 failed) plus the new referral tests, still 0 failures.

- [ ] **Step 7: Commit**

```bash
git add healthcare_rag/referral_loop/eval.py tests/referral_loop/test_eval.py
git commit -m "feat(referral): eval replay harness and pack release gate"
```

---

### Task 15: Orphan attachment — the flywheel

**Files:**
- Modify: `healthcare_rag/referral_loop/registry.py` (add `attach_orphan`)
- Modify: `healthcare_rag/referral_loop/worklist.py` (add the attach route)
- Test: `tests/referral_loop/test_orphan_attach.py`

Spec §5 and §7: every orphan a coordinator attaches is a labeled example, and every auto-match they undo is a labeled false positive — more valuable than a synthetic case because it is a real interface quirk from a real site. Without this the flywheel described in the spec does not exist.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_orphan_attach.py
import pytest

from healthcare_rag.referral_loop.errors import ReferralLoopError
from healthcare_rag.referral_loop.events import LoopState
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore
from healthcare_rag.referral_loop.worklist import create_app
from tests.referral_loop.test_matcher import PACK


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    store = LoopStore(tmp_path / "loops.db")
    registry = Registry(store)
    app = create_app(store=store, registry=registry, pack=PACK)
    app.config["TESTING"] = True
    return store, registry, app.test_client()


def test_attaching_an_orphan_advances_the_target_loop(stack):
    store, registry, _ = stack
    target = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    orphan_id = registry.orphan(control_id="C2", mrn="MRN1", detail={"modality": "CT", "obx11": "F"})

    registry.attach_orphan(orphan_id, target, actor="coord1", role="coordinator")

    assert registry.get(target).state is LoopState.RESULTED


def test_attachment_records_a_label_for_the_eval_corpus(stack):
    store, registry, _ = stack
    target = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    orphan_id = registry.orphan(control_id="C2", mrn="MRN1", detail={"modality": "CT", "obx11": "F"})

    registry.attach_orphan(orphan_id, target, actor="coord1", role="coordinator")

    labels = store.labels()
    assert len(labels) == 1
    assert labels[0]["label_type"] == "orphan_attached"
    assert labels[0]["loop_id"] == target


def test_undoing_an_automatch_records_a_false_positive_label(stack):
    store, registry, _ = stack
    loop_id = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    registry.record_result(loop_id, obx11="F", control_id="C2")

    registry.undo_match(loop_id, actor="coord1", role="coordinator")

    labels = store.labels()
    assert labels[0]["label_type"] == "false_positive"
    assert registry.get(loop_id).state is LoopState.OPEN


def test_a_preliminary_orphan_cannot_be_attached_to_close_a_loop(stack):
    """Safety rule 1 survives the manual path -- attaching must not become a
    back door around 'preliminary never closes'."""
    store, registry, _ = stack
    target = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    orphan_id = registry.orphan(control_id="C2", mrn="MRN1", detail={"modality": "CT", "obx11": "P"})

    registry.attach_orphan(orphan_id, target, actor="coord1", role="coordinator")

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(target, actor="coord1", role="coordinator", control_id="C3")


def test_attach_endpoint_requires_actor_and_role(stack):
    store, registry, http = stack
    target = registry.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    orphan_id = registry.orphan(control_id="C2", mrn="MRN1", detail={"modality": "CT", "obx11": "F"})

    bad = http.post(f"/worklist/{orphan_id}/attach", json={"target_loop_id": target})
    assert bad.status_code == 400

    good = http.post(
        f"/worklist/{orphan_id}/attach",
        json={"target_loop_id": target, "actor": "c1", "role": "coordinator"},
    )
    assert good.status_code == 200


def test_labels_carry_no_identifier(stack):
    """A label is training data. It must survive leaving the building."""
    store, registry, _ = stack
    target = registry.open_loop(mrn="MRN_SENTINEL", modality="CT", control_id="C1")
    orphan_id = registry.orphan(control_id="C2", mrn="MRN_SENTINEL", detail={"modality": "CT", "obx11": "F"})
    registry.attach_orphan(orphan_id, target, actor="coord1", role="coordinator")

    import json
    assert "MRN_SENTINEL" not in json.dumps(store.labels())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_orphan_attach.py -v`
Expected: FAIL with `AttributeError: 'Registry' object has no attribute 'attach_orphan'`

- [ ] **Step 3: Add the labels table to `store.py`**

Append to `_SCHEMA` in `healthcare_rag/referral_loop/store.py`:

```sql
CREATE TABLE IF NOT EXISTS labels (
    label_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    label_type  TEXT NOT NULL,
    loop_id     TEXT NOT NULL,
    modality    TEXT DEFAULT '',
    service_code TEXT DEFAULT '',
    tier        INTEGER,
    actor_role  TEXT DEFAULT '',
    pack_version TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
```

Add these methods to `LoopStore`:

```python
    def record_label(
        self,
        label_type: str,
        loop_id: str,
        modality: str = "",
        service_code: str = "",
        tier: int | None = None,
        actor_role: str = "",
        pack_version: str = "",
    ) -> None:
        """Record a coordinator judgement as a labeled example.

        Deliberately stores no MRN, no actor identity, no free text -- a label is
        training data and must be contributable without re-identifying anyone.
        Only the non-identifying features the matcher actually reasons over.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO labels (label_type, loop_id, modality, service_code, tier, "
                "actor_role, pack_version, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (label_type, loop_id, modality, service_code, tier, actor_role,
                 pack_version, datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def labels(self) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM labels ORDER BY label_id")]
        finally:
            conn.close()
```

- [ ] **Step 4: Add `attach_orphan` and `undo_match` to `registry.py`**

Add to the `Registry` class, after `merge_patient`:

```python
    def attach_orphan(self, orphan_id: str, target_loop_id: str, actor: str, role: str) -> None:
        """A coordinator says this unmatched result belongs to this loop.

        The result is applied through record_result, so the preliminary rule
        still holds -- manual attachment must not become a back door around
        'preliminary never closes'.
        """
        orphan = self.get(orphan_id)
        if orphan.state is not LoopState.ORPHAN:
            raise ReferralLoopError(f"{orphan_id} is not an orphan")

        obx11 = self._latest_result_status(orphan_id) or FINAL
        self.record_result(target_loop_id, obx11=obx11, control_id=f"ATTACH:{orphan_id}")

        self.store.append_event(
            LoopEvent(
                orphan_id, "cancelled", _now(), f"ATTACH:{target_loop_id}",
                {"attached_to": target_loop_id},
            )
        )
        target = self.get(target_loop_id)
        self.store.record_label(
            label_type="orphan_attached",
            loop_id=target_loop_id,
            modality=target.modality,
            service_code=target.service_code,
            actor_role=role,
        )

    def undo_match(self, loop_id: str, actor: str, role: str) -> None:
        """A coordinator says this auto-match was wrong. Return the loop to OPEN
        and record a labeled false positive -- worth more than a synthetic case."""
        loop = self.get(loop_id)
        if loop.state is not LoopState.RESULTED:
            raise ReferralLoopError(f"Cannot undo a match on a loop in state {loop.state}")

        self.store.append_event(
            LoopEvent(loop_id, "created", _now(), "UNDO", {"obx11": ""})
        )
        self.store.record_label(
            label_type="false_positive",
            loop_id=loop_id,
            modality=loop.modality,
            service_code=loop.service_code,
            actor_role=role,
        )
```

- [ ] **Step 5: Add the routes to `worklist.py`**

Add inside `create_blueprint`, before `return bp`:

```python
    @bp.post("/<loop_id>/attach")
    def attach(loop_id: str):
        payload = request.get_json(silent=True) or {}
        target = payload.get("target_loop_id")
        actor = payload.get("actor")
        role = payload.get("role")
        if not target or not actor or not role:
            return jsonify({"error": "target_loop_id, actor and role are all required"}), 400
        try:
            registry.attach_orphan(loop_id, target, actor=actor, role=role)
        except ReferralLoopError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"orphan_id": loop_id, "attached_to": target})

    @bp.post("/<loop_id>/undo-match")
    def undo(loop_id: str):
        payload = request.get_json(silent=True) or {}
        actor = payload.get("actor")
        role = payload.get("role")
        if not actor or not role:
            return jsonify({"error": "actor and role are both required"}), 400
        try:
            registry.undo_match(loop_id, actor=actor, role=role)
        except ReferralLoopError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"loop_id": loop_id, "state": registry.get(loop_id).state.value})
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_orphan_attach.py -v`
Expected: PASS — 6 passed

- [ ] **Step 7: Commit**

```bash
git add healthcare_rag/referral_loop/registry.py healthcare_rag/referral_loop/store.py healthcare_rag/referral_loop/worklist.py tests/referral_loop/test_orphan_attach.py
git commit -m "feat(referral): orphan attachment and match-undo produce labeled examples"
```

---

### Task 16: Retention and purge

**Files:**
- Create: `healthcare_rag/referral_loop/retention.py`
- Modify: `healthcare_rag/referral_loop/cli.py` (add `purge` mode)
- Test: `tests/referral_loop/test_retention.py`

Spec §6: retention is configured, not assumed. Raw messages and closed loops both need a purge policy the hospital sets. Shipping without one means the archive grows without bound on someone else's disk, holding PHI past whatever their policy allows.

- [ ] **Step 1: Write the failing test**

```python
# tests/referral_loop/test_retention.py
from datetime import datetime, timedelta, timezone

import pytest

from healthcare_rag.referral_loop.errors import ReferralLoopError
from healthcare_rag.referral_loop.events import LoopEvent
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.retention import RetentionPolicy, purge
from healthcare_rag.referral_loop.store import LoopStore

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def test_policy_must_be_configured_never_defaulted():
    """An unset retention period is a policy decision the hospital has not made."""
    with pytest.raises(ReferralLoopError):
        RetentionPolicy.from_env({})


def test_policy_reads_both_periods_from_env():
    policy = RetentionPolicy.from_env(
        {"REFERRAL_RAW_RETENTION_DAYS": "30", "REFERRAL_CLOSED_RETENTION_DAYS": "365"}
    )
    assert policy.raw_days == 30
    assert policy.closed_days == 365


def test_purge_removes_raw_messages_past_the_window(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    store.record_raw("OLD", "MSH|old")
    store.record_raw("NEW", "MSH|new")
    store.backdate_raw("OLD", NOW - timedelta(days=40))

    policy = RetentionPolicy(raw_days=30, closed_days=365)
    report = purge(store, policy, now=NOW)

    assert report["raw_deleted"] == 1
    assert store.raw_count() == 1


def test_purge_never_touches_an_open_loop(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    reg = Registry(store)
    open_id = reg.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    store.append_event(LoopEvent(open_id, "created", NOW - timedelta(days=9999), "C1", {"mrn": "MRN1"}))

    policy = RetentionPolicy(raw_days=1, closed_days=1)
    purge(store, policy, now=NOW)

    assert store.replay(open_id).state.value == "OPEN"


def test_purge_removes_closed_loops_past_the_window(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    reg = Registry(store)
    loop_id = reg.open_loop(mrn="MRN1", modality="CT", control_id="C1")
    reg.record_result(loop_id, obx11="F", control_id="C2")
    reg.acknowledge(loop_id, actor="c1", role="coordinator", control_id="C3")
    store.backdate_loop(loop_id, NOW - timedelta(days=400))

    policy = RetentionPolicy(raw_days=30, closed_days=365)
    report = purge(store, policy, now=NOW)

    assert report["loops_deleted"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/referral_loop/test_retention.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'healthcare_rag.referral_loop.retention'`

- [ ] **Step 3: Add the backdating and purge helpers to `store.py`**

Purge is the one operation permitted to delete, so it uses an unguarded connection. Add these methods to `LoopStore`:

```python
    def backdate_raw(self, control_id: str, when: datetime) -> None:
        """Test/migration helper: set a raw message's received_at directly."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE raw_messages SET received_at = ? WHERE control_id = ?",
                (when.isoformat(), control_id),
            )
            conn.commit()
        finally:
            conn.close()

    def backdate_loop(self, loop_id: str, when: datetime) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE loop_events SET occurred_at = ? WHERE loop_id = ?",
                (when.isoformat(), loop_id),
            )
            conn.commit()
        finally:
            conn.close()

    def purge_raw_before(self, cutoff: datetime) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM raw_messages WHERE received_at < ?", (cutoff.isoformat(),))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def purge_loops_before(self, cutoff: datetime, states: tuple[str, ...]) -> int:
        """Delete terminal loops and their events. Never called for open states."""
        placeholders = ",".join("?" for _ in states)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT loop_id FROM loops WHERE state IN ({placeholders})", states
            ).fetchall()
            doomed = []
            for row in rows:
                last = conn.execute(
                    "SELECT MAX(occurred_at) AS last FROM loop_events WHERE loop_id = ?",
                    (row["loop_id"],),
                ).fetchone()["last"]
                if last and datetime.fromisoformat(last) < cutoff:
                    doomed.append(row["loop_id"])
            for loop_id in doomed:
                conn.execute("DELETE FROM loop_events WHERE loop_id = ?", (loop_id,))
                conn.execute("DELETE FROM loops WHERE loop_id = ?", (loop_id,))
            conn.commit()
            return len(doomed)
        finally:
            conn.close()
```

Note these use `self._connect()`, not `self._guarded()` — the authorizer that makes `loop_events` append-only would otherwise block the purge. Purge is the single sanctioned exception and lives in one place for exactly that reason.

- [ ] **Step 4: Write `retention.py`**

```python
# healthcare_rag/referral_loop/retention.py
"""Retention is configured, not assumed.

Raw messages and closed loops both need a purge policy the hospital sets. There
is no default: an unset retention period means the site has not made the
decision yet, and guessing one on their behalf would hold PHI past whatever
their policy actually allows.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import ReferralLoopError
from .store import LoopStore

logger = logging.getLogger(__name__)

# Only terminal states are ever purgeable. An open loop is never deleted by age.
_PURGEABLE_STATES = ("CLOSED", "CANCELLED")


@dataclass(frozen=True)
class RetentionPolicy:
    raw_days: int
    closed_days: int

    @classmethod
    def from_env(cls, env: dict | None = None) -> "RetentionPolicy":
        env = os.environ if env is None else env
        raw = env.get("REFERRAL_RAW_RETENTION_DAYS")
        closed = env.get("REFERRAL_CLOSED_RETENTION_DAYS")
        if not raw or not closed:
            raise ReferralLoopError(
                "Retention is a site policy decision with no safe default. Set "
                "REFERRAL_RAW_RETENTION_DAYS and REFERRAL_CLOSED_RETENTION_DAYS."
            )
        return cls(raw_days=int(raw), closed_days=int(closed))


def purge(store: LoopStore, policy: RetentionPolicy, now: datetime | None = None) -> dict:
    """Delete aged raw messages and terminal loops. Open loops are untouchable."""
    now = now or datetime.now(timezone.utc)

    raw_deleted = store.purge_raw_before(now - timedelta(days=policy.raw_days))
    loops_deleted = store.purge_loops_before(
        now - timedelta(days=policy.closed_days), _PURGEABLE_STATES
    )

    report = {"raw_deleted": raw_deleted, "loops_deleted": loops_deleted}
    logger.info("Retention purge: %s", report)
    return report
```

- [ ] **Step 5: Add the `purge` mode to `cli.py`**

Change the `mode` argument choices line to:

```python
    parser.add_argument("mode", choices=["listen", "filedrop", "worklist", "purge"])
```

And add this branch before the final `else` (the worklist branch):

```python
    elif args.mode == "purge":
        from .retention import RetentionPolicy, purge as run_purge
        report = run_purge(store, RetentionPolicy.from_env())
        logger.info("Purge complete: %s", report)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/referral_loop/test_retention.py -v`
Expected: PASS — 5 passed

- [ ] **Step 7: Run the full referral suite**

Run: `python -m pytest tests/referral_loop/ -v --tb=short`
Expected: PASS — every test from Tasks 1-16

- [ ] **Step 8: Commit**

```bash
git add healthcare_rag/referral_loop/retention.py healthcare_rag/referral_loop/store.py healthcare_rag/referral_loop/cli.py tests/referral_loop/test_retention.py
git commit -m "feat(referral): site-configured retention with no default"
```

---

## Success criteria verification

Run these at the end and paste the output. Each maps to a numbered criterion in spec §10.

| # | Criterion | Command |
|---|---|---|
| 1 | Synthetic stream runs end-to-end, zero model calls, zero non-loopback | `python -m pytest tests/referral_loop/test_proofs.py -v` |
| 2 | All four safety tests pass | `python -m pytest tests/referral_loop/test_registry_safety.py tests/referral_loop/test_merge.py -v` |
| 3 | PHI-sentinel proof passes on every artifact | `python -m pytest tests/referral_loop/test_proofs.py -k sentinel -v` |
| 4 | False-close rate zero at the floor | `python -m pytest tests/referral_loop/test_proofs.py -k false_close -v` |
| 5 | State reconstructible from `loop_events` | `python -m pytest tests/referral_loop/test_store.py -k reconstructible -v` |
| 6 | No ChromaDB or corpus required | `python -m pytest tests/referral_loop/test_import_closure.py -v` |

---

## Still open after this plan

These are carried from spec §12 and are **not** resolved by building it. Two were made non-blocking by construction; one was not.

1. **MLLP vs file-drop for the pilot** — *no longer blocking.* Both are built (Task 10); file-drop was required for archive replay anyway. Pick per site.
2. **Who acknowledges a loop** — *still open, and still the one most likely to change the product.* Task 6 records `ack_role` on every close and Task 11 renders "Acknowledged by \<role\>" rather than "Closed", so the record stays interpretable either way. But if the coordinator is not clinically responsible, `CLOSED` means "someone looked at it" and the safety claim is weaker than the worklist reads. Resolve with a real site before building anything further on top of the worklist.
3. **Staleness thresholds as defaults** — *no longer blocking.* Task 9 ships defaults but refuses to compute staleness until `REFERRAL_THRESHOLDS_ACCEPTED=1`, making the threshold the site's explicit clinical decision.
