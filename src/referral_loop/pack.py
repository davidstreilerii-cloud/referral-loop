"""Signed rule pack. Verified before load; an altered pack must never run.

This is IP protection and a safety control at once -- a tampered pack could
lower the confidence floor and cause false closes, the one failure the product
exists to prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import PackVerificationError


@dataclass(frozen=True)
class RulePack:
    version: str
    confidence_floor: float
    date_windows_hours: dict[str, int]
    staleness_hours: dict[str, int]
    modality_equivalence: dict[str, list[str]]
    tie_breakers: tuple[str, ...]
    tier_confidence: dict[int, float]

    def date_window_hours(self, modality: str) -> int:
        return self.date_windows_hours.get(modality, self.date_windows_hours["_default"])

    def staleness_threshold_hours(self, modality: str) -> int:
        return self.staleness_hours.get(modality, self.staleness_hours["_default"])

    def equivalent_modalities(self, modality: str) -> frozenset[str]:
        return frozenset(self.modality_equivalence.get(modality, [modality]))


def load_pack(pack_dir: Path, public_key_raw: bytes) -> RulePack:
    """Load and verify the pack. Raises PackVerificationError on any doubt."""
    pack_path = Path(pack_dir) / "pack.json"
    sig_path = Path(pack_dir) / "pack.sig"

    if not pack_path.is_file():
        raise PackVerificationError(f"No pack at {pack_path}")
    if not sig_path.is_file():
        raise PackVerificationError(f"No signature at {sig_path}; refusing to load an unsigned pack")

    pack_bytes = pack_path.read_bytes()
    signature = sig_path.read_bytes()

    try:
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(signature, pack_bytes)
    except InvalidSignature as exc:
        raise PackVerificationError("Pack signature invalid; refusing to boot") from exc

    try:
        raw = json.loads(pack_bytes)
    except json.JSONDecodeError as exc:
        raise PackVerificationError("Pack is signed but not valid JSON") from exc

    for required in ("_default",):
        if required not in raw.get("date_windows_hours", {}):
            raise PackVerificationError("date_windows_hours missing '_default'")
        if required not in raw.get("staleness_hours", {}):
            raise PackVerificationError("staleness_hours missing '_default'")

    return RulePack(
        version=raw["version"],
        confidence_floor=float(raw["confidence_floor"]),
        date_windows_hours=raw["date_windows_hours"],
        staleness_hours=raw["staleness_hours"],
        modality_equivalence=raw["modality_equivalence"],
        tie_breakers=tuple(raw["tie_breakers"]),
        tier_confidence={int(k): float(v) for k, v in raw["tier_confidence"].items()},
    )
