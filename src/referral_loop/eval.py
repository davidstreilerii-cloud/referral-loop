"""Replay harness and the pack release gate.

This is what makes matching quality measurable rather than asserted. Without archive
replay a pack revision cannot be evaluated, and "rules as signed data" is
marketing: the raw archive is retained precisely so a candidate pack can be run
against real site traffic and its delta measured (spec section 7).

The metric is false-match rate, not accuracy
--------------------------------------------
A false match is strictly worse than an orphan. An orphan gets human attention;
a false match attributes a result to the wrong order, marks that loop resolved,
and leaves the real loop open *while reporting it handled* -- the tool concealing
the very thing it exists to surface.

*Renamed from false-close rate, which appears in earlier commits and in the plan
this module was written against. Nothing closes in v1 -- `CLOSED` is v2 -- so a
metric named for closure described something the system does not do. Same
definition, same veto, accurate name.*

**And "false-match rate is zero" alone is passed perfectly by a matcher that
attaches nothing.** Every result orphans, nothing is mis-attached, the number
reads 0.000 and the gate goes green. The degenerate implementation scores best,
and it fails invisibly: the metric everyone watches looks ideal precisely when
the product has stopped working, and the only symptom is a coordinator queue
quietly filling with the work the tool was bought to remove. A floor without a
coverage requirement optimises toward silence. So spec section 10.4's criterion
4 has two halves and `check_release_criteria` requires both -- false-match rate
zero *and* auto-match rate at or above the pack's `min_auto_match_rate`.

The harness does not reimplement matching
-----------------------------------------
`replay` drives `MessageHandler`, `Registry`, `LoopStore` and `match_result` --
the same objects live ingest drives. A harness that reimplemented tier logic
would measure the harness. Ground truth enters only as "which loop should this
result have landed on", never as "which tier should have fired".

Replay never touches production data
------------------------------------
Every case gets its own throwaway SQLite database under a scratch directory, and
`replay` refuses a scratch directory that already contains anything. There is no
argument through which a caller can hand it the site's database. The production
store is read in exactly one place -- `corpus_from_site`, which only ever issues
SELECTs -- and what it reads is copied into cases, never written back.

Per-case isolation is also the correct scoring semantic: every case carries the
order messages that create its own candidate loops, so a decline means "this
pack could not place this result among these orders" rather than "some other
case's loop happened to be lying around". It is what makes two runs of the same
corpus produce byte-identical metrics.

PHI posture
-----------
`LabeledCase.messages` holds raw HL7 and therefore holds PHI when the case came
from the site archive. It is excluded from the dataclass repr, so a pytest
failure, a log line or a stack frame summary shows a case name and an expected
placer -- never a patient. `EvalResult` is six floats and seven counts; it is
the artifact an operator pastes into a changelog and it cannot carry an
identifier by construction.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LoopNotFoundError, ReferralLoopError
from .events import LabelOutcome, LoopState
from .listener import MessageHandler
from .pack import RulePack
from .parse_hl7 import peek_control_id
from .phi_files import create_private_directory
from .registry import Registry
from .store import LoopStore

logger = logging.getLogger(__name__)


class EvalError(ReferralLoopError):
    """The harness refuses to produce a number it cannot stand behind.

    Every use is a refusal rather than a failure: an empty corpus, a scratch
    directory that already holds data, a corpus that would have to be replayed
    into a database somebody else owns. Producing a metric in any of those cases
    would be worse than producing none, because a metric gets pasted into a
    changelog and a refusal gets read.

    Not retryable: an evaluation is an operator running a command, not a message
    an engine is holding, and every refusal here needs a person to change
    something before a second run says anything different.
    """

    retryable = False


# Event types that mean "this result was attributed to this loop". `reopened` is
# in the set and its absence would be a silent hole: a corrected result, and any
# result landing on an ACKNOWLEDGED loop, produces `reopened` rather than
# `resulted` (registry.record_result, safety rule 2). Scoring only `resulted`
# would count every correction as an orphan, so a pack that handled corrections
# perfectly would read as one that never matched them.
_ATTACHMENT_EVENTS = frozenset({"resulted", "reopened"})
_ORPHAN_EVENT = "orphaned"
_CREATED_EVENT = "created"
_UNMATCHED_EVENT = "unmatched"
_ATTACHED_FROM = "attached_from"


@dataclass(frozen=True)
class EvalResult:
    """What one corpus, replayed through one pack, measured.

    Six rates, and the counts they were computed from. The counts are here
    because a rate with no denominator is unfalsifiable -- "precision 1.000" over
    one auto-match is not the same claim as over four hundred, and a changelog
    entry that records only the rate cannot be argued with.

    Every field is a float or an int. Nothing here can carry an identifier, which
    is the property that lets this be printed, logged and exported.
    """

    # Safety. An absolute veto in gate_pack_release, and half of criterion 4.
    false_match_rate: float
    # Primary: of auto-matched results, the share attributed to the right loop.
    precision: float
    # Secondary: of results with a correct open loop, the share matched correctly.
    recall: float
    # Coverage. The other half of criterion 4, and the reason a matcher that
    # attaches nothing cannot score a perfect result. Of results with a correct
    # open loop, the share the pack resolved without a human -- correctly or not.
    # Deliberately not `correct / matchable`, which is just recall under another
    # name: coverage and correctness are different questions and the safety
    # metric is what answers wrongness.
    auto_match_rate: float
    # Workload, not failure (spec section 5). Lower is better.
    orphan_rate: float
    # Feed health, watched for drift. Lower is better. Site-derived; see `replay`.
    dismissal_rate: float

    cases: int = 0
    matchable: int = 0
    auto_matched: int = 0
    correct: int = 0
    false_matches: int = 0
    orphans: int = 0
    # Cases the pipeline produced no transition for at all -- a message answered
    # AE, refused as clinically stale, or swallowed as a duplicate. Counted and
    # named rather than folded into orphans, because an orphan is a decision the
    # matcher made and this is the absence of one. It still sits in the
    # denominator of every rate, so a corpus that stops being processed reads as
    # a coverage collapse rather than as an unchanged score.
    unhandled: int = 0
    pack_version: str = ""


@dataclass(frozen=True)
class LabeledCase:
    """One synthetic or site-labeled scenario with a known correct answer.

    `messages` are applied in order and the **last** one is the scored message --
    the result whose attribution is the question. Everything before it is setup:
    the orders that create the candidate loops, and any scheduling or
    cancellation that changes what those loops will accept.

    `expected_loop_placer` is the placer order number of the loop that should
    receive the result, or None when the correct answer is to decline. Naming the
    expectation by placer rather than by loop id is deliberate: loop ids are
    minted at replay time and differ between runs, so an expectation keyed on one
    could never be written down in a corpus.

    None means "declining is correct", and it covers three genuinely different
    situations that the matcher must answer identically: the result belongs to no
    order here, the evidence is ambiguous between two orders, and the only
    candidate is one the failure matrix excludes. In each case attaching anything
    is a false match, which is why they share a label.

    **`messages` is repr=False and that is a PHI control, not tidiness.** A case
    reconstructed from the site archive holds raw HL7 -- names, MRNs, observation
    values. pytest prints the repr of every object in a failing assertion, and
    logging a case at DEBUG is one line away at all times.
    """

    name: str
    messages: tuple[str, ...] = field(repr=False)
    expected_loop_placer: str | None = None
    source: str = "synthetic"


# --------------------------------------------------------------- scoring

@dataclass(frozen=True)
class _CaseOutcome:
    """What the pipeline actually did with one case's scored message."""

    attached_placer: str | None = None
    attached: bool = False
    orphaned: bool = False


