"""Success criterion 6, asserted against the image rather than `sys.modules`.

The referral container must not contain the ML stack. **An import-closure test
cannot show this.** chromadb can be installed and simply never imported, which
is exactly what `pip install ".[referral]"` produces -- chromadb,
sentence-transformers, lightrag-hku, raganything[all], mcp, ollama, biopython
and three tree-sitter packages are unconditional core dependencies of
healthcare-rag, so the extra installs all of them and torch besides.
`test_import_closure.py` would pass against that image, unchanged, while a
security team reviewing it counted several gigabytes of machine learning in a
build whose entire claim is that it contains none.

So the assertion moves from what gets imported to what gets installed, and its
subject is the built image.

Two things keep this from passing vacuously, which matters more here than usual
because the whole test is a negative:

  * `pip list` is asserted to contain flask and cryptography. Without that, an
    empty listing -- a `docker run` that silently produced nothing -- reads as a
    clean image.
  * `--help` is run inside the image. A container that cannot import its own
    entry point also ships no forbidden distributions, and would otherwise pass
    every assertion below.

If Docker is unavailable the Docker-dependent tests skip, and success criterion
6 is then **unverified**. It is not covered by the import-closure test and must
not be reported as though it were. The static tests below still run: they hold
the Dockerfile to the decision recorded in the plan, which is worth something on
a machine without a daemon but is not the criterion.
"""
from __future__ import annotations

import functools
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile.referral"
IMAGE = "referral-loop:test"

# Distribution names as pip reports them, not import names.
FORBIDDEN_DISTRIBUTIONS = [
    "chromadb", "sentence-transformers", "torch", "transformers",
    "lightrag-hku", "raganything", "mcp", "ollama", "biopython",
    "numpy", "tree-sitter", "anthropic", "healthcare-rag",
]

# Files that must not be in the image even though they sit beside ones that are.
# tenant_isolation is the pointed one: spec section 3 keeps it out because
# shipping an unexercised isolation control suggests a guarantee v1 does not
# test, and a `COPY healthcare_rag/guardrails/` would ship it without a word.
FORBIDDEN_PATHS = [
    "/app/healthcare_rag/db.py",
    "/app/healthcare_rag/audit_trail.py",
    "/app/healthcare_rag/claude_cli.py",
    "/app/healthcare_rag/api.py",
    "/app/healthcare_rag/guardrails/tenant_isolation.py",
    "/app/healthcare_rag/guardrails/middleware.py",
    "/app/healthcare_rag/guardrails/phi_redactor.py",
    "/app/healthcare_rag/revenue_integrity",
    "/app/healthcare_rag/denial_rca",
]

REQUIRED_PATHS = [
    "/app/healthcare_rag/__init__.py",
    "/app/healthcare_rag/encryption_check.py",
    "/app/healthcare_rag/guardrails/immutable_audit.py",
    "/app/healthcare_rag/referral_loop/cli.py",
    "/app/healthcare_rag/referral_loop/rules/pack.json",
    "/app/healthcare_rag/referral_loop/rules/pack.sig",
]

# site-packages in the built image measures about 29 MB (flask, cryptography and
# their transitive dependencies). The ceiling is not a tuning target -- it is a
# tripwire, and the thing it trips on is enormous: torch alone is measured in
# gigabytes.
MAX_IMAGE_MEGABYTES = 300


@functools.lru_cache(maxsize=1)
def _docker_reason() -> str | None:
    """None if Docker can actually build, otherwise why not.

    Checking `shutil.which("docker")` alone is not enough and the difference is
    not theoretical: Docker Desktop installs the client and leaves the daemon
    stopped, so the binary exists, `docker build` fails to connect, and a
    `skipif(which(...) is None)` turns a missing daemon into a red suite instead
    of a skip -- which is how a criterion ends up quietly marked verified by
    someone silencing the failure.
    """
    if shutil.which("docker") is None:
        return "docker client not installed"
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker info failed: {exc}"
    if proc.returncode != 0:
        return f"docker daemon unreachable: {proc.stderr.strip().splitlines()[:1]}"
    return None


