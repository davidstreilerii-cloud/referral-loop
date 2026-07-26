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

FORBIDDEN = [
    "chromadb", "sentence_transformers", "torch", "transformers",
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


def test_referral_import_closure_in_a_clean_interpreter():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    )
    loaded = set(json.loads(proc.stdout))
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
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    )
    loaded = set(json.loads(proc.stdout))
    assert "anthropic" in loaded, (
        "If anthropic is no longer in the closure the parent package changed; "
        "re-check that spec test 6 still proves what it claims."
    )
