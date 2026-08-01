"""The eval harness and the pack release gate.

Three failures are in scope and they pull against each other.

  * **A false match ships.** Condition 1 of the gate is an absolute veto and the
    tests here prove it survives a candidate with wildly better recall.
  * **The degenerate matcher ships.** A matcher that attaches nothing scores a
    perfect 0.000 false-match rate. `test_degenerate_matcher_*` build one out of
    a real pack, replay the real corpus through it, and assert that both
    criterion 4 and the gate refuse it. If those pass, the safety metric is
    decorative.
  * **The gate's directions invert.** Orphan rate and dismissal rate are
    lower-is-better and the other three are higher-is-better. A flipped sign is
    invisible in every test that only ever improves things, so each metric is
    tested in *both* directions -- improving it alone must allow, worsening it
    alone must block.

And one that is easy to miss: **a harness that can never report a nonzero
false-match rate makes the veto untested**. `test_a_pack_that_manufactures_a_
false_match_is_caught` builds a pack whose date window is wide enough to attach a
result the corpus says must orphan, and asserts the number moves.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from referral_loop import audit as referral_audit
from referral_loop.cli import PUBKEY_ENV, main
from referral_loop.errors import ReferralLoopError
from referral_loop.eval import (
    EvalError,
    EvalResult,
    LabeledCase,
    check_release_criteria,
    corpus_from_site,
    format_report,
    gate_pack_release,
    improved_metrics,
    replay,
    synthetic_corpus,
)
from referral_loop.eval import _order as _eval_order
from referral_loop.eval import _result as _eval_result
from referral_loop.events import LoopState
from referral_loop.listener import MessageHandler
from referral_loop.pack import RulePack, load_pack
from referral_loop.registry import Registry
from referral_loop.store import LoopStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_PACK_DIR = REPO_ROOT / "src" / "referral_loop" / "rules"


# ------------------------------------------------------------------- fixtures


def _shipped_pack_body() -> dict:
    return json.loads((SHIPPED_PACK_DIR / "pack.json").read_bytes())


def _pack_from(body: dict) -> RulePack:
    """A RulePack straight from a body dict, skipping signature verification.

    Verification is pack.py's property and has its own suite; constructing here
    keeps these tests about the harness rather than about Ed25519.
    """
    return RulePack(
        version=str(body["version"]),
        confidence_floor=float(body["confidence_floor"]),
        date_windows_hours=body["date_windows_hours"],
        staleness_hours=body["staleness_hours"],
        modality_equivalence=body["modality_equivalence"],
        tie_breakers=tuple(body["tie_breakers"]),
        tier_confidence={int(k): float(v) for k, v in body["tier_confidence"].items()},
        field_map=body["field_map"],
        min_auto_match_rate=float(body["min_auto_match_rate"]),
    )


@pytest.fixture()
def shipped_pack() -> RulePack:
    return _pack_from(_shipped_pack_body())


@pytest.fixture()
def corpus() -> list[LabeledCase]:
    return synthetic_corpus()


def _degenerate(pack: RulePack) -> RulePack:
    """A matcher that attaches nothing, built out of pack data alone.

    A confidence floor above every tier makes `_resolve` decline at every tier,
    so every result orphans -- the implementation that scores a perfect
    false-match rate while doing no work. Built from a pack rather than by
    stubbing `match_result`, because a stub would prove the gate rejects a stub.
    """
    return replace(pack, confidence_floor=1.01)


def _wide_window(pack: RulePack) -> RulePack:
    """A pack whose date window is wide enough to manufacture a false match.

    The corpus contains a CT result four days after its order, which the shipped
    24-hour CT window correctly declines. Widen the window and tier 3 fires on
    it -- a result attributed to an order it did not come from.
    """
    return replace(pack, date_windows_hours={**pack.date_windows_hours, "CT": 10_000})


def _tier4_trusted(pack: RulePack) -> RulePack:
    """A plausible pack revision: trust the modality-equivalence tier."""
    return replace(pack, tier_confidence={**pack.tier_confidence, 4: 0.95})


def _result(**overrides) -> EvalResult:
    base = dict(
        false_match_rate=0.0, precision=0.90, recall=0.80,
        auto_match_rate=0.70, orphan_rate=0.10, dismissal_rate=0.05,
    )
    base.update(overrides)
    return EvalResult(**base)


# ------------------------------------------------- the gate: condition 1, veto


def test_release_is_allowed_when_false_match_holds_and_precision_improves():
    baseline = _result(precision=0.90)
    candidate = _result(precision=0.93, recall=0.78)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is True, reason


def test_any_increase_in_false_match_blocks_release():
    """Regression on the safety metric blocks release regardless of recall.

    The candidate here is better on literally every other axis. That is the
    point: missing a match costs a coordinator a lookup, a wrong match costs a
    patient a missed finding, and the asymmetry is the whole gate.
    """
    baseline = _result(false_match_rate=0.0, precision=0.90, recall=0.80)
    candidate = _result(
        false_match_rate=0.001, precision=0.99, recall=0.99,
        auto_match_rate=0.99, orphan_rate=0.01, dismissal_rate=0.01,
    )
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "false-match" in reason


def test_a_false_match_regression_is_reported_before_a_precision_regression():
    """Condition order is load-bearing for the operator, not just for the answer.

    A candidate that regresses both must say "safety", or the person reading the
    refusal goes and tunes precision.
    """
    baseline = _result(false_match_rate=0.0, precision=0.90)
    candidate = _result(false_match_rate=0.01, precision=0.50)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "false-match" in reason
    assert "precision regressed" not in reason


def test_an_improved_false_match_rate_does_not_by_itself_allow_release():
    """It is a veto, not a target metric. Improving it is not a justification."""
    baseline = _result(false_match_rate=0.01)
    candidate = _result(false_match_rate=0.0)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "no target metric improved" in reason


# --------------------------------------------- the gate: condition 2, precision


def test_precision_regression_blocks_even_with_better_recall():
    baseline = _result(precision=0.90, recall=0.80)
    candidate = _result(precision=0.85, recall=0.99)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "precision" in reason


def test_precision_must_hold_but_need_not_improve():
    """The original gate required precision to *improve*, and blocked this.

    A pack that leaves precision untouched and halves the orphan rate is a pure
    coordinator-workload win with no safety cost. Vetoing it on the one metric it
    did not move is the bug spec section 7 records.
    """
    baseline = _result(precision=0.90, orphan_rate=0.30)
    candidate = _result(precision=0.90, orphan_rate=0.15)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is True, reason
    assert "orphan_rate" in reason


# ------------------------------------------ the gate: condition 3, improvement


def test_a_pack_that_changes_nothing_measurable_is_blocked():
    """Condition 3 is not decoration.

    Without it the changelog fills with revisions nobody can justify by replay,
    which is the mechanism that makes matching quality measurable rather than asserted.
    """
    baseline = _result()
    allowed, reason = gate_pack_release(baseline, _result())
    assert allowed is False
    assert "no target metric improved" in reason


@pytest.mark.parametrize("metric", ["precision", "recall", "auto_match_rate"])
def test_each_higher_is_better_metric_improves_by_going_up(metric):
    baseline = _result()
    candidate = _result(**{metric: getattr(baseline, metric) + 0.05})
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is True, reason
    assert metric in improved_metrics(baseline, candidate)


@pytest.mark.parametrize("metric", ["recall", "auto_match_rate"])
def test_a_higher_is_better_metric_going_down_is_not_an_improvement(metric):
    """The inversion test for the three metrics that improve upward.

    Only recall and auto-match rate are parameterised: a precision drop is
    blocked by condition 2 first, and asserting it here would be asserting the
    wrong condition.
    """
    baseline = _result()
    candidate = _result(**{metric: getattr(baseline, metric) - 0.05})
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "no target metric improved" in reason
    assert metric not in improved_metrics(baseline, candidate)


@pytest.mark.parametrize("metric", ["orphan_rate", "dismissal_rate"])
def test_each_lower_is_better_metric_improves_by_going_down(metric):
    """Orphan rate and dismissal rate are lower-is-better.

    Half of the inversion pair. A flipped comparison passes this test's twin
    below and fails here, and vice versa -- neither alone can catch it.
    """
    baseline = _result()
    candidate = _result(**{metric: getattr(baseline, metric) - 0.02})
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is True, reason
    assert improved_metrics(baseline, candidate) == (metric,)


@pytest.mark.parametrize("metric", ["orphan_rate", "dismissal_rate"])
def test_a_lower_is_better_metric_going_up_is_not_an_improvement(metric):
    """The other half. A pack that doubles the coordinator queue has not improved."""
    baseline = _result()
    candidate = _result(**{metric: getattr(baseline, metric) + 0.20})
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is False
    assert "no target metric improved" in reason
    assert metric not in improved_metrics(baseline, candidate)


def test_a_worse_orphan_rate_alone_does_not_block():
    """Orphan rate is workload, not failure -- so it cannot veto on its own."""
    baseline = _result(precision=0.90, orphan_rate=0.05)
    candidate = _result(precision=0.92, orphan_rate=0.30)
    allowed, reason = gate_pack_release(baseline, candidate)
    assert allowed is True, reason


# ------------------------------------------------------------- criterion 4


def test_criterion_4_needs_both_halves(shipped_pack):
    ok, why = check_release_criteria(
        _result(false_match_rate=0.0, auto_match_rate=0.83), shipped_pack
    )
    assert ok is True, why


def test_criterion_4_fails_on_any_false_match(shipped_pack):
    ok, why = check_release_criteria(
        _result(false_match_rate=0.001, auto_match_rate=0.99), shipped_pack
    )
    assert ok is False
    assert "false-match" in why


def test_criterion_4_fails_when_coverage_is_below_the_pack_floor(shipped_pack):
    """The half that stops "false-match rate is zero" being passed by silence."""
    ok, why = check_release_criteria(
        _result(false_match_rate=0.0, auto_match_rate=0.0), shipped_pack
    )
    assert ok is False
    assert "auto-match" in why


def test_the_coverage_floor_comes_from_the_pack_not_from_this_module(shipped_pack):
    """Raising the floor must be able to fail a result that previously passed.

    A hardcoded floor would make `min_auto_match_rate` decorative pack data and
    "raising it is a decision backed by replay evidence" untrue.
    """
    result = _result(false_match_rate=0.0, auto_match_rate=0.60)
    assert check_release_criteria(result, shipped_pack)[0] is True
    strict = replace(shipped_pack, min_auto_match_rate=0.95)
    assert check_release_criteria(result, strict)[0] is False


# ------------------------------------------------ replay through the real pipeline


def test_the_shipped_pack_meets_criterion_4_on_the_synthetic_corpus(shipped_pack, corpus):
    """Success criterion 4, measured rather than asserted."""
    result = replay(corpus, shipped_pack)
    assert result.false_match_rate == 0.0
    assert result.auto_match_rate >= shipped_pack.min_auto_match_rate
    assert result.precision == 1.0
    assert result.unhandled == 0, "every scored message must produce a transition"
    ok, why = check_release_criteria(result, shipped_pack)
    assert ok is True, why


def test_replay_actually_attaches_things(shipped_pack, corpus):
    """The positive control. Without it every replay test passes on a no-op."""
    result = replay(corpus, shipped_pack)
    assert result.auto_matched > 0
    assert result.correct > 0
    assert result.orphans > 0, "a corpus with nothing declined cannot test the veto"


def test_replay_is_reproducible_run_to_run(shipped_pack, corpus):
    """Non-determinism here makes the gate meaningless.

    Loop ids are uuid4 and event timestamps are wall-clock, so this is a real
    question: it holds because no matching decision depends on either.
    """
    assert replay(corpus, shipped_pack) == replay(corpus, shipped_pack)


# ------------------------------------------------------ the degenerate matcher


def test_degenerate_matcher_scores_a_perfect_false_match_rate(shipped_pack, corpus):
    """The trap, demonstrated before it is closed.

    This is what the safety metric alone reports about a product that has
    stopped working.
    """
    degenerate = replay(corpus, _degenerate(shipped_pack))
    assert degenerate.false_match_rate == 0.0
    assert degenerate.auto_matched == 0
    assert degenerate.auto_match_rate == 0.0


def test_degenerate_matcher_fails_criterion_4(shipped_pack, corpus):
    degenerate = replay(corpus, _degenerate(shipped_pack))
    ok, why = check_release_criteria(degenerate, shipped_pack)
    assert ok is False
    assert "auto-match" in why


def test_degenerate_matcher_is_blocked_by_the_release_gate(shipped_pack, corpus):
    """Against a working baseline it dies on precision; the coverage floor is
    what kills it when the baseline is degenerate too."""
    baseline = replay(corpus, shipped_pack)
    degenerate = replay(corpus, _degenerate(shipped_pack))
    allowed, reason = gate_pack_release(baseline, degenerate, pack=shipped_pack)
    assert allowed is False, reason


def test_two_degenerate_packs_cannot_ship_each_other(shipped_pack, corpus):
    """Both attach nothing, so nothing regresses. The coverage floor is the only
    thing standing between that pair and a release.

    The assertion names the floor's own phrasing rather than "auto-match",
    because "no target metric improved" lists every metric by name and would
    satisfy a substring check while the floor sat disabled. Found by mutation.
    """
    degenerate = replay(corpus, _degenerate(shipped_pack))
    allowed, reason = gate_pack_release(degenerate, degenerate, pack=shipped_pack)
    assert allowed is False
    assert "below the pack's minimum" in reason


# ---------------------------------------------- the harness can see a false match


def test_a_pack_that_manufactures_a_false_match_is_caught(shipped_pack, corpus):
    """If this fails, every zero the harness reports is meaningless.

    A harness that can never produce a nonzero false-match rate makes the
    absolute veto untestable and the safety claim unfalsifiable.
    """
    baseline = replay(corpus, shipped_pack)
    reckless = replay(corpus, _wide_window(shipped_pack))
    assert reckless.false_match_rate > 0.0
    assert reckless.false_matches >= 1

    ok, why = check_release_criteria(reckless, shipped_pack)
    assert ok is False and "false-match" in why

    allowed, reason = gate_pack_release(baseline, reckless, pack=shipped_pack)
    assert allowed is False
    assert "false-match" in reason


def test_a_genuine_pack_improvement_is_allowed(shipped_pack, corpus):
    """The gate must let real work through, or it is a ban rather than a gate.

    Trusting the modality-equivalence tier matches the corpus's tier-4 case with
    no false match, which is exactly the evidence a changelog entry needs.
    """
    baseline = replay(corpus, shipped_pack)
    candidate = replay(corpus, _tier4_trusted(shipped_pack))
    assert candidate.false_match_rate == 0.0
    assert candidate.recall > baseline.recall
    allowed, reason = gate_pack_release(baseline, candidate, pack=shipped_pack)
    assert allowed is True, reason
    assert "recall" in reason


# -------------------------------------------------------- division-by-zero paths


def test_an_empty_corpus_is_refused_rather_than_scored(shipped_pack):
    """0.000 false-match over nothing is not a passing evaluation."""
    with pytest.raises(EvalError) as exc:
        replay([], shipped_pack)
    assert "empty corpus" in str(exc.value)


def test_a_corpus_with_nothing_matchable_scores_zero_coverage(shipped_pack, corpus):
    """No ZeroDivisionError, and the fail-closed value rather than 1.0.

    "Resolved everything it was asked to" would be a perfect coverage score for a
    corpus that asked nothing.
    """
    declines = [c for c in corpus if c.expected_loop_placer is None]
    result = replay(declines, shipped_pack)
    assert result.matchable == 0
    assert result.recall == 0.0
    assert result.auto_match_rate == 0.0
    assert check_release_criteria(result, shipped_pack)[0] is False


def test_a_corpus_with_no_orphans_divides_cleanly(shipped_pack, corpus):
    matchable = [c for c in corpus if c.expected_loop_placer is not None
                 and c.name != "tier4-modality-equivalence"]
    result = replay(matchable, shipped_pack)
    assert result.orphans == 0
    assert result.orphan_rate == 0.0
    assert result.precision == 1.0


def test_no_labels_means_a_zero_dismissal_rate(shipped_pack, corpus):
    assert replay(corpus, shipped_pack, labels=None).dismissal_rate == 0.0
    assert replay(corpus, shipped_pack, labels=[]).dismissal_rate == 0.0


def test_the_dismissal_rate_counts_no_loop_here_labels(shipped_pack, corpus):
    labels = [
        {"outcome": "no_loop_here"},
        {"outcome": "no_loop_here"},
        {"outcome": "missed_match"},
        {"outcome": "false_match"},
    ]
    assert replay(corpus, shipped_pack, labels=labels).dismissal_rate == 0.5


# ---------------------------------------------------- replay never touches prod


def test_replay_refuses_a_scratch_directory_that_already_holds_data(
    shipped_pack, corpus, tmp_path
):
    """The production-store guard.

    An operator who points this at the site's data directory must get a refusal,
    not a replay that quietly appends synthetic loops to a coordinator's worklist.
    """
    site = tmp_path / "data"
    site.mkdir()
    LoopStore(site / "referral_loops.db")
    with pytest.raises(EvalError) as exc:
        replay(corpus, shipped_pack, scratch_dir=site)
    assert "already contains data" in str(exc.value)


def test_replay_leaves_an_existing_production_store_untouched(
    shipped_pack, corpus, tmp_path
):
    """Measured, not argued: the store's own counts before and after."""
    site_db = tmp_path / "site" / "loops.db"
    site_db.parent.mkdir()
    store = LoopStore(site_db)
    handler = MessageHandler(
        store=store, registry=Registry(store, pack_version=shipped_pack.version),
        pack=shipped_pack,
    )
    for text in corpus[0].messages:
        handler.handle(text)

    before = (store.raw_count(), store.applied_count(), len(store.all_loops()),
              site_db.stat().st_size)
    replay(corpus, shipped_pack, scratch_dir=tmp_path / "scratch")
    after = (store.raw_count(), store.applied_count(), len(store.all_loops()),
             site_db.stat().st_size)
    assert before == after
    assert sorted(p.name for p in (tmp_path / "site").iterdir()) == ["loops.db"]


