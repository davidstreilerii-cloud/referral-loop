"""One clock-skew policy for every timestamp an interface engine hands us.

An HL7 TS is attacker-controlled and unbounded: four digits of year express
9999 as readily as 2026. Three ingest sites consumed one with no upper bound,
and each produced a distinct clinical failure --

  * **MSH-7 into `Registry._clinical_watermark`**, which is a `max()` over an
    append-only log. One message dated year 9999 raised the watermark past every
    real message that would ever follow, and because the log cannot be lowered
    the loop then refused its own final report, its own correction and its own
    cancellation for the rest of its life. A RIS with a mis-set clock produced
    this by accident and it was equally unrecoverable.
  * **MSH-7 omitted**, which made the same watermark inert: the guard returned
    early on an unreadable timestamp, so a blank `MSH-7` disabled the only
    anti-replay control in the system.
  * **OBR-7 into a loop's `ordered_at`**, where `staleness.age()` clamps a
    future order to zero. The clamp is right -- the failure matrix says accept
    the message, clamp for staleness math, and flag elsewhere -- but nothing
    flagged, so the loop reported `0.0 h` forever: never stale, never red, dead
    last on every worklist.

One bug in three places, so the bound is one constant read at every site rather
than three guards that drift apart. **Two windows**, because those sites ask two
different questions:

  * `is_readable_clock` -- "is this a timestamp at all?" Wide, and wide in the
    past on purpose: a prior study's `OBR-7` is legitimately years old and a
    narrow floor would discard real clinical history. Its only job is to keep a
    year no clock could produce out of every downstream calculation.
  * `is_future_dated` -- "may this be trusted as *this message's* clock?"
    Narrow, because that is the question the watermark and the staleness
    arithmetic actually ask.

One threshold cannot do both jobs. Narrow enough to protect the watermark
rejects a decade of legitimate history; wide enough to admit that history leaves
a year of poison available, and a year of poison is as permanent as a millennium
of it.

Only the future is bounded by `MAX_CLOCK_SKEW`. A past-dated message needs no
bound here because every consumer already fails toward visibility on one: the
registry refuses it as clinically stale, and staleness ranks it maximally
overdue -- the top of the worklist, not the bottom.

This module imports nothing from the package, so it sits beneath the matcher,
the registry, the listener and staleness alike and all four read the same number
without any of them importing another.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# How wrong a clinical clock may be before this system stops treating its
# timestamp as evidence of when something happened. A day covers a RIS in the
# wrong timezone, a daylight-saving misconfiguration, and an interface engine
# that has not synchronised since yesterday. It does not cover a year, which is
# the smallest skew that made a loop permanently deaf to its own correction.
MAX_CLOCK_SKEW = timedelta(hours=24)

# The parse window. A century back reaches past any living patient's imaging
# history, so nothing real is refused; a year forward is longer than any
# legitimately post-dated clinical event and short enough that year 9999 never
# reaches a caller.
_READABLE_PAST = timedelta(days=365 * 100)
_READABLE_FUTURE = timedelta(days=366)


def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are read as UTC rather than compared against an aware
    `now`, which raises TypeError. An `MSH-7` frequently carries no offset, and
    a TypeError raised inside a guard would send every such message down an
    error path instead of merely ranking one message wrong -- the same reasoning,
    and the same fix, as `matcher._as_utc`."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_future_dated(value: datetime, now: datetime | None = None) -> bool:
    """True when `value` is further ahead than a clock is plausibly wrong.

    What to do about it is the caller's decision, and the two callers decide
    differently on purpose: the registry drops the stamp and applies the message
    anyway, the listener declines the order time and lets the loop age from
    ingest instead. Neither refuses the message. A fast clock is an operational
    fault, and refusing its traffic would strand the real clinical results this
    subsystem exists to keep.
    """
    reference = datetime.now(timezone.utc) if now is None else _as_utc(now)
    return _as_utc(value) > reference + MAX_CLOCK_SKEW


def is_readable_clock(value: datetime, now: datetime | None = None) -> bool:
    """True when `value` could be a real clinical timestamp at all.

    Deliberately not the same question as `is_future_dated`: a timestamp twelve
    hours ahead is readable and untrusted, and both facts matter to different
    callers.
    """
    reference = datetime.now(timezone.utc) if now is None else _as_utc(now)
    return reference - _READABLE_PAST <= _as_utc(value) <= reference + _READABLE_FUTURE
