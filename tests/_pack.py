"""The RulePack the tests run against.

This lived in test_matcher.py and was imported across test modules, which made
collecting one module a prerequisite for collecting others. It is fixture data, not a
test, so it belongs in a module pytest does not collect -- hence the leading underscore.

Deliberately not the shipped pack: the shipped pack is signature-verified and its
thresholds are a product decision. test_pack.py and test_spec_proofs.py exercise the
shipped one; everything else exercises this.
"""

from referral_loop.pack import RulePack

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
        # Added here rather than making `content_key`'s read of it tolerant. A
        # pack with no `appointment_id` is a pack whose content key cannot tell a
        # rebooking from a retransmit, and the tolerant read would give it back
        # the collision the concept exists to close -- on a system that booted
        # clean and looked upgraded. `field_candidates` raising on the concept is
        # the whole reason it raises rather than returning an empty tuple. That an
        # absent *field* stays benign is a separate promise, kept by
        # `concept_value` returning "" for a message with no SCH, and asserted by
        # test_listener's `..._a_message_with_no_sch_...` tests.
        "appointment_id": ["SCH-1", "SCH-2"],
    },
    min_auto_match_rate=0.5,
)
