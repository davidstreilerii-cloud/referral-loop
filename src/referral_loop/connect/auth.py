"""SMART Backend Services: the assertion we sign, and the token it buys.

There is no `now()` anywhere in this module. Every function that needs the current time takes
`now: datetime | None = None` and falls back to `datetime.now(timezone.utc)` -- the convention
clock.py already uses for is_future_dated and is_readable_clock, and what makes `exp` and the
cache refresh margin deterministically testable.

clock.py itself is not used here, and the distinction is worth keeping: it is a validation
module answering "is this attacker-supplied HL7 timestamp trustworthy", not a time source. It
exposes no now(). Routing a JWT exp through a guard built for MSH-7 skew would be a category
error.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import urllib.parse  # parse only -- urllib.request lives in egress.py and the AST test enforces it
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..errors import ReferralLoopError
from .connectors import ConnectorProfile, ConnectorRegistry
from .egress import fetch

logger = logging.getLogger(__name__)

# The SMART spec caps this at five minutes. Stated as a constant because the test asserts
# against it rather than against a literal.
ASSERTION_LIFETIME = timedelta(minutes=5)

# Re-acquire this far before expiry, so a token is never presented in the window between our
# clock saying it is valid and theirs saying it is not.
REFRESH_MARGIN = timedelta(seconds=60)

_HASHES = {"RS256": hashes.SHA256, "RS384": hashes.SHA384}


class AuthFailure(ReferralLoopError):
    """The credential flow failed.

    Distinguishes ours from theirs in the message: an invalid_client means our key or client id
    is wrong and no retry will help, a 5xx is the authorization server's problem.
    """


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(payload: dict[str, object]) -> str:
    # Compact and key-sorted so the bytes are reproducible; a signature over a dict whose
    # serialisation varies is not reproducible in a test.
    return _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _warn_if_world_readable(path: Path, connector_id: str) -> None:
    """A warning, not a refusal, and only on POSIX.

    Refusing would strand a deployment whose key is mode 0644 behind an error it cannot fix
    without a shell on the box, which is a worse failure than the one being prevented. The
    check is skipped on Windows with a notice rather than silently: the ACL equivalent is not a
    one-liner, and a check that quietly does nothing on the platform someone is developing on
    is worse than an honest absence.
    """
    if os.name != "posix":
        logger.debug(
            "%s: key file permissions not checked on this platform; verify by hand that %s "
            "is readable only by the service account",
            connector_id, path,
        )
        return
    mode = path.stat().st_mode
    if mode & 0o077:
        logger.warning(
            "%s: private key %s is mode %o -- readable beyond its owner. The signing key is "
            "the whole proof of our identity to the remote.",
            connector_id, path, mode & 0o777,
        )


def _load_private_key(profile: ConnectorProfile) -> rsa.RSAPrivateKey:
    path = profile.auth.private_key_file
    try:
        _warn_if_world_readable(path, profile.connector_id)
        material = path.read_bytes()
    except OSError as exc:
        raise AuthFailure(
            f"{profile.connector_id}: cannot read the private key at {path}: {exc}"
        ) from exc
    try:
        key = serialization.load_pem_private_key(material, password=None)
    except (ValueError, TypeError) as exc:
        raise AuthFailure(f"{profile.connector_id}: private key at {path} is not readable PEM") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthFailure(
            f"{profile.connector_id}: private key at {path} is not RSA, but "
            f"auth.algorithm is {profile.auth.algorithm}"
        )
    return key


def build_assertion(
    profile: ConnectorProfile,
    *,
    now: datetime | None = None,
    jti: str | None = None,
) -> str:
    """The signed JWT we present as a client_assertion."""
    moment = datetime.now(timezone.utc) if now is None else now
    key = _load_private_key(profile)

    header = {"alg": profile.auth.algorithm, "typ": "JWT", "kid": profile.auth.key_id}
    claims = {
        "iss": profile.auth.client_id,
        "sub": profile.auth.client_id,
        # The configured token_url, never one discovered from the remote. See spec 4.3: a
        # discovery document that chooses our audience chooses where a replayable credential
        # is valid.
        "aud": profile.token_url,
        # From secrets, not the clock. A clock-derived jti collides under a frozen clock, which
        # is exactly when a replay test would stop catching anything.
        "jti": jti or secrets.token_hex(32),
        "exp": int((moment + ASSERTION_LIFETIME).timestamp()),
    }

    signing_input = f"{_segment(header)}.{_segment(claims)}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), _HASHES[profile.auth.algorithm]())
    return f"{signing_input.decode('ascii')}.{_b64u(signature)}"