def _outcome_for(store: LoopStore, control_id: str) -> _CaseOutcome:
    """Find what the scored message did, by the control id it carried.

    Attribution by control id rather than by "which loops are RESULTED now".
    Reading the projection would credit this case with a transition some *other*
    message in the same case produced: a case where one result matches and a
    later one must orphan leaves a RESULTED loop sitting there, and a state scan
    would score the orphan as a match -- a false match manufactured by the
    harness, in the metric that vetoes releases.

    The whole set is scanned rather than returning on the first hit. `all_loops`
    has no ORDER BY, so returning early would make the answer depend on SQLite's
    row order if a control id ever touched two records. Attachment wins over an
    orphan, because a message that produced both did attribute a result.
    """
    if not control_id:
        return _CaseOutcome()
    orphaned = False
    for loop in store.all_loops():
        for event in store.events_for(loop.loop_id):
            if event.control_id != control_id:
                continue
            if event.event_type in _ATTACHMENT_EVENTS:
                return _CaseOutcome(attached_placer=loop.placer_order_number, attached=True)
            if event.event_type == _ORPHAN_EVENT and loop.state is LoopState.ORPHAN:
                orphaned = True
    return _CaseOutcome(orphaned=orphaned)


def _scratch_root(
    scratch_dir: Path | str | None, cleanup: list, *, scratch_parent: Path | str | None = None
) -> Path:
    """A directory nothing else owns, or a refusal.

    A caller who passes the site's data directory here gets an EvalError, not a
    replay that quietly appends synthetic loops to a coordinator's worklist. The
    check is "empty or absent", not "not the production path", because there is
    no reliable way to recognise a production path and every wrong guess fails in
    the direction that writes.

    **`scratch_parent` is where the volume decision is made.** With neither
    argument this creates its throwaway directory under `tempfile.gettempdir()`,
    which is the OS temp directory and is not necessarily on the volume the
    encryption gate attested -- `store._reclaim` refuses to let SQLite put a
    VACUUM copy there for exactly that reason, and a case database holds the same
    verbatim HL7. `cli` therefore always passes the site database's own parent,
    so the per-case files land inside the boundary the gate checked, beside the
    file they were reconstructed from. `replay` refuses a site corpus outright
    when neither is given, so the OS-temp branch cannot carry PHI at all.

    The parent is not subjected to the empty-or-absent check, because it is the
    *site's data directory* -- of course it is not empty, the database is in it.
    A `scratch_dir` an operator named is checked, because naming a directory is a
    claim to own it.

    **Both arguments end at the same `TemporaryDirectory`, and the single code
    path is deliberate.** It is unique per run, so the candidate replay and the
    baseline replay of one `eval` invocation do not collide -- two calls into one
    explicit `--scratch-dir` used to be the second one finding the first one's
    case databases and refusing. It is 0700 at the instant it appears rather than
    after a `chmod`. And it is removed in `replay`'s `finally`, which now covers
    `--scratch-dir` as well: an operator who pointed the harness at a second disk
    was previously left holding a directory of case databases full of verbatim
    HL7, indefinitely, which is the same defect as the temp directory one wearing
    different clothes.
    """
    parent: Path | None = None
    if scratch_dir is not None:
        parent = Path(scratch_dir)
        if parent.exists():
            if not parent.is_dir():
                raise EvalError(f"Scratch path {parent} exists and is not a directory")
            if any(parent.iterdir()):
                raise EvalError(
                    f"Refusing to replay into {parent}: it already contains data. A pack "
                    "evaluation must never write into a database somebody else owns -- "
                    "point --scratch-dir at an empty or non-existent directory."
                )
    elif scratch_parent is not None:
        parent = Path(scratch_parent)
    if parent is not None:
        # 0700 at creation, and left exactly as it is when it already exists --
        # see phi_files.create_private_directory for why this process
        # re-permissions only what it made.
        create_private_directory(parent)
    tmp = tempfile.TemporaryDirectory(
        prefix="referral-eval-", dir=None if parent is None else str(parent)
    )
    cleanup.append(tmp)
    return Path(tmp.name)


