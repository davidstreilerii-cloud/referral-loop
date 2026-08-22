"""The one gate that has to hold before anything touches disk.

HIPAA 164.312(a)(2)(iv) is addressable rather than required, which in practice
means every site answers it differently and none of them answers it in Python.
This subsystem's answer is the one the design already committed to: **a plain
SQLite file on an OS-encrypted volume**. There is no SQLCipher dependency and no
key management here, because a key this process can read is a key a stolen
laptop can read, and the volume is where the platform already solved that.

So this module does not encrypt anything. It answers one question -- *is the
volume that is about to hold MRNs, order numbers and result text encrypted?* --
and under `PHI_MODE=full` it refuses the boot when the answer is not yes.
`cli.py` runs it before `LoopStore` is constructed for exactly that reason: a
site that fails this gate must not be left holding a PHI-shaped file on the
volume the gate refused.

Two ways to answer, and the order matters
-----------------------------------------
**Operator attestation first.** `PHI_ENCRYPTION_VERIFIED=1` is a human saying
they have checked, and it is checked before any probe because it is the only
answer that covers the cases a probe cannot see: a LUKS volume behind LVM, a
cloud provider's transparent disk encryption, a SAN doing it at the array, a
self-encrypting drive. A gate whose only path to "yes" is a probe this file
knows how to run is a gate that refuses to boot on correctly configured sites,
and a gate that refuses correct configurations gets disabled.

**OS detection second, and narrowly.** The probes below are a convenience for
the two cases we can check without guessing, not the definition of the control.
They are deliberately hard to satisfy.

What this module got wrong, written down because the shape recurs
-----------------------------------------------------------------
This file was carried across from the repository this package was extracted
from, and it was the only one that arrived without being rewritten. Four defects
came with it, and three of them share a root: **the gate was never told which
volume it was protecting.**

  * `verify_encryption_at_rest(phi_mode)` took the mode and nothing else, so the
    Windows probe inspected a drive it derived from an environment variable
    belonging to a subsystem this repository does not contain -- unset
    everywhere, and so defaulting to `C:`. PHI on an unencrypted `D:` with
    BitLocker on `C:` passed the gate and logged "detected via OS" as a
    confirmation. A manufactured pass is worse than no check: it puts a
    confirmation in the log an auditor will read.
  * The Linux probe ran `lsblk -o NAME,TYPE` over the **whole host** and asked
    whether the string `crypt` appeared anywhere in the output. One encrypted
    swap device -- a distribution default -- satisfied it for a plaintext PHI
    volume, and so did a device someone had named `cryptic`. A substring search
    over unrelated output is not a check; it is a coin weighted towards yes.
  * `manage-bde` was invoked by bare name. Windows `CreateProcess` searches the
    application directory and the current directory before `PATH`, so a
    `manage-bde.exe` dropped beside the service -- printing nothing but
    "Protection On" -- satisfied `PHI_MODE=full`. The strongest gate in the
    system, defeated by writing a file next to it.
  * The whole probe was wrapped in `except ...: pass`. The fall-through happened
    to be `return None`, which is the right answer, but it was right by accident.

The fix for the first three is one change: the gate takes the path it is
protecting, derives the volume from *that*, and identifies the specific device
backing it. The fix for the fourth is to say `return None` where it is meant.

**Anything that cannot be identified is not encrypted.** Every unknown here --
no drive letter, no `SystemRoot`, a tool that is missing, a device lsblk will
not answer for, a mount whose source is not a block device -- returns `None`.
Under `PHI_MODE=full` that is a refused boot with a message naming the variable
to set once a human has looked. That is the whole design: this file may only
ever *lower* the burden on an operator by recognising a case it is sure about,
and it may never raise the confidence in one it is not.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess

logger = logging.getLogger(__name__)

# Long enough for `manage-bde` to talk to the BitLocker service on a cold boot,
# short enough that a hung probe does not become a hung listener. A timeout is a
# detection failure like any other and lands on the same `None`.
_PROBE_TIMEOUT_SECONDS = 5

ATTESTATION_ENV = "PHI_ENCRYPTION_VERIFIED"


def verify_encryption_at_rest(phi_mode: str, db_path: str | os.PathLike[str]) -> None:
    """Refuse the boot unless the volume holding `db_path` is encrypted.

    `db_path` is required, and that is the fix rather than an ergonomic
    preference: the version of this function that could be called without it was
    called without it, from all three entry points, and spent two years
    inspecting a drive nobody had pointed it at.

    Three modes, three behaviours, and only one of them is a refusal:

      * `full` -- the operating mode (spec section 3). Raises `RuntimeError`.
      * `deidentified` -- warns. There is no MRN in the file, so an unencrypted
        volume is a posture problem rather than a reportable one.
      * `disabled` -- returns. Nothing clinical is being written.
    """
    if phi_mode == "disabled":
        return

    # The path is resolved once, here, so the probe and the refusal message
    # cannot disagree about which volume is under discussion -- and a relative
    # `--db` does not silently become "whatever the working directory is" a
    # frame later.
    volume = os.path.abspath(os.fspath(db_path))

    if os.environ.get(ATTESTATION_ENV, "0") == "1":
        # Logged with the path, because the attestation is about a specific
        # volume and an operator reading this line a month later needs to know
        # which one they attested to.
        logger.info("Encryption at rest: operator attestation confirmed for %s", volume)
        return

    detected = _detect_os_encryption(volume)
    if detected:
        logger.info("Encryption at rest: detected via OS (%s)", detected)
        return

    msg = (
        f"Encryption at rest NOT verified for {volume}. Set {ATTESTATION_ENV}=1 after "
        "confirming BitLocker/LUKS/KMS is active on the volume holding that file. "
        "Automatic detection covers only BitLocker on a lettered Windows drive and a "
        "dm-crypt device directly backing the mount; LVM-on-LUKS, self-encrypting "
        "drives and array- or hypervisor-level encryption are real and are not "
        "detectable from here, which is what the attestation is for."
    )
    if phi_mode == "full":
        raise RuntimeError(f"PHI_MODE=full requires encryption at rest. {msg}")
    logger.warning("PHI_MODE=%s: %s", phi_mode, msg)


def _detect_os_encryption(db_path: str) -> str | None:
    """Describe the encryption protecting `db_path`'s volume, or `None`.

    `None` means "not shown to be encrypted", never "probably fine". Every
    branch that cannot reach a definite yes falls here, and under
    `PHI_MODE=full` that is a refused boot.
    """
    try:
        system = platform.system()
        if system == "Windows":
            return _bitlocker_protecting(db_path)
        if system == "Linux":
            return _dm_crypt_backing(db_path)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # A missing tool, a timeout, a probe killed by the sandbox it is running
        # in. Stated rather than left to a bare `pass`: the answer is the same
        # `None` the rest of the function returns, and the point of saying so is
        # that an unrunnable probe must never be distinguishable from one that
        # ran and found nothing.
        logger.debug("Encryption probe for %s could not run (%s)", db_path, type(exc).__name__)
    return None


def _bitlocker_protecting(db_path: str) -> str | None:
    """BitLocker on the lettered drive that `db_path` is on.

    The drive comes from the path the caller passed in. That sentence is the
    entire fix for the defect this module is named for.
    """
    drive = os.path.splitdrive(db_path)[0]
    if not drive.endswith(":"):
        # No drive letter: a UNC path, or something relative that abspath did
        # not resolve to a local volume. `splitdrive(r"\\\\server\\share\\x")`
        # answers `\\\\server\\share`, which manage-bde cannot speak about --
        # and a file server's disks are not this process's to attest to anyway.
        return None

    # An absolute path under SystemRoot, never the bare name. Windows
    # `CreateProcess` resolves an unqualified program against the application
    # directory and the current directory before it reaches PATH, so
    # `["manage-bde", ...]` is an invitation to drop a `manage-bde.exe` that
    # prints "Protection On" next to the service and unlock PHI_MODE=full with
    # it. This is the same class of defect as an unqualified DLL load and it has
    # the same fix.
    system_root = os.environ.get("SystemRoot", "")
    if not system_root:
        return None
    program = os.path.join(system_root, "System32", "manage-bde.exe")
    if not os.path.isfile(program):
        return None

    result = subprocess.run(
        [program, "-status", drive],
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
    )
    # "Protection Status: Protection On", for the one drive we asked about. The
    # question is scoped to that drive, so the substring cannot be satisfied by
    # some other volume's status the way the Linux probe's used to be.
    if "Protection On" in result.stdout:
        return f"BitLocker protection on {drive} (holding {db_path})"
    return None


def _dm_crypt_backing(db_path: str) -> str | None:
    """dm-crypt/LUKS on the device that actually backs `db_path`'s mount.

    Two questions, deliberately, where the old code asked neither: *which device
    is this file on* (`findmnt`), and *is that device a crypt target* (`lsblk`).
    The answer must be the device's own TYPE, exactly `crypt` -- not the string
    `crypt` occurring somewhere in a host-wide listing, which is what made an
    encrypted swap partition on an unrelated disk read as a pass.

    LVM-on-LUKS answers `lvm` here and is therefore *not* detected, even though
    it is genuinely encrypted. That is the conservative direction and it is
    deliberate: walking the device-mapper tree upward to decide whether every
    ancestor is a crypt target is exactly the kind of inference that produces a
    confident wrong yes. Those sites attest.
    """
    # `--target` asks about the mount containing the path, which works for a
    # database file that does not exist yet -- and at boot it usually does not,
    # because this gate deliberately runs before anything creates it.
    source = _probe(["findmnt", "-no", "SOURCE", "--target", _nearest_existing(db_path)])
    if not source:
        return None
    # btrfs reports a subvolume as `/dev/mapper/x[/subvol]`, and that whole
    # string is not a device lsblk will answer for.
    device = source.split("[", 1)[0].strip()
    if not device.startswith("/dev/"):
        # tmpfs, overlay, an NFS export: not a block device, so not something
        # whose encryption this can speak to.
        return None

    # `-d`: the device itself, without its children. Without it a crypt device
    # holding a filesystem lists its descendants too, and a first line that is
    # not the device would put us back to matching on unrelated output.
    kind = _probe(["lsblk", "-dno", "TYPE", device])
    if kind != "crypt":
        return None
    return f"dm-crypt/LUKS device {device} backs {db_path}"


def _nearest_existing(path: str) -> str:
    """The closest ancestor of `path` that exists, for probes that need a real
    target. The database file is usually absent at boot -- the gate runs before
    `LoopStore` creates it, on purpose -- but its directory, or its directory's
    parent, resolves to the same mount."""
    current = path
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return path
        current = parent
    return current


def _probe(argv: list[str]) -> str:
    """One line of stdout from a read-only query, or "" if it did not answer.

    No shell, a fixed argument vector, and a timeout. `check` is deliberately
    not set: a non-zero exit is one of the ways these tools say "I do not know",
    and it is the same answer as an empty line.
    """
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
