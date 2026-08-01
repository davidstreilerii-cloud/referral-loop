"""Success criterion 6: the referral install needs neither ChromaDB nor the corpus.

This MUST run in a clean subprocess. An in-process sys.modules snapshot passes
vacuously: under `pytest tests/` a forbidden module already imported by an
earlier test never appears as "newly imported", so the assertion silently proves
nothing about what this package pulls in. The subprocess is what makes the
closure the interpreter's whole closure rather than this test's share of it.

Same principle as spec test 7: assert on the real end state, not on a proxy.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

# The ML-stack entries are what stop a convenience import dragging torch back in.
# `healthcare_rag` is here so a stray reference to the namespace this package was
# extracted from fails loudly instead of resolving against whatever copy of the
# monorepo happens to be installed. `anthropic` is here because there is no longer
# a parent package installing a CLI shim -- see the test below.
FORBIDDEN = [
    "chromadb", "sentence_transformers", "torch", "transformers",
    "lightrag", "raganything", "mcp", "ollama",
    "anthropic", "healthcare_rag",
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


def test_anthropic_is_not_in_the_closure_and_that_is_the_point():
    """The inverse of what this test asserted in the monorepo, and the reason is
    the extraction.

    There, `healthcare_rag/__init__.py` called `claude_cli.install_shim()`, which
    imports anthropic, so every `healthcare_rag.*` import pulled it in and the
    honest thing to do was document that it could not be avoided. There is no such
    parent package here. Nothing in this closure has any reason to reach for a
    model client, so anthropic appearing in it means someone added one.

    This is still an assertion about the import graph, not about behaviour --
    importing a module is not a model call. The behavioural claim is spec 13,
    which poisons anthropic and runs the whole suite against it.
    """
    assert "anthropic" not in _run_probe(), (
        "referral_loop pulled in a model client; v1 makes no model calls, and "
        "spec 13 proves that behaviourally only for the clients it knows to poison."
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