def _dismissal_rate(labels) -> float:
    """Share of recorded coordinator decisions that said "no loop here".

    Site-derived, not corpus-derived, and the distinction matters. Nothing is
    dismissed during a replay -- there is no coordinator in a replay -- so any
    corpus-internal definition would either be a constant or, worse, would move
    the wrong way: as a pack gets better at matching, fewer results orphan, so
    the *share* of orphans that were genuinely unmatchable rises. A pack revision
    that improved matching would read as a feed getting worse.

    Measured over the labels table instead, it says what spec section 7 says it
    says: of the decisions coordinators recorded, how many were "this result
    belongs to no loop at this site". Rising is a feed problem.

    One consequence, stated plainly rather than left for someone to discover:
    within a single gate run both packs are handed the same labels, so this
    metric is identical on both sides and can never by itself satisfy condition
    3. That is honest rather than broken -- nothing a pack does during a replay
    changes decisions coordinators already made. It moves between *releases*,
    once a revision has run at the site long enough to change what coordinators
    are recording, which is the timescale feed-health drift is watched on.
    """
    rows = list(labels or ())
    if not rows:
        return 0.0
    dismissed = sum(1 for row in rows if row.get("outcome") == LabelOutcome.NO_LOOP_HERE.value)
    return dismissed / len(rows)


def replay(
    cases,
    pack: RulePack,
    *,
    scratch_dir: Path | str | None = None,
    scratch_parent: Path | str | None = None,
    labels=None,
) -> EvalResult:
    """Run a labeled corpus through the real pipeline and score the outcome.

    `scratch_dir` rather than `db_path`, and that rename is the safety control.
    A parameter called `db_path` invites an operator to pass the database they
    already have; this one takes a directory the harness fills with throwaway
    files and refuses one that is not empty.

    `scratch_parent` is the other half, and it decides the *volume* rather than
    the directory: the harness makes its own unique, auto-removed subdirectory
    inside it. `cli` passes the site database's parent, so a replay's per-case
    files sit on the disk the encryption gate attested. See `_scratch_root`.

    `labels` are the site's coordinator decisions (`LoopStore.labels()`), used
    only for the dismissal rate. They are deliberately *not* folded into the
    false-match count: those labels were produced under whichever pack was
    running at the time, and attributing them to a candidate pack that has not
    been asked the question is exactly the unearned claim this harness exists to
    replace.
    """
    corpus = list(cases)
    if not corpus:
        # A green gate over nothing measured is the failure this whole module is
        # about, one level up. `precision 0.000, false-match 0.000` on an empty
        # corpus is not a passing evaluation, it is the absence of one.
        raise EvalError(
            "Refusing to score an empty corpus. Every rate would be 0.0, which for "
            "false-match rate reads as a perfect safety result and for auto-match "
            "rate reads as a total coverage failure -- the same number meaning "
            "opposite things because nothing was measured."
        )

    if scratch_dir is None and scratch_parent is None and any(
        case.source == "site" for case in corpus
    ):
        # Belt and braces behind `cli` always naming a parent, and the reason it
        # is here rather than there: this is the property, and a property
        # enforced only by its one caller is one the next caller does not get.
        # A site case's `messages` are verbatim HL7 out of the raw archive
        # (`corpus_from_site`), and with neither argument the per-case databases
        # would be written under `tempfile.gettempdir()` -- off the volume the
        # encryption gate attested, which is the whole claim `--db` rests on.
        # Refused rather than relocated by guesswork: this function has no
        # database and therefore no volume it could be right about.
        raise EvalError(
            "Refusing to replay site-reconstructed cases without a scratch location. "
            "Those cases hold verbatim HL7 from the raw archive, and the default "
            "location is the OS temporary directory, which is not necessarily on the "
            "encrypted volume this site attested. Pass scratch_parent (the site "
            "database's own directory, which is what `referral-loop eval` does) or "
            "scratch_dir."
        )

    cleanup: list = []
    try:
        root = _scratch_root(scratch_dir, cleanup, scratch_parent=scratch_parent)

        correct = 0
        false_matches = 0
        orphans = 0
        unhandled = 0
        matchable = 0
        auto_matched = 0
        auto_matched_matchable = 0

        for index, case in enumerate(corpus):
            store = LoopStore(root / f"case-{index:05d}.db")
            handler = MessageHandler(
                store=store,
                registry=Registry(store, pack_version=pack.version),
                pack=pack,
            )
            for text in case.messages:
                handler.handle(text)

            scored = peek_control_id(case.messages[-1]) if case.messages else ""
            outcome = _outcome_for(store, scored)
            expected = case.expected_loop_placer

            if expected is not None:
                matchable += 1

            if outcome.attached:
                auto_matched += 1
                if expected is not None:
                    auto_matched_matchable += 1
                if expected is not None and outcome.attached_placer == expected:
                    correct += 1
                else:
                    # Both branches are false matches and they are the same
                    # failure: a result attributed to an order it did not come
                    # from. Attaching to the wrong loop and attaching a result
                    # that should have orphaned differ only in whether the wrong
                    # loop was one the corpus named.
                    false_matches += 1
                    logger.debug("Case %s: false match (source %s)", case.name, case.source)
            elif outcome.orphaned:
                orphans += 1
            else:
                unhandled += 1
                logger.warning(
                    "Case %s produced no transition at all; the scored message was not "
                    "applied. Counted as unhandled and left in every denominator.",
                    case.name,
                )

        total = len(corpus)
        return EvalResult(
            false_match_rate=false_matches / total,
            precision=correct / auto_matched if auto_matched else 0.0,
            recall=correct / matchable if matchable else 0.0,
            # Zero when nothing is matchable, which fails the coverage floor.
            # That is the fail-closed direction: a corpus containing nothing a
            # pack could match proves nothing about coverage, and 1.0 ("resolved
            # everything it was asked to") would be a perfect score for a corpus
            # that asked nothing.
            auto_match_rate=auto_matched_matchable / matchable if matchable else 0.0,
            orphan_rate=orphans / total,
            dismissal_rate=_dismissal_rate(labels),
            cases=total,
            matchable=matchable,
            auto_matched=auto_matched,
            correct=correct,
            false_matches=false_matches,
            orphans=orphans,
            unhandled=unhandled,
            pack_version=pack.version,
        )
    finally:
        for tmp in cleanup:
            tmp.cleanup()


