"""Tiered matcher: the tier table, the confidence floor, and the coverage floor.

Two failures are in scope here and they pull in opposite directions.

A **false match** attributes a result to the wrong order and reports that loop
handled while the real one stays open -- strictly worse than an orphan, which at
least gets a human. Most of this file pushes on that.

A **degenerate matcher** attaches nothing at all. It scores a perfect 0.000
false-match rate, the safety gate goes green, and the only symptom is a
coordinator queue quietly filling with the work the tool was bought to remove
(spec section 10.4). `test_auto_match_rate_clears_the_pack_floor` and
`test_a_confident_match_actually_attaches` exist so that implementation fails
this suite instead of passing it perfectly.
"""
import itertools
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from healthcare_rag.referral_loop.errors import PackVerificationError
from healthcare_rag.referral_loop.events import Loop, LoopState
from healthcare_rag.referral_loop.matcher import (
    ResultKey,
    concept_value,
    field_value,
    hl7_datetime,
    match_result,
    result_key_from_message,
)
from healthcare_rag.referral_loop.pack import RulePack
from healthcare_rag.referral_loop.parse_hl7 import parse_hl7_text

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

# Deliberately narrower than the shipped pack: filler_order_number lists only
# OBR-3 and ORC-3, so spec test 17's relocation to OBR-18 genuinely requires a
# pack change. The shipped pack already lists OBR-18 as a fallback, which would
# have made that test pass without the pack doing any work at all.
PACK = RulePack(
    version="test",
    confidence_floor=0.90,
    date_windows_hours={"CT": 24, "MG": 720, "_default": 168},
    staleness_hours={"CT": 4, "_default": 336},
    modality_equivalence={"CT": ["CT", "CAT"], "MG": ["MG", "MAM"]},
    tie_breakers=("nearest_order_date", "same_ordering_provider", "most_specific_modality"),
    tier_confidence={1: 1.0, 2: 0.98, 3: 0.92, 4: 0.70},
    field_map={
        "placer_order_number": ["OBR-2", "ORC-2"],
        "filler_order_number": ["OBR-3", "ORC-3"],
        "service_code": ["OBR-4.1"],
        "modality": ["OBR-24", "OBR-4.2"],
        "ordering_provider": ["OBR-16.1"],
        "mrn": ["PID-3.1"],
    },
    min_auto_match_rate=0.5,
)


def _pack(**overrides) -> RulePack:
    return replace(PACK, **overrides)


def _loop(loop_id, **kw) -> Loop:
    base = dict(
        mrn="MRN1",
        state=LoopState.OPEN,
        modality="CT",
        ordered_at=NOW - timedelta(hours=2),
    )
    base.update(kw)
    return Loop(loop_id=loop_id, **base)


def _key(**kw) -> ResultKey:
    base = dict(mrn="MRN1", placer="", filler="", service_code="", modality="CT", observed_at=NOW)
    base.update(kw)
    return ResultKey(**base)


# --------------------------------------------------------------- HL7 fixtures


def _segment(seg_id: str, fields: dict[int, str], width: int = 26) -> str:
    slots = [""] * width
    slots[0] = seg_id
    for index, value in fields.items():
        slots[index] = value
    return "|".join(slots).rstrip("|")


def _oru(
    *,
    mrn="MRN1",
    placer="",
    accession="",
    accession_field=3,
    service="71260^CT CHEST W CONTRAST^C4",
    observed="20260725120000",
    provider="1234^WELBY^MARCUS",
    modality="CT",
    control="MSG001",
    orc=None,
) -> str:
    """A synthetic ORU^R01, generated from the HL7 v2 field definitions.

    `accession_field` is the whole point of spec test 17: the same accession is
    written into OBR-3 at one site and OBR-18 at another, and matching must
    survive that with a pack revision rather than a code change.
    """
    obr_fields = {1: "1", 2: placer, 4: service, 7: observed, 16: provider, 24: modality}
    obr_fields[accession_field] = accession
    segments = [
        f"MSH|^~\\&|RIS|SITE|TRACKER|SITE|20260725120500||ORU^R01|{control}|P|2.5",
        _segment("PID", {1: "1", 3: f"{mrn}^^^SITE^MR", 5: "DOE^JANE", 7: "19700101", 8: "F"}),
    ]
    if orc:
        segments.append(_segment("ORC", orc))
    segments.append(_segment("OBR", obr_fields))
    segments.append(_segment("OBX", {1: "1", 2: "TX", 3: "IMP^Impression", 5: "No acute finding", 11: "F"}))
    return "\r".join(segments) + "\r"


