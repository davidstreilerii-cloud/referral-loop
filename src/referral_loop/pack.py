"""Signed rule pack. Verified before load; an altered pack must never run.

This is IP protection and a safety control at once -- a tampered pack could
lower the confidence floor, or point a tier at the wrong field, and cause false
matches: the one failure the product exists to prevent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .audit import SYSTEM_ACTOR, SYSTEM_ROLE, AuditAction, audited
from .errors import PackConceptMissingError, PackVerificationError
from .parse_hl7 import ALLOWED_SEGMENTS

# The concepts this build reads through `matcher.concept_value` with no
# `in pack.field_map` guard. A pack omitting one would otherwise load clean and
# raise `PackVerificationError` out of `field_candidates` on the first message
# that needed it -- a booted, ACKing site failing partway through ingest, worded
# as if the pack were corrupt when it is merely older than the build. Refusing
# here makes it the same class of failure as a concept pointed at the wrong
# segment, and for the same reason given below: a pack defect must fail before
# the first message rather than on it.
#
# Concepts read *behind* such a guard are deliberately absent: each has a
# fallback placement, so an old pack still reads the right field and has no
# business being refused (`listener._prior_mrn`, `matcher._observed_at`).
#
# Exhaustive against the source, and said here only because
# `test_pack.py::test_required_concepts_matches_the_unguarded_reads_in_src`
# walks `src/` for those calls and fails on any difference in either direction.
# A hand-maintained set drifts the moment a read is added without a thought for
# this line, and the drift is invisible until a site boots a pack from last year.
REQUIRED_CONCEPTS = frozenset({
    "appointment_id",
    "filler_order_number",
    "modality",
    "mrn",
    "ordering_provider",
    "placer_order_number",
    "service_code",
})

# SEG-N or SEG-N.C: a 3-character HL7 segment id (a letter followed by two
# alphanumeric characters -- HL7 segment ids are not always all-letters, e.g.
# PV1, RF1, NK1, GT1), a positive field number, and an optional positive
# component number (components are ^-delimited within a field). Field
# *placement* varies by site/RIS; field *concept* names (this regex governs
# what a placement string may look like) do not.
_FIELD_REF_RE = re.compile(r"^[A-Z][A-Z0-9]{2}-[1-9][0-9]*(\.[1-9][0-9]*)?$")


@dataclass(frozen=True)
class RulePack:
    version: str
    confidence_floor: float
    date_windows_hours: dict[str, int]
    staleness_hours: dict[str, int]
    modality_equivalence: dict[str, list[str]]
    tie_breakers: tuple[str, ...]
    tier_confidence: dict[int, float]
    field_map: dict[str, list[str]]
    min_auto_match_rate: float
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
        (an orphan, not a false match) but silently costing recall on exactly the
        interface quirk this table exists to absorb.
        """
        return self._equivalence_index.get(modality, frozenset({modality}))

    def field_candidates(self, concept: str) -> tuple[str, ...]:
        """Priority-ordered field references for a concept; the first populated one wins.

        Raises PackVerificationError for an unknown concept rather than returning
        an empty tuple -- a typo'd concept silently matching nothing is exactly the
        failure mode that turns a tier into a false negative.

        `load_pack` refuses a pack lacking any of REQUIRED_CONCEPTS, so a caller
        reading one of those no longer gets here on a loaded pack. What still
        does: a typo, a RulePack built by hand rather than loaded, and an
        optional concept read without its `in pack.field_map` guard.

        Behaviour note for callers walking this list (the matcher, Task 8): a
        candidate naming a segment-field absent from the message is not an
        error. Fall through to the next candidate for that concept; if all are
        absent, the tier simply does not fire. Never raise, never a partial
        match on a missing field.
        """
        try:
            return tuple(self.field_map[concept])
        except KeyError as exc:
            raise PackVerificationError(f"Unknown field-map concept: {concept!r}") from exc


def load_pack(pack_dir: Path, public_key_raw: bytes) -> RulePack:
    """Load and verify the pack. Raises PackVerificationError on any doubt.

    Audited, because which pack is in force is an answer an auditor needs: the
    pack carries the field map and the tiers, so it decides which result is
    attributed to which order. A refusal is audited too -- "the site booted on
    pack 1.4.0" and "the site refused to boot on a pack that failed
    verification" are both facts about what was running, and the second is the
    more interesting one.

    Only the version reaches the audit row, and only if it matches
    `audit._PACK_VERSION_RE`. Not the pack path: a filesystem path is a site
    detail with no auditable value here, and it is the sort of string that turns
    out to contain a hospital's name.
    """
    with audited(AuditAction.PACK_LOADED, actor=SYSTEM_ACTOR, role=SYSTEM_ROLE) as _audit:
        pack = _load_pack(pack_dir, public_key_raw)
        _audit.pack_version = pack.version
        return pack


def _load_pack(pack_dir: Path, public_key_raw: bytes) -> RulePack:
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
                "modality_equivalence", "tie_breakers", "tier_confidence",
                "field_map", "min_auto_match_rate")
    missing = [k for k in required if k not in raw]
    if missing:
        raise PackVerificationError(f"Pack missing required field(s): {', '.join(missing)}")

    # field_map governs where clinical concepts are read from on the wire.
    # Pointing a concept at the wrong field (or accepting a typo'd reference)
    # would silently degrade matches rather than fail loudly, so this is
    # validated before construction, not discovered later at match time.
    field_map = raw["field_map"]
    if not isinstance(field_map, dict):
        raise PackVerificationError("field_map must be a JSON object")
    for concept, candidates in field_map.items():
        if not isinstance(candidates, list) or not candidates:
            raise PackVerificationError(
                f"field_map['{concept}'] must be a non-empty list of field references"
            )
        for entry in candidates:
            if not isinstance(entry, str) or not _FIELD_REF_RE.match(entry):
                raise PackVerificationError(
                    f"field_map['{concept}'] has a malformed field reference: {entry!r}"
                )
            # A pack must not be able to widen the parser's read surface. NK1
            # (next of kin) and GT1 (guarantor) are deliberately excluded from
            # ALLOWED_SEGMENTS; a field map naming them would turn the signed
            # pack into a route around parse_hl7's PHI boundary. This is the
            # mistake case, not an attacker-without-the-key case: someone
            # onboarding a site maps a concept to whatever field a sample
            # message happened to show.
            segment = entry.split("-", 1)[0]
            if segment not in ALLOWED_SEGMENTS:
                raise PackVerificationError(
                    f"field_map['{concept}'] names segment {segment!r}, which is "
                    f"outside ALLOWED_SEGMENTS; refusing to widen the parser's read surface"
                )

    # Checked after the shape rules above, so a pack that is both malformed and
    # old is reported as malformed first: a bad field reference is a defect in
    # the pack in front of you, while a missing concept is usually a defect in
    # the choice of pack.
    missing_concepts = sorted(REQUIRED_CONCEPTS - field_map.keys())
    if missing_concepts:
        raise PackConceptMissingError(missing_concepts)

    try:
        return RulePack(
            version=str(raw["version"]),
            confidence_floor=float(raw["confidence_floor"]),
            date_windows_hours=raw["date_windows_hours"],
            staleness_hours=raw["staleness_hours"],
            modality_equivalence=raw["modality_equivalence"],
            tie_breakers=tuple(raw["tie_breakers"]),
            tier_confidence={int(k): float(v) for k, v in raw["tier_confidence"].items()},
            field_map=field_map,
            min_auto_match_rate=float(raw["min_auto_match_rate"]),
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise PackVerificationError(f"Pack is signed but malformed: {exc}") from exc