def test_replay_writes_nothing_outside_its_scratch_directory(shipped_pack, corpus, tmp_path):
    scratch = tmp_path / "scratch"
    replay(corpus, shipped_pack, scratch_dir=scratch)
    assert {p.name for p in tmp_path.iterdir()} == {"scratch"}
    assert all(p.suffix == ".db" for p in scratch.iterdir())


def test_the_default_scratch_directory_is_cleaned_up(shipped_pack, corpus, tmp_path, monkeypatch):
    """A pack evaluation must not leave PHI-shaped databases on disk.

    The corpus reconstructed from a site archive holds real messages, so the
    throwaway databases replay writes hold real MRNs -- outside the encrypted
    location the site attested for `--db`.

    `tempfile.tempdir` is set directly rather than through TMPDIR/TEMP/TMP.
    `gettempdir()` caches its answer in that attribute on first use, so by the
    time a test runs the environment variables have already been consulted and
    setting them changes nothing -- the test would pass over an empty directory
    without having exercised anything. Found by mutation: removing the cleanup
    entirely left this test green.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    replay(corpus, shipped_pack)
    assert list(tmp_path.iterdir()) == []


def test_replay_isolates_cases_from_one_another(shipped_pack):
    """One database per case, and the isolation is behavioural.

    Two cases sharing a store is not a tidiness question. The second case here
    carries the first one's placer order number and no order of its own -- with a
    shared store it tier-1 matches the loop the previous case left RESULTED, and
    the harness reports a false match that the pack never made.
    """
    matched = LabeledCase(
        name="iso-order-and-result",
        messages=(
            _eval_order(control="ISO-ORD", mrn="ISO0001", placer="ISO-P", filler="ISO-F"),
            _eval_result(control="ISO-RES", mrn="ISO0001", placer="ISO-P"),
        ),
        expected_loop_placer="ISO-P",
    )
    stray = LabeledCase(
        name="iso-same-placer-no-order",
        messages=(_eval_result(control="ISO-RES-2", mrn="ISO0001", placer="ISO-P"),),
        expected_loop_placer=None,
    )
    result = replay([matched, stray], shipped_pack)
    assert result.false_match_rate == 0.0
    assert result.orphans == 1
    assert result.correct == 1


def test_a_case_the_pipeline_never_applied_is_unhandled_not_an_orphan(shipped_pack):
    """The absence of a decision is not a decision to decline.

    The scored message here repeats an earlier result's content under a fresh
    control id, which the listener no-ops as a content-key duplicate. Folding
    that into the orphan count would let a corpus the pipeline had stopped
    processing read as one the matcher was carefully declining.
    """
    case = LabeledCase(
        name="content-key-duplicate-scored",
        messages=(
            _eval_order(control="DUP-ORD", mrn="DUP0001", placer="DUP-P", filler="DUP-F"),
            _eval_result(control="DUP-RES-A", mrn="DUP0001", placer="DUP-P"),
            _eval_result(control="DUP-RES-B", mrn="DUP0001", placer="DUP-P"),
        ),
        expected_loop_placer="DUP-P",
    )
    result = replay([case], shipped_pack)
    assert result.unhandled == 1
    assert result.orphans == 0
    assert result.auto_matched == 0
    assert result.auto_match_rate == 0.0


def test_auto_match_rate_is_coverage_and_recall_is_correctness(shipped_pack):
    """They are not the same number, and collapsing them disarms the coverage floor.

    The pack attaches this result to an order the site says it did not belong
    to: resolved without a human (coverage 1.0), and wrong (recall 0.0). If
    auto-match rate were defined as `correct / matchable` it would read 0.0 here
    and be an alias for recall -- and condition 3 could never be satisfied by a
    coverage improvement on its own.
    """
    case = LabeledCase(
        name="attached-to-the-wrong-order",
        messages=(
            _eval_order(control="WL-ORD-A", mrn="WL0001", placer="WL-PA", filler="WL-FA",
                        service="70450^CT HEAD WO CONTRAST^C4"),
            _eval_order(control="WL-ORD-B", mrn="WL0001", placer="WL-PB", filler="WL-FB",
                        service="71260^CT CHEST W CONTRAST^C4"),
            _eval_result(control="WL-RES", mrn="WL0001",
                         service="71260^CT CHEST W CONTRAST^C4"),
        ),
        expected_loop_placer="WL-PA",
    )
    result = replay([case], shipped_pack)
    assert result.matchable == 1
    assert result.auto_matched == 1
    assert result.auto_match_rate == 1.0
    assert result.recall == 0.0
    assert result.false_match_rate == 1.0


def test_replay_writes_no_audit_rows(shipped_pack, corpus):
    """Evaluating a pack must not append to the audit trail.

    An audit row says something happened to a patient's loop. A replay of a
    corpus is a measurement, and rows claiming otherwise would corrupt the one
    artifact an auditor reads.
    """
    audit_db = Path(referral_audit._module().AUDIT_DB)

    def _rows() -> int:
        if not audit_db.exists():
            return 0
        conn = sqlite3.connect(audit_db)
        try:
            return conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        finally:
            conn.close()

    before = _rows()
    replay(corpus, shipped_pack)
    assert _rows() == before


# ------------------------------------------------------------------ PHI posture


def test_a_labeled_case_never_reprs_its_messages():
    """pytest prints the repr of every object in a failing assertion.

    A site-reconstructed case holds raw HL7. If it reprs, one failing assertion
    in a CI log is a PHI disclosure.
    """
    case = LabeledCase(
        name="phi-probe",
        messages=("MSH|^~\\&|||||||ORU^R01|C1|P|2.5.1\rPID|1||MRN-SENTINEL^^^H^MR|"
                  "|SECRETNAME^PATIENT\r",),
        expected_loop_placer="P1",
    )
    text = repr(case)
    assert "SECRETNAME" not in text
    assert "MRN-SENTINEL" not in text
    assert "phi-probe" in text


def test_the_operator_report_carries_only_numbers(shipped_pack, corpus):
    report = format_report(replay(corpus, shipped_pack), title="candidate")
    assert "SYNTHETIC" not in report
    assert "SYN0001" not in report
    for token in report.split():
        assert "MSH" not in token and "PID" not in token


def test_a_gate_reason_carries_only_numbers(shipped_pack, corpus):
    baseline = replay(corpus, shipped_pack)
    candidate = replay(corpus, _tier4_trusted(shipped_pack))
    _, reason = gate_pack_release(baseline, candidate, pack=shipped_pack)
    assert "SYN" not in reason
    assert "MRN" not in reason


# ---------------------------------------------------------- the site corpus


def _sited(tmp_path, pack: RulePack) -> tuple[LoopStore, Registry, MessageHandler]:
    store = LoopStore(tmp_path / "site.db")
    registry = Registry(store, pack_version=pack.version)
    return store, registry, MessageHandler(store=store, registry=registry, pack=pack)


def test_an_attached_orphan_becomes_a_labeled_case(shipped_pack, corpus, tmp_path):
    """Spec section 7's flywheel, end to end.

    A coordinator attaches an orphan; the label says which loop; `loop_events`
    turns that into control ids and the archive turns those into messages. The
    reconstructed case then says what the matcher should have done -- and a pack
    that trusts tier 4 gets it right.
    """
    store, registry, handler = _sited(tmp_path, shipped_pack)
    tier4 = next(c for c in corpus if c.name == "tier4-modality-equivalence")
    for text in tier4.messages:
        handler.handle(text)

    orphan = next(loop for loop in store.all_loops() if loop.state is LoopState.ORPHAN)
    target = next(loop for loop in store.all_loops() if loop.state is LoopState.OPEN)
    registry.attach_orphan(orphan.loop_id, target.loop_id, actor="coord", role="coordinator")

    cases = corpus_from_site(store)
    assert len(cases) == 1
    assert cases[0].source == "site"
    assert cases[0].expected_loop_placer == target.placer_order_number

    assert replay(cases, shipped_pack).recall == 0.0
    assert replay(cases, _tier4_trusted(shipped_pack)).recall == 1.0


def test_an_undone_match_becomes_a_case_the_unchanged_pack_still_fails(
    shipped_pack, corpus, tmp_path
):
    """The most valuable label the system produces, replayed.

    A coordinator says the matcher was wrong. Replaying the same messages through
    the same pack must reproduce the false match, or the label taught nothing.
    """
    store, registry, handler = _sited(tmp_path, shipped_pack)
    tier1 = next(c for c in corpus if c.name == "tier1-placer-exact")
    for text in tier1.messages:
        handler.handle(text)

    matched = next(loop for loop in store.all_loops() if loop.state is LoopState.RESULTED)
    registry.undo_match(matched.loop_id, actor="coord", role="coordinator",
                        reason="wrong patient on the accession")

    cases = corpus_from_site(store)
    assert len(cases) == 1
    assert cases[0].expected_loop_placer is None

    result = replay(cases, shipped_pack)
    assert result.false_match_rate == 1.0
    assert check_release_criteria(result, shipped_pack)[0] is False


def test_a_coordinators_own_mistake_never_becomes_a_false_match_case(
    shipped_pack, corpus, tmp_path
):
    """A human attached a result to the wrong loop and undid it.

    Counting that as a matcher false positive would let a mis-click veto a pack
    release under an absolute rule.

    The attachment is recorded under a control id that **is** in the raw archive,
    and that detail is the test. The worklist stamps coordinator actions
    `WORKLIST`, which no archived message is keyed on, so the archive lookup
    happens to skip these cases for free -- and an earlier version of this test
    passed for that reason while both real guards were removed. MSH-10 is
    sender-controlled, so "no archived message is called WORKLIST" is an
    assumption about a remote system, which is exactly the class of assumption
    this codebase refuses elsewhere. Found by mutation.
    """
    store, registry, handler = _sited(tmp_path, shipped_pack)
    # No placer order number on the order, deliberately. With one, the same two
    # messages also reconstruct as a *missed-match* case, and the corpus
    # de-duplicator absorbs the false-match case before the guard is ever
    # consulted -- so the test passed with both guards deleted. Without a placer
    # the missed-match path declines to express an expectation, and this guard is
    # the only thing left standing. Found by mutation, twice.
    handler.handle(_eval_order(control="MA-ORD", mrn="MA0001", placer="", filler="MA-F",
                               service="70450^CT HEAD WO CONTRAST^C4"))
    handler.handle(_eval_result(control="MA-RES", mrn="MA0001",
                                service="70460^CT HEAD W CONTRAST^C4", modality="CAT"))

    orphan = next(loop for loop in store.all_loops() if loop.state is LoopState.ORPHAN)
    target = next(loop for loop in store.all_loops() if loop.state is LoopState.OPEN)
    registry.attach_orphan(orphan.loop_id, target.loop_id, actor="coord", role="coordinator",
                           control_id="MA-RES")
    registry.undo_match(target.loop_id, actor="coord", role="coordinator",
                        reason="attached the wrong one", control_id="MA-RES")

    outcomes = {row["outcome"] for row in store.labels()}
    assert "mistaken_attachment" in outcomes
    assert corpus_from_site(store) == []


def test_two_labels_disagreeing_about_the_same_messages_are_both_dropped(
    shipped_pack, corpus, tmp_path
):
    """Ground truth cannot be decided by which button a coordinator pressed first.

    Attaching an orphan to a loop and then undoing that attachment produces one
    label saying the result belongs there and one saying it does not, over the
    same two archived messages. Keeping the first and discarding the second
    resolves a contradiction by arrival order, in a corpus that feeds an absolute
    veto.
    """
    store, registry, handler = _sited(tmp_path, shipped_pack)
    tier1 = next(c for c in corpus if c.name == "tier1-placer-exact")
    for text in tier1.messages:
        handler.handle(text)

    matched = next(loop for loop in store.all_loops() if loop.state is LoopState.RESULTED)
    orphan_id = registry.undo_match(matched.loop_id, actor="coord", role="coordinator",
                                    reason="looked wrong", control_id="S-T1-RES")
    assert [c.expected_loop_placer for c in corpus_from_site(store)] == [None]

    # The coordinator changes their mind and puts it back on the same loop. The
    # two labels now describe the same two archived messages and disagree.
    registry.attach_orphan(orphan_id, matched.loop_id, actor="coord", role="coordinator",
                           control_id="S-T1-RES")
    assert corpus_from_site(store) == []


def test_a_store_with_no_labels_yields_no_site_cases(shipped_pack, tmp_path):
    store, _, _ = _sited(tmp_path, shipped_pack)
    assert corpus_from_site(store) == []


def test_a_label_whose_messages_have_left_the_archive_is_skipped_not_guessed(
    shipped_pack, corpus, tmp_path, monkeypatch
):
    """A guessed case would move the release gate on evidence nobody can check."""
    store, registry, handler = _sited(tmp_path, shipped_pack)
    tier1 = next(c for c in corpus if c.name == "tier1-placer-exact")
    for text in tier1.messages:
        handler.handle(text)
    matched = next(loop for loop in store.all_loops() if loop.state is LoopState.RESULTED)
    registry.undo_match(matched.loop_id, actor="coord", role="coordinator", reason="wrong")

    assert len(corpus_from_site(store)) == 1
    monkeypatch.setattr(type(store), "raw_payloads", lambda self: [])
    assert corpus_from_site(store) == []


def test_an_attached_orphan_whose_messages_are_gone_is_skipped_too(
    shipped_pack, corpus, tmp_path, monkeypatch
):
    """The same guard on the missed-match path.

    Both reconstruction paths need it and each has its own copy; a test of one
    says nothing about the other. Found by mutation -- removing the archive check
    from the missed-match path left the false-match test green.
    """
    store, registry, handler = _sited(tmp_path, shipped_pack)
    tier4 = next(c for c in corpus if c.name == "tier4-modality-equivalence")
    for text in tier4.messages:
        handler.handle(text)
    orphan = next(loop for loop in store.all_loops() if loop.state is LoopState.ORPHAN)
    target = next(loop for loop in store.all_loops() if loop.state is LoopState.OPEN)
    registry.attach_orphan(orphan.loop_id, target.loop_id, actor="coord", role="coordinator")

    assert len(corpus_from_site(store)) == 1
    monkeypatch.setattr(type(store), "raw_payloads", lambda self: [])
    assert corpus_from_site(store) == []


# ---------------------------------------------------------------- the CLI mode


def _sign_into(directory: Path, key: Ed25519PrivateKey, overrides: dict | None = None) -> None:
    body = _shipped_pack_body()
    body.update(overrides or {})
    packed = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pack.json").write_bytes(packed)
    (directory / "pack.sig").write_bytes(key.sign(packed))


@pytest.fixture()
def eval_env(monkeypatch, tmp_path):
    """Every boot gate satisfied, and one signing key both packs verify against."""
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    monkeypatch.setenv(PUBKEY_ENV, key.public_key().public_bytes_raw().hex())
    return key


def test_eval_mode_reports_metrics_and_exits_zero(eval_env, tmp_path, capsys):
    """An operator can actually evaluate a pack. Without this the harness is a
    library nobody at the site can run."""
    pack_dir = tmp_path / "candidate"
    _sign_into(pack_dir, eval_env)
    code = main(["eval", "--db", str(tmp_path / "loops.db"), "--pack-dir", str(pack_dir)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "false-match rate" in out
    assert "MEETS criterion 4" in out


def test_eval_mode_refuses_a_pack_that_attaches_nothing(eval_env, tmp_path, capsys):
    """The degenerate matcher, refused through the command an operator runs."""
    pack_dir = tmp_path / "degenerate"
    _sign_into(pack_dir, eval_env, {"confidence_floor": 1.01, "version": "9.9.9"})
    code = main(["eval", "--db", str(tmp_path / "loops.db"), "--pack-dir", str(pack_dir)])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "auto-match rate" in out


def test_eval_mode_blocks_a_candidate_that_improves_nothing(eval_env, tmp_path, capsys):
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    _sign_into(baseline_dir, eval_env)
    _sign_into(candidate_dir, eval_env, {"version": "1.1.1"})
    code = main(["eval", "--db", str(tmp_path / "loops.db"),
                 "--pack-dir", str(candidate_dir),
                 "--baseline-pack-dir", str(baseline_dir)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "no target metric improved" in out


def test_eval_mode_allows_a_measured_improvement(eval_env, tmp_path, capsys):
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    _sign_into(baseline_dir, eval_env)
    _sign_into(candidate_dir, eval_env,
               {"version": "1.2.0",
                "tier_confidence": {"1": 1.0, "2": 0.98, "3": 0.92, "4": 0.95}})
    code = main(["eval", "--db", str(tmp_path / "loops.db"),
                 "--pack-dir", str(candidate_dir),
                 "--baseline-pack-dir", str(baseline_dir)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "OK: false-match" in out
    assert "recall" in out


def test_eval_mode_does_not_write_loops_into_the_operators_database(
    eval_env, tmp_path, capsys
):
    """`--db` is the site's real database. Eval reads labels and the archive from
    it and must add nothing."""
    db = tmp_path / "loops.db"
    pack_dir = tmp_path / "candidate"
    _sign_into(pack_dir, eval_env)
    assert main(["eval", "--db", str(db), "--pack-dir", str(pack_dir)]) == 0
    capsys.readouterr()
    store = LoopStore(db)
    assert store.all_loops() == []
    assert store.raw_count() == 0


def test_eval_mode_is_listed_in_help(eval_env, tmp_path, capsys):
    """`--help` is the first thing an operator runs. A mode nobody can find is a
    mode that does not exist."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    out = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "eval" in out
    assert "--baseline-pack-dir" in out


