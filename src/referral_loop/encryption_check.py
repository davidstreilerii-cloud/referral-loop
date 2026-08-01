"""Encryption-at-rest startup validation (HIPAA 164.312(a)(2)(iv)).

Operator attestation via PHI_ENCRYPTION_VERIFIED=1, with optional OS-level checks.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess

logger = logging.getLogger(__name__)


def verify_encryption_at_rest(phi_mode: str) -> None:
    """Validate encryption at rest. full=fail-closed, deidentified=warn, disabled=skip."""
    if phi_mode == "disabled":
        return
    attested = os.environ.get("PHI_ENCRYPTION_VERIFIED", "0") == "1"
    if attested:
        logger.info("Encryption at rest: operator attestation confirmed")
        return
    detected = _detect_os_encryption()
    if detected:
        logger.info("Encryption at rest: detected via OS (%s)", detected)
        return
    msg = ("Encryption at rest NOT verified. Set PHI_ENCRYPTION_VERIFIED=1 after "
           "confirming BitLocker/LUKS/KMS is active on the data volume.")
    if phi_mode == "full":
        raise RuntimeError(f"PHI_MODE=full requires encryption at rest. {msg}")
    else:
        logger.warning("PHI_MODE=%s: %s", phi_mode, msg)


def _detect_os_encryption() -> str | None:
    """Optional OS-level encryption detection. Returns description or None."""
    try:
        if platform.system() == "Windows":
            drive = os.path.splitdrive(os.environ.get("CHROMA_DB_PATH", "C:"))[0] or "C:"
            r = subprocess.run(["manage-bde", "-status", drive],
                               capture_output=True, text=True, timeout=5)
            if "Protection On" in r.stdout:
                return f"BitLocker active on {drive}"
        elif platform.system() == "Linux":
            r = subprocess.run(["lsblk", "-o", "NAME,TYPE"],
                               capture_output=True, text=True, timeout=5)
            if "crypt" in r.stdout:
                return "LUKS/dm-crypt detected"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None
