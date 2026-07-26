# Dockerfile.referral -- the separate deployable.
#
# The image is the security-review surface, so anything not needed to track a
# referral loop must not be in it.
#
# Deliberately NOT `pip install ".[referral]"`. chromadb, sentence-transformers,
# lightrag-hku, raganything[all], mcp, ollama, biopython and three tree-sitter
# packages are *unconditional core dependencies* of healthcare-rag
# (pyproject.toml), so the extra drags the entire ML stack -- and torch,
# transitively -- into an image whose whole claim is that it contains none of
# it. The import-closure test passes against that image anyway, because those
# packages are installed and simply never imported. Installed is what a security
# team reviews. See "RESOLVED: success criterion 6" in the plan.
#
# Instead: copy only the modules referral_loop actually reaches, install an
# explicit pinned set, and put the package on PYTHONPATH.
#
# This works because healthcare_rag/__init__.py wraps install_shim() in
# try/except, so the package imports cleanly with claude_cli and anthropic
# absent. The useful consequence is that no model client exists in this image at
# all: "no model calls" becomes structurally true in production rather than
# enforced by a test. Spec test 13 still earns its place -- it proves the
# property in the dev environment, where anthropic *is* importable.
FROM python:3.12-slim

# flask serves the coordinator worklist; cryptography verifies the Ed25519 pack
# signature. Everything else the subsystem uses is stdlib -- sqlite3, socket,
# socketserver, hashlib, json. Nothing here reaches the network at runtime.
RUN pip install --no-cache-dir "flask>=3.1,<4.0" "cryptography>=42.0,<47"

WORKDIR /app

# One COPY per file, not one per directory. `COPY healthcare_rag/guardrails/`
# would ship tenant_isolation.py -- a control spec section 3 says is
# deliberately not imported, because shipping an unexercised isolation control
# suggests a guarantee this build does not test -- along with middleware.py and
# phi_redactor.py, neither of which referral_loop reaches.
#
# guardrails/__init__.py is omitted on purpose: audit.py loads immutable_audit
# by file path precisely so that importing it does not execute the guardrails
# package __init__, and the __init__ imports every module named above.
COPY healthcare_rag/__init__.py                     healthcare_rag/__init__.py
COPY healthcare_rag/encryption_check.py             healthcare_rag/encryption_check.py
COPY healthcare_rag/guardrails/immutable_audit.py   healthcare_rag/guardrails/immutable_audit.py
COPY healthcare_rag/referral_loop/                  healthcare_rag/referral_loop/

# The loop database and the append-only audit database both live here.
# immutable_audit resolves AUDIT_DB to /app/data/audit_trail.db from its own
# file location, so this directory is not optional.
#
# Non-root. This process holds PHI; a container running as uid 0 turns any
# escape into host root, and there is nothing in this image that needs it.
RUN mkdir -p /app/data \
 && useradd --system --uid 10001 --home /app referral \
 && chown -R referral:referral /app
USER referral

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHI_MODE=full

# Both must be supplied at run time, and the process refuses to start without
# them. Not baked in: REFERRAL_PACK_PUBKEY is the site's own key once they sign
# their own pack, and REFERRAL_THRESHOLDS_ACCEPTED is the site asserting that
# the shipped staleness thresholds are their clinical decision, not ours --
# defaulting it here would take that decision back (spec open question 3).
#   -e REFERRAL_PACK_PUBKEY=<hex>
#   -e REFERRAL_THRESHOLDS_ACCEPTED=1
#   -e PHI_ENCRYPTION_VERIFIED=1   (or an OS-detected encrypted volume)
#   -v /encrypted/volume:/app/data

EXPOSE 2575 5055

# 0.0.0.0 is this container's own network namespace, not the host's. Publish
# 2575 only to the interface engine. The worklist has no authentication and
# refuses any non-loopback bind, so it is reachable only from inside the
# container unless a reverse proxy on the host is put in front of it.
CMD ["python", "-m", "healthcare_rag.referral_loop.cli", "listen", \
     "--host", "0.0.0.0", "--port", "2575", "--db", "/app/data/referral_loops.db"]
