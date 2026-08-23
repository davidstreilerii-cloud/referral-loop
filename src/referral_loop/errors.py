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

Every name carries the -Error suffix.
"""


class ReferralLoopError(Exception):
    """Base for every referral-loop failure.

    `retryable` is the one property this taxonomy does not otherwise express,
    and it is orthogonal to every distinction below. The classes here name
    *causes*; `listener._process` needs to answer a different question about
    each of them -- **would a redelivery of this same message ever succeed?** --
    because that is what decides `AE` (the sending engine queues the message and
    tries again) against `AA` (the engine considers it delivered and forgets
    it). Nothing about "this is a merge cycle" or "this is a store fault" says
    which of those two a clinical message gets, so the answer is written down
    here rather than left to a ladder of `except` clauses to imply.

    It lived in prose until it was a field. Every docstring below already stated
    it -- "the next resolution gets it right", "never retryable", "the message
    must not be retried" -- which meant the property was real, checked by review,
    and invisible to the code that acted on it. A new subclass added anywhere in
    the package joined the `AA` side of `_process`'s final clause silently: the
    engine dropped the message from its outbound queue, the referral moved
    nowhere, and it appeared on no worklist. That is the failure this whole
    subsystem exists to prevent, arriving through the error taxonomy.

    **The default is `False`, and it is not the same thing as an answer.** `AE`
    on a message that can never become acceptable wedges the interface behind it
    forever -- an engine retries a queued `AE` at the head of its outbound queue,
    so one such message stops the entire clinical feed (see `ReservedStateError`
    and `NoAppointmentError`). So a class that says nothing must not queue. But a
    class that says nothing has not decided anything either, and a silent default
    is how the fail-open case got here in the first place, so
    `test_every_referral_loop_error_declares_its_own_retryability` walks every
    subclass and requires the declaration to be in the class's own `__dict__`.
    The default exists to make the *base* class's behaviour safe, not to be
    inherited.

    The name is about redelivery of an HL7 message by an interface engine, and
    only that. It says nothing about whether an *outbound* HTTP call should be
    re-attempted -- `connect/retry.py` owns that question and answers it from
    HTTP status codes, which is a different mechanism with a different failure
    mode. Two of the classes in `connect/` are transient in that sense and still
    declare `retryable = False` here, because no interface engine is holding a
    message for them.
    """

    retryable: bool = False


class FramingError(ReferralLoopError):
    """MLLP framing malformed. Respond AR; the engine retries.

    `retryable = False` despite that sentence, and the two are not in conflict:
    `AR` is a rejection the engine may re-send on its own initiative, while
    `retryable` names the `AE`-versus-`AA` choice inside `_process`. Framing is
    settled by `mllp_server` before a byte of HL7 is parsed and never reaches
    that choice, so `False` here is "not applicable" rather than "do not retry".
    Declared anyway, because a class that reaches `_process` by some future route
    must not do so on an answer nobody gave.
    """

    retryable = False


class UnparseableSegmentError(ReferralLoopError):
    """One segment failed to parse. Skip it, keep the message, flag for review.

    Never retryable: the same bytes parse the same way next time, and the
    message is kept rather than refused, so there is nothing to redeliver.
    """

    retryable = False


class PackVerificationError(ReferralLoopError):
    """Pack signature missing, invalid, or altered. Refuse to boot.

    Not retryable, and not reachable from `_process` either -- this refuses the
    boot, so no message has been accepted to answer for. Declared for the same
    reason `FramingError` declares one.
    """

    retryable = False


class PackConceptMissingError(PackVerificationError):
    """A pack whose signature verified but whose field_map omits a concept this
    build reads without an `in pack.field_map` guard.

    A *subclass*, so every `except PackVerificationError` in the boot path keeps
    refusing exactly as it did -- this is still "the pack must not run". Typed
    separately for the reason MrnRetiredError is: a caller has to tell two
    failures apart out of one call, and matching on an error string is the
    fragile version of that. Here the two are "this pack is corrupt" and "this
    pack is older than this build", and they send an operator to different
    people -- whoever signed it, or whoever picks the baseline to gate against.
    `cli._run_eval` is the caller that needs the distinction.

    Carries the concept names, not only a message, because the caller naming a
    remedy needs the list and re-deriving it from the text is the same fragility
    one layer down.
    """

    retryable = False

    def __init__(self, missing: tuple[str, ...] | list[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(
            "Pack field_map is missing concept(s) this build reads on every message: "
            + ", ".join(self.missing)
        )


class StoreUnavailableError(ReferralLoopError):
    """Durable write failed. Respond AE so the engine queues. Never ACK.

    The original retryable failure, and the one the property is calibrated
    against: a full disk is emptied, a lock is released, a volume comes back,
    and the identical message then lands. Spec section 6 -- never ACK what
    cannot be stored.
    """

    retryable = True


class LoopNotFoundError(ReferralLoopError):
    """No events exist for the requested loop, so no state is derivable.

    A ReferralLoopError rather than a bare KeyError so callers can handle every
    referral-loop failure with one except clause.

    Not retryable. A loop id that names no events names none on the redelivery
    either -- the event log is append-only, so the absence this reports cannot
    be un-observed by asking again.
    """

    retryable = False


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

    retryable = False


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

    retryable = False


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

    retryable = True


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
    message type, ORC-1, the order numbers, the appointment identifier and the MRN but
    not MSH-10, and a second copy names the same appointment because it is the same
    message, so it is answered as a content duplicate before `_apply` is reached.
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

    retryable = False


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

    Not retryable, and the reason is the opposite of the usual one: the
    watermark this was refused against only ever moves *forward*, so a
    redelivery is refused harder than the first delivery was. AE here would
    queue a message that is guaranteed to be re-refused, at the head of the
    engine's outbound queue, forever.
    """

    retryable = False


class ThresholdsNotAcceptedError(ReferralLoopError):
    """Staleness thresholds shipped as defaults but not accepted by the site.

    Not retryable: this refuses the boot and the fix is an environment variable,
    not a redelivery. Like PackVerificationError, no message has been accepted
    at the point it is raised.
    """

    retryable = False
