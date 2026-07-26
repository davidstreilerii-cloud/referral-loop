"""Typed failures for the referral loop subsystem.

Most map to a row of the spec failure matrix (section 8). ThresholdsNotAcceptedError
does not -- it encodes the resolution of open question 3 (section 12): staleness
thresholds ship as defaults but the site must accept them explicitly, so a
threshold stays the hospital's clinical decision rather than ours.

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
