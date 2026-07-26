"""Signed rule pack. Verified before load; an altered pack must never run.

This is IP protection and a safety control at once -- a tampered pack could
lower the confidence floor and cause false closes, the one failure the product
exists to prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

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
    _equivalence_index: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        index: dict[str, frozenset[str]] = {}
        for canonical, aliases in self.modality_equivalence.items():
            klass = frozenset({canonical, *aliases})
            for member in klass:
                index[member] = klass
        # frozen dataclass: assign through object.__setattr__
        object.__setattr__(self, "_equivalence_index", MappingProxyType(index))

    def date_window_hours(self, modality: str) -> int:
        return self.date_windows_hours.get(modality, self.date_windows_hours["_default"])

    def staleness_threshold_hours(self, modality: str) -> int:
        return self.staleness_hours.get(modality, self.staleness_hours["_default"])

    def equivalent_modalities(self, modality: str) -> frozenset[str]:
        """Symmetric: querying from an alias returns the same class as the canonical.

        The pack keys equivalences canonically ("CT": ["CT", "CAT"]), but sending
        systems emit either spelling. A naive .get(modality) returns {"CAT"} for
        the alias, so a CAT result would never match a CT order -- failing safe
        (an orphan, not a false close) but silently costing recall on exactly the
        interface quirk this table exists to absorb.
        """
        return self._equivalence_index.get(modality, frozenset({modality}))


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

    # A signed body that parses as JSON but is not an object would otherwise
    # escape as AttributeError, and a caller catching PackVerificationError to
    # refuse boot would crash instead of refusing.
    if not isinstance(raw, dict):
        raise PackVerificationError(f"Pack must be a JSON object, got {type(raw).__name__}")

    if "_default" not in raw.get("date_windows_hours", {}):
        raise PackVerificationError("date_windows_hours missing '_default'")
    if "_default" not in raw.get("staleness_hours", {}):
        raise PackVerificationError("staleness_hours missing '_default'")

    # Signing is a manual step and nothing validates shape before signing, so a
    # validly-signed-but-malformed pack (missing field, non-numeric tier key,
    # ...) must still refuse cleanly rather than crash the boot path with an
    # exception the caller isn't catching -- same reasoning as the checks above.
    required = ("version", "confidence_floor", "date_windows_hours", "staleness_hours",
                "modality_equivalence", "tie_breakers", "tier_confidence")
    missing = [k for k in required if k not in raw]
    if missing:
        raise PackVerificationError(f"Pack missing required field(s): {', '.join(missing)}")

    try:
        return RulePack(
            version=str(raw["version"]),
            confidence_floor=float(raw["confidence_floor"]),
            date_windows_hours=raw["date_windows_hours"],
            staleness_hours=raw["staleness_hours"],
            modality_equivalence=raw["modality_equivalence"],
            tie_breakers=tuple(raw["tie_breakers"]),
            tier_confidence={int(k): float(v) for k, v in raw["tier_confidence"].items()},
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise PackVerificationError(f"Pack is signed but malformed: {exc}") from exc
