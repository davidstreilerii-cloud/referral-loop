"""Spec test 9: pack tamper. Mutate one byte; assert refusal to load."""
import ast
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from referral_loop.errors import PackConceptMissingError, PackVerificationError
from referral_loop.listener import _prior_mrn
from referral_loop.matcher import result_key_from_message
from referral_loop.pack import REQUIRED_CONCEPTS, RulePack, load_pack
from referral_loop.parse_hl7 import parse_hl7_text

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
        # Carried here because `load_pack` refuses a pack whose field_map omits
        # any of pack.REQUIRED_CONCEPTS, so a body without it no longer reaches
        # the checks most of this module is about. The pack that deliberately
        # omits it is built in `_without()` below, where that is the point.
        "appointment_id": ["SCH-1", "SCH-2"],
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
    assert pack.version == "1.2.0"
    assert pack.confidence_floor == 0.9


@pytest.mark.skipif(
    not os.environ.get("REFERRAL_PACK_PUBKEY"), reason="REFERRAL_PACK_PUBKEY not set"
)
def test_shipped_pack_is_1_2_0_and_exposes_field_map():
    pack = load_pack(SHIPPED_PACK, bytes.fromhex(os.environ["REFERRAL_PACK_PUBKEY"]))
    assert pack.version == "1.2.0"
    assert pack.field_candidates("placer_order_number") == ("OBR-2", "ORC-2")
    assert pack.field_candidates("filler_order_number") == (
        "OBR-3", "ORC-3", "OBR-18", "OBR-19",
    )
    assert pack.min_auto_match_rate == 0.5


def _shipped_body() -> dict:
    return json.loads((SHIPPED_PACK / "pack.json").read_bytes())


def test_the_shipped_body_loads_and_names_the_appointment():
    """`appointment_id` reaches a constructed pack, in preference order.

    Re-signed here with a throwaway key rather than checked against the shipped
    signature: that the shipped bytes verify against the shipped key is a
    property test_boot_gates and test_spec_proofs already hold, and pinning the
    public key in one more module would widen what a rotation breaks without
    buying coverage. What this adds is that the real body survives the checks
    `_load_pack` runs before construction -- SCH has to be in ALLOWED_SEGMENTS
    for it to, so removing SCH from the parser's read surface fails here rather
    than at a site's boot.
    """
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        pubkey = _write_pack(tmp_path, _shipped_body())
        pack = load_pack(tmp_path, pubkey)
    assert pack.field_candidates("appointment_id") == ("SCH-1", "SCH-2")


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


# ============================================ the concepts a build cannot run without


def _without(*concepts: str) -> dict:
    """PACK with those concepts dropped from its field_map, and nothing else changed."""
    return {
        **PACK,
        "field_map": {k: v for k, v in PACK["field_map"].items() if k not in concepts},
    }


def test_a_pack_missing_a_required_concept_refuses_to_load(tmp_path):
    """The pre-1.2.0 shape, which is what an eval baseline actually is.

    Refused at load for the reason a field reference outside ALLOWED_SEGMENTS is:
    the alternative is a process that boots, ACKs, and raises out of
    `field_candidates` on the first message carrying an SCH -- an ingest failure
    worded as a pack defect, discovered by traffic rather than by boot.
    """
    pubkey = _write_pack(tmp_path, _without("appointment_id"))
    with pytest.raises(PackConceptMissingError) as caught:
        load_pack(tmp_path, pubkey)
    assert "appointment_id" in str(caught.value)
    assert caught.value.missing == ("appointment_id",)


def test_the_refusal_names_every_missing_concept_not_just_the_first(tmp_path):
    """One boot, one list. Naming them one per attempt makes an operator
    re-sign and re-run for each, learning the next only after fixing the last."""
    pubkey = _write_pack(tmp_path, _without("appointment_id", "modality"))
    with pytest.raises(PackConceptMissingError) as caught:
        load_pack(tmp_path, pubkey)
    assert caught.value.missing == ("appointment_id", "modality")
    assert "appointment_id" in str(caught.value) and "modality" in str(caught.value)


def test_the_refusal_is_still_a_pack_verification_error(tmp_path):
    """Every `except PackVerificationError` in the boot path predates this type
    and none of them were touched, so the subclassing is the whole reason boot
    still refuses rather than crashing on an uncaught exception."""
    pubkey = _write_pack(tmp_path, _without("mrn"))
    with pytest.raises(PackVerificationError):
        load_pack(tmp_path, pubkey)