def _parsed(**kw):
    return parse_hl7_text(_oru(**kw))


# ============================================================ tier table


def test_tier1_placer_order_number_exact():
    loops = [_loop("L1", placer_order_number="PLACER1"), _loop("L2", placer_order_number="PLACER2")]
    result = match_result(_key(placer="PLACER2"), loops, PACK)
    assert result.loop_id == "L2"
    assert result.tier == 1


def test_tier2_filler_order_number_exact():
    loops = [_loop("L1", filler_order_number="FILLER9")]
    result = match_result(_key(filler="FILLER9"), loops, PACK)
    assert result.loop_id == "L1"
    assert result.tier == 2


def test_tier3_mrn_service_code_and_date_window():
    loops = [_loop("L1", service_code="71260")]
    result = match_result(_key(service_code="71260"), loops, PACK)
    assert result.loop_id == "L1"
    assert result.tier == 3


def test_outside_the_date_window_does_not_match_at_tier3():
    loops = [_loop("L1", service_code="71260", ordered_at=NOW - timedelta(hours=200))]
    assert match_result(_key(service_code="71260"), loops, PACK).tier == 5


def test_date_window_is_per_modality_not_global():
    """A 25-day-old screening mammogram is in window; a 25-day-old CT is not."""
    old = NOW - timedelta(days=25)
    mg = [_loop("L1", modality="MG", service_code="77067", ordered_at=old)]
    assert match_result(_key(service_code="77067", modality="MG"), mg, PACK).tier == 3

    ct = [_loop("L1", modality="CT", service_code="71260", ordered_at=old)]
    assert match_result(_key(service_code="71260", modality="CT"), ct, PACK).tier == 5


def test_tier4_falls_below_confidence_floor_and_refuses_to_auto_match():
    """The product's equivalent of INSUFFICIENT_REGULATORY_EVIDENCE: decline, do not guess."""
    loops = [_loop("L1", service_code="OTHER", modality="CAT")]
    result = match_result(_key(), loops, PACK)
    assert result.tier == 4
    assert result.confidence < PACK.confidence_floor
    assert result.loop_id is None, "below the floor the matcher must route to review, not attach"
    assert "floor" in result.reason


def test_no_candidate_is_tier5_orphan():
    result = match_result(_key(mrn="MRN_UNKNOWN"), [], PACK)
    assert result.tier == 5
    assert result.loop_id is None


def test_tiebreak_prefers_nearest_order_date():
    loops = [
        _loop("L_far", service_code="71260", ordered_at=NOW - timedelta(hours=20)),
        _loop("L_near", service_code="71260", ordered_at=NOW - timedelta(hours=1)),
    ]
    assert match_result(_key(service_code="71260"), loops, PACK).loop_id == "L_near"


# ============================================ tier ordering and precedence


def test_tier1_wins_when_a_lower_tier_would_also_fire():
    """An order number outranks proximity. A worse tier must not win by tie-break."""
    loops = [
        _loop("L_placer", placer_order_number="P1", service_code="71260",
              ordered_at=NOW - timedelta(hours=20)),
        _loop("L_nearer", service_code="71260", ordered_at=NOW - timedelta(minutes=5)),
    ]
    result = match_result(_key(placer="P1", service_code="71260"), loops, PACK)
    assert result.loop_id == "L_placer"
    assert result.tier == 1


def test_tier1_outranks_tier2_when_each_points_at_a_different_loop():
    """A result carrying both identifiers that disagree: the placer decides.

    Written because the tier order is otherwise unfalsifiable -- every other test
    exercises one identifier at a time, so swapping tiers 1 and 2 in the
    implementation changed nothing any assertion could see.
    """
    loops = [
        _loop("L_by_placer", placer_order_number="P1"),
        _loop("L_by_filler", filler_order_number="F1"),
    ]
    result = match_result(_key(placer="P1", filler="F1"), loops, PACK)
    assert result.loop_id == "L_by_placer"
    assert result.tier == 1


