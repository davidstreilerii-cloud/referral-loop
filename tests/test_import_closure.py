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
    "import referral_loop.core, referral_loop.core.states, referral_loop.core.models,"
    "referral_loop.core.transitions, referral_loop.core.machine;"
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
# The third-party clients are in the set even though none of them is installed: this check
# reads source, not sys.modules, so it costs nothing to name the libraries somebody would
# actually reach for, and naming them is the difference between a check that fires the day
# `requests` is added to pyproject.toml and one that has to be remembered and updated then.
_NETWORK_MODULES = {
    "urllib.request", "urllib.error", "http.client", "ftplib",
    "requests", "httpx", "urllib3", "aiohttp",
}


def _imported_modules(path: Path) -> set[str]:
    """Every module name this file imports, under every spelling of the import.

    `from urllib import request` binds the same callable as `import urllib.request`, so
    both have to arrive here as "urllib.request" or `_NETWORK_MODULES` polices a naming
    convention instead of a capability -- and the README points reviewers at this file.
    Hence the dotted join for each alias of an `ImportFrom`. The bare `node.module` is
    still recorded alongside it, because `from requests import get` has to be caught by
    the entry "requests", and dropping it would trade one blind spot for another.

    `node.level == 0` keeps relative imports out: `from . import request` inside this
    package is not urllib, and joining it onto the parent's name would make it look like it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
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


# The control above is only as good as the spellings it recognises. README points reviewers
# at this file as "a test that fails the build if a second [importer] appears", so a spelling
# it cannot see is a one-line evasion of an advertised guarantee -- worse than not advertising
# it. These parametrised cases are the evasions, written as source rather than described:
# `from urllib import request` binds exactly the same callable as `import urllib.request`,
# and an AST walk that records only `node.module` sees "urllib" for the first and
# "urllib.request" for the second.
_EVASIONS = [
    ("import urllib.request", "urllib.request"),
    ("from urllib import request", "urllib.request"),
    ("from urllib.request import urlopen", "urllib.request"),
    ("from urllib import error", "urllib.error"),
    ("import http.client", "http.client"),
    ("from http import client", "http.client"),
    ("from http.client import HTTPSConnection", "http.client"),
    ("import ftplib", "ftplib"),
    ("from urllib import request as _r", "urllib.request"),
    ("from urllib import parse, request", "urllib.request"),
]


@pytest.mark.parametrize("source,expected", _EVASIONS)
def test_the_egress_check_sees_every_spelling_of_a_network_import(tmp_path, source, expected):
    """Every way of naming the same module has to land in the same set entry, or the
    closure test above polices a naming convention rather than a capability."""
    probe = tmp_path / "probe.py"
    probe.write_text(source + "\n", encoding="utf-8")
    found = _imported_modules(probe)
    assert expected in found, f"{source!r} was recorded as {sorted(found)}"
    assert _imported_modules(probe) & _NETWORK_MODULES, (
        f"{source!r} would pass the egress closure test unnoticed"
    )


@pytest.mark.parametrize(
    "source",
    [
        "import requests",
        "from requests import get",
        "import httpx",
        "from httpx import AsyncClient",
        "import urllib3",
        "from urllib3 import PoolManager",
        "import aiohttp",
        "from aiohttp import ClientSession",
    ],
)
def test_third_party_http_clients_count_as_network_libraries(tmp_path, source):
    """None of these is in the install closure today, which is exactly why the check has
    to name them: the failure mode is somebody adding `requests` to pyproject.toml and an
    import of it to a module that is not egress.py, and a stdlib-only set would not notice."""
    probe = tmp_path / "probe.py"
    probe.write_text(source + "\n", encoding="utf-8")
    assert _imported_modules(probe) & _NETWORK_MODULES, (
        f"{source!r} opens connections and is not treated as a network import"
    )


def test_a_relative_import_is_not_mistaken_for_a_network_module(tmp_path):
    """`from . import request` inside the package is not urllib. `node.level == 0` is what
    keeps the check from firing on it, and widening the check must not lose that."""
    probe = tmp_path / "probe.py"
    probe.write_text("from . import request\nfrom .errors import x\n", encoding="utf-8")
    assert not (_imported_modules(probe) & _NETWORK_MODULES)


def test_fetch_is_the_only_place_in_egress_that_opens_a_connection():
    """The closure test above polices which file may import urllib.request. This polices how
    many call sites inside that file reach the network.

    check_allowed runs in fetch, so "egress is bounded to configured connectors" is true only
    while fetch is the sole caller of .open(). build_opener is public and returns a generic
    opener bound to no checked destination -- a pagination or streaming helper added later
    inside egress.py, the one file allowed to touch urllib.request, would pass every other
    test in this suite while breaking the README's central claim.
    """
    tree = ast.parse((_SRC / "connect" / "egress.py").read_text(encoding="utf-8"))
    openers: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "open"
            ):
                openers.setdefault(node.name, []).append(inner.lineno)
    assert set(openers) == {"fetch"}, (
        "only fetch may open a connection, because only fetch calls check_allowed first; "
        f"found .open() in {openers}"
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


def test_clock_imports_nothing_from_the_package():
    """`clock.py` is the layer everything else's time handling sits on, and that is
    only true while it depends on nothing above it.

    Its docstring makes the claim in so many words -- "this module imports nothing
    from the package, so it sits beneath the matcher, the registry, the listener and
    staleness alike" -- and the claim has since been leaned on twice: once for
    `MAX_CLOCK_SKEW`, and again for `as_utc`, which this module, the matcher, the
    registry, staleness, the store and the worklist had each written out for
    themselves -- six byte-identical copies -- before one of them moved here. Seven
    modules import `clock` as a result, and that is exactly the fan-in at which a
    single convenience import back up the stack becomes an import cycle. A cycle
    would surface as an ImportError at whichever module happened to be imported
    first, which is a failure that looks like a bug in the importer rather than in
    the module that caused it.

    Asserted on the source and not by probing `sys.modules`, for the same reason the
    egress check reads source: an import probe answers "what got loaded", and what
    got loaded is a property of import order. This is a claim about what is written
    in one file.

    `node.level > 0` is the whole check on the relative side -- `from .errors import`
    is the spelling anything in this package would actually use -- with the absolute
    spelling covered too, because `import referral_loop.errors` binds the same module
    and would otherwise walk straight past.
    """
    tree = ast.parse((_SRC / "clock.py").read_text(encoding="utf-8"), filename="clock.py")
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            reached.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("referral_loop"):
            reached.add(node.module or "")
        elif isinstance(node, ast.Import):
            reached.update(a.name for a in node.names if a.name.startswith("referral_loop"))
    assert not reached, (
        "clock.py must import nothing from referral_loop -- everything else's time "
        f"policy is layered on top of it; found {sorted(reached)}"
    )