# ------------------------------------------------------------- the release gate

_TARGET_METRICS = ("precision", "recall", "auto_match_rate", "orphan_rate", "dismissal_rate")

# Orphan rate and dismissal rate improve by going DOWN; the other three by going
# up. Getting this backwards inverts the gate silently -- a pack that doubled the
# coordinator queue would read as an improvement and ship on it -- so the
# direction is data here rather than a sign buried in three comparisons.
_LOWER_IS_BETTER = frozenset({"orphan_rate", "dismissal_rate"})


def improved_metrics(baseline: EvalResult, candidate: EvalResult) -> tuple[str, ...]:
    """Target metrics that moved in the good direction, in a stable order."""
    return tuple(
        name
        for name in _TARGET_METRICS
        if (
            getattr(candidate, name) < getattr(baseline, name)
            if name in _LOWER_IS_BETTER
            else getattr(candidate, name) > getattr(baseline, name)
        )
    )


def check_release_criteria(result: EvalResult, pack: RulePack) -> tuple[bool, str]:
    """Spec section 10.4, criterion 4. Both halves, and both are required.

    Not a comparison against a baseline -- this is the absolute floor a pack must
    clear on its own, which is what makes it the answer to the very first pack, a
    pack with no predecessor to regress against, and a matcher that attaches
    nothing.

    The second half is the whole reason this function is not one line. "False
    match rate is zero" is satisfied perfectly by a matcher that declines
    everything, and it fails invisibly: the safety metric reads ideal exactly
    when the product has stopped working.
    """
    if result.false_match_rate > 0.0:
        return False, (
            f"FAILS criterion 4: false-match rate is {result.false_match_rate:.4f}, not zero "
            f"({result.false_matches} of {result.cases} cases attributed a result to the "
            f"wrong loop). A false match leaves the real loop open while reporting it handled."
        )
    if result.auto_match_rate < pack.min_auto_match_rate:
        return False, (
            f"FAILS criterion 4: auto-match rate {result.auto_match_rate:.4f} is below the "
            f"pack's minimum {pack.min_auto_match_rate:.4f} "
            f"({result.auto_matched} of {result.matchable} matchable cases resolved without "
            f"a human). A zero false-match rate over near-zero coverage is what a matcher "
            f"that attaches nothing scores; the coordinator queue absorbs the difference."
        )
    return True, (
        f"MEETS criterion 4: false-match rate {result.false_match_rate:.4f} with auto-match "
        f"rate {result.auto_match_rate:.4f} at or above the pack floor "
        f"{pack.min_auto_match_rate:.4f}."
    )


def gate_pack_release(
    baseline: EvalResult, candidate: EvalResult, pack: RulePack | None = None
) -> tuple[bool, str]:
    """Three conditions, in order. The first is an absolute veto.

    1. **False-match rate must not regress** -- regardless of every other number.
       Missing a match costs a coordinator a lookup; a wrong match costs a patient
       a missed finding, and that asymmetry is the whole point of the gate.
    2. **Precision must not regress.**
    3. **At least one target metric must improve** -- precision, recall,
       auto-match rate, orphan rate or dismissal rate.

    Condition 3 is not decoration. Without it a pack that changes nothing
    measurable ships, and the changelog fills with revisions nobody can justify by
    replay -- which is precisely the mechanism that makes matching quality measurable
    rather than asserted.

    Note what condition 2 is *not*: it is not "precision must improve". The
    original gate required that, and it blocked releases it should have allowed --
    a pack that leaves precision untouched but halves the orphan rate is a pure
    coordinator-workload win with no safety cost, vetoed by the one metric it did
    not move.

    `pack` is optional and adds the coverage floor as condition 1b, immediately
    after the safety veto because it is the other half of the same safety
    criterion rather than a separate concern. Without it the gate is passable by a
    candidate that attaches nothing whenever the baseline attached nothing either:
    false-match rate holds at 0.000, precision holds at 0.000, and any corpus
    churn that nudged a rate satisfies condition 3. Supply the candidate's pack.

    Comparisons are exact, with no tolerance. A tolerance on condition 1 would let
    a real safety regression smaller than the tolerance ship, which is the wrong
    direction to be approximate in; both results are computed over the same corpus
    with the same denominators, so equal outcomes produce equal floats.
    """
    if candidate.false_match_rate > baseline.false_match_rate:
        return False, (
            f"BLOCKED: false-match rate rose {baseline.false_match_rate:.4f} -> "
            f"{candidate.false_match_rate:.4f}. Safety regression is an absolute veto, "
            f"regardless of every other metric -- including recall."
        )

    if pack is not None and candidate.auto_match_rate < pack.min_auto_match_rate:
        return False, (
            f"BLOCKED: auto-match rate {candidate.auto_match_rate:.4f} is below the pack's "
            f"minimum {pack.min_auto_match_rate:.4f}. A pack that resolves nothing scores a "
            f"perfect false-match rate; the coverage floor is what stops that shipping."
        )

    if candidate.precision < baseline.precision:
        return False, (
            f"BLOCKED: precision regressed "
            f"({baseline.precision:.4f} -> {candidate.precision:.4f})."
        )

    improved = improved_metrics(baseline, candidate)
    if not improved:
        return False, (
            "BLOCKED: no target metric improved (precision, recall, auto-match rate, "
            "orphan rate, dismissal rate). A pack revision must be justified by replay "
            "evidence, not by intent."
        )

    return True, (
        f"OK: false-match {baseline.false_match_rate:.4f} -> {candidate.false_match_rate:.4f}, "
        f"precision {baseline.precision:.4f} -> {candidate.precision:.4f}, "
        f"improved: {', '.join(improved)}."
    )