def test_tier2_wins_over_tier3_and_tier4():
    loops = [
        _loop("L_filler", filler_order_number="F1", service_code="OTHER"),
        _loop("L_service", service_code="71260", ordered_at=NOW - timedelta(minutes=1)),
    ]
    result = match_result(_key(filler="F1", service_code="71260"), loops, PACK)
    assert result.loop_id == "L_filler"
    assert result.tier == 2


def test_tier3_wins_over_tier4():
    loops = [
        _loop("L_service", service_code="71260", modality="CT"),
        _loop("L_modality", service_code="OTHER", modality="CAT",
              ordered_at=NOW - timedelta(minutes=1)),
    ]
    result = match_result(_key(service_code="71260"), loops, PACK)
    assert result.loop_id == "L_service"
    assert result.tier == 3


def test_confidence_floor_applies_at_every_tier_not_only_the_weak_ones():
    """A site that distrusts an identifier lowers its tier confidence; the gate must hear it."""
    distrustful = _pack(tier_confidence={1: 0.50, 2: 0.98, 3: 0.92, 4: 0.70})
    loops = [_loop("L1", placer_order_number="P1")]
    result = match_result(_key(placer="P1"), loops, distrustful)
    assert result.tier == 1
    assert result.loop_id is None, "a floor with exceptions is not a floor"
    assert "floor" in result.reason


def test_a_tier_missing_from_the_pack_declines_rather_than_raising():
    partial = _pack(tier_confidence={2: 0.98, 3: 0.92, 4: 0.70})
    result = match_result(_key(placer="P1"), [_loop("L1", placer_order_number="P1")], partial)
    assert result.loop_id is None
    assert result.tier == 1


# ================================================== ambiguity and determinism


def test_duplicate_placer_order_number_declines_instead_of_picking_one():
    """Two open loops sharing an order number is a data fault, not a 50/50 bet."""
    loops = [_loop("L1", placer_order_number="P1"), _loop("L2", placer_order_number="P1")]
    result = match_result(_key(placer="P1"), loops, PACK)
    assert result.loop_id is None
    assert result.tier == 1
    assert "ambiguous" in result.reason


def test_duplicate_placer_does_not_fall_through_to_a_weaker_tier():
    """The tier that fired stands. A heuristic must not overrule an ambiguous identifier."""
    loops = [
        _loop("L1", placer_order_number="P1", service_code="71260",
              ordered_at=NOW - timedelta(hours=10)),
        _loop("L2", placer_order_number="P1", service_code="OTHER"),
    ]
    result = match_result(_key(placer="P1", service_code="71260"), loops, PACK)
    assert result.loop_id is None
    assert result.tier == 1


def test_the_same_loop_listed_twice_is_not_an_ambiguity():
    """Callers build candidates from more than one store query (open + resulted).

    Treating one loop that appears twice as two competing orders would turn a
    caller's bookkeeping into a false orphan.
    """
    loop = _loop("L1", placer_order_number="P1")
    result = match_result(_key(placer="P1"), [loop, loop], PACK)
    assert result.loop_id == "L1"
    assert result.tier == 1


def test_assigning_authority_survives_so_two_placing_systems_do_not_collide():
    """A bare `SEG-N` keeps the `^`-namespace, which is what stops a cross-patient match.

    Placer order numbers are unique per placing application, not per site. Two
    feeds that both number from 1000000 collide on the digits alone, and tier 1
    keys on the order number without consulting the MRN -- so the namespace is
    the only thing standing between them and a result attached to another
    patient's loop. A pack that narrows this candidate to `OBR-2.1` strips it.
    """
    message = _parsed(placer="1000001^EPIC")
    assert field_value(message, "OBR-2") == "1000001^EPIC"
    assert field_value(message, "OBR-2.1") == "1000001"

    other_system = [_loop("L_other", mrn="MRN_OTHER", placer_order_number="1000001^ATHENA")]
    key = result_key_from_message(message, PACK)
    assert match_result(key, other_system, PACK).loop_id is None


def test_duplicate_filler_order_number_declines():
    loops = [_loop("L1", filler_order_number="F1"), _loop("L2", filler_order_number="F1")]
    result = match_result(_key(filler="F1"), loops, PACK)
    assert result.loop_id is None
    assert result.tier == 2