_skip_without_docker = pytest.mark.skipif(
    _docker_reason() is not None,
    reason=f"success criterion 6 UNVERIFIED -- {_docker_reason()}",
)


def requires_docker(test):
    """Skip without a daemon, and carry the `docker` marker either way.

    The marker is what lets the spec 12/13 meta-test deselect these from its
    inner run with `-m "not docker"`. It is applied unconditionally -- including
    on a machine with no daemon, where the test would only skip -- because a
    selector that changed meaning with the environment is worse than either
    behaviour on its own.
    """
    return pytest.mark.docker(_skip_without_docker(test))


@pytest.fixture(scope="session")
def referral_image() -> str:
    build = subprocess.run(
        ["docker", "build", "-f", str(DOCKERFILE), "-t", IMAGE, str(REPO_ROOT)],
        capture_output=True, text=True, timeout=3600,
    )
    if build.returncode != 0:
        pytest.fail(f"docker build failed:\n{build.stdout[-4000:]}\n{build.stderr[-4000:]}")
    return IMAGE


def _in_image(image: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", image, *argv],
        capture_output=True, text=True, timeout=600,
    )


def _installed(image: str) -> set[str]:
    proc = _in_image(image, "pip", "list", "--format=freeze")
    assert proc.returncode == 0, proc.stderr
    return {line.split("==")[0].strip().lower() for line in proc.stdout.splitlines() if line.strip()}


# ------------------------------------------------------------- against the image


@requires_docker
def test_the_listing_is_not_empty(referral_image):
    """Anti-vacuity. Every test below is a negative; this one is the positive
    that makes them mean something."""
    installed = _installed(referral_image)
    assert "flask" in installed, installed
    assert "cryptography" in installed, installed


@requires_docker
def test_referral_image_contains_no_ml_stack(referral_image):
    installed = _installed(referral_image)
    leaked = sorted(d for d in FORBIDDEN_DISTRIBUTIONS if d.lower() in installed)
    assert leaked == [], f"referral image ships forbidden distributions: {leaked}"


@requires_docker
def test_referral_image_has_no_model_client_installed(referral_image):
    """Structural, not behavioral.

    Spec test 13 proves "no model calls" in the dev environment by making
    `anthropic` raise. This proves production cannot make one, because no client
    is present to call. `healthcare_rag/__init__.py` wraps `install_shim()` in
    try/except, so the package imports cleanly without it.
    """
    assert "anthropic" not in _installed(referral_image)
    listing = _in_image(referral_image, "pip", "list", "--format=freeze").stdout.lower()
    assert "anthropic==" not in listing


@requires_docker
def test_the_entry_point_runs_inside_the_image(referral_image):
    """An image that cannot import its own CLI would pass every negative above.

    Also the only proof that the Dockerfile's file-by-file COPY set is complete:
    a missing module surfaces here as an ImportError and nowhere else.
    """
    proc = _in_image(referral_image, "python", "-m", "healthcare_rag.referral_loop.cli", "--help")
    assert proc.returncode == 0, proc.stderr
    for mode in ("listen", "filedrop", "worklist", "purge"):
        assert mode in proc.stdout


@requires_docker
def test_the_image_refuses_to_boot_without_the_public_key(referral_image):
    """The boot gates hold in the image, not only under pytest."""
    proc = _in_image(referral_image, "python", "-m", "healthcare_rag.referral_loop.cli",
                     "filedrop")
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "REFERRAL_PACK_PUBKEY" in proc.stderr
    assert "Traceback" not in proc.stderr


@requires_docker
def test_the_image_carries_only_the_modules_referral_loop_reaches(referral_image):
    probe = (
        "import os,sys;"
        "print('\\n'.join(p for p in sys.argv[1:] if os.path.exists(p)))"
    )
    proc = _in_image(referral_image, "python", "-c", probe, *FORBIDDEN_PATHS)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"image ships files it should not:\n{proc.stdout}"

    proc = _in_image(referral_image, "python", "-c", probe, *REQUIRED_PATHS)
    assert sorted(proc.stdout.split()) == sorted(REQUIRED_PATHS), (
        "the anti-vacuity half: these must exist, or the probe above is checking nothing"
    )


