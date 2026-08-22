"""A policy over repeated requests, kept apart from the request itself.

egress.fetch answers "one bounded request to an allowlisted host". This answers "how many times,
and how long between". Separating them is not tidiness: fetch is the only place in the package
that opens a connection, and tests/test_import_closure.py pins that. Retry wraps fetch and opens
nothing, so the pin stays true for free.

The sleep function is injected for the same reason every timestamp in auth.py is: a policy whose
intervals can only be observed by waiting for them is a policy nobody tests precisely.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from time import sleep as _real_sleep

from .connectors import ConnectorProfile, ConnectorRegistry
from .egress import ConnectorUnreachable, Response, fetch

logger = logging.getLogger(__name__)

# 429 is a rate limit and 503 is a server saying "not now"; both become a different answer if
# asked again. Everything else in the 4xx range is a fault in what we sent -- the same rubric
# auth._OUR_FAULT applies to token errors, for the same reason.
RETRYABLE_STATUSES = frozenset({429, 503})

MAX_ATTEMPTS = 3

# Backoff between attempts when the server did not say. Deliberately not jittered: jitter needs
# randomness, randomness makes the interval untestable, and there is one client here rather than
# a thundering herd of them.
BACKOFF_SECONDS = (1.0, 4.0)

# A server may ask for any delay it likes. We are not obliged to grant it -- a preflight parked
# for a day is a failed preflight that looks like a hung one.
MAX_RETRY_AFTER_SECONDS = 30


def _wait_for(response: Response, attempt: int) -> float:
    raw = response.header("retry-after")
    if raw is not None:
        try:
            asked = float(raw)
        except ValueError:
            # An HTTP-date, or nonsense. Neither is worth failing over, and neither may be read
            # as "retry immediately" -- so fall through to our own backoff.
            logger.debug("unparseable Retry-After %r; using backoff instead", raw)
        else:
            if not (math.isfinite(asked) and asked > 0):
                # Parsing is not validating. `float()` accepts far more than a
                # delay -- "-1", "nan" and "inf" all succeed -- and `min()` hands
                # the first two straight to the sleep, where `time.sleep(-5.0)`
                # and `time.sleep(nan)` both raise ValueError. That escapes every
                # handler in `fetch_retrying`, `acquire_token` and `preflight`,
                # which are written for transport failures, so any endpoint
                # answering 429 with `Retry-After: -1` ends the fetch in an
                # unhandled traceback. Zero is refused alongside them: "wait no
                # time at all" is precisely the reading the branch above already
                # says a server does not get to impose.
                #
                # Same answer as an unparseable value, and deliberately the same:
                # a header we will not act on is a header we do not have.
                logger.debug("out-of-range Retry-After %r; using backoff instead", raw)
            else:
                capped = min(asked, float(MAX_RETRY_AFTER_SECONDS))
                if capped < asked:
                    logger.warning(
                        "server asked us to wait %ss; waiting %ss instead", asked, capped
                    )
                return capped
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]


def fetch_retrying(
    registry: ConnectorRegistry,
    profile: ConnectorProfile,
    url: str,
    *,
    _fetch: Callable[..., Response] = fetch,
    _sleep: Callable[[float], None] = _real_sleep,
    **kwargs: object,
) -> Response:
    """fetch, repeated only for the failures that repeating can fix.

    Returns the last response rather than raising when the attempts run out: the caller has the
    status and the body and is better placed to say what a persistent 503 means for it than a
    transport helper is.
    """
    last: Response | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            last = _fetch(registry, profile, url, **kwargs)
        except ConnectorUnreachable:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            _sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
            continue

        if last.status not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS - 1:
            return last

        _sleep(_wait_for(last, attempt))

    assert last is not None  # unreachable: the loop runs at least once
    return last
