"""The listener: persist-before-ACK, content-key idempotency, wire discipline.

Three properties carry the weight here and each has a test that would fail if
the property were merely asserted rather than implemented:

  * **Persist before ACK.** `test_raw_survives_process_death_mid_parse` kills a
    real OS process between the durable write and the parse and then rebuilds
    the state from the archive. An in-process `del` or a monkeypatched raise is
    not process death; the plan's version of this test was exactly that, and it
    would pass against an implementation that ACKed first.
  * **Content-key idempotency.** A retry under a fresh MSH-10 is one transition,
    and a genuine amendment is *not* swallowed. Dedup that eats corrections
    would be far worse than the double-counting it fixes, so both directions are
    asserted.
  * **Wire discipline.** A body carrying `FS CR` must never be answered AA on
    its truncated half. That is the failure this whole subsystem exists to
    prevent and it is tested through a real socket, not through `deframe`.

Messages are built field-by-index rather than written out as pipe-delimited
string literals. Hand-counting to OBR-16 is how the ordering-provider tie-break
silently became a no-op once already; a builder makes the field number the thing
the test states.
"""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from healthcare_rag.referral_loop import listener as listener_module
from healthcare_rag.referral_loop.errors import StoreUnavailableError
from healthcare_rag.referral_loop.events import LoopState
from healthcare_rag.referral_loop.listener import (
    _MRN_RESOLUTION_ATTEMPTS,
    FileDropSource,
    MessageHandler,
    ack_code,
    content_key,
    make_mllp_server,
)
from healthcare_rag.referral_loop.matcher import field_value
from healthcare_rag.referral_loop.mllp import CR, FS, VT, frame
from healthcare_rag.referral_loop.mllp_server import DESYNC_GRACE_SECONDS
from healthcare_rag.referral_loop.parse_hl7 import parse_hl7_text
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.staleness import is_stale, staleness_ratio
from healthcare_rag.referral_loop.store import LoopStore
from tests.referral_loop.test_matcher import PACK

REPO_ROOT = Path(__file__).resolve().parents[2]

MRN = "MRN123456"
SURVIVING_MRN = "MRN999999"
PLACER = "PLACER987"
FILLER = "FILLER654"
SERVICE = "71260^CT CHEST W CONTRAST^CT"
PROVIDER = "REF001^SMITH^JOHN"

ORDERED_AT = "20260724080000"
RESULTED_AT = "20260725120000"


# --------------------------------------------------------------- message builders


def _fields(values: dict[int, str]) -> list[str]:
    width = max(values) if values else 0
    out = [""] * (width + 1)
    for index, value in values.items():
        out[index] = value
    return out


def segment(seg_id: str, values: dict[int, str]) -> str:
    """`segment("OBR", {2: "PLACER987"})` -> `OBR|1-indexed fields`."""
    fields = _fields(values)
    fields[0] = seg_id
    return "|".join(fields)


def msh(message_type: str, control_id: str, message_at: str = RESULTED_AT) -> str:
    """MSH-1 *is* the field separator, so MSH fields shift by one."""
    fields = _fields(
        {
            2: r"^~\&", 3: "EHR", 4: "HOSP", 5: "RIS", 6: "HOSP",
            7: message_at, 9: message_type, 10: control_id, 11: "P", 12: "2.5.1",
        }
    )
    return "MSH|" + "|".join(fields[2:])


def message(*segments: str) -> str:
    return "".join(s + "\r" for s in segments)


def pid(mrn: str = MRN) -> str:
    return segment("PID", {1: "1", 3: f"{mrn}^^^HOSP^MR", 5: "DOE^JANE", 8: "F"})


def obr(placer: str = PLACER, filler: str = FILLER, when: str = ORDERED_AT) -> str:
    return segment(
        "OBR",
        {1: "1", 2: placer, 3: filler, 4: SERVICE, 7: when, 16: PROVIDER},
    )


def order(control_id: str = "CTRL_ORM", mrn: str = MRN, placer: str = PLACER,
          filler: str = FILLER, message_at: str = ORDERED_AT,
          message_type: str = "ORM^O01", observed_at: str = ORDERED_AT) -> str:
    return message(
        msh(message_type, control_id, message_at),
        pid(mrn),
        segment("ORC", {1: "NW", 2: placer}),
        obr(placer, filler, observed_at),
    )


def result(control_id: str = "CTRL_ORU", mrn: str = MRN, placer: str = PLACER,
           filler: str = FILLER, obx11: str = "F", value: str = "No acute finding",
           message_at: str = RESULTED_AT, observed_at: str = ORDERED_AT) -> str:
    return message(
        msh("ORU^R01", control_id, message_at),
        pid(mrn),
        obr(placer, filler, observed_at),
        segment("OBX", {1: "1", 2: "TX", 3: "71260^CT CHEST^CT", 5: value, 11: obx11}),
    )


def result_naming_no_patient(control_id: str = "CTRL_NO_PID", placer: str = "",
                             filler: str = FILLER, obx11: str = "F",
                             message_at: str = RESULTED_AT) -> str:
    """An `ORU^R01` with the PID segment omitted entirely.

    The H3 exploit shape: a result that names no patient at all, carrying an
    order number lifted from a requisition or a worklist. Built by leaving the
    segment out rather than by blanking PID-3, because that is what the wire
    carries and because a blank PID-3 and an absent PID are two different
    messages that must reach the same answer.
    """
    return message(
        msh("ORU^R01", control_id, message_at),
        obr(placer, filler, ORDERED_AT),
        segment("OBX", {1: "1", 2: "TX", 3: "71260^CT CHEST^CT", 5: "No acute finding",
                        11: obx11}),
    )


def scheduling(control_id: str, message_type: str, mrn: str = MRN,
               placer: str = "", message_at: str = RESULTED_AT) -> str:
    segments = [msh(message_type, control_id, message_at), pid(mrn),
                segment("SCH", {1: "APPT1", 2: "APPT1"})]
    if placer:
        segments.append(segment("ORC", {1: "SC", 2: placer}))
    return message(*segments)


def merge(control_id: str, prior: str = MRN, surviving: str = SURVIVING_MRN,
          message_at: str = RESULTED_AT) -> str:
    return message(
        msh("ADT^A40", control_id, message_at),
        pid(surviving),
        segment("MRG", {1: f"{prior}^^^HOSP^MR"}),
    )


# ------------------------------------------------------------------- fixtures


@pytest.fixture()
def store(tmp_path):
    return LoopStore(tmp_path / "loops.db")


@pytest.fixture()
def handler(store):
    return MessageHandler(store=store, registry=Registry(store), pack=PACK)


def loops(handler):
    return [loop for loop in handler.store.all_loops() if loop.loop_id.startswith("L-")]


def orphans(handler):
    return [loop for loop in handler.store.all_loops() if loop.loop_id.startswith("O-")]


def events_of(handler, loop_id):
    return [event.event_type for event in handler.store.events_for(loop_id)]


# ------------------------------------------------- the builders build what they claim


def test_builders_place_fields_where_the_test_says_they_do():
    """Guards every other test in this file: a miscounted pipe would make the
    ordering-provider and accession assertions vacuous rather than failing."""
    parsed = parse_hl7_text(result())
    assert parsed.control_id == "CTRL_ORU"
    assert parsed.message_type == "ORU^R01"
    assert field_value(parsed, "MSH-7") == RESULTED_AT
    assert field_value(parsed, "PID-3.1") == MRN
    assert field_value(parsed, "OBR-2") == PLACER
    assert field_value(parsed, "OBR-3") == FILLER
    assert field_value(parsed, "OBR-16.1") == "REF001"
    assert field_value(parsed, "OBX-11") == "F"
    assert field_value(parsed, "MRG-1.1") == ""
    assert field_value(parse_hl7_text(merge("M1")), "MRG-1.1") == MRN


# ------------------------------------------------------------------ ACK ordering


def test_valid_order_returns_aa_after_a_durable_write(handler):
    ack = handler.handle(order())
    assert ack_code(ack) == "AA"
    assert handler.store.raw_count() == 1
    assert len(loops(handler)) == 1


def test_raw_is_persisted_before_parsing(handler, monkeypatch):
    """In-process cousin of the out-of-process proof below. Kept because it
    pins the *ordering* cheaply on every run."""
    import healthcare_rag.referral_loop.listener as listener_mod

    def exploding_parse(_text):
        raise RuntimeError("parser blew up")

    monkeypatch.setattr(listener_mod, "parse_hl7_text", exploding_parse)
    handler.handle(order())
    assert handler.store.raw_count() == 1, "raw must survive a parse failure"
    assert handler.parse_failure_count == 1