def test_indistinguishable_tier3_candidates_decline():
    """Equal distance, equal provider, equal modality: nothing left to break the tie."""
    loops = [
        _loop("L1", service_code="71260", ordered_at=NOW - timedelta(hours=3)),
        _loop("L2", service_code="71260", ordered_at=NOW - timedelta(hours=3)),
    ]
    result = match_result(_key(service_code="71260"), loops, PACK)
    assert result.loop_id is None
    assert result.tier == 3
    assert "ambiguous" in result.reason


def test_tiebreak_is_stable_under_every_input_permutation():
    """No sort order, dict order, or store replay order may decide a match."""
    loops = [
        _loop("L_near", service_code="71260", ordered_at=NOW - timedelta(hours=1)),
        _loop("L_mid", service_code="71260", ordered_at=NOW - timedelta(hours=5)),
        _loop("L_far", service_code="71260", ordered_at=NOW - timedelta(hours=9)),
    ]
    key = _key(service_code="71260")
    outcomes = {
        match_result(key, list(order), PACK).loop_id
        for order in itertools.permutations(loops)
    }
    assert outcomes == {"L_near"}


def test_repeated_runs_return_the_same_loop():
    loops = [
        _loop("L1", service_code="71260", ordered_at=NOW - timedelta(hours=1)),
        _loop("L2", service_code="71260", ordered_at=NOW - timedelta(hours=2)),
    ]
    key = _key(service_code="71260")
    assert len({match_result(key, loops, PACK).loop_id for _ in range(200)}) == 1


# =========================================================== tie-breaker data


def test_tiebreak_same_ordering_provider():
    loops = [
        _loop("L_other", service_code="71260", ordering_provider="9999"),
        _loop("L_same", service_code="71260", ordering_provider="1234"),
    ]
    key = _key(service_code="71260", ordering_provider="1234")
    assert match_result(key, loops, PACK).loop_id == "L_same"


def test_tiebreak_most_specific_modality():
    loops = [
        _loop("L_alias", service_code="71260", modality="CAT"),
        _loop("L_exact", service_code="71260", modality="CT"),
    ]
    assert match_result(_key(service_code="71260"), loops, PACK).loop_id == "L_exact"


def test_tiebreak_order_is_pack_data_not_code():
    """Reordering the pack's tie-breakers changes the winner. If it does not, they are hardcoded."""
    loops = [
        _loop("L_near_other_provider", service_code="71260",
              ordered_at=NOW - timedelta(hours=1), ordering_provider="9999"),
        _loop("L_far_same_provider", service_code="71260",
              ordered_at=NOW - timedelta(hours=5), ordering_provider="1234"),
    ]
    key = _key(service_code="71260", ordering_provider="1234")

    date_first = _pack(tie_breakers=("nearest_order_date", "same_ordering_provider"))
    provider_first = _pack(tie_breakers=("same_ordering_provider", "nearest_order_date"))

    assert match_result(key, loops, date_first).loop_id == "L_near_other_provider"
    assert match_result(key, loops, provider_first).loop_id == "L_far_same_provider"


def test_a_tiebreaker_the_matcher_does_not_implement_declines_rather_than_guesses():
    """A typo in the pack must cost a lookup, never produce an arbitrary attachment."""
    typo = _pack(tie_breakers=("nearest_order_dates",))
    loops = [
        _loop("L1", service_code="71260", ordered_at=NOW - timedelta(hours=1)),
        _loop("L2", service_code="71260", ordered_at=NOW - timedelta(hours=5)),
    ]
    result = match_result(_key(service_code="71260"), loops, typo)
    assert result.loop_id is None
    assert "ambiguous" in result.reason


def test_ordering_provider_tiebreak_is_skipped_when_the_result_names_nobody():
    """An absent provider must not filter to loops whose provider is also absent."""
    loops = [
        _loop("L_blank", service_code="71260", ordering_provider="",
              ordered_at=NOW - timedelta(hours=5)),
        _loop("L_named", service_code="71260", ordering_provider="1234",
              ordered_at=NOW - timedelta(hours=1)),
    ]
    assert match_result(_key(service_code="71260"), loops, PACK).loop_id == "L_named"


# ================================================= empty fields never match


