"""Typed failures, one per row of the spec failure matrix (section 8)."""


class ReferralLoopError(Exception):
    """Base for every referral-loop failure."""


class FramingError(ReferralLoopError):
    """MLLP framing malformed. Respond AR; the engine retries."""


class UnparseableSegment(ReferralLoopError):
    """One segment failed to parse. Skip it, keep the message, flag for review."""


class PackVerificationError(ReferralLoopError):
    """Pack signature missing, invalid, or altered. Refuse to boot."""


class StoreUnavailable(ReferralLoopError):
    """Durable write failed. Respond AE so the engine queues. Never ACK."""


class ThresholdsNotAccepted(ReferralLoopError):
    """Staleness thresholds shipped as defaults but not accepted by the site."""