def test_raw_survives_process_death_mid_parse(tmp_path):
    """Spec test 15, proved out of process.

    A child process is wedged inside `parse_hl7_text` -- after the durable
    write, before anything is applied -- and then killed by the OS. The parent
    reopens the file and rebuilds the state from the archive. `del handler` or a
    monkeypatched exception proves nothing about a process that stops existing
    between two statements; this does.
    """
    db_path = tmp_path / "loops.db"
    message_path = tmp_path / "message.hl7"
    message_path.write_text(order(), encoding="utf-8")

    script = tmp_path / "wedge.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import healthcare_rag.referral_loop.listener as listener_mod\n"
        "from healthcare_rag.referral_loop.listener import MessageHandler\n"
        "from healthcare_rag.referral_loop.registry import Registry\n"
        "from healthcare_rag.referral_loop.store import LoopStore\n"
        "from tests.referral_loop.test_matcher import PACK\n"
        "store = LoopStore(sys.argv[1])\n"
        "handler = MessageHandler(store=store, registry=Registry(store), pack=PACK)\n"
        "def wedged(_text):\n"
        "    sys.stdout.write('PARSING\\n')\n"
        "    sys.stdout.flush()\n"
        "    while True:\n"
        "        time.sleep(0.05)\n"
        "listener_mod.parse_hl7_text = wedged\n"
        "handler.handle(open(sys.argv[2], encoding='utf-8').read())\n",
        encoding="utf-8",
    )

    child = subprocess.Popen(
        [sys.executable, str(script), str(db_path), str(message_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(REPO_ROOT),
    )
    try:
        ready = child.stdout.readline()
        assert ready.strip() == "PARSING", f"child never reached parse: {child.stderr.read()}"
        child.kill()
        child.wait(timeout=30)
    finally:
        if child.poll() is None:  # pragma: no cover - only on a hung child
            child.kill()
        child.stdout.close()
        child.stderr.close()

    assert child.returncode != 0, "the child must have been killed, not have exited cleanly"

    reopened = LoopStore(db_path)
    assert reopened.raw_count() == 1, "the raw message must have survived process death"
    assert reopened.all_loops() == [], "nothing was applied before the kill"

    # Replay the archive: the state the killed process never got to build.
    replayed = MessageHandler(store=reopened, registry=Registry(reopened), pack=PACK)
    for payload in reopened.raw_payloads():
        assert ack_code(replayed.handle(payload)) == "AA"
    rebuilt = loops(replayed)
    assert len(rebuilt) == 1
    assert rebuilt[0].state is LoopState.OPEN
    assert rebuilt[0].placer_order_number == PLACER


def test_store_failure_returns_ae_and_never_aa(handler, tmp_path):
    """Failure matrix: DB unwritable -> AE so the engine queues.

    The store is made genuinely unwritable -- the database file is replaced by a
    directory -- rather than having `record_raw` mocked to raise. A mocked
    return value tests the handler's `except` clause; this tests that a real
    disk failure reaches it as a StoreUnavailableError at all.
    """
    db_path = Path(handler.store.db_path)
    db_path.unlink()
    db_path.mkdir()

    with pytest.raises(StoreUnavailableError):
        handler.store.record_raw("PROBE", "x")  # the failure is real, not mocked

    ack = handler.handle(order())
    assert ack_code(ack) == "AE"
    assert "|AA|" not in ack
    assert handler.store_failure_count == 1


def test_store_failure_during_apply_also_returns_ae(handler, monkeypatch):
    """The raw landed; the event did not. AE, so the engine redelivers -- and
    the redelivery must not be swallowed as an MSH-10 duplicate, which is what
    `applied_messages` (rather than `raw_messages`) being the dedup key buys."""
    real_append = handler.store.append_event

    def failing_append(_event):
        raise StoreUnavailableError("disk full")

    monkeypatch.setattr(handler.store, "append_event", failing_append)
    assert ack_code(handler.handle(order())) == "AE"
    assert handler.store.raw_count() == 1
    assert loops(handler) == []

    monkeypatch.setattr(handler.store, "append_event", real_append)
    assert ack_code(handler.handle(order())) == "AA", "redelivery under the same MSH-10 must apply"
    assert len(loops(handler)) == 1


def test_message_with_no_control_id_is_never_acked_aa(handler):
    """A message we cannot key is one we cannot promise not to double-process."""
    unkeyed = message(msh("ORM^O01", ""), pid(), obr())
    assert ack_code(handler.handle(unkeyed)) == "AE"


# ------------------------------------------------------------------- idempotency


def test_duplicate_control_id_is_a_noop_but_still_acked(handler):
    handler.handle(order())
    ack = handler.handle(order())
    assert ack_code(ack) == "AA"
    assert handler.store.raw_count() == 1
    assert len(loops(handler)) == 1, "a duplicate must not create a second loop"
    assert handler.duplicate_control_id_count == 1
    assert handler.duplicate_content_key_count == 0


def test_retry_under_a_fresh_control_id_is_not_a_second_transition(handler):
    """Spec test 16. Engines stamp a new MSH-10 on retry; MSH-10 dedup alone
    would let the same result produce a second `resulted` event."""
    handler.handle(order())
    handler.handle(result(control_id="ORU_1"))
    handler.handle(result(control_id="ORU_2"))

    assert len(loops(handler)) == 1
    assert events_of(handler, loops(handler)[0].loop_id).count("resulted") == 1
    assert handler.duplicate_content_key_count == 1
    assert handler.duplicate_control_id_count == 0, "counted separately -- they mean different things"
    assert handler.store.raw_count() == 3, "every delivery is still archived verbatim"


def test_content_duplicate_and_control_id_duplicate_are_counted_separately(handler):
    handler.handle(order())
    handler.handle(result(control_id="ORU_1"))
    handler.handle(result(control_id="ORU_1"))   # ordinary engine chatter
    handler.handle(result(control_id="ORU_2"))   # a retry configuration
    assert handler.duplicate_control_id_count == 1
    assert handler.duplicate_content_key_count == 1


def test_a_genuine_correction_is_not_swallowed(handler):
    """Safety rule 2. Dedup that ate amendments would be far worse than the
    double-counting it fixes."""
    handler.handle(order())
    handler.handle(result(control_id="ORU_1", obx11="F", value="No acute finding"))
    loop_id = loops(handler)[0].loop_id
    handler.registry.acknowledge(loop_id, actor="coordinator", role="RN", control_id="UI")
    assert handler.registry.get(loop_id).state is LoopState.ACKNOWLEDGED

    ack = handler.handle(
        result(control_id="ORU_2", obx11="C", value="4mm nodule, follow up",
               message_at="20260725130000")
    )
    assert ack_code(ack) == "AA"
    assert handler.duplicate_content_key_count == 0, "a correction is not a duplicate"
    assert handler.registry.get(loop_id).state is LoopState.RESULTED
    assert "reopened" in events_of(handler, loop_id)


def test_a_correction_carrying_the_same_value_is_still_not_swallowed(handler):
    """OBX-11 is part of the content key, so C-after-F differs even when the
    radiologist's text did not change."""
    handler.handle(order())
    handler.handle(result(control_id="ORU_1", obx11="F", value="No acute finding"))
    handler.handle(result(control_id="ORU_2", obx11="C", value="No acute finding",
                          message_at="20260725130000"))
    loop_id = loops(handler)[0].loop_id
    assert handler.duplicate_content_key_count == 0
    assert "reopened" in events_of(handler, loop_id)


def test_the_same_result_under_a_retired_and_a_surviving_mrn_dedups(handler):
    """Content keys are built from resolved values, so dedup keeps working
    exactly when a merge has happened -- which is when it matters most."""
    handler.handle(order(control_id="ORM_1", mrn=MRN))
    handler.handle(merge("A40_1"))               # MRN -> SURVIVING_MRN
    assert handler.store.resolve_mrn(MRN) == SURVIVING_MRN

    handler.handle(result(control_id="ORU_1", mrn=MRN))            # engine still on the old id
    handler.handle(result(control_id="ORU_2", mrn=SURVIVING_MRN))  # engine caught up

    loop_id = loops(handler)[0].loop_id
    assert handler.duplicate_content_key_count == 1
    assert events_of(handler, loop_id).count("resulted") == 1


def test_content_key_is_computed_from_the_resolved_mrn(handler):
    parsed = parse_hl7_text(result())
    assert content_key(parsed, PACK, mrn="A") != content_key(parsed, PACK, mrn="B")
    assert content_key(parsed, PACK, mrn="A") == content_key(parsed, PACK, mrn="A")


def test_content_key_separates_message_types_carrying_the_same_identifiers(handler):
    """Without the message type in the tuple an SIU and an A40 for a patient
    with no order numbers hash identically, and the second is swallowed."""
    schedule_msg = parse_hl7_text(scheduling("S1", "SIU^S12"))
    merge_msg = parse_hl7_text(merge("M1", prior="OTHER", surviving=MRN))
    assert content_key(schedule_msg, PACK, mrn=MRN) != content_key(merge_msg, PACK, mrn=MRN)


def test_two_merges_into_the_same_survivor_do_not_collide(handler):
    """The retired MRN is part of the tuple; without it the second A40 would be
    swallowed as a content duplicate and its loops stranded."""
    first = parse_hl7_text(merge("M1", prior="MRN_AAA", surviving=SURVIVING_MRN))
    second = parse_hl7_text(merge("M2", prior="MRN_BBB", surviving=SURVIVING_MRN))
    assert content_key(first, PACK, mrn=SURVIVING_MRN) != content_key(second, PACK, mrn=SURVIVING_MRN)


def test_a_message_with_no_identifying_content_is_not_content_deduped(handler):
    """An empty tuple would make every such message a duplicate of the first."""
    bare = parse_hl7_text(message(msh("ORU^R01", "C1"), segment("OBR", {1: "1"})))
    assert content_key(bare, PACK, mrn="") is None


# ------------------------------------------------------------------- dispatch


@pytest.mark.parametrize("message_type", ["ORM^O01", "OMG^O19", "REF^I12"])
def test_order_messages_open_a_loop(handler, message_type):
    handler.handle(order(message_type=message_type))
    assert len(loops(handler)) == 1
    assert loops(handler)[0].state is LoopState.OPEN


def test_an_order_populates_the_ordering_provider(handler):
    """The pack's second tie-breaker is a permanent no-op if the listener never
    writes this field. It was, in the version of the listener this replaces."""
    handler.handle(order())
    assert loops(handler)[0].ordering_provider == "REF001"


def test_an_order_takes_every_field_from_the_pack_not_from_a_hardcoded_index(handler, store):
    """Spec test 18, at the ingest boundary: relocate the accession to OBR-18
    in both fixture and pack and assert the loop still carries it."""
    relocated_pack = replace(
        PACK, field_map={**PACK.field_map, "filler_order_number": ["OBR-18"]}
    )
    relocated_handler = MessageHandler(store=store, registry=Registry(store), pack=relocated_pack)
    relocated = message(
        msh("ORM^O01", "CTRL_REL", ORDERED_AT),
        pid(),
        segment("OBR", {1: "1", 2: PLACER, 4: SERVICE, 7: ORDERED_AT, 16: PROVIDER,
                        18: FILLER}),
    )
    relocated_handler.handle(relocated)
    assert loops(relocated_handler)[0].filler_order_number == FILLER


def test_an_order_records_the_ordered_at_from_the_message(handler):
    handler.handle(order())
    assert loops(handler)[0].ordered_at == datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)


def test_oru_matching_an_open_order_advances_it(handler):
    handler.handle(order())
    handler.handle(result())
    assert loops(handler)[0].state is LoopState.RESULTED
    assert handler.matched_count == 1


def test_oru_with_no_matching_order_creates_an_orphan(handler):
    handler.handle(result(placer="NOSUCH", filler="NOSUCH"))
    assert len(orphans(handler)) == 1
    assert handler.orphan_count == 1


# ------------------------------- a result that names no patient (defect H3)
#
# Accession and placer numbers are frequently sequential and are printed on
# requisitions and worklists, so an order number is guessable in a way an MRN
# paired with one is not. An ORU carrying one but no PID therefore has to be
# treated as evidence about an order and no evidence at all about a patient:
# it may reach a coordinator as a candidate, and it may not attach itself.


def _result_with_a_blank_patient_id(control_id: str = "CTRL_BLANK_PID") -> str:
    """The PID segment present and PID-3 empty, rather than the segment absent.

    A different message on the wire and the same absence of a patient, so it
    must reach the same answer -- and it is the shape a misconfigured feed
    produces, where the omitted segment is the shape a forgery produces.
    """
    return message(
        msh("ORU^R01", control_id, RESULTED_AT),
        segment("PID", {1: "1", 5: "DOE^JANE", 8: "F"}),
        obr("", FILLER, ORDERED_AT),
        segment("OBX", {1: "1", 2: "TX", 3: "71260^CT CHEST^CT", 5: "No acute finding", 11: "F"}),
    )


@pytest.mark.parametrize(
    "build", [result_naming_no_patient, _result_with_a_blank_patient_id],
    ids=["pid-segment-absent", "pid-3-blank"],
)
def test_an_oru_naming_no_patient_never_auto_attaches_to_a_loop(handler, build):
    """Defect H3. Before the fix: tier 2 fired at confidence 1.0 and the loop
    reached RESULTED -- "awaiting acknowledgement" -- on a result that named
    nobody, so a coordinator confirming the queue would report patient A's
    referral handled."""
    handler.handle(order())
    handler.handle(build())

    assert loops(handler)[0].state is LoopState.OPEN, (
        "a result naming no patient must not advance a patient's loop"
    )
    assert handler.matched_count == 0
    assert len(orphans(handler)) == 1


def test_the_same_result_carrying_its_pid_segment_still_attaches(handler):
    """The other direction of the same fix. The decline must cost the ordinary
    ORU nothing: same order number, same OBX, one segment more, and it attaches
    exactly as before. A matcher that stops attaching scores a perfect
    false-match rate while the product quietly stops working (spec 10.4)."""
    handler.handle(order())
    handler.handle(result(control_id="ORU_WITH_PID"))

    assert loops(handler)[0].state is LoopState.RESULTED
    assert handler.matched_count == 1
    assert handler.unattributable_result_count == 0
    assert orphans(handler) == []


def test_a_result_whose_pid_names_another_patient_still_falls_through(handler):
    """Unchanged by this fix, and asserted here because the two cases are one
    line apart in `_mrn_check`: a *disagreeing* MRN drops the loop from the tier
    without declining, so the collision cannot consume the result's turn at the
    lower tiers."""
    handler.handle(order())
    handler.handle(result(control_id="ORU_OTHER", mrn="MRN_SOMEONE_ELSE"))

    assert loops(handler)[0].state is LoopState.OPEN
    assert handler.unattributable_result_count == 0, "nothing here is unattributable"
    detail = handler.store.events_for(orphans(handler)[0].loop_id)[0].detail
    assert "MRN disagreed" in detail["match_reason"]


def test_a_result_naming_no_patient_is_counted_and_logged_by_control_id(handler, caplog):
    """The `mrn_rejected` warning never fires for this shape -- nothing was
    rejected -- so the decline needs a counter and a line of its own, or the
    only symptom of a feed omitting PID segments is a quietly growing queue.

    The line carries the control id and the running total and no identifier: a
    separate finding in this subsystem is that MRNs reach logs through exception
    paths, and the message that names a *missing* patient identifier is a poor
    place to print a present one.
    """
    handler.handle(order())
    caplog.set_level(logging.DEBUG)
    handler.handle(result_naming_no_patient(control_id="ORU_NO_PID", filler=FILLER))

    assert handler.unattributable_result_count == 1
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "ORU_NO_PID" in text
    assert MRN not in text


def test_a_result_naming_no_patient_reaches_the_queue_as_a_named_near_miss(handler):
    """Option (b): the information survives the decline. The orphan carries the
    tier that fired and why it was not acted on, so a coordinator can attach it
    deliberately -- which is a labeled example (spec section 7) -- rather than
    triaging it as a result nobody ordered."""
    handler.handle(order())
    handler.handle(result_naming_no_patient(filler=FILLER))

    detail = handler.store.events_for(orphans(handler)[0].loop_id)[0].detail
    assert detail["match_tier"] == 2
    assert "names no patient" in detail["match_reason"]


def _candidates_offered(handler, monkeypatch) -> list:
    """Capture the loops ingest hands the matcher, without replacing it.

    The real matcher still runs and still decides; the spy only records what it
    was shown. Which loops reach it is not observable from the outcome -- an
    unrelated patient's loop changes no answer -- and is exactly the property
    under test, so it is asserted at the boundary where it exists.
    """
    offered: list = []
    real = listener_module.match_result

    def spy(key, loops, pack):
        offered.append(list(loops))
        return real(key, loops, pack)

    monkeypatch.setattr(listener_module, "match_result", spy)
    return offered


def test_ingest_offers_the_matcher_one_patients_loops_not_the_whole_table(handler, monkeypatch):
    """The other half of defect H3: nothing narrowed the candidate set by
    patient before matching began, so every loop in the site was a candidate for
    every arriving result and the MRN comparison was the only thing between
    them."""
    handler.handle(order(control_id="ORM_A", mrn="MRN_A", placer="P_A", filler="F_A"))
    handler.handle(order(control_id="ORM_B", mrn="MRN_B", placer="P_B", filler="F_B"))
    offered = _candidates_offered(handler, monkeypatch)

    handler.handle(result(control_id="ORU_A", mrn="MRN_A", placer="P_A", filler="F_A"))

    assert [loop.mrn for loop in offered[0]] == ["MRN_A"]


def test_ingest_still_offers_a_loop_that_shares_an_order_number(handler, monkeypatch):
    """Scoping must not blind the matcher to a cross-feed numbering collision.
    It can only report a collision it was shown, and "two placing systems
    numbering from the same seed" is a site-wide fault that gets worse
    silently."""
    handler.handle(order(control_id="ORM_B", mrn="MRN_B", placer="P_SHARED", filler="F_B"))
    handler.handle(result(control_id="ORU_A", mrn="MRN_A", placer="P_SHARED", filler="F_A"))

    detail = handler.store.events_for(orphans(handler)[0].loop_id)[0].detail
    assert "MRN disagreed" in detail["match_reason"]


def test_a_result_naming_neither_a_patient_nor_an_order_number_gets_no_candidates(
    handler, monkeypatch
):
    """No tier can fire on such a key, so the answer is the same either way --
    but "no candidates" and "every loop in the site" are the same answer only
    for today's four tiers, and the second is what defect H3 needed to work."""
    handler.handle(order())
    offered = _candidates_offered(handler, monkeypatch)

    handler.handle(message(
        msh("ORU^R01", "ORU_BARE", RESULTED_AT),
        segment("OBR", {1: "1", 4: SERVICE, 7: ORDERED_AT}),
        segment("OBX", {1: "1", 2: "TX", 3: "71260^CT CHEST^CT", 5: "text", 11: "F"}),
    ))

    assert offered[0] == []
    assert loops(handler)[0].state is LoopState.OPEN


def test_a_scheduling_message_naming_no_patient_changes_no_loop(handler):
    """`open_loops("")` is every open loop in the site, so an `SIU^S15` with no
    PID segment reached the "the patient has exactly one open loop" fallback
    with the *site's* only open loop -- and cancelling a loop removes it from
    every worklist while it is still clinically open."""
    handler.handle(order())
    handler.handle(message(
        msh("SIU^S15", "S_NO_PID", RESULTED_AT),
        segment("SCH", {1: "APPT1", 2: "APPT1"}),
    ))

    assert loops(handler)[0].state is LoopState.OPEN
    assert handler.untargeted_count == 1


def test_a_correction_reaches_an_acknowledged_loop(handler):
    """The matcher offers ACKNOWLEDGED loops at the exact tiers precisely so
    safety rule 2 can fire. A listener that only supplied open and resulted
    loops would send every correction to the orphan queue while the loop it
    corrects went on reporting `handled`."""
    handler.handle(order())
    handler.handle(result(control_id="ORU_1"))
    loop_id = loops(handler)[0].loop_id
    handler.registry.acknowledge(loop_id, actor="coordinator", role="RN", control_id="UI")

    handler.handle(result(control_id="ORU_2", obx11="C", value="revised",
                          message_at="20260725130000"))
    assert orphans(handler) == []
    assert handler.registry.get(loop_id).state is LoopState.RESULTED


def test_an_unreadable_obx11_is_treated_as_preliminary_never_as_final(handler):
    """Safety rule 1 is an allowlist. Defaulting an unknown status to `F` -- as
    the plan's listener did -- makes an unreadable read acknowledgeable."""
    handler.handle(order())
    handler.handle(result(obx11=""))
    loop_id = loops(handler)[0].loop_id
    assert handler.registry.get(loop_id).state is LoopState.RESULTED
    with pytest.raises(Exception):
        handler.registry.acknowledge(loop_id, actor="c", role="RN", control_id="UI")


def test_the_weakest_obx11_in_a_multi_obx_report_decides(handler):
    """One preliminary OBX means the report is not final."""
    handler.handle(order())
    mixed = message(
        msh("ORU^R01", "ORU_MIX", RESULTED_AT),
        pid(),
        obr(),
        segment("OBX", {1: "1", 2: "TX", 3: "71260^CT^CT", 5: "final part", 11: "F"}),
        segment("OBX", {1: "2", 2: "TX", 3: "71260^CT^CT", 5: "pending", 11: "P"}),
    )
    handler.handle(mixed)
    loop_id = loops(handler)[0].loop_id
    with pytest.raises(Exception):
        handler.registry.acknowledge(loop_id, actor="c", role="RN", control_id="UI")


def test_siu_s12_schedules_the_loop_named_by_its_order_number(handler):
    handler.handle(order())
    handler.handle(scheduling("S1", "SIU^S12", placer=PLACER))
    assert loops(handler)[0].state is LoopState.SCHEDULED


def test_siu_s15_cancels_the_loop_named_by_its_order_number(handler):
    handler.handle(order())
    handler.handle(scheduling("S2", "SIU^S15", placer=PLACER))
    assert loops(handler)[0].state is LoopState.CANCELLED


def test_siu_with_no_order_number_falls_back_to_a_single_open_loop(handler):
    handler.handle(order())
    handler.handle(scheduling("S1", "SIU^S12"))
    assert loops(handler)[0].state is LoopState.SCHEDULED


def test_siu_never_touches_more_than_one_loop(handler):
    """The plan's listener looped over every open loop for the patient. One
    SIU^S15 would then cancel unrelated open orders, and CANCELLED appears on
    no worklist -- loops vanishing while clinically open is the failure this
    product exists to prevent."""
    handler.handle(order(control_id="ORM_1", placer="P1", filler="F1"))
    handler.handle(order(control_id="ORM_2", placer="P2", filler="F2"))
    handler.handle(scheduling("S15", "SIU^S15"))
    assert [loop.state for loop in loops(handler)] == [LoopState.OPEN, LoopState.OPEN]
    assert handler.untargeted_count == 1


def test_scheduling_refuses_tier_3_evidence(handler):
    """Found by mutation: widening `_EXACT_TIERS` to (1, 2, 3, 4) passed the
    whole suite. Scheduling and cancelling are applied on an exact order
    identifier or on a single unambiguous open loop -- never on MRN + service
    code inside a date window, which is the weakest evidence class the matcher
    has and would let one appointment message cancel a different order."""
    handler.handle(order())
    weak = message(
        msh("SIU^S15", "S_WEAK", RESULTED_AT),
        pid(),
        segment("SCH", {1: "APPT1", 2: "APPT1"}),
        segment("ORC", {1: "SC", 2: "AN_ORDER_NUMBER_WE_DO_NOT_HAVE"}),
        segment("OBR", {1: "1", 4: SERVICE, 7: ORDERED_AT}),
    )
    handler.handle(weak)
    assert loops(handler)[0].state is LoopState.OPEN, "a tier-3 guess must not cancel a loop"
    assert handler.untargeted_count == 1


def test_a_redelivered_unmatched_result_does_not_create_a_second_orphan(handler):
    """The spec names both halves: a retry under a fresh control id must not
    produce a second `resulted` transition *or* a second orphan."""
    handler.handle(result(control_id="ORU_1", placer="NOSUCH", filler="NOSUCH"))
    handler.handle(result(control_id="ORU_2", placer="NOSUCH", filler="NOSUCH"))
    assert len(orphans(handler)) == 1
    assert handler.duplicate_content_key_count == 1


def test_an_mrn_retired_since_ingest_is_answered_ae_never_aa(handler, monkeypatch):
    """Spec test 7. The refusal is retryable, so the engine must be told to
    redeliver. Answering AA would drop a referral loop on the floor at the exact
    moment a merge made it invisible to the surviving patient."""
    # A moving target: every resolution disagrees with the last, so the local
    # re-resolve can never converge and the refusal has to leave the process.
    monkeypatch.setattr(handler.store, "resolve_mrn", lambda m: (m + "X") if m else m)

    ack = handler.handle(order())
    assert ack_code(ack) == "AE"
    assert "|AA|" not in ack
    assert loops(handler) == [], "never land a loop on a retired identifier"
    assert handler.mrn_retired_count == 1
    assert handler.mrn_reresolution_count == _MRN_RESOLUTION_ATTEMPTS - 1
    assert handler.store.raw_count() == 1, "the raw is archived even though nothing applied"


def test_the_redelivery_after_an_ae_is_applied_not_deduped(handler, monkeypatch):
    """The other half of answering AE: an engine that reuses the MSH-10 must not
    have its retry swallowed as a duplicate. This is why dedup is keyed on
    having been *applied* rather than on having arrived."""
    monkeypatch.setattr(handler.store, "resolve_mrn", lambda m: (m + "X") if m else m)
    assert ack_code(handler.handle(order())) == "AE"

    monkeypatch.undo()
    assert ack_code(handler.handle(order())) == "AA"
    assert len(loops(handler)) == 1
    assert handler.duplicate_control_id_count == 0


def test_adt_a40_carries_loops_and_records_the_alias(handler):
    handler.handle(order())
    handler.handle(merge("A40_1"))
    assert loops(handler)[0].mrn == SURVIVING_MRN
    assert handler.store.resolve_mrn(MRN) == SURVIVING_MRN


def test_unknown_message_type_is_counted_never_an_error(handler):
    ack = handler.handle(order(message_type="ZZZ^Z99"))
    assert ack_code(ack) == "AA"
    assert handler.unknown_type_count == 1
    assert loops(handler) == []


def test_an_unknown_type_redelivered_is_not_counted_twice(handler):
    handler.handle(order(message_type="ZZZ^Z99"))
    handler.handle(order(message_type="ZZZ^Z99"))
    assert handler.unknown_type_count == 1


# --------------------------------------------------- identity resolved at ingest


def test_an_order_carrying_a_retired_mrn_opens_on_the_surviving_one(handler):
    """Spec test 4: the retired MRN keeps arriving for hours after the merge."""
    handler.handle(merge("A40_1"))
    handler.handle(order(control_id="ORM_LATE", mrn=MRN))
    assert loops(handler)[0].mrn == SURVIVING_MRN


def test_the_submitted_mrn_is_recorded_alongside_the_resolved_one(handler):
    handler.handle(merge("A40_1"))
    handler.handle(order(control_id="ORM_LATE", mrn=MRN))
    created = handler.store.events_for(loops(handler)[0].loop_id)[0]
    assert created.detail["mrn"] == SURVIVING_MRN
    assert created.detail["submitted_mrn"] == MRN


def test_a_result_carrying_a_retired_mrn_still_matches(handler):
    """Spec test 5. Tiers 3 and 4 key on MRN, so an unresolved alias silently
    demotes a matchable result to an orphan."""
    handler.handle(order(control_id="ORM_1", mrn=MRN))
    handler.handle(merge("A40_1"))
    tier34_only = result(control_id="ORU_1", mrn=MRN, placer="", filler="")
    handler.handle(tier34_only)
    assert orphans(handler) == []
    assert loops(handler)[0].state is LoopState.RESULTED


def test_a_merge_committing_between_ingest_and_the_write_is_retried_not_lost(handler, monkeypatch):
    """Spec test 7's other half. `open_loop` refuses an MRN retired since ingest
    resolved it -- correctly -- but bouncing that back to the engine is the one
    recovery that might not recover: a redelivery under the same MSH-10 is a
    duplicate and would never be applied. So the listener re-resolves locally."""
    real_resolve = handler.store.resolve_mrn
    calls = {"n": 0}

    def racing_resolve(mrn):
        calls["n"] += 1
        if calls["n"] == 1:
            # Ingest resolved; the merge commits immediately afterwards.
            handler.store.record_alias(MRN, SURVIVING_MRN, datetime.now(timezone.utc), "A40")
            return mrn
        return real_resolve(mrn)

    monkeypatch.setattr(handler.store, "resolve_mrn", racing_resolve)
    assert ack_code(handler.handle(order())) == "AA"
    assert loops(handler)[0].mrn == SURVIVING_MRN, "never land a loop on a retired identifier"
    assert handler.mrn_reresolution_count == 1


def test_peek_agrees_with_the_parser_about_the_control_id(handler):
    """The archive is keyed on the peeked value and everything downstream on the
    parsed one. Two implementations that disagree key the archive on an id the
    rest of the pipeline never sees."""
    from healthcare_rag.referral_loop.parse_hl7 import peek_control_id

    for text in (order(), result(), merge("M1"), scheduling("S1", "SIU^S12")):
        assert peek_control_id(text) == parse_hl7_text(text).control_id
    assert peek_control_id("not an hl7 message at all") == ""


def test_the_raw_archive_keeps_the_message_verbatim(handler):
    """Resolution rewrites what the registry sees, never what was received."""
    handler.handle(merge("A40_1"))
    handler.handle(order(control_id="ORM_LATE", mrn=MRN))
    assert any(MRN in payload for payload in handler.store.raw_payloads())


# --------------------------------------------------------------- the watermark


def test_a_cancel_clinically_older_than_an_applied_schedule_is_refused(handler):
    """MSH-7 must reach the registry or the watermark is inert for live traffic
    -- and a late SIU^S15 would silently cancel a loop out of every worklist."""
    handler.handle(order())
    handler.handle(scheduling("S1", "SIU^S12", placer=PLACER, message_at="20260725120000"))
    assert loops(handler)[0].state is LoopState.SCHEDULED

    ack = handler.handle(
        scheduling("S2", "SIU^S15", placer=PLACER, message_at="20260725110000")
    )
    assert ack_code(ack) == "AA", "the raw is archived and routed for review, not retried forever"
    assert loops(handler)[0].state is LoopState.SCHEDULED, "the loop must not regress"
    assert handler.stale_message_count == 1


def test_a_result_clinically_older_than_an_applied_result_is_refused(handler):
    handler.handle(order())
    handler.handle(result(control_id="ORU_1", obx11="F", message_at="20260725120000"))
    handler.handle(
        result(control_id="ORU_0", obx11="P", value="earlier read",
               message_at="20260725100000")
    )
    loop_id = loops(handler)[0].loop_id
    assert handler.stale_message_count == 1
    assert events_of(handler, loop_id).count("resulted") == 1


def test_message_at_is_stamped_on_every_message_driven_event(handler):
    handler.handle(order())
    created = handler.store.events_for(loops(handler)[0].loop_id)[0]
    assert created.detail["message_at"].startswith("2026-07-24T08:00:00")


# ------------------------------------------------------- one clock-skew policy
# Three findings with one root cause: an attacker-controlled timestamp consumed
# with no upper bound. Proved here on the wire, because each one is a sequence
# of real messages a coordinator would never see go wrong.


def _control_ids_on(handler, loop_id) -> list[str]:
    return [event.control_id for event in handler.store.events_for(loop_id)]


def _skewed_msh7(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y%m%d%H%M%S")


@pytest.mark.parametrize(
    "poison_at",
    ["99991231235959", _skewed_msh7(30), _skewed_msh7(2)],
    ids=["year_9999", "a_month_ahead", "two_days_ahead"],
)
def test_a_future_dated_result_cannot_deafen_a_loop_to_its_own_correction(handler, poison_at):
    """H1 on the wire, the clinically severe version, across the whole band.

    An ORU matching at tier 1 -- the placer number the ordering feed already
    knows -- dated in the future. The watermark is a max() over an append-only
    log, so once it runs ahead every later message for that loop is refused: the
    final report, the correction, the cancellation. A coordinator acknowledges
    what looks like a normal final read, the genuine amendment arrives saying
    the prior read was preliminary, and it is refused while the worklist goes on
    reporting the loop handled.

    Parametrised over the magnitude of the skew on purpose. An earlier fix gave
    year 9999 and `now + 30d` two different code paths -- refuse-message and
    drop-stamp -- and only the second was documented, so this test passed
    against a version that still failed for the band it was written to defend.
    """
    handler.handle(order())
    handler.handle(result(control_id="ORU_F", obx11="F", message_at="20260725120000"))
    loop_id = loops(handler)[0].loop_id
    handler.registry.acknowledge(
        loop_id, actor="coord1", role="coordinator", control_id="ACK1"
    )
    assert handler.store.replay(loop_id).state is LoopState.ACKNOWLEDGED

    handler.handle(
        result(control_id="ORU_POISON", obx11="F", value="poison", message_at=poison_at)
    )
    ack = handler.handle(
        result(control_id="ORU_CORR", obx11="C",
               value="3cm mass, prior read was preliminary",
               message_at="20260726120000")
    )

    assert ack_code(ack) == "AA"
    applied = _control_ids_on(handler, loop_id)
    assert "ORU_CORR" in applied, "the genuine amendment must be applied, not refused"
    assert handler.registry._latest_result_status(loop_id) == "C", (
        "and a read whose clock we refused to trust must not outrank it"
    )
    assert handler.registry.future_dated_message_count == 1, (
        "one policy, one counter, whatever the magnitude of the skew"
    )

    loop = handler.store.replay(loop_id)
    assert loop.state is LoopState.RESULTED
    assert not loop.ack_at, "safety rule 2 must clear the acknowledgement"
    assert handler.store.resulted_unacknowledged(MRN), "and put it back on a queue"


@pytest.mark.parametrize("msh7", ["", "X"])
def test_a_cancel_with_an_unreadable_msh7_cannot_close_a_watermarked_loop(handler, msh7):
    """H2. Omitting MSH-7 disabled the only anti-replay control in the system.

    `CANCELLED` appears in neither open_loops() nor resulted_unacknowledged(),
    so a cancel that lands on a scheduled loop takes a clinically open referral
    off every coordinator queue at once -- which is the failure this product
    exists to prevent, caused by the product.
    """
    handler.handle(order())
    handler.handle(scheduling("S1", "SIU^S12", placer=PLACER, message_at="20260725120000"))
    assert loops(handler)[0].state is LoopState.SCHEDULED

    ack = handler.handle(scheduling("S2", "SIU^S15", placer=PLACER, message_at=msh7))

    assert ack_code(ack) == "AA", "archived and routed for review, not retried forever"
    assert loops(handler)[0].state is LoopState.SCHEDULED
    assert handler.store.open_loops(MRN), "the loop must stay on a coordinator's queue"
    assert handler.stale_message_count == 1


def test_a_future_dated_order_still_ages_and_can_turn_stale(handler, monkeypatch):
    """M7. `staleness.age()` clamps a future `ordered_at` to zero -- correctly,
    the failure matrix says clamp and flag elsewhere -- so an OBR-7 in the
    future left `is_stale` permanently False and `staleness_ratio` permanently
    0.0, which the worklist sorts dead last. No STALE badge, no counter, no log
    line: on a queue of a few hundred the loop is functionally invisible. The
    clamp is not the bug; the missing flag is.
    """
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    now = datetime.now(timezone.utc)
    ahead = (now + timedelta(days=30)).strftime("%Y%m%d%H%M%S")

    handler.handle(order(observed_at=ahead))

    loop = loops(handler)[0]
    # Past this pack's `_default` threshold of 336h. A clamped loop reports 0.0
    # at every `now` there will ever be, so the assertion fails on the defect
    # rather than on the size of the window chosen here.
    later = now + timedelta(days=30)
    assert is_stale(loop, later, PACK) is True
    assert staleness_ratio(loop, later, PACK) > 1.0
    assert handler.future_dated_order_count == 1


def test_a_year_2099_order_still_ages_and_can_turn_stale(handler, monkeypatch):
    """The same defect one layer up: an OBR-7 no clock could produce is refused
    by the parse, and the loop then ages from ingest like any order whose OBR-7
    was absent -- visible, rather than pinned to the bottom of the queue."""
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    handler.handle(order(observed_at="20991231120000"))

    loop = loops(handler)[0]
    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert is_stale(loop, later, PACK) is True
    assert staleness_ratio(loop, later, PACK) > 1.0
    assert handler.future_dated_order_count == 1, (
        "and the flag the staleness docstring promised must fire for the "
        "reported OBR-7, not only for skew small enough to survive the parse"
    )


# ------------------------------------------------------------------- MLLP wire


@contextmanager
def running_server(handler, **kwargs):
    server = make_mllp_server(handler, host="127.0.0.1", port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def read_ack(sock: socket.socket) -> str:
    sock.settimeout(10)
    buffer = b""
    while not buffer.endswith(FS + CR):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    return buffer.decode("utf-8", errors="replace")


def test_server_binds_loopback_by_default(handler):
    """Found by mutation: changing the default to 0.0.0.0 passed the whole
    suite, because the only test asserting loopback *passed host explicitly*.
    It was checking that the value it had just supplied came back. The default
    is the claim -- "v1 makes no outbound connection to anyone, us included",
    and a PHI-bearing port on every interface is the ingress half of that -- so
    the default is what has to be exercised. port=0 only, host omitted.
    """
    server = make_mllp_server(handler, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_an_explicit_host_is_still_honoured(handler):
    with running_server(handler) as (host, _port):
        assert host == "127.0.0.1"


def test_a_message_delivered_over_the_wire_is_acked_and_applied(handler):
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(frame(order()))
            assert "|AA|" in read_ack(sock)
    assert len(loops(handler)) == 1


def test_two_messages_in_one_tcp_segment_are_both_processed(handler):
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(frame(order()) + frame(result()))
            acks = b""
            sock.settimeout(10)
            while acks.count(FS + CR) < 2:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                acks += chunk
    assert acks.decode().count("|AA|") == 2
    assert loops(handler)[0].state is LoopState.RESULTED


def test_a_message_split_across_tcp_segments_is_reassembled(handler):
    payload = frame(order())
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            for start in range(0, len(payload), 7):
                sock.sendall(payload[start:start + 7])
                time.sleep(0.001)
            assert "|AA|" in read_ack(sock)
    assert len(loops(handler)) == 1


def test_a_connection_closing_mid_message_acknowledges_nothing(handler):
    payload = frame(order())
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(payload[: len(payload) // 2])
            time.sleep(0.05)
        time.sleep(0.2)
    assert loops(handler) == [], "a half-delivered order must not create a loop"
    assert handler.store.raw_count() == 0


def test_a_body_carrying_fs_cr_is_rejected_not_answered_aa_on_its_truncated_half(handler):
    """The failure this subsystem exists to prevent. `mllp.frame` refuses to
    *build* such a message; nothing stops one arriving on the wire."""
    body = order().replace("DOE^JANE", "DOE" + FS.decode("latin-1") + CR.decode("latin-1") + "JANE")
    hostile = VT + body.encode("utf-8") + FS + CR
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(hostile)
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert "|AA|" not in ack
    assert loops(handler) == [], "no truncated half may be applied"
    assert handler.framing_error_count == 1


def test_a_body_carrying_fs_cr_is_rejected_when_the_remainder_has_not_arrived(handler):
    """Found by mutation: deleting the CR-termination check passed the whole
    suite, because the other FS CR test happened to deliver both halves in one
    TCP segment and was caught by the next-frame-must-start-with-VT rule
    instead. Here only the truncated half is on the wire when the ACK is due,
    which is the case that rule cannot see -- and the case a real sender
    produces whenever the message straddles a segment boundary."""
    body = order().replace("DOE^JANE", "DOE" + FS.decode("latin-1") + CR.decode("latin-1") + "JANE")
    hostile = VT + body.encode("utf-8") + FS + CR
    first_half = hostile[: hostile.index(FS + CR) + 2]

    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(first_half)          # the remainder is still in the sender
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert "|AA|" not in ack
    assert loops(handler) == []
    assert handler.framing_error_count == 1


def _cr_fs_cr_hostile() -> tuple[bytes, int]:
    """A body carrying `CR FS CR`, and the offset the wire splits it at.

    The half ends with CR, so the CR-termination check passes it -- this is the
    one shape that defeats every in-frame check.
    """
    body = order().replace("DOE^JANE", "DOE\r" + FS.decode("latin-1") + CR.decode("latin-1") + "JANE")
    hostile = VT + body.encode("utf-8") + FS + CR
    return hostile, hostile.index(FS + CR) + 2


def test_a_cr_terminated_embedded_fs_cr_is_refused_when_both_halves_are_on_the_wire(handler):
    """A sender emits one message with one write, so both halves arrive with no
    ACK between them. Previously measured `acks=['AA','AR'], loops:1` -- the
    half applied and acknowledged. It must now be refused outright."""
    hostile, _ = _cr_fs_cr_hostile()
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(hostile)
            ack = read_ack(sock)
    assert "|AA|" not in ack, "the truncated half must not be acknowledged"
    assert "|AR|" in ack
    assert loops(handler) == [], "and it must not be applied"
    assert handler.framing_error_count == 1


def test_a_cr_terminated_embedded_fs_cr_is_refused_when_the_remainder_is_still_in_flight(handler):
    """The case the pre-fix reader could not see, and the reason for the grace
    window. The two halves land in separate `recv` calls, so at the instant the
    frame completes there is nothing behind it -- but the remainder is part of
    the same sender write and is already in flight, so waiting a bounded moment
    finds it. No ACK is read between the two writes: a sender splitting its own
    message does not stop to wait for one.

    The 5ms pause is an absolute figure, not a fraction of the grace: derived
    from the constant it would shrink with it, the two halves would land in one
    `recv`, and check 4 would catch them from the buffer without the lookahead
    ever running -- so the test would keep passing against a default of zero.
    Found by mutation (M11), which is exactly how it read before.
    """
    assert DESYNC_GRACE_SECONDS >= 0.02, (
        "the default grace must cover a real network hop, or the guard is decorative"
    )
    hostile, cut = _cr_fs_cr_hostile()
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(hostile[:cut])
            time.sleep(0.005)
            sock.sendall(hostile[cut:])
            ack = read_ack(sock)
    assert "|AA|" not in ack, "the truncated half must not be acknowledged"
    assert "|AR|" in ack
    assert loops(handler) == []
    assert handler.suspect_truncation_count == 0, "refused up front, not flagged after"
    assert handler.framing_error_count == 1

    # AR means the engine will not redeliver, so the archive is the only
    # remaining copy of this clinical message. It has to hold all of it --
    # both the half that completed the frame and the bytes behind it.
    archived = "".join(handler.store.raw_payloads())
    assert "MRN123456" in archived, "the half that arrived is not in the archive"
    assert "ORC|NW|PLACER987" in archived, "the discarded remainder is not in the archive"


def test_a_desync_flags_the_frame_acknowledged_immediately_before_it(handler):
    """One whole message, then a `CR FS CR` truncation, in a single write.

    The first message is genuinely whole and is genuinely acknowledged before
    the desync is visible, so nothing can un-send that `AA`. What is still owed
    is the flag: once the stream proves desynchronised, the reader cannot claim
    the frame it just accepted was whole either. False alarms are accepted here
    on the same asymmetry the matcher uses -- a wasted look at an archived
    message costs less than a silently truncated result.
    """
    hostile, _ = _cr_fs_cr_hostile()
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(frame(order("WHOLE1")) + hostile)
            acks = b""
            sock.settimeout(10)
            while acks.count(FS + CR) < 2:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                acks += chunk
    decoded = acks.decode("utf-8", errors="replace")
    assert decoded.count("|AA|") == 1, "only the whole message is acknowledged"
    assert "|AR|" in decoded, "the truncation is refused"
    assert handler.suspect_truncation_count == 1, "and WHOLE1 is flagged as suspect"
    assert len(loops(handler)) == 1, "the truncated half created nothing"


def test_a_remainder_delayed_past_the_grace_window_is_the_narrowed_residual(handler):
    """What the grace window does NOT close, pinned so nobody overclaims it.

    The sender here withholds the remainder until it has read an ACK for the
    half -- longer than any bounded wait can cover. The half is accepted, and
    the requirement falls back to what it was before: it must not be silent.
    When the remainder arrives and proves the stream desynchronised, the
    already-acknowledged message is flagged by control id so a human can pull
    the raw and compare.

    `desync_grace` is set to a value the delay provably exceeds rather than
    relying on the default, so this test states the residual instead of racing
    the scheduler for it.
    """
    hostile, cut = _cr_fs_cr_hostile()
    with running_server(handler, desync_grace=0.01) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(hostile[:cut])
            first = read_ack(sock)          # the sender waits for this
            sock.sendall(hostile[cut:])
            time.sleep(0.3)

    assert "|AA|" in first, "documented residual: a stalled remainder is not caught in time"
    assert handler.suspect_truncation_count == 1, "but it must not be silent"
    assert handler.framing_error_count == 1, "and the remainder is rejected AR"


def test_the_grace_window_is_what_closes_the_cr_fs_cr_hole(handler):
    """Turning the lookahead off reproduces the original defect exactly.

    Without this, a mutation that made `desync_grace=0` (or any value) behave
    like the default would pass -- the guard would be untestable from outside.
    It also pins the opt-out as a real, documented setting rather than dead
    configuration: a site trading this defence for throughput gets the
    pre-fix behaviour, and gets it knowingly.
    """
    hostile, cut = _cr_fs_cr_hostile()
    with running_server(handler, desync_grace=0.0) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(hostile[:cut])
            first = read_ack(sock)
            sock.sendall(hostile[cut:])
            time.sleep(0.3)
    assert "|AA|" in first
    assert len(loops(handler)) == 1, "the original defect, reproduced with the guard off"


def test_the_configured_grace_is_the_one_that_is_actually_waited(handler):
    """Found by mutation (M10): hard-coding DESYNC_GRACE_SECONDS inside the
    lookahead, ignoring the per-server setting, passed every other test --
    because every other test's remainder either arrives instantly or not at
    all, so any positive window behaves the same.

    Here the remainder is delayed by three times the default and the server is
    configured to wait far longer than that. Only a reader honouring its own
    setting still has the window open when those bytes land.
    """
    delay = 3 * DESYNC_GRACE_SECONDS
    hostile, cut = _cr_fs_cr_hostile()
    with running_server(handler, desync_grace=delay + 1.0) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(hostile[:cut])
            time.sleep(delay)
            sock.sendall(hostile[cut:])
            ack = read_ack(sock)
    assert "|AA|" not in ack, "the configured window was not honoured"
    assert "|AR|" in ack
    assert loops(handler) == []


def test_the_desync_lookahead_does_not_deadlock_a_sender_waiting_on_the_ack(handler):
    """The failure mode the lookahead could have introduced, and the reason it
    is a bounded `select` rather than "read until the next frame".

    An MLLP sender blocks on the ACK before writing its next message, so bytes
    that would resolve the ambiguity will never come. The reader must give up
    and answer. Three messages in sequence, each written only after the previous
    ACK came back: if the wait were unbounded this hangs on the first one.
    """
    deadline = 3 * (DESYNC_GRACE_SECONDS + 2.0)
    started = time.monotonic()
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            for index, control_id in enumerate(("SEQ1", "SEQ2", "SEQ3")):
                sock.sendall(frame(order(control_id, placer=f"P{index}", filler=f"F{index}")))
                assert "|AA|" in read_ack(sock), f"{control_id} was never acknowledged"
    assert time.monotonic() - started < deadline
    assert len(loops(handler)) == 3


def test_a_peer_that_half_closes_after_a_frame_is_still_acknowledged(handler):
    """End-of-stream during the lookahead means "nothing more is coming", not
    "give up on the frame in hand". A sender that shuts down its write side
    immediately after the message is still owed its ACK."""
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(frame(order()))
            sock.shutdown(socket.SHUT_WR)
            ack = read_ack(sock)
    assert "|AA|" in ack
    assert len(loops(handler)) == 1


def test_pipelined_frames_do_not_each_pay_the_grace_window(handler):
    """Only the last frame of a delivery has an empty buffer behind it, so only
    that one waits. A lookahead that ran unconditionally would multiply the
    latency of every pipelined batch by its size."""
    payload = b"".join(
        frame(order(cid, placer=f"PP{i}", filler=f"FF{i}"))
        for i, cid in enumerate(("PIPE1", "PIPE2", "PIPE3", "PIPE4"))
    )
    # An exaggerated grace, so "one wait" and "four waits" are 0.4s apart
    # rather than 0.15s apart and the assertion is not racing the scheduler.
    grace = 0.4
    with running_server(handler, desync_grace=grace) as address:
        with socket.create_connection(address, timeout=10) as sock:
            started = time.monotonic()      # server startup is not the thing measured
            sock.sendall(payload)
            acks = b""
            sock.settimeout(10)
            while acks.count(FS + CR) < 4:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                acks += chunk
            elapsed = time.monotonic() - started
    assert acks.decode().count("|AA|") == 4
    assert elapsed < 2 * grace, (
        f"four pipelined frames took {elapsed:.3f}s against a {grace}s grace; "
        "only the last frame may wait"
    )


def test_a_slow_legitimate_message_split_inside_its_terminator_is_still_accepted(handler):
    """The grace window must not turn a slow sender into a rejected one. Split
    between the FS and the CR -- the frame only completes when the second write
    lands, and the delay exceeds the window -- and it is still one good
    message."""
    payload = frame(order())
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(payload[:-1])
            time.sleep(3 * DESYNC_GRACE_SECONDS)
            sock.sendall(payload[-1:])
            ack = read_ack(sock)
    assert "|AA|" in ack
    assert len(loops(handler)) == 1


def test_an_empty_frame_over_the_wire_is_rejected(handler):
    """VT FS CR carries no MSH at all. `deframe` returns "" for it happily, so
    the refusal has to come from the body-must-end-with-CR check."""
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(VT + FS + CR)
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert "|AA|" not in ack
    assert handler.framing_error_count == 1


def test_a_body_carrying_a_bare_fs_with_no_cr_after_it_is_rejected(handler):
    """The frame terminates at the sender's real FS CR, so the body reaches
    `deframe` with the stray FS still in it. Nothing is truncated here -- the
    point is that it is refused rather than accepted as message content."""
    body = order().replace("DOE^JANE", "DOE" + FS.decode("latin-1") + "JANE")
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(VT + body.encode("utf-8") + FS + CR)
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert "|AA|" not in ack
    assert loops(handler) == []
    assert handler.framing_error_count == 1


def test_a_start_block_inside_the_body_is_rejected(handler):
    """Found by mutation: deleting this check passed the whole suite. A VT
    inside a body is a start block where there cannot be one, so the reader
    can no longer say which VT began the message it is holding."""
    body = order().replace("DOE^JANE", "DOE" + VT.decode("latin-1") + "JANE")
    hostile = VT + body.encode("utf-8") + FS + CR
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(hostile)
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert loops(handler) == []


def test_a_malformed_frame_is_answered_ar_and_the_raw_is_still_archived(handler):
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(b"no start block here" + FS + CR)
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert handler.store.raw_count() == 1, "failure matrix: AR, archive raw, alert"
    assert handler.store.raw_payloads()[0].startswith("no start block")


def test_undecodable_bytes_are_rejected_and_archived_losslessly(handler):
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(VT + b"MSH|\xff\xfe" + CR + FS + CR)
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert handler.store.raw_count() == 1


def test_trailing_bytes_that_do_not_start_a_new_frame_are_rejected(handler):
    """And the frame *before* them is not applied either. Once the stream is
    desynchronised the reader cannot tell that the first frame was whole, so
    accepting it would be the same guess this check exists to refuse."""
    with running_server(handler) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(frame(order()) + b"garbage")
            ack = read_ack(sock)
    assert "|AR|" in ack
    assert "|AA|" not in ack
    assert loops(handler) == []


def test_an_oversized_frame_is_rejected_rather_than_buffered_forever(handler):
    with running_server(handler, max_frame_bytes=4096) as address:
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(VT + b"MSH|" + b"x" * 8192)
            ack = read_ack(sock)
    assert "|AR|" in ack


def test_two_messages_in_one_frame_are_refused(handler):
    """Two MSH segments inside one MLLP frame is a framing fault, and taking
    the last one would archive the message under a control id the sender did
    not use for the first."""
    doubled = order(control_id="ORM_1") + order(control_id="ORM_2")
    ack = handler.handle(doubled)
    assert ack_code(ack) == "AR"
    assert loops(handler) == []


# ------------------------------------------------------------------ concurrency


def test_two_connections_delivering_the_same_result_produce_one_transition(handler):
    handler.handle(order())
    acks: list[str] = []
    barrier = threading.Barrier(2)

    def deliver(control_id):
        barrier.wait()
        acks.append(handler.handle(result(control_id=control_id)))

    threads = [threading.Thread(target=deliver, args=(f"ORU_{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert [ack_code(a) for a in acks] == ["AA", "AA"]
    loop_id = loops(handler)[0].loop_id
    assert events_of(handler, loop_id).count("resulted") == 1
    assert handler.duplicate_content_key_count == 1


def test_two_connections_delivering_different_results_both_land(handler):
    handler.handle(order(control_id="ORM_1", placer="P1", filler="F1"))
    handler.handle(order(control_id="ORM_2", placer="P2", filler="F2"))
    barrier = threading.Barrier(2)

    def deliver(control_id, placer, filler):
        barrier.wait()
        handler.handle(result(control_id=control_id, placer=placer, filler=filler))

    threads = [
        threading.Thread(target=deliver, args=("ORU_1", "P1", "F1")),
        threading.Thread(target=deliver, args=("ORU_2", "P2", "F2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(loop.state for loop in loops(handler)) == [
        LoopState.RESULTED, LoopState.RESULTED
    ]
    assert handler.duplicate_content_key_count == 0


# ------------------------------------------------------------------- file drop


def test_file_drop_drains_and_removes_accepted_messages(handler, tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "1.hl7").write_text(order(), encoding="utf-8")
    (drop / "2.hl7").write_text(result(), encoding="utf-8")

    source = FileDropSource(handler, drop)
    assert source.drain() == 2
    assert list(drop.glob("*.hl7")) == []
    assert loops(handler)[0].state is LoopState.RESULTED


def test_file_drop_processes_in_name_order(handler, tmp_path):
    """The result must not be drained before the order it closes."""
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "02-result.hl7").write_text(result(), encoding="utf-8")
    (drop / "01-order.hl7").write_text(order(), encoding="utf-8")
    FileDropSource(handler, drop).drain()
    assert orphans(handler) == []


def test_file_drop_leaves_a_file_that_could_not_be_stored(handler, tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "1.hl7").write_text(order(), encoding="utf-8")
    db_path = Path(handler.store.db_path)
    db_path.unlink()
    db_path.mkdir()

    source = FileDropSource(handler, drop)
    assert source.drain() == 0
    assert source.deferred_count == 1
    assert (drop / "1.hl7").exists(), "an AE file must survive for the next drain"


def test_file_drop_quarantines_a_malformed_file(handler, tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    bad = drop / "1.hl7"
    bad.write_bytes(VT + b"MSH|no end block")
    source = FileDropSource(handler, drop)
    assert source.drain() == 0
    assert source.rejected_count == 1
    assert not bad.exists()
    assert list(drop.glob("*.rejected"))


def test_file_drop_accepts_mllp_framed_files(handler, tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "1.hl7").write_bytes(frame(order()))
    assert FileDropSource(handler, drop).drain() == 1
    assert len(loops(handler)) == 1


def test_replaying_the_archive_reproduces_the_same_state(tmp_path):
    """Spec section 7: a pack revision is evaluated by replaying the archive,
    so the archive has to be sufficient on its own."""
    live_store = LoopStore(tmp_path / "live.db")
    live = MessageHandler(store=live_store, registry=Registry(live_store), pack=PACK)
    for text in (order(), scheduling("S1", "SIU^S12", placer=PLACER), result()):
        live.handle(text)

    replay_store = LoopStore(tmp_path / "replay.db")
    replayed = MessageHandler(store=replay_store, registry=Registry(replay_store), pack=PACK)
    for payload in live_store.raw_payloads():
        replayed.handle(payload)

    assert [loop.state for loop in loops(live)] == [loop.state for loop in loops(replayed)]
    assert loops(replayed)[0].state is LoopState.RESULTED


# ------------------------------------------------------------------ no egress


def test_the_listener_makes_no_outbound_connection(handler, monkeypatch):
    """Spec test 12, scoped to this module: handling a full message set must
    open no socket at all."""
    real_connect = socket.socket.connect

    def refuse(self, address):  # pragma: no cover - the assertion is the point
        raise AssertionError(f"listener attempted an outbound connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    for text in (order(), scheduling("S1", "SIU^S12", placer=PLACER), result(), merge("A40_1")):
        handler.handle(text)
    monkeypatch.setattr(socket.socket, "connect", real_connect)
    assert len(loops(handler)) == 1


def test_stale_future_dated_observation_is_accepted_not_dropped(handler):
    """Failure matrix: future-dated observation -> accept, clamp for staleness
    math, flag. Accepting is this module's half."""
    future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y%m%d%H%M%S")
    handler.handle(order(message_at=future))
    assert len(loops(handler)) == 1