def test_empty_placer_does_not_match_an_empty_placer():
    """The classic false match: "" == "" turning a whole feed into one equivalence class."""
    loops = [_loop("L1", placer_order_number="", service_code="", modality="MG")]
    result = match_result(_key(placer="", modality="XX"), loops, PACK)
    assert result.loop_id is None
    assert result.tier == 5


def test_empty_filler_does_not_match_an_empty_filler():
    loops = [_loop("L1", filler_order_number="", modality="MG")]
    result = match_result(_key(filler="", modality="XX"), loops, PACK)
    assert result.tier == 5


def test_empty_service_code_does_not_fire_tier3():
    loops = [_loop("L1", service_code="", modality="MG")]
    result = match_result(_key(service_code="", modality="XX"), loops, PACK)
    assert result.tier == 5


def test_empty_modality_does_not_fire_tier4():
    loops = [_loop("L1", service_code="", modality="")]
    result = match_result(_key(service_code="", modality=""), loops, PACK)
    assert result.tier == 5


def test_empty_mrn_never_reaches_the_heuristic_tiers():
    loops = [_loop("L1", mrn="", service_code="71260")]
    result = match_result(_key(mrn="", service_code="71260"), loops, PACK)
    assert result.tier == 5


# ================================================== candidate loop states


@pytest.mark.parametrize("state", [LoopState.OPEN, LoopState.SCHEDULED, LoopState.RESULTED])
def test_states_that_can_still_receive_a_result_are_candidates(state):
    loops = [_loop("L1", placer_order_number="P1", state=state)]
    assert match_result(_key(placer="P1"), loops, PACK).loop_id == "L1"


@pytest.mark.parametrize(
    "state",
    [LoopState.CANCELLED, LoopState.CLOSED, LoopState.ORPHAN, LoopState.DISMISSED],
)
def test_terminal_and_reserved_states_are_never_candidates(state):
    loops = [_loop("L1", placer_order_number="P1", state=state)]
    assert match_result(_key(placer="P1"), loops, PACK).tier == 5


def test_closed_loops_are_never_match_candidates():
    loops = [_loop("L1", placer_order_number="PLACER1", state=LoopState.CLOSED)]
    assert match_result(_key(placer="PLACER1"), loops, PACK).tier == 5


def test_acknowledged_loop_still_matches_on_an_exact_identifier():
    """Safety rule 2: a corrected result must reach the loop it corrects.

    A correction carries the same order numbers as the read it supersedes, so
    tiers 1-2 are where it lands. Excluding ACKNOWLEDGED entirely would drop the
    correction into the orphan queue while the loop went on reporting handled --
    the concealment this product exists to prevent, one layer up.
    """
    loops = [_loop("L1", placer_order_number="P1", state=LoopState.ACKNOWLEDGED)]
    result = match_result(_key(placer="P1"), loops, PACK)
    assert result.loop_id == "L1"
    assert result.tier == 1


def test_acknowledged_loop_is_not_reopened_by_a_heuristic_tier():
    """Unsettling human-confirmed work is the highest-consequence transition here.

    Tiers 3-4 are the weakest evidence class. They may open new work; they may
    not overturn a coordinator's confirmation.
    """
    loops = [_loop("L1", service_code="71260", state=LoopState.ACKNOWLEDGED)]
    assert match_result(_key(service_code="71260"), loops, PACK).tier == 5


# ============================================================ time handling


def test_naive_ordered_at_does_not_crash_the_window():
    """A naive stored timestamp must not raise inside matching.

    A TypeError here is not a failed match, it is a result that reaches no queue
    at all: the listener archives the raw, logs, and moves on.
    """
    naive = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    loops = [_loop("L1", service_code="71260", ordered_at=naive)]
    assert match_result(_key(service_code="71260"), loops, PACK).loop_id == "L1"


def test_offset_bearing_observation_time_is_compared_correctly():
    """20:00-05:00 is 01:00 UTC the next day -- 13h after the order, inside the CT window."""
    observed = hl7_datetime("20260725200000-0500")
    assert observed == datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)
    loops = [_loop("L1", service_code="71260", ordered_at=NOW)]
    assert match_result(_key(service_code="71260", observed_at=observed), loops, PACK).tier == 3


