"""M4: an MRN must not reach an application log through an exception message.

Four refusal paths out of `MessageHandler._process` log `%s` of the exception
they caught, and four exception classes interpolate an identifier into the
message they carry: `MrnRetiredError`, `CircularMergeError`, a bare
`ReferralLoopError` from the merge guard, and `StoreUnavailableError` from the
alias writer. The result is a patient identifier in a log file -- an artifact
with a different lifetime, a different audience and none of the retention,
encryption or purge machinery the store has.

This is not theoretical. Two of the three end-to-end tests below need no
monkeypatching at all: an `ADT^A40` whose `MRG-1` is empty is ordinary
malformed wire traffic, and a merge cycle is what a registration interface
produces when two operators fix the same duplicate in opposite directions.

**Why the MRN is taken out of the exception rather than out of the log call.**
Filtering at the log call would leave the identifier in `str(exc)` for the next
caller to log, print or put in an HTTP response -- the leak would be closed at
one of four sites and open at every site added afterwards. So the identifier
never enters the message, and the refusals name the message instead: the MSH-10
is enough to find it, the message is in the raw archive, and the archive is
where identifiers are supposed to be. That is the rule `registry.py` already
states for the merge-into-itself warning; these are the paths it was not
applied to.

`str`, `repr` and `args` are all checked at the raise sites, for the reason
`test_machine.py` gives: a caller that logs `%r` gets a different string from
one that logs `%s`, and only one of them is usually tested.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from referral_loop.errors import (
    CircularMergeError,
    MrnRetiredError,
    ReferralLoopError,
    StoreUnavailableError,
)
from referral_loop.listener import MessageHandler, ack_code
from referral_loop.registry import Registry
from referral_loop.store import LoopStore
from tests._pack import PACK
from tests.test_listener import merge, order

# Distinctive enough that a substring scan cannot match anything else in a log
# line, and shaped like an MRN because that is what is being kept out.
SENTINEL = "ZZMRNSENTINEL4211"
SENTINEL_SURVIVOR = "ZZMRNSENTINEL9907"

T0 = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return LoopStore(tmp_path / "loops.db")


@pytest.fixture()
def handler(store):
    return MessageHandler(store=store, registry=Registry(store), pack=PACK)


@pytest.fixture()
def logged(caplog):
    """Every log record this test produced, as one string."""
    caplog.set_level(logging.DEBUG)
    return caplog


def text_of(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def assert_no_identifier(caplog, *, and_names: str) -> str:
    """The scan, plus the debuggability floor it must not be satisfied by.

    A log line that says only "refused" would pass a PHI scan perfectly and be
    useless, so every one of these asserts the control id survives alongside.
    """
    body = text_of(caplog)
    assert body, "nothing was logged at all, so the scan proves nothing"
    for identifier in (SENTINEL, SENTINEL_SURVIVOR):
        assert identifier not in body, f"{identifier} reached a log record:\n{body}"
    assert and_names in body, (
        f"the refusal no longer names {and_names!r}; an operator cannot find the "
        f"message it refused:\n{body}"
    )
    return body


# ------------------------------------------------- through the listener, end to end


def test_a_merge_missing_its_prior_mrn_does_not_log_the_surviving_one(handler, logged):
    """No monkeypatching: an `ADT^A40` carrying an empty `MRG-1`.

    `registry._merge_patient` refuses it and interpolates both endpoints into
    the message; `handle` catches it as a plain ReferralLoopError and logs
    `%s`. The surviving MRN is right there in PID-3 of a message we archived
    correctly, and then again in a log file that has no retention policy.
    """
    ack = handler.handle(merge("A40_NO_MRG", prior="", surviving=SENTINEL))

    assert ack_code(ack) == "AA", "a permanently unacceptable message is not retried"
    assert handler.apply_failure_count == 1
    assert_no_identifier(logged, and_names="A40_NO_MRG")


def test_a_circular_merge_does_not_log_either_identifier(handler, store, logged):
    """Two A40s in opposite directions, which registration interfaces produce.

    Ingest normally resolves both endpoints and the second arrives as a
    merge-into-itself no-op, so the store's cycle guard is reached from the
    wire only when the alias commits after ingest resolved -- the race
    `_apply_alias` exists for. Resolution is pinned to the identity here to put
    the listener on that side of it; everything below is the real path.
    """
    store.resolve_mrn = lambda mrn: mrn

    assert ack_code(handler.handle(
        merge("A40_ONE", prior=SENTINEL, surviving=SENTINEL_SURVIVOR))) == "AA"
    ack = handler.handle(merge("A40_TWO", prior=SENTINEL_SURVIVOR, surviving=SENTINEL))

    assert ack_code(ack) == "AA"
    assert handler.circular_merge_count == 1
    body = assert_no_identifier(logged, and_names="A40_TWO")
    assert "cyclic" in body, "the refusal no longer says what was wrong"


def test_an_mrn_retired_between_ingest_and_the_write_is_not_logged(handler, store, logged):
    """Spec test 7's refusal, logged with the identifier it refused.

    `_open_loop_retrying` recovers from this whenever a second resolution
    settles it, so reaching the log line needs the case its own comment calls
    unsettleable: the alias table answering the guard and the re-resolution
    differently. That is what the flapping resolver models -- the retry is
    exhausted, `MrnRetiredError` escapes, and the listener logs it.
    """
    store.record_alias(SENTINEL, SENTINEL_SURVIVOR, T0, "A40_EARLIER")
    real_resolve = store.resolve_mrn
    calls = {"n": 0}

    def flapping(mrn):
        calls["n"] += 1
        return real_resolve(mrn) if calls["n"] % 2 == 0 else mrn

    store.resolve_mrn = flapping

    ack = handler.handle(order(control_id="ORM_RACE", mrn=SENTINEL))

    assert ack_code(ack) == "AE", "the engine must be asked to redeliver"
    assert handler.mrn_retired_count == 1
    body = assert_no_identifier(logged, and_names="ORM_RACE")
    assert "retired" in body, "the refusal no longer says what was wrong"


def test_the_log_scan_can_actually_fail(handler, logged):
    """The companion that stops the three above passing vacuously.

    Every one of them would still be green against a listener that logged
    nothing, or that logged an MRN this scan happened not to look for.
    """
    handler.handle(order(control_id="ORM_PLAIN", mrn=SENTINEL))
    logging.getLogger("referral_loop.listener").error("planted: %s", SENTINEL)

    with pytest.raises(AssertionError):
        assert_no_identifier(logged, and_names="ORM_PLAIN")


# ------------------------------------------------------ at the raise sites themselves


def test_no_store_or_registry_refusal_carries_the_identifier_it_refused(store):
    """Swept, not spot-checked, for the reason test_machine.py sweeps its own:
    the one that leaks is the branch nobody wrote a test for.

    Each entry is a real call to a real refusal. `StoreUnavailableError` from
    the alias writer is here rather than in an end-to-end test because the
    registry guard in front of it refuses an empty MRN first, so the wire
    cannot reach it -- but a replay, an admin tool or the next caller can, and
    `handle` logs `%s` of that class too.
    """
    registry = Registry(store)
    store.record_alias(SENTINEL, SENTINEL_SURVIVOR, T0, "A40_SETUP")

    refusals = {
        "alias with an empty MRN": (
            StoreUnavailableError,
            lambda: store.record_alias("", SENTINEL, T0, "A40_X"),
        ),
        "alias to itself": (
            CircularMergeError,
            lambda: store.record_alias(SENTINEL, SENTINEL, T0, "A40_X"),
        ),
        "cyclic alias": (
            CircularMergeError,
            lambda: store.record_alias(SENTINEL_SURVIVOR, SENTINEL, T0, "A40_X"),
        ),
        "merge with no prior": (
            ReferralLoopError,
            lambda: registry.merge_patient("", SENTINEL, control_id="A40_X"),
        ),
        "loop opened on a retired MRN": (
            MrnRetiredError,
            lambda: registry.open_loop(mrn=SENTINEL, control_id="ORM_X"),
        ),
        "reversing a merge that never happened": (
            ReferralLoopError,
            lambda: store.reverse_alias(
                SENTINEL_SURVIVOR, actor="a", role="r", reason="why", control_id="X"
            ),
        ),
    }

    leaked = {}
    for name, (expected, call) in refusals.items():
        with pytest.raises(expected) as caught:
            call()
        exc = caught.value
        for rendering in (str(exc), repr(exc), str(exc.args)):
            if SENTINEL in rendering or SENTINEL_SURVIVOR in rendering:
                leaked[name] = rendering
    assert leaked == {}, leaked