@requires_docker
def test_the_image_does_not_run_as_root(referral_image):
    """This process holds PHI. A container escape as uid 0 is host root."""
    proc = _in_image(referral_image, "python", "-c", "import os; print(os.getuid())")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != "0"


@requires_docker
def test_no_build_machine_bytecode_was_copied_into_the_image(referral_image):
    """The repository's .dockerignore listed `__pycache__/` and shipped it anyway.

    Docker's .dockerignore is not .gitignore: a bare `__pycache__/` matches only
    at the root of the build context, so every nested one was copied. Sixteen
    .pyc files from the build machine were in the first image built here. A
    stale .pyc whose source has since changed is what actually executes when its
    embedded size and mtime still match, which means the image can run code that
    is not visible in it -- and the image content otherwise depends on whatever
    a developer happened to have cached.
    """
    probe = (
        "import os;"
        "print('\\n'.join(os.path.join(r, f) for r, _, fs in os.walk('/app') for f in fs "
        "if f.endswith('.pyc')))"
    )
    proc = _in_image(referral_image, "python", "-c", probe)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"build-machine bytecode in the image:\n{proc.stdout}"


@requires_docker
def test_the_installed_stack_is_small_enough_that_nothing_could_be_hiding(referral_image):
    """Measured inside the image, on site-packages, not from `docker inspect`.

    `docker image inspect --format {{.Size}}` and `docker images --format
    {{.Size}}` disagreed by a factor of four on this image (51 MB against 220
    MB) under the containerd image store, and neither is documented as "the
    bytes a security team will review". site-packages is unambiguous, and it is
    the thing success criterion 6 is actually about: torch alone is measured in
    gigabytes, and this reads about 29 MB.
    """
    probe = (
        "import os, sysconfig;"
        "p = sysconfig.get_paths()['purelib'];"
        "print(sum(os.path.getsize(os.path.join(r, f)) "
        "for r, _, fs in os.walk(p) for f in fs))"
    )
    proc = _in_image(referral_image, "python", "-c", probe)
    assert proc.returncode == 0, proc.stderr
    megabytes = int(proc.stdout.strip()) / (1024 * 1024)
    assert 1 < megabytes < MAX_IMAGE_MEGABYTES, (
        f"site-packages is {megabytes:.0f} MB (a near-zero reading means the probe, "
        "not the image, is what is empty)"
    )


# ------------------------------------------------------- without a daemon at all


def test_the_dockerfile_does_not_install_the_parent_package():
    """Guards the decision, on machines that cannot check the artifact.

    `pip install ".[referral]"` is the wrong answer here for a reason that is
    invisible from the extras list: the ML stack is in `dependencies`, not in an
    extra, so the referral extra installs it whether it wants to or not. Nothing
    about `.[referral]` looks wrong, which is exactly why this needs an
    assertion rather than a comment.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    installs = [line for line in text.splitlines()
                if re.search(r"pip\s+install", line) and not line.strip().startswith("#")]
    assert installs, "the Dockerfile installs nothing at all"
    for line in installs:
        assert "[referral]" not in line, line
        assert not re.search(r"pip\s+install[^|&]*\s\.(\s|$|\[)", line), line
        assert "-r " not in line, f"requirements file would reintroduce the parent's deps: {line}"


def test_the_dockerfile_copies_files_not_the_guardrails_directory():
    """`COPY healthcare_rag/guardrails/` would ship tenant_isolation.py, which
    spec section 3 keeps out deliberately, and phi_redactor and middleware with
    it."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    copies = [line for line in text.splitlines()
              if line.strip().startswith("COPY")]
    assert copies
    assert not any(re.search(r"COPY\s+healthcare_rag/guardrails/\s", line) for line in copies), copies
    assert any("immutable_audit.py" in line for line in copies)
    assert not any("tenant_isolation" in line for line in copies)