def test_future_dated_observation_inside_the_window_still_matches():
    """The failure matrix says accept and flag, not discard."""
    loops = [_loop("L1", service_code="71260", ordered_at=NOW)]
    key = _key(service_code="71260", observed_at=NOW + timedelta(hours=6))
    assert match_result(key, loops, PACK).loop_id == "L1"


def test_wildly_future_dated_observation_falls_outside_the_window():
    loops = [_loop("L1", service_code="71260", ordered_at=NOW)]
    key = _key(service_code="71260", observed_at=NOW + timedelta(days=30))
    assert match_result(key, loops, PACK).tier == 5


def test_missing_observation_time_cannot_fire_a_windowed_tier():
    """An unreadable timestamp must cost a match, never manufacture one."""
    loops = [_loop("L1", service_code="71260")]
    assert match_result(_key(service_code="71260", observed_at=None), loops, PACK).tier == 5


def test_missing_observation_time_does_not_block_an_exact_identifier():
    loops = [_loop("L1", placer_order_number="P1")]
    assert match_result(_key(placer="P1", observed_at=None), loops, PACK).tier == 1


@pytest.mark.parametrize("raw", ["", "   ", "notadate", "2026", "20261325120000", "0"])
def test_unparseable_hl7_datetimes_return_none(raw):
    assert hl7_datetime(raw) is None


def test_date_only_timestamp_parses_to_midnight_utc():
    assert hl7_datetime("20260725") == datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)


# ==================================================== the field map drives it


def test_field_map_extraction_populates_every_concept():
    key = result_key_from_message(_parsed(placer="PL1", accession="ACC1"), PACK)
    assert key == ResultKey(
        mrn="MRN1",
        placer="PL1",
        filler="ACC1",
        service_code="71260",
        modality="CT",
        observed_at=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
        ordering_provider="1234",
    )


def test_spec_test_17_relocated_accession_still_matches_at_tier2():
    """Spec test 17. The accession moves OBR-3 -> OBR-18; only the pack changes.

    Both halves are asserted, because only the pair proves anything. The relocated
    message under the old pack must MISS -- otherwise the new pack is not what
    made the match, and the field map is decorative.
    """
    relocated_pack = _pack(
        field_map={**PACK.field_map, "filler_order_number": ["OBR-18"]}
    )
    loops = [_loop("L1", filler_order_number="ACC1")]

    at_obr3 = _parsed(accession="ACC1", accession_field=3)
    at_obr18 = _parsed(accession="ACC1", accession_field=18)

    assert match_result(result_key_from_message(at_obr3, PACK), loops, PACK).tier == 2
    assert match_result(result_key_from_message(at_obr18, relocated_pack), loops, relocated_pack).tier == 2

    missed = match_result(result_key_from_message(at_obr18, PACK), loops, PACK)
    assert result_key_from_message(at_obr18, PACK).filler == ""
    assert missed.tier != 2, "the old pack must not find the relocated accession"
    assert missed.loop_id is None, "and must not attach by some other route"


def test_candidate_priority_first_populated_wins():
    """OBR-2 is empty, so the placer comes from the pack's second candidate, ORC-2."""
    message = _parsed(placer="", orc={1: "NW", 2: "FROM_ORC"})
    assert concept_value(message, PACK, "placer_order_number") == "FROM_ORC"


def test_first_candidate_wins_when_both_are_populated():
    message = _parsed(placer="FROM_OBR", orc={1: "NW", 2: "FROM_ORC"})
    assert concept_value(message, PACK, "placer_order_number") == "FROM_OBR"


def test_absent_segment_is_not_an_error_it_is_a_fall_through():
    """No ORC in the message at all: the tier simply does not fire on that candidate."""
    message = _parsed(placer="")
    assert "ORC" not in message.segments
    assert concept_value(message, PACK, "placer_order_number") == ""
    assert match_result(result_key_from_message(message, PACK), [], PACK).tier == 5


def test_component_notation_extracts_the_named_component():
    message = _parsed(service="71260^CT CHEST^C4")
    assert field_value(message, "OBR-4.1") == "71260"
    assert field_value(message, "OBR-4.2") == "CT CHEST"
    assert field_value(message, "OBR-4.3") == "C4"


