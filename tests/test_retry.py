"""The retry policy, and the three things it must not do."""

from __future__ import annotations

import pytest

from referral_loop.connect.egress import Response
from referral_loop.connect.retry import (
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    RETRYABLE_STATUSES,
    fetch_retrying,
)


class _Calls:
    """Records what fetch was asked for and what the policy slept."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = 0
        self.slept: list[float] = []

    def fetch(self, *_args, **_kwargs) -> Response:
        self.requests += 1
        return self.responses.pop(0)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _ok() -> Response:
    return Response(status=200, body=b"{}")


def _retryable(status: int, retry_after: str | None = None) -> Response:
    headers = (("Retry-After", retry_after),) if retry_after else ()
    return Response(status=status, body=b"{}", headers=headers)


def test_a_success_is_returned_without_sleeping():
    calls = _Calls([_ok()])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == 200
    assert calls.requests == 1
    assert calls.slept == []


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_a_retryable_status_is_retried(status):
    calls = _Calls([_retryable(status), _ok()])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == 200
    assert calls.requests == 2
    assert len(calls.slept) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_fault_is_never_retried(status):
    """Same rubric as _OUR_FAULT in auth.py: a malformed query does not become well-formed on a
    second attempt, and retrying it spends the budget a genuine transient needs."""
    calls = _Calls([Response(status=status, body=b"{}")])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == status
    assert calls.requests == 1, "a client fault must not be retried"
    assert calls.slept == []


def test_retry_after_is_honoured():
    calls = _Calls([_retryable(429, retry_after="7"), _ok()])
    fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert calls.slept == [7.0]


def test_a_retry_after_beyond_the_ceiling_is_capped_not_obeyed():
    """A server answering Retry-After: 86400 must not park a coordinator's preflight for a day.
    Capped rather than ignored, because ignoring it would hammer a server that just asked us to
    stop."""
    calls = _Calls([_retryable(503, retry_after="86400"), _ok()])
    fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert calls.slept == [float(MAX_RETRY_AFTER_SECONDS)]


def test_a_malformed_retry_after_falls_back_to_backoff_rather_than_raising():
    """Retry-After may be an HTTP-date, and some servers send nonsense. Neither is worth failing
    a request over -- but neither may be read as 'retry immediately'."""
    calls = _Calls([_retryable(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), _ok()])
    fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert len(calls.slept) == 1
    assert calls.slept[0] > 0


def test_attempts_are_bounded_and_the_last_response_is_returned():
    calls = _Calls([_retryable(503) for _ in range(MAX_ATTEMPTS)])
    got = fetch_retrying(None, None, "https://x/y", _fetch=calls.fetch, _sleep=calls.sleep)
    assert got.status == 503
    assert calls.requests == MAX_ATTEMPTS, "must stop at the cap rather than retrying forever"
