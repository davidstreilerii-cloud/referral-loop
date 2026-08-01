# The referral-loop deployable.
#
# The image is the security-review surface, so anything not needed to track a
# referral loop must not be in it. That the image contains no ML stack and no
# model client is a product claim, and tests/test_install_closure.py asserts it
# against the built image rather than against this file -- a Dockerfile that
# looks right and an image that is right are different things.
FROM python:3.12-slim

WORKDIR /app

# Install from the package metadata rather than by copying modules one at a
# time. The monorepo could not do this: its `referral` extra sat inside a
# project whose *unconditional* dependencies were chromadb,
# sentence-transformers, lightrag-hku, raganything[all], mcp, ollama, biopython
# and three tree-sitter packages, so `pip install .[referral]` dragged the whole
# stack -- and torch, transitively -- into an image whose entire claim is that it
# holds none of it. Here the base dependency list is one line long, which is the
# point of the extraction.
#
# `[worklist]` adds flask, and nothing else: the coordinator queue is an HTTP
# surface and this image serves it. Everything else the subsystem uses is stdlib
# -- sqlite3, socket, socketserver, hashlib, json. Nothing here reaches the
# network at runtime.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[worklist]" \
 && rm -rf /app/src /app/pyproject.toml
# The build inputs are deleted in the same layer that installs them. /app/src
# would otherwise be a second copy of every module, free to drift from the
# installed one in site-packages that actually executes, and a reviewer would
# have to work out which of the two the container runs.
#
# Note this empties the final filesystem, not the earlier COPY layers, which
# still hold what they copied -- so it is the .dockerignore, not this `rm`, that
# keeps build-machine __pycache__ and the PHI-bearing `data/` directory out of
# the image altogether. The two are doing different jobs; neither replaces the
# other.

# The loop database and the append-only audit database both live here, on the
# volume a deployment mounts. After the rm above this is the only thing in /app.
#
# Non-root. This process holds PHI; a container running as uid 0 turns any
# escape into host root, and nothing in this image needs it.
RUN mkdir -p /app/data \
 && useradd --system --uid 10001 --home /app referral \
 && chown -R referral:referral /app
USER referral

# REFERRAL_AUDIT_DB is not optional here. Its default is package-relative, which
# in a source checkout resolves to <repo>/data beside the loop database -- but
# from site-packages it lands beside site-packages itself, which is read-only in
# this image and is nowhere PHI-adjacent state belongs. Unset, the container's
# first audit write fails, and audit.py swallows write failures by design, so
# the symptom would be an empty audit trail rather than an error.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHI_MODE=full \
    REFERRAL_AUDIT_DB=/app/data/audit_trail.db

# 2575 MLLP, 5055 coordinator worklist.
#
# 5055 is the CLI default (`--worklist-port`), and the CLI is this image's entry
# point, so 5055 is what a `worklist` container actually binds. Note that
# `make_worklist_server`'s own signature defaults to 5057 and the worklist tests
# use 5057 throughout: the library default and the CLI default disagree in the
# extracted tree exactly as they did in the monorepo. Left alone here on
# purpose -- reconciling them is a code change, and this extraction does not make
# any. Pass --worklist-port explicitly and the question does not arise.
EXPOSE 2575 5055

# Required at run time, no defaults, and each refuses the boot rather than
# assuming a value. Not baked in: REFERRAL_PACK_PUBKEY is the site's own key
# once they sign their own pack, and REFERRAL_THRESHOLDS_ACCEPTED is the site
# asserting that the shipped staleness thresholds are their clinical decision,
# not ours -- defaulting it here would take that decision back.
#   -e REFERRAL_PACK_PUBKEY=<hex>
#   -e REFERRAL_THRESHOLDS_ACCEPTED=1
#   -e PHI_ENCRYPTION_VERIFIED=1   (or an OS-detected encrypted volume)
#   -v /encrypted/volume:/app/data
#
# purge mode additionally requires REFERRAL_RAW_RETENTION_DAYS and
# REFERRAL_RESOLVED_RETENTION_DAYS, which likewise have no defaults.
#
# Capacity planning for that volume. `--older-than` retention ages out the raw
# HL7 archive and resolved loops once the site configures a period, but two
# tables are excluded from it by design (retention.py's module docstring):
# applied_messages (the idempotency ledger -- deleting a row re-arms double
# application of a redelivered message) and mrn_alias_events / mrn_aliases (a
# merge is a permanent fact; expiring one silently re-strands a loop). Both grow
# without bound for the life of the install. Estimated, and later withdrawn as unmeasurable: ~166
# bytes/row for applied_messages, ~342 bytes/row for the alias tables combined,
# both including their indexes. At sustained volumes of 1,000 / 10,000 / 50,000
# HL7 messages per day, applied_messages alone (one row per applied message, 1:1
# with traffic) reaches roughly 58MB / 577MB / 2.8GB after one year and
# 289MB / 2.8GB / 14GB after five -- the alias tables stay well under that even
# at a generous assumed merge rate. `referral-loop stats --db
# /app/data/referral_loops.db` reports the real figures for this install rather
# than the projection above.

# 0.0.0.0 is this container's own network namespace, not the host's. Publish
# 2575 only to the interface engine. The worklist has no authentication and
# refuses any non-loopback bind, so it is reachable only from inside the
# container unless a reverse proxy on the host is put in front of it.
CMD ["referral-loop", "listen", \
     "--host", "0.0.0.0", "--port", "2575", "--db", "/app/data/referral_loops.db"]