def format_report(result: EvalResult, *, title: str = "eval") -> str:
    """An operator-readable block. Counts as well as rates, and no identifiers.

    Every value printed here is a float or an int off EvalResult, so this cannot
    become a PHI egress path by someone later adding a field to a case.
    """
    return "\n".join(
        [
            f"--- {title} (pack {result.pack_version or 'unknown'}) ---",
            f"  cases            {result.cases:>6}   matchable {result.matchable}",
            f"  false-match rate {result.false_match_rate:>6.4f}   ({result.false_matches})",
            f"  precision        {result.precision:>6.4f}   ({result.correct}/{result.auto_matched})",
            f"  recall           {result.recall:>6.4f}   ({result.correct}/{result.matchable})",
            f"  auto-match rate  {result.auto_match_rate:>6.4f}   (floor is pack data)",
            f"  orphan rate      {result.orphan_rate:>6.4f}   ({result.orphans}) lower is better",
            f"  dismissal rate   {result.dismissal_rate:>6.4f}   lower is better",
            f"  unhandled        {result.unhandled:>6}   no transition at all",
        ]
    )


# ------------------------------------------------------------ synthetic corpus
#
# Generated from the HL7 v2 field definitions rather than captured from a feed,
# so it ships with the repository and contains no PHI (spec section 7: "synthetic
# pairs generated from the HL7 v2 spec (ships with the repo, no PHI)"). Names and
# identifiers are visibly manufactured for exactly that reason.

_SYNTH_ORDERED_AT = "20260720080000"
_SYNTH_OBSERVED_AT = "20260720140000"     # six hours later; inside every window
_SYNTH_LATE_AT = "20260724140000"         # four days later; outside the CT window
_SYNTH_CORRECTED_AT = "20260720160000"

_SYNTH_PROVIDER = "PRV001^SYNTHETIC^ORDERER"
_SYNTH_NAME = "SYNTHETIC^PATIENT"
_SYNTH_CT_SERVICE = "71260^CT CHEST W CONTRAST^C4"
_SYNTH_CT_HEAD = "70450^CT HEAD WO CONTRAST^C4"
_SYNTH_CT_HEAD_W = "70460^CT HEAD W CONTRAST^C4"


def _segment(seg_id: str, fields: dict[int, str]) -> str:
    """Build a segment by field number, never by counting pipes.

    Hand-counting to OBR-18 is how a field-map fallback silently becomes
    untested; naming the index makes the field number the thing the corpus
    states.
    """
    width = max(fields) if fields else 0
    slots = [""] * (width + 1)
    slots[0] = seg_id
    for index, value in fields.items():
        slots[index] = value
    return "|".join(slots)


def _msh(message_type: str, control_id: str, when: str) -> str:
    """MSH-1 *is* the field separator, so MSH fields shift by one."""
    fields = {
        2: r"^~\&", 3: "RIS", 4: "SYNTH", 5: "TRACKER", 6: "SYNTH",
        7: when, 9: message_type, 10: control_id, 11: "P", 12: "2.5.1",
    }
    width = max(fields)
    slots = [""] * (width + 1)
    for index, value in fields.items():
        slots[index] = value
    return "MSH|" + "|".join(slots[2:])


def _pid(mrn: str) -> str:
    return _segment("PID", {1: "1", 3: f"{mrn}^^^SYNTH^MR", 5: _SYNTH_NAME, 8: "F"})


def _message(*segments: str) -> str:
    return "".join(s + "\r" for s in segments)


def _order(
    *, control: str, mrn: str, placer: str, filler: str,
    service: str = _SYNTH_CT_SERVICE, modality: str = "CT",
    when: str = _SYNTH_ORDERED_AT, provider: str = _SYNTH_PROVIDER,
) -> str:
    return _message(
        _msh("ORM^O01", control, when),
        _pid(mrn),
        _segment("ORC", {1: "NW", 2: placer, 3: filler}),
        _segment("OBR", {1: "1", 2: placer, 3: filler, 4: service, 7: when,
                         16: provider, 24: modality}),
    )


def _schedule(*, control: str, mrn: str, placer: str, filler: str,
              when: str = _SYNTH_ORDERED_AT) -> str:
    # No SCH segment, matching `_cancel` below, and a segment present in one of a
    # matched pair and absent from the other invites a later reader to think the
    # difference is load-bearing.
    #
    # `content_key` does read SCH now, through `appointment_id`, so the older form of
    # this note -- that nothing in the ingest path reads one -- has stopped being true.
    # It does not change what belongs here: this corpus measures *matching*, and
    # `_target_loop` still resolves an SIU on ORC/OBR order numbers alone. An absent
    # SCH keys these cases on the tuple they were always keyed on. Adding one would
    # buy the harness no new signal either, because the rebooking collision it would
    # exercise is a suppressed message rather than a false match, an orphan or a
    # dismissal, and those are the outcomes `EvalResult` counts.
    return _message(
        _msh("SIU^S12", control, when),
        _pid(mrn),
        _segment("ORC", {1: "SC", 2: placer, 3: filler}),
        _segment("OBR", {1: "1", 2: placer, 3: filler, 7: when}),
    )


