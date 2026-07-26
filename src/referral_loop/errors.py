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


class ThresholdsNotAcceptedError(ReferralLoopError):
    """Staleness thresholds shipped as defaults but not accepted by the site."""