def test_a_pack_missing_an_optional_concept_loads_and_its_fallback_still_reads(tmp_path):
    """`observation_datetime` and `prior_patient_id` are read behind an
    `in pack.field_map` guard and must stay out of REQUIRED_CONCEPTS.

    Asserted through the fallbacks rather than by loading alone: promoting these
    into the required set to make every read uniform is the tempting
    simplification, and it would refuse every pack signed before someone thought
    to add a concept that has a perfectly good default placement. A load-only
    assertion would keep passing if the fallbacks were then deleted as
    unreachable.
    """
    body = _without("observation_datetime", "prior_patient_id")
    pubkey = _write_pack(tmp_path, body)
    pack = load_pack(tmp_path, pubkey)

    merge = parse_hl7_text(
        "MSH|^~\\&|RIS|SITE|TRACKER|SITE|20260725120500||ADT^A40|M1|P|2.5\r"
        "PID|1||SURVIVOR^^^SITE^MR\r"
        "MRG|RETIRED^^^SITE^MR\r"
    )
    assert _prior_mrn(merge, pack) == "RETIRED", "MRG-1 fallback stopped reading"

    result = parse_hl7_text(
        "MSH|^~\\&|RIS|SITE|TRACKER|SITE|20260725120500||ORU^R01|M2|P|2.5\r"
        "PID|1||MRN1^^^SITE^MR\r"
        "OBR|1||ACC1|71260^CT^C4|||20260725120000||||||||||REF1\r"
    )
    assert result_key_from_message(result, pack).observed_at == datetime(
        2026, 7, 25, 12, 0, tzinfo=timezone.utc
    ), "OBR-7 fallback stopped reading"


# ================================== REQUIRED_CONCEPTS, checked against the source

_SRC = Path(__file__).resolve().parents[1] / "src" / "referral_loop"

# The calls that read a concept out of a field map. `field_candidates` is
# scanned as well as `concept_value` so that a read reaching past the ordinary
# route is not invisible to this scan.
_READING_CALLS = frozenset({"concept_value", "field_candidates"})

# Where the concept argument sits, by call.
_CONCEPT_ARG_INDEX = {"concept_value": 2, "field_candidates": 0}

# `matcher.concept_value` forwards its own `concept` parameter down to
# `field_candidates`, so that call names no particular concept. Exempted by name
# rather than by silently skipping every argument the scan cannot resolve: an
# unresolvable concept anywhere else is a hole in this scan, and a hole has to
# be reported as a failure or the scan reads as coverage it does not have.
_FORWARDING_SITES = frozenset({("matcher.py", "concept_value")})