def _cancel(*, control: str, mrn: str, placer: str, filler: str,
            when: str = _SYNTH_ORDERED_AT) -> str:
    return _message(
        _msh("SIU^S15", control, when),
        _pid(mrn),
        _segment("ORC", {1: "CA", 2: placer, 3: filler}),
        _segment("OBR", {1: "1", 2: placer, 3: filler, 7: when}),
    )


def _result(
    *, control: str, mrn: str, placer: str = "", filler: str = "",
    filler_field: int = 3, service: str = _SYNTH_CT_SERVICE, modality: str = "CT",
    when: str = _SYNTH_OBSERVED_AT, status: str = "F", value: str = "NO ACUTE FINDING",
    provider: str = _SYNTH_PROVIDER,
) -> str:
    obr = {1: "1", 2: placer, 4: service, 7: when, 16: provider, 24: modality}
    obr[filler_field] = filler
    return _message(
        _msh("ORU^R01", control, when),
        _pid(mrn),
        _segment("OBR", obr),
        _segment("OBX", {1: "1", 2: "TX", 3: "IMP^IMPRESSION^L", 5: value, 11: status}),
    )


def synthetic_corpus() -> list[LabeledCase]:
    """Labeled pairs generated from the HL7 v2 spec. Ships with the repo, no PHI.

    Deliberately a mix of what a pack should match and what it must decline, in
    roughly the proportion a real feed produces. A corpus of only matchable pairs
    would let a matcher that attaches everything score perfectly on the safety
    metric, which is the false-match failure arriving through the door the
    degenerate matcher left open.

    The tier-4 case is expected to match and is *not* matched by the shipped pack,
    whose tier-4 confidence (0.70) sits below its floor (0.90). That is not a
    broken case -- ground truth is a property of the corpus, not of the pack, and
    a corpus every pack already satisfies leaves condition 3 of the release gate
    unsatisfiable. It is the headroom a pack revision can be measured against.
    """
    cases: list[LabeledCase] = []

    # ------------------------------------------------ should match

    cases.append(LabeledCase(
        name="tier1-placer-exact",
        messages=(
            _order(control="S-T1-ORD", mrn="SYN0001", placer="SYNP001", filler="SYNF001"),
            _result(control="S-T1-RES", mrn="SYN0001", placer="SYNP001"),
        ),
        expected_loop_placer="SYNP001",
    ))

    cases.append(LabeledCase(
        name="tier2-filler-exact",
        messages=(
            _order(control="S-T2-ORD", mrn="SYN0002", placer="SYNP002", filler="SYNF002"),
            _result(control="S-T2-RES", mrn="SYN0002", filler="SYNF002"),
        ),
        expected_loop_placer="SYNP002",
    ))

    # Spec test 17: the same accession lives in OBR-3 at one site and OBR-18 at
    # another. The order writes it to OBR-3, the result to OBR-18, and only the
    # pack's candidate list bridges them -- a code change would be the wrong fix.
    cases.append(LabeledCase(
        name="tier2-filler-relocated-to-obr18",
        messages=(
            _order(control="S-T2B-ORD", mrn="SYN0003", placer="SYNP003", filler="SYNF003"),
            _result(control="S-T2B-RES", mrn="SYN0003", filler="SYNF003", filler_field=18),
        ),
        expected_loop_placer="SYNP003",
    ))

    cases.append(LabeledCase(
        name="tier3-service-code-in-window",
        messages=(
            _order(control="S-T3-ORD", mrn="SYN0004", placer="SYNP004", filler="SYNF004"),
            _result(control="S-T3-RES", mrn="SYN0004"),
        ),
        expected_loop_placer="SYNP004",
    ))

    # Tier 4: different service code, equivalent modality spelling ("CAT" for
    # "CT"), inside the window. Correct answer is the loop; the shipped pack
    # declines because tier 4 sits below its confidence floor.
    cases.append(LabeledCase(
        name="tier4-modality-equivalence",
        messages=(
            _order(control="S-T4-ORD", mrn="SYN0005", placer="SYNP005", filler="SYNF005",
                   service=_SYNTH_CT_HEAD),
            _result(control="S-T4-RES", mrn="SYN0005", service=_SYNTH_CT_HEAD_W,
                    modality="CAT"),
        ),
        expected_loop_placer="SYNP005",
    ))

    # Safety rule 2: a correction lands on the loop it corrects, as `reopened`.
    # Scored on the correction, which is the message whose attribution matters.
    cases.append(LabeledCase(
        name="corrected-result-reaches-the-same-loop",
        messages=(
            _order(control="S-C-ORD", mrn="SYN0006", placer="SYNP006", filler="SYNF006"),
            _result(control="S-C-FIN", mrn="SYN0006", placer="SYNP006"),
            _result(control="S-C-COR", mrn="SYN0006", placer="SYNP006", status="C",
                    when=_SYNTH_CORRECTED_AT, value="SMALL EFFUSION"),
        ),
        expected_loop_placer="SYNP006",
    ))

    # ------------------------------------------------ must decline

    cases.append(LabeledCase(
        name="no-order-anywhere",
        messages=(
            _result(control="S-N1-RES", mrn="SYN0100", placer="SYNP100"),
        ),
        expected_loop_placer=None,
    ))

    cases.append(LabeledCase(
        name="order-numbers-match-nothing",
        messages=(
            _order(control="S-N2-ORD", mrn="SYN0101", placer="SYNP101", filler="SYNF101",
                   service=_SYNTH_CT_HEAD, modality="CT"),
            _result(control="S-N2-RES", mrn="SYN0101", placer="SYNP999", filler="SYNF999",
                    service="76700^US ABDOMEN^C4", modality="US"),
        ),
        expected_loop_placer=None,
    ))

    # Two orders the pack's tie-breakers cannot separate: same patient, same
    # service code, same order time, same provider, same modality. Attaching to
    # either is a coin flip dressed as a match.
    cases.append(LabeledCase(
        name="ambiguous-between-two-identical-orders",
        messages=(
            _order(control="S-N3-ORD-A", mrn="SYN0102", placer="SYNP102", filler="SYNF102"),
            _order(control="S-N3-ORD-B", mrn="SYN0102", placer="SYNP103", filler="SYNF103"),
            _result(control="S-N3-RES", mrn="SYN0102"),
        ),
        expected_loop_placer=None,
    ))

    cases.append(LabeledCase(
        name="observation-outside-the-modality-window",
        messages=(
            _order(control="S-N4-ORD", mrn="SYN0104", placer="SYNP104", filler="SYNF104"),
            _result(control="S-N4-RES", mrn="SYN0104", when=_SYNTH_LATE_AT),
        ),
        expected_loop_placer=None,
    ))

    # The un-schedule fix, measured rather than asserted -- the before/after the kernel
    # design document asked this vocabulary change to carry.
    #
    # It was labelled the other way and named "result-for-a-cancelled-loop", because an
    # SIU^S15 drove Registry.cancel: the loop reached CANCELLED, CANCELLED is outside
    # _EXACT_TIER_STATES, and the report that followed orphaned. The scenario is the
    # ordinary one -- the specialist's office moves the CT, then the scan happens and is
    # reported -- and orphaning that result was the corpus scoring the defect as correct
    # behaviour. An S15 un-books; the referral stays open; the result that arrives against
    # it belongs to it.
    cases.append(LabeledCase(
        name="result-after-a-cancelled-appointment",
        messages=(
            _order(control="S-N5-ORD", mrn="SYN0105", placer="SYNP105", filler="SYNF105"),
            _schedule(control="S-N5-SCH", mrn="SYN0105", placer="SYNP105", filler="SYNF105"),
            _cancel(control="S-N5-CAN", mrn="SYN0105", placer="SYNP105", filler="SYNF105"),
            _result(control="S-N5-RES", mrn="SYN0105", placer="SYNP105"),
        ),
        expected_loop_placer="SYNP105",
    ))

    # A patient with one order gets two results: one that matches, and one for a
    # study nobody here ordered. The second must orphan even though the loop
    # standing next to it is RESULTED and still a candidate. This is the case
    # that makes attribution-by-control-id load-bearing rather than tidy -- score
    # it by scanning states and the orphan is credited to the loop the *earlier*
    # message matched, which is a false match manufactured by the harness.
    cases.append(LabeledCase(
        name="second-result-for-a-study-nobody-ordered",
        messages=(
            _order(control="S-N7-ORD", mrn="SYN0108", placer="SYNP108", filler="SYNF108"),
            _result(control="S-N7-RES-A", mrn="SYN0108", placer="SYNP108"),
            _result(control="S-N7-RES-B", mrn="SYN0108", service="76700^US ABDOMEN^C4",
                    modality="US", when=_SYNTH_CORRECTED_AT),
        ),
        expected_loop_placer=None,
    ))

    # Tiers 3 and 4 key on MRN. A result for another patient with the same
    # service code inside the same window must not reach this order.
    cases.append(LabeledCase(
        name="same-study-different-patient",
        messages=(
            _order(control="S-N6-ORD", mrn="SYN0106", placer="SYNP106", filler="SYNF106"),
            _result(control="S-N6-RES", mrn="SYN0107"),
        ),
        expected_loop_placer=None,
    ))

    return cases


