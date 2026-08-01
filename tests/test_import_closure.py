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
from pathlib import Path

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
import referral_loop
import referral_loop.errors
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
    (a later task), which monkeypatches anthropic and claude_cli to raise and runs
    the whole suite -- an assertion about behavior, not about the import graph. A
    module being importable is not a model call.
    """
    assert "anthropic" in _run_probe(), (
        "If anthropic is no longer in the closure the parent package changed; "
        "re-check that spec test 6 still proves what it claims."
    )


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
