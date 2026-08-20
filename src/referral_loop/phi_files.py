"""Owner-only creation for the files this package puts patient data in.

Audit finding M3. `sqlite3.connect()` creates a database with SQLite's
`SQLITE_DEFAULT_FILE_PERMISSIONS` (0644) masked by the umask, and there is no
way to hand the driver a mode. `raw_messages.payload` holds verbatim HL7, so on
a default Linux install every local account on the host could read every
patient's identifiers and every result. Volume encryption answers none of that:
the volume is decrypted for exactly as long as the service is running, which is
exactly when the file is there to be opened.

Two mechanisms, and the difference between them is the point:

  * **Creation** goes through `os.open` with `O_CREAT | O_EXCL` and mode 0600,
    and directories through `os.mkdir` with 0700. The mode is chosen at the
    instant the inode appears, so there is no window in which a readable file
    exists. `umask` can only clear bits and neither mode has a group or other
    bit to clear, so it cannot widen either.
  * **Tightening** is `chmod`, and it exists for one case: a file an earlier
    build of this package already created 0644. That file cannot be created
    again, and leaving it as it is would mean the fix only ever protects
    greenfield installs.

Nothing here reaches into the rest of the package -- no imports beyond `os` --
so `immutable_audit`, which deliberately depends on nothing else in
`referral_loop`, can use it without acquiring the store's import closure.
Failures are raised as the `OSError`s they are; each caller translates them
into whatever its own layer already answers with.

The sidecars are covered by the database file rather than by anything here.
SQLite creates a rollback journal, a WAL and a shared-memory file with the
permissions of the database they belong to (`findCreateFileMode` in os_unix.c),
so a 0600 database gets a 0600 journal. `tests/test_phi_file_modes.py` asserts
that on POSIX rather than trusting it, because a 0600 database beside a 0644
journal holding the same page images would not be a fix.
"""
from __future__ import annotations

import os

# Owner read/write, nothing else. The database holds verbatim HL7.
PHI_FILE_MODE = 0o600
# Owner only, including the execute bit that makes a directory traversable:
# without it no other account can open a file inside even by full path, which
# is the second line of defence behind the file's own mode.
PHI_DIR_MODE = 0o700


def create_private_file(path: str | os.PathLike[str]) -> None:
    """Put an empty, owner-only file at `path`, or tighten one already there.

    A zero-length file is a valid empty SQLite database, so calling this before
    `sqlite3.connect()` means the driver opens a file that already exists and
    never chooses a mode at all.

    Only a regular file is tightened. A `--db` that points at a directory is a
    refusal SQLite gives far more legibly a moment later, and re-permissioning
    the directory on the way past would be gratuitous; a symlink or a device
    node is not something to quietly chmod either.
    """
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PHI_FILE_MODE)
    except FileExistsError:
        harden_file(path)
        return
    os.close(handle)


def harden_file(path: str | os.PathLike[str]) -> None:
    """Take group and other off an existing regular file. A no-op for anything else."""
    if os.path.isfile(path):
        os.chmod(path, PHI_FILE_MODE)


def create_private_directory(path: str | os.PathLike[str]) -> list[str]:
    """Create `path` and any missing ancestors, each one owner-only.

    Returns the directories actually created, outermost first, so a caller can
    say what it did. An empty list means the directory was already there.

    A directory that already exists is not touched, deliberately. This process
    owns the directories it creates; it does not own a mount point, a shared
    volume or a home directory an operator pointed `--db` into, and silently
    taking one of those private underneath a deployment would be a surprise
    with no way to see it coming. The file's own 0600 is what carries the
    guarantee in that case.

    `os.makedirs` is not used: it passes `mode` only to the leaf and creates
    every intermediate with the default, so the parents of a nested default
    path would be 0755. Each level is created here instead, in order, so every
    one of them gets 0700 at the moment it appears.
    """
    target = os.path.abspath(os.fspath(path))
    missing: list[str] = []
    probe = target
    while not os.path.isdir(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:      # the root of the filesystem, which always exists
            break
        probe = parent

    for directory in reversed(missing):
        # exist_ok by hand: another process may have won the race between the
        # walk above and this call, and losing that race is not a failure --
        # but a *file* sitting where a directory should be is, and swallowing
        # that would turn a legible refusal into a confusing one two calls
        # later.
        try:
            os.mkdir(directory, PHI_DIR_MODE)
        except FileExistsError:
            if not os.path.isdir(directory):
                raise
    return list(reversed(missing))
