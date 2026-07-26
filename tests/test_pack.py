"""Spec test 9: pack tamper. Mutate one byte; assert refusal to load."""
import json
import os
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from healthcare_rag.referral_loop.errors import PackVerificationError
from healthcare_rag.referral_loop.pack import RulePack, load_pack

PACK = {
    "version": "1.0.0",
    "confidence_floor": 0.90,
    "date_windows_hours": {"CT": 24, "MG": 720, "_default": 168},
    "staleness_hours": {"CT": 4, "MG": 720, "_default": 336},
    "modality_equivalence": {"CT": ["CT", "CAT"], "MG": ["MG", "MAM"]},
    "tie_breakers": ["nearest_order_date", "same_ordering_provider", "most_specific_modality"],
    "tier_confidence": {"1": 1.0, "2": 0.98, "3": 0.92, "4": 0.70},
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


SHIPPED_PACK = Path(__file__).parent.parent.parent / "healthcare_rag" / "referral_loop" / "rules"


@pytest.mark.skipif(
    not os.environ.get("REFERRAL_PACK_PUBKEY"), reason="REFERRAL_PACK_PUBKEY not set"
)
def test_shipped_pack_verifies_against_the_shipped_public_key():
    """A pack that cannot verify is a pack that cannot boot."""
    pack = load_pack(SHIPPED_PACK, bytes.fromhex(os.environ["REFERRAL_PACK_PUBKEY"]))
    assert pack.version == "1.0.0"
    assert pack.confidence_floor == 0.9


def test_shipped_pack_and_signature_both_exist():
    assert (SHIPPED_PACK / "pack.json").is_file()
    assert (SHIPPED_PACK / "pack.sig").is_file(), (
        "load_pack refuses an unsigned pack; shipping pack.json alone cannot boot"
    )


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
