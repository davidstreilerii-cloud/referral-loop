"""Spec test 9: pack tamper. Mutate one byte; assert refusal to load."""
import json
import os
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from referral_loop.errors import PackVerificationError
from referral_loop.pack import RulePack, load_pack

PACK = {
    "version": "1.0.0",
    "confidence_floor": 0.90,
    "date_windows_hours": {"CT": 24, "MG": 720, "_default": 168},
    "staleness_hours": {"CT": 4, "MG": 720, "_default": 336},
    "modality_equivalence": {"CT": ["CT", "CAT"], "MG": ["MG", "MAM"]},
    "tie_breakers": ["nearest_order_date", "same_ordering_provider", "most_specific_modality"],
    "tier_confidence": {"1": 1.0, "2": 0.98, "3": 0.92, "4": 0.70},
    "field_map": {
        "placer_order_number": ["OBR-2", "ORC-2"],
        "filler_order_number": ["OBR-3", "ORC-3", "OBR-18", "OBR-19"],
        "service_code": ["OBR-4.1"],
        "modality": ["OBR-24", "OBR-4.2"],
        "ordering_provider": ["OBR-16.1"],
        "mrn": ["PID-3.1"],
    },
    "min_auto_match_rate": 0.5,
}


def _write_pack(tmp_path, pack_dict, corrupt=False):
    key = Ed25519PrivateKey.generate()
    pack_bytes = json.dumps(pack_dict, sort_keys=True, separators=(",", ":")).encode()
    sig = key.sign(pack_bytes)
    if corrupt:
        mutable = bytearray(pack_bytes)
        mutable[10] = mutable[10] ^ 0x01
        pack_bytes = bytes(mutable)
    (tmp_path / "pack.json").write_bytes(pack_bytes)
    (tmp_path / "pack.sig").write_bytes(sig)
    return key.public_key().public_bytes_raw()


def _loaded_test_pack() -> RulePack:
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        pubkey = _write_pack(tmp_path, PACK)
        return load_pack(tmp_path, pubkey)


def test_valid_pack_loads(tmp_path):
    pubkey = _write_pack(tmp_path, PACK)
    pack = load_pack(tmp_path, pubkey)
    assert pack.version == "1.0.0"
    assert pack.confidence_floor == 0.90


def test_single_byte_mutation_refuses_to_load(tmp_path):
    pubkey = _write_pack(tmp_path, PACK, corrupt=True)
    with pytest.raises(PackVerificationError):
        load_pack(tmp_path, pubkey)


def test_missing_signature_refuses_to_load(tmp_path):
    pubkey = _write_pack(tmp_path, PACK)
    (tmp_path / "pack.sig").unlink()
    with pytest.raises(PackVerificationError):
        load_pack(tmp_path, pubkey)


SHIPPED_PACK = Path(__file__).parent.parent / "src" / "referral_loop" / "rules"


@pytest.mark.skipif(
    not os.environ.get("REFERRAL_PACK_PUBKEY"), reason="REFERRAL_PACK_PUBKEY not set"
)
def test_shipped_pack_verifies_against_the_shipped_public_key():
    """A pack that cannot verify is a pack that cannot boot."""
    pack = load_pack(SHIPPED_PACK, bytes.fromhex(os.environ["REFERRAL_PACK_PUBKEY"]))
    assert pack.version == "1.1.0"
    assert pack.confidence_floor == 0.9


@pytest.mark.skipif(
    not os.environ.get("REFERRAL_PACK_PUBKEY"), reason="REFERRAL_PACK_PUBKEY not set"
)
def test_shipped_pack_is_1_1_0_and_exposes_field_map():
    pack = load_pack(SHIPPED_PACK, bytes.fromhex(os.environ["REFERRAL_PACK_PUBKEY"]))
    assert pack.version == "1.1.0"
    assert pack.field_candidates("placer_order_number") == ("OBR-2", "ORC-2")
    assert pack.field_candidates("filler_order_number") == (
        "OBR-3", "ORC-3", "OBR-18", "OBR-19",
    )
    assert pack.min_auto_match_rate == 0.5


def test_shipped_pack_and_signature_both_exist():
    assert (SHIPPED_PACK / "pack.json").is_file()
    assert (SHIPPED_PACK / "pack.sig").is_file(), (
        "load_pack refuses an unsigned pack; shipping pack.json alone cannot boot"
    )


def test_the_signing_script_points_at_the_pack_it_signs():
    """`--sign` is exercised by hand, months apart, and only when a pack changes.

    Its `PACK_DIR` survived the extraction pointing at the pre-extraction layout
    and `--sign` exited "No pack at ..." for anyone who tried, which nobody did
    until a pack needed re-signing. Asserting the two paths agree is the cheapest
    thing that fails on the wrong side of a move rather than at the next release.
    """
    from scripts.sign_pack import PACK_DIR

    assert PACK_DIR.resolve() == SHIPPED_PACK.resolve()
    assert (PACK_DIR / "pack.json").is_file()


def test_missing_pack_file_refuses(tmp_path):
    with pytest.raises(PackVerificationError, match="No pack"):
        load_pack(tmp_path, b"\x00" * 32)