def test_component_beyond_the_fields_arity_is_empty_not_an_error():
    message = _parsed(service="71260")
    assert field_value(message, "OBR-4.1") == "71260"
    assert field_value(message, "OBR-4.9") == ""


def test_empty_component_falls_through_to_the_next_candidate():
    """modality is OBR-24 then OBR-4.2; an empty OBR-24 must not shadow the fallback."""
    message = _parsed(modality="", service="71260^CT^C4")
    assert concept_value(message, PACK, "modality") == "CT"


def test_repeating_field_uses_the_first_repetition():
    """`A~B` is two values. Returning the raw field would match neither.

    The bare-field assertion is the load-bearing one. Asserting only on
    `OBR-16.1` proves nothing: `^`-splitting a raw `1234^WELBY^MARCUS~5678^...`
    still yields "1234" as component 1, so a matcher that ignores repetitions
    passes that assertion while mangling every whole-field read.
    """
    message = _parsed(provider="1234^WELBY^MARCUS~5678^KILDARE^JAMES")
    assert field_value(message, "OBR-16") == "1234^WELBY^MARCUS"
    assert field_value(message, "OBR-16.1") == "1234"

    repeated_placer = _parsed(placer="PL1~PL2")
    assert field_value(repeated_placer, "OBR-2") == "PL1"
    assert result_key_from_message(repeated_placer, PACK).placer == "PL1"


def test_a_repeated_order_number_still_matches_its_loop():
    """The whole point: `PL1~PL2` must resolve to the loop for PL1, not to nothing."""
    loops = [_loop("L1", placer_order_number="PL1")]
    key = result_key_from_message(_parsed(placer="PL1~PL2"), PACK)
    assert match_result(key, loops, PACK).loop_id == "L1"


def test_bare_field_reference_returns_the_whole_field():
    message = _parsed(service="71260^CT CHEST^C4")
    assert field_value(message, "OBR-4") == "71260^CT CHEST^C4"


def test_malformed_field_reference_is_refused():
    bad = _pack(field_map={**PACK.field_map, "service_code": ["OBR4"]})
    with pytest.raises(PackVerificationError):
        concept_value(_parsed(), bad, "service_code")


def test_unknown_concept_is_refused_by_the_pack():
    with pytest.raises(PackVerificationError):
        concept_value(_parsed(), PACK, "radiologist_mood")


def test_observation_datetime_placement_can_come_from_the_pack():
    """OBR-7 is the fallback, not a hardcode: a pack that maps the concept wins."""
    relocated = _pack(
        field_map={**PACK.field_map, "observation_datetime": ["OBR-22"]}
    )
    message = parse_hl7_text(
        "MSH|^~\\&|RIS|SITE|TRACKER|SITE|20260725120500||ORU^R01|M1|P|2.5\r"
        + _segment("PID", {1: "1", 3: "MRN1^^^SITE^MR"}) + "\r"
        + _segment("OBR", {1: "1", 4: "71260^CT^C4", 7: "20260101010000",
                           22: "20260725120000", 24: "CT"}) + "\r"
    )
    assert result_key_from_message(message, relocated).observed_at == NOW
    assert result_key_from_message(message, PACK).observed_at == datetime(
        2026, 1, 1, 1, 0, tzinfo=timezone.utc
    )


def test_resolved_mrn_overrides_what_the_message_carried():
    """Alias resolution happens once, at ingest. The matcher never resolves again."""
    message = _parsed(mrn="RETIRED1")
    key = result_key_from_message(message, PACK, mrn="SURVIVOR1")
    assert key.mrn == "SURVIVOR1"


def test_pid_component_notation_reads_the_id_not_the_assigning_authority():
    key = result_key_from_message(_parsed(mrn="MRN9"), PACK)
    assert key.mrn == "MRN9"


# ======================================= the matcher must not be degenerate


def test_a_confident_match_actually_attaches():
    """A matcher that declines everything scores a perfect false-match rate.

    This is the assertion that makes the degenerate implementation fail rather
    than pass with the best numbers in the suite.
    """
    loops = [_loop("L1", placer_order_number="P1")]
    result = match_result(result_key_from_message(_parsed(placer="P1"), PACK), loops, PACK)
    assert result.loop_id == "L1"
    assert result.tier == 1
    assert result.confidence >= PACK.confidence_floor


