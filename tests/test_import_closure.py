"""Success criterion 6: the referral install needs neither ChromaDB nor the corpus.

This MUST run in a clean subprocess. An in-process sys.modules snapshot passes
vacuously: under `pytest tests/` a forbidden module already imported by an
earlier test never appears as "newly imported", so the assertion silently proves
nothing about what this package pulls in. The subprocess is what makes the
closure the interpreter's whole closure rather than this test's share of it.

Same principle as spec test 7: assert on the real end state, not on a proxy.
"""
import ast
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

_CORE_PROBE = (
    "import referral_loop.core, referral_loop.core.states, referral_loop.core.models;"
    "import json,sys; print(json.dumps(sorted(sys.modules)))"
)


def _modules_in_a_clean_interpreter(probe: str) -> set[str]:
    """Run `probe` in a clean interpreter, return everything it loaded.

    Surfaces stderr on failure rather than using check=True: a real ImportError
    inside the probe would otherwise arrive as an opaque non-zero exit with the
    actual traceback swallowed.
    """
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        pytest.fail(f"probe failed (exit {proc.returncode}):\n{proc.stderr}")
    return set(json.loads(proc.stdout))


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
    # The domain layer must not reach the network any more than it reaches the store.
    # core/ has to stay callable from a batch job with no connector configured at all.
    "referral_loop.connect",
    "urllib.request",
    "ssl",
    "sqlite3",
    "flask",
    "jinja2",
    "cryptography",
)


def test_referral_import_closure_in_a_clean_interpreter():
    loaded = _modules_in_a_clean_interpreter(_PROBE)
    leaked = sorted(m for m in loaded if any(m == f or m.startswith(f + ".") for f in FORBIDDEN))
    assert leaked == [], f"referral_loop pulled in forbidden modules: {leaked}"


def test_the_domain_core_imports_no_protocol_persistence_or_projection_code():
    loaded = _modules_in_a_clean_interpreter(_CORE_PROBE)
    leaked = [m for m in loaded if any(m == f or m.startswith(f + ".") for f in CORE_FORBIDDEN)]
    assert not leaked, f"core/ reached outside the domain layer: {leaked}"


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
    assert "anthropic" not in _modules_in_a_clean_interpreter(_PROBE), (
        "referral_loop pulled in a model client; v1 makes no model calls, and "
        "spec 13 proves that behaviourally only for the clients it knows to poison."
    )


_SRC = Path(__file__).resolve().parents[1] / "src" / "referral_loop"

# The single permitted egress site. This is the property the README's narrowed claim rests on --
# "no model calls, and egress only to configured connectors" -- and it is a claim about which
# file contains the import, not about which modules a probe happened to load. So this reads
# source rather than sys.modules; an import-probe cannot express it.
EGRESS_MODULE = "connect/egress.py"
# socket is deliberately absent: mllp_server.py imports it directly for the inbound listener's
# socketserver-based TCP server, which is legitimate and has nothing to do with egress. Adding
# mllp_server.py to a per-file exemption list instead would have been worse -- an allowlist of
# exempt files is how this test stops meaning anything -- so the module is dropped from the set
# that applies to every file rather than one file being excused from the set.
_NETWORK_MODULES = {"urllib.request", "urllib.error", "http.client", "ftplib"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_only_the_egress_module_imports_a_network_library():
    offenders = {}
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative == EGRESS_MODULE:
            continue
        leaked = sorted(_imported_modules(path) & _NETWORK_MODULES)
        if leaked:
            offenders[relative] = leaked
    assert not offenders, (
        f"egress must stay confined to {EGRESS_MODULE}; these also import a network "
        f"library: {offenders}"
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