# ------------------------------------------------------------ site-label corpus


def _archive_index(store: LoopStore) -> dict[str, str]:
    """control id -> archived payload, for the messages a label points at.

    Built by re-reading MSH-10 out of each payload with the same helper the
    listener keyed the archive on, so the index cannot disagree with the archive
    about which message a control id names.
    """
    index: dict[str, str] = {}
    for payload in store.raw_payloads():
        control_id = peek_control_id(payload)
        if control_id and control_id not in index:
            index[control_id] = payload
    return index


def _created_control_id(events) -> str:
    for event in events:
        if event.event_type == _CREATED_EVENT:
            return event.control_id
    return ""


def corpus_from_site(store: LoopStore) -> list[LabeledCase]:
    """Labeled cases reconstructed from real coordinator decisions.

    Spec section 7's flywheel: every orphan a coordinator attaches is a labeled
    example, and every auto-match they undo is a labeled false positive -- more
    valuable than a synthetic case because it is a real interface quirk from a
    real site.

    **The `labels` table alone cannot be replayed and is not meant to be.** It
    deliberately carries no MRN, no actor identity, no free text and no control
    id, which is exactly what makes it the exportable artifact. What it does
    carry is a loop id, and `loop_events` turns that into the control ids of the
    messages involved, and the raw archive turns those into the messages
    themselves. The label says *which* loops a human corrected; the log and the
    archive say what to replay. That is why the archive is retained.

    Two label outcomes are reconstructible and the rest are deliberately not:

      * `MISSED_MATCH` (an attached orphan). The order that created the target
        loop plus the ORU that orphaned. Ground truth: this result belongs to
        that loop.
      * `FALSE_MATCH` (an undone auto-match). The order that created the loop
        plus the ORU that wrongly matched it. Ground truth: decline -- and
        because the reconstructed case contains only that one order, "must not
        match this loop" and "must decline" are the same assertion.

    `MISTAKEN_ATTACHMENT` is excluded on purpose. A coordinator attached a result
    to the wrong loop and undid it; the matcher never made that claim, and
    turning a human's slip into a case whose ground truth is "decline" would put
    clerical error into the metric that vetoes pack releases.
    `ACKNOWLEDGEMENT_WITHDRAWN` says nothing about matching at all, and
    `NO_LOOP_HERE` is already counted as the dismissal rate.

    Anything that cannot be reconstructed exactly -- an order whose message has
    aged out of the archive, a loop with no placer order number to express the
    expectation with -- is skipped and counted in the log. A guessed case is
    worse than a missing one: it would move the release gate on evidence nobody
    can check.
    """
    # Kept as (key, case) pairs rather than as cases, so the conflict branch can
    # withdraw a case it already accepted without matching on its name.
    accepted: list[tuple[tuple[str, str], LabeledCase]] = []
    # key -> the expectation already recorded for it. A dict rather than a set,
    # because two labels over the same pair of messages can disagree about what
    # the right answer was, and resolving that by arrival order would let a
    # coordinator's sequence of clicks decide what the release gate measures.
    seen: dict[tuple[str, str], str | None] = {}
    conflicts: set[tuple[str, str]] = set()
    skipped = 0

    reconstructors = {
        LabelOutcome.MISSED_MATCH.value: _missed_match_cases,
        LabelOutcome.FALSE_MATCH.value: _false_match_cases,
    }

    rows = store.labels()
    if not any(row.get("outcome") in reconstructors for row in rows):
        # Built only when something needs it. The index is the whole raw archive
        # in memory, and a site with no reconstructable labels yet -- which is
        # every site on day one -- should not pay for reading it.
        return []
    archive = _archive_index(store)

    for row in rows:
        # An ignore rather than an annotation, because no annotation removes this
        # one: `store.labels()` returns untyped dicts, so
        # `row.get("outcome")` is `Any | None`, and `dict.get` insists on its exact
        # key type. Widening `reconstructors` to accept a None key would be a lie
        # -- nothing ever puts one there. A row with no "outcome" is a legitimate
        # miss and the very thing the outer `.get` is here to absorb: it returns
        # None and the `build is None` guard on the next line skips the row.
        build = reconstructors.get(row.get("outcome"))  # type: ignore[arg-type]
        loop_id = row.get("loop_id") or ""
        if build is None or not loop_id:
            continue
        try:
            events = store.events_for(loop_id)
        except ReferralLoopError:
            skipped += 1
            continue
        if not events:
            skipped += 1
            continue
        for case, key in build(store, archive, loop_id, events):
            if case is None:
                skipped += 1
                continue
            if key in seen:
                if seen[key] != case.expected_loop_placer:
                    # Two labels over the same two messages, disagreeing about
                    # what the right answer was. Both cannot be ground truth,
                    # and keeping whichever the coordinator clicked first would
                    # make the corpus depend on the order of their afternoon. So
                    # the pair is withdrawn entirely: an ambiguous case in a
                    # corpus that feeds an absolute veto is worse than no case.
                    conflicts.add(key)
                    logger.warning(
                        "Site corpus: two labels disagree about the same pair of archived "
                        "messages; the pair is excluded rather than resolved by arrival "
                        "order. Both cannot be ground truth."
                    )
                continue
            seen[key] = case.expected_loop_placer
            accepted.append((key, case))

    cases = [case for key, case in accepted if key not in conflicts]

    if skipped:
        logger.info(
            "Site corpus: %d label(s) could not be reconstructed exactly and were skipped "
            "(archived message missing, or the loop carries no placer order number). "
            "A guessed case would move the release gate on evidence nobody can check.",
            skipped,
        )
    logger.info("Site corpus: %d case(s) reconstructed from coordinator decisions", len(cases))
    return cases


