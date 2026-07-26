"""Spec tests 12 and 13, as a pytest plugin the *whole* referral suite runs under.

Both spec tests are worded as properties of the suite, not of a module:

  12. **No egress.** Block non-loopback `socket.connect`; full suite passes.
  13. **No model calls.** Monkeypatch `anthropic` and `claude_cli` to raise; full
      suite passes.

Two per-module tests already assert "this function opened no socket"
(`test_listener.py::test_the_listener_makes_no_outbound_connection`,
`test_boot_gates.py::test_booting_and_draining_opens_no_outbound_connection`).
Neither is the spec's claim. They prove that two code paths someone thought of
are clean; the claim is that *no* path is, including the ones nobody thought to
write a test for. That difference is only observable by arming the guard and
running everything.

`test_spec_proofs.py::test_spec_12_and_13_the_whole_suite_under_both_guards`
re-runs the suite in a subprocess with `-p tests.referral_loop.spec_guards`.
The guards are installed **only** when loaded that way, so importing this module
is inert -- an autouse fixture here would arm them for the ordinary run too, and
then the meta-test would prove nothing the plain run had not already shown.

**Why the guards raise rather than record.** A recording guard turns a violation
into a report that something has to remember to read. Raising turns it into the
red test the spec asks for, at the exact line that attempted it.
"""
from __future__ import annotations

import contextlib
import os
import socket
import sys
import types

# The env var the subprocess sets, and the recursion brake: the meta-test skips
# when it sees this, so the inner run does not spawn its own inner run.
ARMED_ENV = "REFERRAL_SPEC_GUARDS_ARMED"

# Loopback in every spelling a caller can reach it by, plus the two wildcard
# forms that mean "bind everything" and never appear as a connect target.
LOOPBACK_HOSTS = frozenset(
    {"127.0.0.1", "::1", "localhost", "ip6-localhost", "0.0.0.0", "::", ""}
)

_INET_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})


class EgressAttempted(AssertionError):
    """Spec test 12 violated: something tried to connect off this host."""


class ModelCallAttempted(AssertionError):
    """Spec test 13 violated: something reached for a model client."""


def _host_of(address) -> str | None:
    """The host out of a connect() address, or None if it is not an IP address.

    AF_UNIX and AF_PIPE addresses are strings or bytes and are not egress; they
    are how SQLite, Docker's client and the Windows named-pipe transports talk
    to things already on this machine.
    """
    if isinstance(address, (str, bytes)):
        return None
    try:
        host = address[0]
    except (TypeError, IndexError, KeyError):
        return None
    if isinstance(host, bytes):
        host = host.decode("utf-8", errors="replace")
    return host if isinstance(host, str) else None


def is_loopback(sock_family, address) -> bool:
    """True when this connect stays on the machine.

    A hostname that is not one of the known loopback spellings counts as egress
    even before resolution. That is deliberate and it is the strict direction:
    resolving it to decide would itself be a DNS packet leaving the host, which
    is the thing under test.
    """
    if sock_family not in _INET_FAMILIES:
        return True
    host = _host_of(address)
    if host is None:
        return True
    return host in LOOPBACK_HOSTS


def _raising_model_module(name: str) -> types.ModuleType:
    """A stand-in module where touching *any* attribute raises.

    Replacing `sys.modules[name]` alone would leave every already-bound
    reference live -- `healthcare_rag/__init__.py` imports `claude_cli` at
    package import, long before this plugin loads -- so `install_guards` also
    poisons the real module objects in place. This covers the other direction:
    code that imports the name *after* the guard is armed.
    """
    module = types.ModuleType(name)
    module.__doc__ = f"{name} disabled by spec test 13."

    def _refuse(attribute: str):
        raise ModelCallAttempted(
            f"spec test 13: something reached for {name}.{attribute}; v1 makes "
            "no model calls"
        )

    module.__getattr__ = _refuse  # type: ignore[attr-defined]
    return module


def _poison_in_place(module: types.ModuleType, name: str) -> None:
    """Replace every public callable on an already-imported module with a raiser.

    This is what catches the realistic failure: a module that captured
    `from anthropic import Anthropic` at import time holds the class object, not
    the module, so swapping `sys.modules` misses it entirely.
    """
    for attribute in list(vars(module)):
        if attribute.startswith("__"):
            continue
        value = getattr(module, attribute, None)
        if not callable(value):
            continue

        def refuse(*args, _n=name, _a=attribute, **kwargs):
            raise ModelCallAttempted(
                f"spec test 13: {_n}.{_a}() was called; v1 makes no model calls"
            )

        try:
            setattr(module, attribute, refuse)
        except Exception:  # pragma: no cover - immutable attribute
            pass


def install_guards() -> None:
    """Arm both guards. Idempotent, and safe to call before pytest collects."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        if not is_loopback(self.family, address):
            raise EgressAttempted(
                f"spec test 12: connect to {address!r} is not loopback; v1 makes "
                "no outbound connection to anyone, us included"
            )
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not is_loopback(self.family, address):
            raise EgressAttempted(
                f"spec test 12: connect_ex to {address!r} is not loopback"
            )
        return real_connect_ex(self, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex

    for name in ("anthropic", "healthcare_rag.claude_cli"):
        existing = sys.modules.get(name)
        if isinstance(existing, types.ModuleType):
            _poison_in_place(existing, name)
        stub = _raising_model_module(name)
        sys.modules[name] = stub
        # A submodule is reachable as an attribute of its package as well as
        # through sys.modules, and `healthcare_rag.claude_cli` is bound on the
        # package object by `healthcare_rag/__init__.py`'s own import.
        package, _, leaf = name.rpartition(".")
        parent = sys.modules.get(package) if package else None
        if parent is not None:
            setattr(parent, leaf, stub)


@contextlib.contextmanager
def armed():
    """Arm both guards for a block, then put everything back.

    `install_guards` is deliberately one-way -- once the plugin has armed a
    process it stays armed for the run. A test that arms them in-process needs
    the opposite, because leaving `socket.socket.connect` wrapped would leak the
    guard into every test that happens to run afterwards, and calling
    `install_guards` twice would stack a wrapper on a wrapper.
    """
    saved_connect = socket.socket.connect
    saved_connect_ex = socket.socket.connect_ex
    names = ("anthropic", "healthcare_rag.claude_cli")
    saved_modules = {name: sys.modules.get(name) for name in names}
    saved_attrs = {}
    for name in names:
        module = saved_modules[name]
        if isinstance(module, types.ModuleType):
            saved_attrs[name] = dict(vars(module))

    install_guards()
    try:
        yield
    finally:
        socket.socket.connect = saved_connect
        socket.socket.connect_ex = saved_connect_ex
        for name in names:
            module = saved_modules[name]
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
            if name in saved_attrs:
                for attribute, value in saved_attrs[name].items():
                    try:
                        setattr(module, attribute, value)
                    except Exception:  # pragma: no cover - immutable attribute
                        pass
            package, _, leaf = name.rpartition(".")
            parent = sys.modules.get(package) if package else None
            if parent is not None and module is not None:
                setattr(parent, leaf, module)


def pytest_configure(config):  # pragma: no cover - exercised in the subprocess
    os.environ[ARMED_ENV] = "1"
    install_guards()