class _ConceptReads(NamedTuple):
    """What one scan found. `unaccounted` is the honest half."""

    unguarded: set[str]
    guarded: set[str]
    unaccounted: list[str]

    def merged_with(self, other: "_ConceptReads") -> "_ConceptReads":
        return _ConceptReads(
            self.unguarded | other.unguarded,
            self.guarded | other.guarded,
            self.unaccounted + other.unaccounted,
        )


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"`. matcher.py names its concepts this way,
    listener.py passes some of them as bare literals, and both spellings have to
    resolve or half the call sites are invisible."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _resolved(node: ast.expr | None, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _concepts_guarded_by(test: ast.expr, constants: dict[str, str]) -> set[str]:
    """The concepts an `if <concept> in <anything>.field_map` test makes optional.

    Matched on the attribute name alone, so `pack.field_map` and
    `self.pack.field_map` both count. `and`-joined tests are walked because a
    guard picking up a second condition should not silently stop being one.
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        found: set[str] = set()
        for value in test.values:
            found |= _concepts_guarded_by(value, constants)
        return found
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.In)
        and isinstance(test.comparators[0], ast.Attribute)
        and test.comparators[0].attr == "field_map"
    ):
        concept = _resolved(test.left, constants)
        return {concept} if concept else set()
    return set()


def _scan_concept_reads(filename: str, source: str) -> _ConceptReads:
    """The concept reads in one module, split by whether a guard covers each.

    What it cannot see goes into `unaccounted` rather than being dropped, since
    the point of the scan is the calls nobody remembered.

    A read counts as guarded only when it sits in the *body* of an
    `if <concept> in ....field_map` whose concept is the one being read -- so a
    read in the `else`, or under a guard for a different concept, is reported as
    what it is. Ancestors are walked rather than the immediately enclosing
    statement, because the guarded reads in this codebase are nested inside a
    return and a second call.
    """
    tree = ast.parse(source, filename=filename)
    constants = _string_constants(tree)
    unguarded: set[str] = set()
    guarded: set[str] = set()
    unaccounted: list[str] = []

    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._scan_parent = parent  # type: ignore[attr-defined]

    for node in ast.walk(tree):
        # An aliased import hides the call name this scan matches on. Reported
        # rather than handled: the rename is the moment to look at the scan.
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _READING_CALLS and alias.asname:
                    unaccounted.append(
                        f"{filename}:{node.lineno} imports {alias.name} as {alias.asname}"
                    )
            continue
        if not isinstance(node, ast.Call):
            continue
        called = (node.func.attr if isinstance(node.func, ast.Attribute)
                  else node.func.id if isinstance(node.func, ast.Name) else "")
        if called not in _READING_CALLS:
            continue

        guards: set[str] = set()
        function = ""
        child = node
        while (parent := getattr(child, "_scan_parent", None)) is not None:
            if isinstance(parent, ast.If) and any(child is stmt for stmt in parent.body):
                guards |= _concepts_guarded_by(parent.test, constants)
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and not function:
                function = parent.name
            child = parent
        if (filename, function) in _FORWARDING_SITES:
            continue

        index = _CONCEPT_ARG_INDEX[called]
        argument = (node.args[index] if len(node.args) > index else
                    next((kw.value for kw in node.keywords if kw.arg == "concept"), None))
        concept = _resolved(argument, constants)
        if concept is None:
            unaccounted.append(f"{filename}:{node.lineno} {called}() names no resolvable concept")
        elif concept in guards:
            guarded.add(concept)
        else:
            unguarded.add(concept)

    return _ConceptReads(unguarded, guarded, unaccounted)


def _reads_across_src() -> _ConceptReads:
    reads = _ConceptReads(set(), set(), [])
    for path in sorted(_SRC.rglob("*.py")):
        # Keyed on the path below src/, not the bare filename: two modules can
        # share a basename across subpackages, and `_FORWARDING_SITES` would
        # then exempt a call in whichever one it did not mean.
        reads = reads.merged_with(
            _scan_concept_reads(path.relative_to(_SRC).as_posix(),
                                path.read_text(encoding="utf-8"))
        )
    return reads


def test_required_concepts_matches_the_unguarded_reads_in_src():
    """REQUIRED_CONCEPTS is a hand-written set, and this is what keeps it honest.

    Read off the source rather than asserted as a list, because the failure it
    exists for is an unguarded `concept_value` call added without a thought for
    that set: nothing fails, the pack the developer is holding carries the
    concept, and the defect surfaces at whichever site next boots an older pack.
    Equality in both directions -- a stale entry is as wrong as a missing one,
    since it refuses packs over a concept nothing reads.
    """
    reads = _reads_across_src()
    assert reads.unaccounted == [], (
        f"this scan could not account for {reads.unaccounted}; a scan with a hole "
        "in it reads as coverage it does not have"
    )
    assert reads.unguarded == set(REQUIRED_CONCEPTS), (
        f"unguarded reads not in REQUIRED_CONCEPTS: "
        f"{sorted(reads.unguarded - REQUIRED_CONCEPTS)}; "
        f"REQUIRED_CONCEPTS entries nothing reads unguarded: "
        f"{sorted(REQUIRED_CONCEPTS - reads.unguarded)}"
    )
    assert reads.guarded.isdisjoint(REQUIRED_CONCEPTS), (
        f"read behind an `in pack.field_map` guard yet required at load: "
        f"{sorted(reads.guarded & REQUIRED_CONCEPTS)} -- the guard is then dead code"
    )


def test_the_scan_catches_an_unguarded_read_of_a_concept_not_in_the_required_set():
    """The test above passes today whether or not the scan works. This is the
    one that shows it fails when it should -- fed a module the repo does not
    contain, so it stays true as the real sources move."""
    added = _scan_concept_reads(
        "invented.py",
        "def read(message, pack):\n"
        "    return concept_value(message, pack, 'radiologist_mood')\n",
    )
    reads = _reads_across_src().merged_with(added)
    assert reads.unguarded - set(REQUIRED_CONCEPTS) == {"radiologist_mood"}
    assert reads.unguarded != set(REQUIRED_CONCEPTS)


def test_the_scan_does_not_count_a_guarded_read_as_required():
    """Both spellings of a guard, and the negative half: a scan that called
    everything guarded would let an unguarded read through unnoticed."""
    guarded = _scan_concept_reads(
        "invented.py",
        "_CONCEPT = 'radiologist_mood'\n"
        "def read(message, pack):\n"
        "    if _CONCEPT in pack.field_map:\n"
        "        return str(concept_value(message, pack, _CONCEPT))\n"
        "    return ''\n",
    )
    assert guarded.guarded == {"radiologist_mood"}
    assert guarded.unguarded == set()

    elsewhere = _scan_concept_reads(
        "invented.py",
        "def read(message, pack):\n"
        "    if 'modality' in pack.field_map:\n"
        "        return concept_value(message, pack, 'radiologist_mood')\n"
        "    return ''\n",
    )
    assert elsewhere.unguarded == {"radiologist_mood"}, (
        "a guard on a different concept is not a guard on this one"
    )


def test_the_scan_reports_a_concept_it_cannot_resolve_rather_than_skipping_it():
    """The blind spot, made loud. A concept arriving as a parameter or a lookup
    cannot be resolved statically, and skipping it silently is how this scan
    would come to miss the call it was written for."""
    forwarded = _scan_concept_reads(
        "invented.py",
        "def read(message, pack, which):\n"
        "    return concept_value(message, pack, which)\n",
    )
    assert forwarded.unaccounted and "invented.py:2" in forwarded.unaccounted[0]
    assert forwarded.unguarded == set()

    renamed = _scan_concept_reads(
        "invented.py", "from .matcher import concept_value as read_concept\n"
    )
    assert renamed.unaccounted and "read_concept" in renamed.unaccounted[0]
