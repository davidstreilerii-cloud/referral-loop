"""Typed failures for the referral loop subsystem.

Many map to a row of the spec failure matrix (section 8). Do not read the absence
of a row as a gap in the code: the matrix names the failures that were foreseen
when it was written, and this module names the ones the implementation actually
has to answer, so it runs ahead of section 8 by construction. ThresholdsNotAcceptedError
encodes the resolution of open question 3 (section 12) rather than any matrix row;
NoAppointmentError describes a shape the matrix could not have contemplated,
because it was written when an SIU^S15 drove Registry.cancel and a loop's booking
history did not decide whether one could be applied. Where the two disagree it is
the spec trailing the code.

Every name carries the -Error suffix, matching the convention already used
across this codebase (AnthropicClientError, SpendLimitError, MissingColumnsError).
"""


class ReferralLoopError(Exception):
    """Base for every referral-loop failure."""


class FramingError(ReferralLoopError):
    """MLLP framing malformed. Respond AR; the engine retries."""


class UnparseableSegmentError(ReferralLoopError):
    """One segment failed to parse. Skip it, keep the message, flag for review."""


class PackVerificationError(ReferralLoopError):
    """Pack signature missing, invalid, or altered. Refuse to boot."""


class StoreUnavailableError(ReferralLoopError):
    """Durable write failed. Respond AE so the engine queues. Never ACK."""


class LoopNotFoundError(ReferralLoopError):
    """No events exist for the requested loop, so no state is derivable.

    A ReferralLoopError rather than a bare KeyError so callers can handle every
    referral-loop failure with one except clause.
    """


class ReservedStateError(ReferralLoopError):
    """An attempt to enter a state reserved for a later version.

    v1 stops at ACKNOWLEDGED -- a coordinator confirming that this result belongs
    to this loop, a clerical claim they can support. CLOSED asserts that a
    clinically responsible actor dispositioned the finding, which nothing in v1
    observes, so it is reserved and asserted unreachable (spec section 4, test 5).

    Typed rather than asserted: an assertion vanishes under python -O, and this
    has to hold in production. Distinct from StoreUnavailableError because it must
    never be retried -- the message is not going to become acceptable later.
    """


class CircularMergeError(ReferralLoopError):
    """An ADT^A40 whose application would make a patient identity cyclic.

    A40 says "A is retired, B survives"; a later one says "B is retired, A
    survives". Both cannot hold. Resolving it by rule -- last writer wins, or
    stopping the walk where it started -- silently picks an arbitrary survivor
    and strands every loop on the losing side, which is this section's own
    failure mode chosen deliberately rather than suffered.

    So the merge is refused entirely: the alias table is unmodified, no loop
    moves, and a human is told. A circular merge is an upstream registration
    error and needs a person, not a tiebreak. Same posture as the confidence
    floor -- decline rather than guess.

    Not a StoreUnavailableError: the message must not be retried, because it
    will never become acceptable without someone fixing registration.
    """


class MrnRetiredError(ReferralLoopError):
    """An MRN that stopped being current between ingest resolution and the write.

    Identity is resolved once, at ingest, before the registry or the matcher
    sees anything (spec section 4) -- and that resolution is not atomic with
    committing a merge. A listener can resolve an MRN, an ADT^A40 can commit,
    and the loop is then written onto an identifier retired microseconds
    earlier: invisible to every query on the surviving patient, and missed by
    that merge's straggler scan, which has already run.

    Typed, and distinct from the ReferralLoopError the rest of open_loop raises,
    because the two need opposite answers on the wire. This one is **retryable**
    -- the next resolution gets it right -- so the listener answers AE and the
    engine redelivers. A generic failure is not retryable and is answered AA
    with the raw archived and flagged; answering AE to that would wedge the
    interface behind a message that will never become acceptable. Distinguishing
    them by parsing an error message would be the fragile version of this.
    """


class NoAppointmentError(ReferralLoopError):
    """An `SIU^S15` naming a referral that carries no booking to cancel.

    Benign and expected on a live feed, and worth naming its causes because the
    counter it drives (`listener.unbooked_cancel_count`) is only useful if a rising
    one points somewhere:

      * An `SIU^S12` that never reached this listener at all -- the usual case, and a
        feed gap somebody can go and look for.
      * An `S12` that reached it and was **declined**, which leaves us unbooked while
        the receiving organisation believes it booked. Two routes, and both leave a
        number behind: an `S12` arriving before the order that opens the loop finds no
        loop to target and raises `untargeted_count`, which an out-of-order feed does
        routinely; an `S12` clinically older than a message already applied is refused
        by `_refuse_if_stale` and raises `stale_message_count`. Neither refusal touches
        the `S15` that follows, because both turn on the loop's own history rather than
        on anything the `S15` carries.

    Not a redelivery of the S15 itself, in the ordinary case: `content_key` hashes the
    message type, ORC-1, the order numbers and the MRN but not MSH-10, so a second copy
    from the same peer is answered as a content duplicate before `_apply` is reached.
    It becomes reachable only across peers, since both dedup scopes are keyed on
    `peer_id` -- which needs two peers holding cancel authority for one order, and is
    rare enough that an operator handed it as a first hypothesis would be sent looking
    for something that is almost certainly not there.

    Typed for the reason MrnRetiredError is: the listener has to answer three
    different failures out of one call, and matching on an error string is the
    fragile version of that. A bare ReferralLoopError here would be counted as
    `apply_failure_count` beside a store fault, so the daily rhythm of a scheduling
    feed would read to an operator as the system failing to apply messages, and the
    signal that actually matters -- an S12 stream that has stopped arriving -- would
    be buried under it.

    Never retryable. The loop has no appointment now and a redelivery finds none
    either, so the answer is AA: AE would wedge the interface behind a message that
    can never become acceptable.
    """


class StaleMessageError(ReferralLoopError):
    """A message clinically older than one already applied to the loop.

    loop_events is replayed in arrival order, deliberately, so nothing below the
    registry guards clinical ordering. Applying a message whose MSH-7 predates
    the newest one already accepted can only regress the loop -- a SIU landing
    after an ORU would return a resulted loop to SCHEDULED, and a final that
    predates an applied correction would re-arm CLOSED on a superseded read.

    Refusing is not dropping: the raw message is already durably archived by
    record_raw before the registry ever sees it, and this is typed so the
    listener can route it for human review rather than swallow it.
    """


class ThresholdsNotAcceptedError(ReferralLoopError):
    """Staleness thresholds shipped as defaults but not accepted by the site."""