def _corpus() -> tuple[list[Loop], list[tuple[ResultKey, str, bool]]]:
    """Twelve labeled results, each with a correct open loop in the store.

    Ten are resolvable at or above the floor; two are tier-4-only and must
    decline. Every result is matched against the whole store, not against its
    own loop, so a matcher that ignores the other eleven loops is not rewarded.
    """
    loops: list[Loop] = []
    cases: list[tuple[ResultKey, str, bool]] = []

    def add(loop: Loop, message_kw: dict, expect_auto: bool):
        loops.append(loop)
        key = result_key_from_message(parse_hl7_text(_oru(**message_kw)), PACK)
        cases.append((key, loop.loop_id, expect_auto))

    # Tier 1: the result carries the placer order number.
    for i in range(4):
        add(
            _loop(f"T1_{i}", mrn=f"M1{i}", placer_order_number=f"PL{i}",
                  service_code="71260", ordered_at=NOW - timedelta(hours=3)),
            dict(mrn=f"M1{i}", placer=f"PL{i}", observed="20260725120000"),
            True,
        )

    # Tier 2: a RIS that returns only the accession on the result.
    for i in range(3):
        add(
            _loop(f"T2_{i}", mrn=f"M2{i}", filler_order_number=f"ACC{i}",
                  service_code="71260", ordered_at=NOW - timedelta(hours=3)),
            dict(mrn=f"M2{i}", accession=f"ACC{i}", observed="20260725120000"),
            True,
        )

    # Tier 3: a referral-created loop with no order numbers yet.
    for i in range(3):
        add(
            _loop(f"T3_{i}", mrn=f"M3{i}", service_code="71260",
                  ordered_at=NOW - timedelta(hours=6)),
            dict(mrn=f"M3{i}", observed="20260725120000"),
            True,
        )

    # Tier 4 only: modality equivalence, no service-code agreement. Below the floor.
    for i in range(2):
        add(
            _loop(f"T4_{i}", mrn=f"M4{i}", service_code="99999", modality="CAT",
                  ordered_at=NOW - timedelta(hours=6)),
            dict(mrn=f"M4{i}", service="71260^CT^C4", modality="CT",
                 observed="20260725120000"),
            False,
        )

    return loops, cases


def test_auto_match_rate_clears_the_pack_floor_with_no_false_matches():
    """Success criterion 4, both halves. Either alone is passed by a broken matcher."""
    loops, cases = _corpus()

    attached = 0
    false_matches = 0
    for key, expected_loop_id, _ in cases:
        result = match_result(key, loops, PACK)
        if result.loop_id is None:
            continue
        attached += 1
        if result.loop_id != expected_loop_id:
            false_matches += 1

    auto_match_rate = attached / len(cases)
    assert false_matches == 0, "a result attached to the wrong loop"
    assert auto_match_rate >= PACK.min_auto_match_rate, (
        f"auto-match rate {auto_match_rate:.3f} is below the pack floor "
        f"{PACK.min_auto_match_rate:.3f}: the matcher has stopped resolving"
    )
    assert auto_match_rate == pytest.approx(10 / 12)


def test_every_corpus_case_lands_where_it_was_labeled():
    loops, cases = _corpus()
    for key, expected_loop_id, expect_auto in cases:
        result = match_result(key, loops, PACK)
        if expect_auto:
            assert result.loop_id == expected_loop_id
        else:
            assert result.loop_id is None
            assert result.tier == 4


# ================================================================ artifacts


def test_match_reasons_carry_no_identifiers():
    """Reasons land in audit detail, which spec section 3 requires to be non-identifying."""
    loops = [
        _loop("L1", placer_order_number="SECRET_PLACER", service_code="SECRET_CODE"),
        _loop("L2", placer_order_number="SECRET_PLACER"),
    ]
    reasons = [
        match_result(_key(placer="SECRET_PLACER"), loops, PACK).reason,
        match_result(_key(mrn="SECRET_MRN"), [], PACK).reason,
        match_result(_key(service_code="OTHER", modality="CT"),
                     [_loop("L3", service_code="X", modality="CAT")], PACK).reason,
    ]
    for reason in reasons:
        for sentinel in ("SECRET_PLACER", "SECRET_MRN", "SECRET_CODE", "L1", "L2", "L3"):
            assert sentinel not in reason