def _missed_match_cases(store: LoopStore, archive: dict[str, str], loop_id: str, events):
    """One case per orphan a coordinator attached to this loop."""
    order_control = _created_control_id(events)
    try:
        loop = store.replay(loop_id)
    except LoopNotFoundError:
        yield None, ("", "")
        return
    placer = loop.placer_order_number

    for event in events:
        if event.event_type not in _ATTACHMENT_EVENTS:
            continue
        orphan_id = str(event.detail.get(_ATTACHED_FROM) or "")
        if not orphan_id:
            continue
        orphan_events = store.events_for(orphan_id)
        oru_control = next(
            (e.control_id for e in orphan_events if e.event_type == _ORPHAN_EVENT), ""
        )
        if not placer or order_control not in archive or oru_control not in archive:
            yield None, (order_control, oru_control)
            continue
        yield (
            LabeledCase(
                name=f"site-missed-match-{oru_control}",
                messages=(archive[order_control], archive[oru_control]),
                expected_loop_placer=placer,
                source="site",
            ),
            (order_control, oru_control),
        )


def _false_match_cases(store: LoopStore, archive: dict[str, str], loop_id: str, events):
    """One case per auto-match a coordinator undid on this loop.

    The falsely-matched ORU is the result event immediately preceding each
    `unmatched` event. Walking backwards from the undo rather than taking the
    latest result is what keeps a loop that was matched, undone, rematched and
    undone again from producing two copies of the same case.
    """
    order_control = _created_control_id(events)

    for index, event in enumerate(events):
        if event.event_type != _UNMATCHED_EVENT:
            continue
        prior = None
        for candidate in reversed(events[:index]):
            if candidate.event_type in _ATTACHMENT_EVENTS:
                prior = candidate
                break
        if prior is None:
            continue
        if prior.detail.get(_ATTACHED_FROM):
            # A coordinator's own attachment, undone. Human error, never a
            # matcher false positive -- see the module docstring on why this
            # distinction guards the veto.
            continue
        oru_control = prior.control_id
        if order_control not in archive or oru_control not in archive:
            yield None, (order_control, oru_control)
            continue
        yield (
            LabeledCase(
                name=f"site-false-match-{oru_control}",
                messages=(archive[order_control], archive[oru_control]),
                expected_loop_placer=None,
                source="site",
            ),
            (order_control, oru_control),
        )
