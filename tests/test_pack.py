"""Spec test 9: pack tamper. Mutate one byte; assert refusal to load."""
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from healthcare_rag.referral_loop.errors import PackVerificationError
from healthcare_rag.referral_loop.pack import load_pack

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