def test_signed_but_non_json_body_refuses(tmp_path):
    key = Ed25519PrivateKey.generate()
    body = b"this is signed but is not json"
    (tmp_path / "pack.json").write_bytes(body)
    (tmp_path / "pack.sig").write_bytes(key.sign(body))
    with pytest.raises(PackVerificationError, match="not valid JSON"):
        load_pack(tmp_path, key.public_key().public_bytes_raw())


def test_signed_json_that_is_not_an_object_refuses(tmp_path):
    """Must raise PackVerificationError, not AttributeError -- a caller catching
    it to refuse boot would otherwise crash instead of refusing."""
    key = Ed25519PrivateKey.generate()
    body = b"[1, 2, 3]"
    (tmp_path / "pack.json").write_bytes(body)
    (tmp_path / "pack.sig").write_bytes(key.sign(body))
    with pytest.raises(PackVerificationError, match="must be a JSON object"):
        load_pack(tmp_path, key.public_key().public_bytes_raw())


@pytest.mark.parametrize("field", ["date_windows_hours", "staleness_hours"])
def test_missing_default_window_refuses(tmp_path, field):
    broken = {**PACK, field: {"CT": 24}}
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match=f"{field} missing"):
        load_pack(tmp_path, pubkey)


def test_equivalent_modalities_is_symmetric():
    pack = _loaded_test_pack()
    assert pack.equivalent_modalities("CT") == pack.equivalent_modalities("CAT")
    assert "CT" in pack.equivalent_modalities("CAT")
    assert "CAT" in pack.equivalent_modalities("CT")


def test_unknown_modality_is_its_own_class():
    pack = _loaded_test_pack()
    assert pack.equivalent_modalities("NM") == frozenset({"NM"})


def test_date_window_and_staleness_fall_back_to_default():
    pack = _loaded_test_pack()
    assert pack.date_window_hours("CT") == 24
    assert pack.date_window_hours("UNKNOWN") == 168
    assert pack.staleness_threshold_hours("CT") == 4
    assert pack.staleness_threshold_hours("UNKNOWN") == 336


def test_missing_version_refuses(tmp_path):
    broken = {k: v for k, v in PACK.items() if k != "version"}
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match="missing required field"):
        load_pack(tmp_path, pubkey)


def test_non_integer_tier_confidence_key_refuses(tmp_path):
    broken = {**PACK, "tier_confidence": {"not-an-int": 1.0}}
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match="signed but malformed"):
        load_pack(tmp_path, pubkey)


def test_missing_field_map_refuses(tmp_path):
    broken = {k: v for k, v in PACK.items() if k != "field_map"}
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match="missing required field"):
        load_pack(tmp_path, pubkey)


def test_missing_min_auto_match_rate_refuses(tmp_path):
    broken = {k: v for k, v in PACK.items() if k != "min_auto_match_rate"}
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match="missing required field"):
        load_pack(tmp_path, pubkey)


@pytest.mark.parametrize("bad_entry", ["OBR", "OBR-0", "OBR-x", "TOOLONG-2", "OBR-2.0"])
def test_malformed_field_map_entry_refused(tmp_path, bad_entry):
    broken = {
        **PACK,
        "field_map": {**PACK["field_map"], "mrn": [bad_entry]},
    }
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match="field_map"):
        load_pack(tmp_path, pubkey)


def test_empty_field_map_candidate_list_refused(tmp_path):
    broken = {
        **PACK,
        "field_map": {**PACK["field_map"], "mrn": []},
    }
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match="field_map"):
        load_pack(tmp_path, pubkey)


def test_unknown_field_map_concept_raises():
    pack = _loaded_test_pack()
    with pytest.raises(PackVerificationError, match="Unknown field-map concept"):
        pack.field_candidates("not_a_real_concept")


def test_field_candidates_priority_order_preserved():
    pack = _loaded_test_pack()
    assert pack.field_candidates("filler_order_number") == (
        "OBR-3", "ORC-3", "OBR-18", "OBR-19",
    )
    assert pack.field_candidates("mrn") == ("PID-3.1",)


@pytest.mark.parametrize("out_of_allowlist_entry", ["NK1-2", "GT1-3", "NTE-3"])
def test_field_map_segment_outside_allowlist_refused(tmp_path, out_of_allowlist_entry):
    """A pack must not be able to widen the parser's read surface.

    NK1 (next of kin) and GT1 (guarantor) are deliberately excluded from
    ALLOWED_SEGMENTS so a PHI-sentinel proof has something real to assert. A
    field map naming them would turn the signed pack into a route around
    parse_hl7's PHI boundary -- this is the mistake case (someone onboarding a
    site maps a concept to whatever field a sample message happened to show),
    not an attacker-without-the-key case.
    """
    broken = {
        **PACK,
        "field_map": {**PACK["field_map"], "mrn": [out_of_allowlist_entry]},
    }
    pubkey = _write_pack(tmp_path, broken)
    with pytest.raises(PackVerificationError, match="ALLOWED_SEGMENTS"):
        load_pack(tmp_path, pubkey)


def test_legal_remap_within_allowlist_still_loads(tmp_path):
    remapped = {
        **PACK,
        "field_map": {**PACK["field_map"], "filler_order_number": ["OBR-18"]},
    }
    pubkey = _write_pack(tmp_path, remapped)
    pack = load_pack(tmp_path, pubkey)
    assert pack.field_candidates("filler_order_number") == ("OBR-18",)