def test_a_missing_baseline_pack_is_a_refusal_not_a_traceback(eval_env, tmp_path, capsys):
    pack_dir = tmp_path / "candidate"
    _sign_into(pack_dir, eval_env)
    code = main(["eval", "--db", str(tmp_path / "loops.db"), "--pack-dir", str(pack_dir),
                 "--baseline-pack-dir", str(tmp_path / "nope")])
    captured = capsys.readouterr()
    assert code == 2
    assert "refusing to start" in captured.err


def test_load_pack_still_verifies_signatures_in_eval_mode(eval_env, tmp_path, capsys):
    """The baseline pack goes through the same gate as the candidate.

    A comparison against an unsigned baseline would let anyone justify a release
    by writing the numbers they wanted into a JSON file.
    """
    baseline_dir = tmp_path / "baseline"
    _sign_into(baseline_dir, eval_env)
    body = json.loads((baseline_dir / "pack.json").read_bytes())
    body["confidence_floor"] = 0.1
    (baseline_dir / "pack.json").write_bytes(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    )
    pack_dir = tmp_path / "candidate"
    _sign_into(pack_dir, eval_env)
    code = main(["eval", "--db", str(tmp_path / "loops.db"), "--pack-dir", str(pack_dir),
                 "--baseline-pack-dir", str(baseline_dir)])
    assert code == 2
    assert "signature invalid" in capsys.readouterr().err


def test_the_shipped_pack_verifies_and_evaluates(tmp_path):
    """The pack that actually ships, loaded through its real signature path."""
    pubkey = bytes.fromhex(
        "adb7af9938740d48d237fc2e191c7a52d41000654d7e8335d08edd51a97ed105"
    )
    try:
        pack = load_pack(SHIPPED_PACK_DIR, pubkey)
    except ReferralLoopError as exc:  # pragma: no cover - key rotation
        pytest.skip(f"shipped pack does not verify against the pinned key: {exc}")
    result = replay(synthetic_corpus(), pack)
    ok, why = check_release_criteria(result, pack)
    assert ok is True, why
