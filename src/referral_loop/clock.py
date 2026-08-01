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
than three guards that drift apart.

**Exactly one future bound, with exactly one behaviour.** Anything dated beyond
`MAX_CLOCK_SKEW` is applied, not trusted, and counted -- whether it is two days
ahead or in the year 9999. A first version of this module got that wrong in a
way worth recording, because it is the failure mode the whole file exists to
prevent: it bounded the *parse* at a year and *trust* at a day, which gave the
same attack two different outcomes depending only on its magnitude. Skew inside
the year was applied unstamped and counted; the year-9999 exploit that motivated
the work was nulled by the parse instead, took a different path entirely, and
never reached the counter written for it. Two bounds meant the documented policy
was not the one that ran.

So `is_readable_clock` deliberately does **not** bound the future. A future
timestamp has to survive the parse for the guard downstream to see it, decline
it, log it and count it; nulling it first would hide the anomaly from the very
counters that exist to surface it.

The **past** is bounded here, and the asymmetry is deliberate rather than
overlooked. A timestamp from before living memory has no consumer that wants to
see it: the registry refuses it as clinically stale and staleness ranks it
maximally overdue -- the top of the worklist, not the bottom -- so nothing is
hidden by nulling it, and nothing downstream distinguishes "very old" from
"merely old" in a way that changes a decision. It is only the future direction
that poisons a monotonic `max()` and clamps a loop out of sight.

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

# How far back a timestamp may reach and still be a clinical event rather than
# a garbled field. A century (in Julian years, so the number means what it says)
# is past any living patient's imaging history, so nothing real is refused.
# There is no forward counterpart on purpose -- see the module docstring.
_READABLE_PAST = timedelta(days=36525)


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
    """True unless `value` is too old to be a clinical event at all.

    One-sided, and not the complement of `is_future_dated`. A timestamp twelve
    hours ahead is readable *and* untrusted; a timestamp in the year 9999 is
    readable and untrusted too, so that the one guard that acts on skew is the
    one that sees it. This function's only job is the other end -- keeping a
    garbled field out of the arithmetic -- and it must never grow a future bound
    without moving `MAX_CLOCK_SKEW`'s behaviour with it.
    """
    reference = datetime.now(timezone.utc) if now is None else _as_utc(now)
    return _as_utc(value) >= reference - _READABLE_PAST
