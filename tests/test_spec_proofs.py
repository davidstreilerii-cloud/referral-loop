"""The nineteen spec tests and the six success criteria, on the assembled system.

Every other module in this suite proves its own properties. This file proves the
spec's, and the difference is not pedantic: section 9 makes claims about *the
system* -- "a result carrying the retired MRN still matches", "no PHI in
artifacts", "the full suite passes with egress blocked" -- and a claim about the
system is only tested at the level the system is assembled. A registry test that
calls `record_result` directly has skipped ingest, resolution and matching, which
is precisely where three of the nine safety tests live.

So the proofs here go in through `cli.boot()` with the **shipped, signature-
verified** pack, deliver HL7 over a **real MLLP socket**, and act through the
worklist's **HTTP surface**. Nothing is hand-constructed that the product would
construct itself.

**Every proof has a companion that breaks the property and asserts the same
proof goes red.** This is not ceremony. Over sixteen tasks this build has
shipped, and later caught, a loopback test that passed `host` explicitly and
asserted on its own argument; a tier test that passed `match_tier=3` and
asserted the tier was 3; an import-closure test that passed with the forbidden
module loaded; a durability test that passed with `synchronous=OFF`. Each was a
green assertion that could not fail. The `_can_fail` companions are the only
thing standing between this file and the same outcome, so a proof is written as
a plain function and called twice -- once clean, once with the property
deliberately broken inside `pytest.raises(AssertionError)`.

Where an existing test already proves one of the nineteen end to end -- spec 15's
real-OS-process kill in `test_listener.py`, spec 14's audit-disk grep in
`test_audit.py` -- this file does not duplicate it. It fills what was missing:
the system-level chain, and the properties nobody had asserted at all.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from referral_loop import audit as audit_module
from referral_loop import registry as registry_module
from referral_loop import store as store_module
from referral_loop.audit import referral_audit_entries
from referral_loop.cli import PUBKEY_ENV, boot
from referral_loop.core import machine as machine_module
from referral_loop.core.states import DocumentationStatus
from referral_loop.errors import PackVerificationError
from referral_loop.eval import (
    check_release_criteria,
    format_report,
    replay,
    synthetic_corpus,
)
from referral_loop.events import Loop, LoopEvent, LoopState
from referral_loop.listener import MessageHandler, make_mllp_server
from referral_loop.mllp import CR, FS, frame
from referral_loop.pack import RulePack, load_pack
from referral_loop.peers import PeerRegistry
from referral_loop.registry import Registry
from referral_loop.store import LoopStore
from referral_loop.worklist import create_app
from tests import spec_guards

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_PACK_DIR = REPO_ROOT / "src" / "referral_loop" / "rules"

# The key the shipped pack was signed with. Pinned as a literal in
# test_boot_gates and test_eval too; if it rotates all three fail together,
# which is the correct blast radius for a signing key.
SHIPPED_PUBKEY = "adb7af9938740d48d237fc2e191c7a52d41000654d7e8335d08edd51a97ed105"


# ============================================================ synthetic HL7

# Fixtures are synthetic, generated from the HL7 v2 specification (spec section
# 9). The identifiers are sentinels rather than plausible values so that spec
# test 14's grep has something unambiguous to look for: "ZZSENTINELMRN" cannot
# appear in an artifact by coincidence the way "12345" can.
S_MRN = "ZZSENTINELMRNAAA"
S_MRN_SURVIVOR = "ZZSENTINELMRNBBB"
S_MRN_THIRD = "ZZSENTINELMRNCCC"
S_MRN_OTHER = "ZZSENTINELMRNDDD"
S_NAME = "ZZSENTINELNAME"
S_KIN = "ZZSENTINELKIN"
S_GUARANTOR = "ZZSENTINELGUARANTOR"
S_NOTE = "ZZSENTINELNOTE"

SENTINELS = {
    "PID-3 MRN": S_MRN,
    "PID-3 surviving MRN": S_MRN_SURVIVOR,
    "PID-5 patient name": S_NAME,
    "NK1-2 next of kin": S_KIN,
    "GT1-3 guarantor": S_GUARANTOR,
    "OBX-5 / NTE-3 note text": S_NOTE,
}

CT_SERVICE = "71260^CT CHEST W CONTRAST^C4"
PROVIDER = "PRV001^SYNTHETIC^ORDERER"
ORDERED_AT = "20260720080000"
OBSERVED_AT = "20260720140000"      # six hours later, inside the CT window
LATER_AT = "20260720160000"


def _fields(values: dict[int, str]) -> list[str]:
    width = max(values) if values else 0
    out = [""] * (width + 1)
    for index, value in values.items():
        out[index] = value
    return out


def seg(seg_id: str, values: dict[int, str]) -> str:
    """`seg("OBR", {2: "PL1"})` -> an OBR whose *second* field is PL1.

    Field-by-index rather than a pipe-delimited literal. Hand-counting to OBR-16
    is how the ordering-provider tie-break silently became a no-op once already
    in this build; a builder makes the field number the thing the test states.
    """
    fields = _fields(values)
    fields[0] = seg_id
    return "|".join(fields)


def msh(message_type: str, control_id: str, message_at: str) -> str:
    """MSH-1 *is* the field separator, so MSH fields shift by one."""
    fields = _fields({
        2: r"^~\&", 3: "EHR", 4: "HOSP", 5: "RIS", 6: "HOSP",
        7: message_at, 9: message_type, 10: control_id, 11: "P", 12: "2.5.1",
    })
    return "MSH|" + "|".join(fields[2:])


def hl7(*segments: str) -> str:
    return "".join(s + "\r" for s in segments)


def pid(mrn: str) -> str:
    return seg("PID", {1: "1", 3: f"{mrn}^^^HOSP^MR", 5: f"{S_NAME}^JANE",
                       7: "19800101", 8: "F"})


def nk1() -> str:
    return seg("NK1", {1: "1", 2: f"{S_KIN}^JOHN", 3: "SPO", 4: "555 ELM ST"})


def gt1() -> str:
    return seg("GT1", {1: "1", 3: f"{S_GUARANTOR}^JOHN", 6: "555 ELM ST"})


def order(control_id: str, *, mrn: str = S_MRN, placer: str = "PL1",
          filler: str = "ACC1", service: str = CT_SERVICE,
          ordered_at: str = ORDERED_AT, accession_field: int = 3) -> str:
    """An ORM^O01. `accession_field` exists for spec test 18 and nothing else."""
    obr = {1: "1", 2: placer, 4: service, 7: ordered_at, 16: PROVIDER}
    obr[accession_field] = filler
    return hl7(
        msh("ORM^O01", control_id, ordered_at),
        pid(mrn), nk1(), gt1(),
        seg("ORC", {1: "NW", 2: placer}),
        seg("OBR", obr),
    )


def result(control_id: str, *, mrn: str = S_MRN, placer: str = "PL1",
           filler: str = "ACC1", service: str = CT_SERVICE, obx11: str = "F",
           observed_at: str = OBSERVED_AT, message_at: str | None = None,
           accession_field: int = 3) -> str:
    obr = {1: "1", 2: placer, 4: service, 7: observed_at, 16: PROVIDER}
    obr[accession_field] = filler
    return hl7(
        msh("ORU^R01", control_id, message_at or observed_at),
        pid(mrn), nk1(), gt1(),
        seg("OBR", obr),
        seg("OBX", {1: "1", 2: "TX", 3: service, 5: S_NOTE, 11: obx11}),
        seg("NTE", {1: "1", 3: S_NOTE}),
    )


def merge_message(control_id: str, *, prior: str, surviving: str,
                  message_at: str = LATER_AT) -> str:
    return hl7(
        msh("ADT^A40", control_id, message_at),
        pid(surviving), nk1(), gt1(),
        seg("MRG", {1: f"{prior}^^^HOSP^MR"}),
    )


# ================================================================ the system


class System:
    """The assembled product: booted stack plus its HTTP surface.

    Constructed by `cli.boot`, so the encryption gate, the pack signature gate
    and the thresholds gate all ran. A test that built a `LoopStore` and a
    `Registry` by hand would be testing the same objects with the gates skipped.
    """

    def __init__(self, stack, client):
        self.stack = stack
        self.store: LoopStore = stack.store
        self.registry: Registry = stack.registry
        self.handler: MessageHandler = stack.handler
        self.pack: RulePack = stack.pack
        self.http = client

    # ---- ingress

    @contextmanager
    def _serving(self):
        server = make_mllp_server(self.handler, port=0,
                                  peers=PeerRegistry.plaintext_loopback())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)

    def over_the_wire(self, *messages: str) -> list[str]:
        """Deliver over a real MLLP socket and return the ACKs.

        The socket matters. Framing, the ACK the engine actually receives and
        the persist-before-ACK ordering are all properties of the wire path, and
        an in-process `handler.handle()` call exercises none of them.
        """
        acks = []
        with self._serving() as address:
            for message in messages:
                with socket.create_connection(address, timeout=15) as sock:
                    sock.sendall(frame(message))
                    buffer = b""
                    while not buffer.endswith(FS + CR):
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                    acks.append(buffer.decode("utf-8", errors="replace"))
        return acks

    def accepted(self, *messages: str) -> list[str]:
        acks = self.over_the_wire(*messages)
        for message, ack in zip(messages, acks):
            assert "|AA|" in ack, f"the engine was not told to stop retrying: {ack!r}"
        return acks

    # ---- reading what happened

    def loops(self) -> list[Loop]:
        return sorted(self.store.all_loops(), key=lambda loop: loop.loop_id)

    def in_state(self, state: LoopState) -> list[Loop]:
        return [loop for loop in self.loops() if loop.state is state]

    def only(self, **kwargs) -> Loop:
        found = [loop for loop in self.loops()
                 if all(getattr(loop, k) == v for k, v in kwargs.items())]
        assert len(found) == 1, f"expected exactly one loop matching {kwargs}, got {found}"
        return found[0]

    def events(self, loop_id: str) -> list[LoopEvent]:
        return self.store.events_for(loop_id)

    def event_detail(self, loop_id: str, event_type: str) -> dict:
        for event in self.events(loop_id):
            if event.event_type == event_type:
                return dict(event.detail)
        raise AssertionError(
            f"{loop_id} has no {event_type!r} event; it has "
            f"{[e.event_type for e in self.events(loop_id)]}"
        )

    def queues(self) -> dict:
        response = self.http.get("/worklist/?format=json")
        assert response.status_code == 200, response.data
        return response.get_json()["queues"]

    def queued_ids(self) -> set[str]:
        return {row["loop_id"] for rows in self.queues().values() for row in rows}


@pytest.fixture()
def system(tmp_path, monkeypatch) -> System:
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")
    monkeypatch.setenv(PUBKEY_ENV, SHIPPED_PUBKEY)
    stack = boot(
        db_path=tmp_path / "loops.db",
        pack_dir=SHIPPED_PACK_DIR,
        public_key_hex=SHIPPED_PUBKEY,
    )
    app = create_app(store=stack.store, registry=stack.registry, pack=stack.pack)
    app.config["TESTING"] = True
    return System(stack, app.test_client())


@pytest.fixture()
def shipped_pack() -> RulePack:
    """The pack through its real signature path, not a dict lifted out of JSON."""
    return load_pack(SHIPPED_PACK_DIR, bytes.fromhex(SHIPPED_PUBKEY))


def ack(system: System, loop_id: str, **body):
    payload = {"actor": "coordinator-a", "role": "referral_coordinator"}
    payload.update(body)
    return system.http.post(f"/worklist/{loop_id}/acknowledge", json=payload)


def act(system: System, loop_id: str, action: str, **body):
    payload = {"actor": "coordinator-a", "role": "referral_coordinator"}
    payload.update(body)
    return system.http.post(f"/worklist/{loop_id}/{action}", json=payload)


# ========================================= 1. preliminary never resolves


def _proof_1(system: System) -> None:
    """A preliminary read reaches RESULTED and stops there, through the product."""
    system.accepted(order("ORD-1"), result("RES-1", obx11="P"))

    loop = system.only(state=LoopState.RESULTED)
    # Positive control on the ingest half: the P actually arrived and was read
    # off the message rather than defaulted. Without this the proof passes on a
    # pipeline that dropped the result entirely.
    assert system.event_detail(loop.loop_id, "resulted")["obx11"] == "P"
    assert loop.loop_id in system.queued_ids(), "the loop must still be visible to a human"

    response = ack(system, loop.loop_id)
    assert response.status_code >= 400, (
        f"a preliminary read was acknowledged ({response.status_code})"
    )
    assert system.store.replay(loop.loop_id).state is LoopState.RESULTED
    assert system.in_state(LoopState.ACKNOWLEDGED) == []


def test_spec_1_preliminary_never_resolves_end_to_end(system):
    _proof_1(system)


def test_spec_1_the_same_chain_with_a_final_result_does_acknowledge(system):
    """The other half of the control. Without it, `_proof_1` is also passed by a
    worklist whose acknowledge endpoint refuses everything."""
    system.accepted(order("ORD-1"), result("RES-1", obx11="F"))
    loop = system.only(state=LoopState.RESULTED)
    assert ack(system, loop.loop_id).status_code < 400
    assert system.store.replay(loop.loop_id).state is LoopState.ACKNOWLEDGED


def test_spec_1_can_fail(system, monkeypatch):
    """Let P through the reconcilable-documentation gate; the proof must go red.

    Repointed in Plan 2b Task 5. This sabotaged registry._ACKNOWLEDGEABLE_STATUSES until
    that frozenset stopped being the enforcement point -- spec rule 1 is now
    machine._RECONCILABLE_DOCUMENTATION, fed by registry._documentation's fold. Sabotaging
    the dead constant left this companion toothless and proof 1 green for a reason nobody
    had checked, which for the artifact a governance committee reads is worse than an
    absent proof: a claim with evidence that does not support it.

    The harness caught it by construction -- a companion whose target is dead fails with
    DID NOT RAISE rather than passing quietly. That property is why the sabotage must
    always name the live enforcement point and never a convenient proxy for it.
    """
    monkeypatch.setattr(
        machine_module, "_RECONCILABLE_DOCUMENTATION",
        frozenset({DocumentationStatus.FINAL, DocumentationStatus.CORRECTED,
                   DocumentationStatus.PRELIMINARY}),
    )
    with pytest.raises(AssertionError):
        _proof_1(system)


# ============================================= 2. corrected result reopens


def _proof_2(system: System) -> None:
    system.accepted(order("ORD-1"), result("RES-1", obx11="F"))
    loop_id = system.only(state=LoopState.RESULTED).loop_id
    assert ack(system, loop_id).status_code < 400
    acknowledged = system.store.replay(loop_id)
    assert acknowledged.state is LoopState.ACKNOWLEDGED
    assert acknowledged.ack_by, "nothing was acknowledged, so nothing can reopen"

    system.accepted(result("RES-2", obx11="C", observed_at=LATER_AT))

    reopened = system.store.replay(loop_id)
    assert reopened.state is LoopState.RESULTED, "a correction did not reopen the loop"
    assert reopened.ack_by == "", "the stale acknowledgement survived the correction"
    assert loop_id in system.queued_ids(), "the reopened loop is on no queue"


def test_spec_2_corrected_result_reopens_end_to_end(system):
    _proof_2(system)


def test_spec_2_can_fail(system, monkeypatch):
    """Treat C as an ordinary repeat result. The proof must notice."""
    real = Registry.record_result

    def swallow_corrections(self, loop_id, obx11, control_id, **kwargs):
        if obx11 == registry_module.CORRECTED:
            return None
        return real(self, loop_id, obx11, control_id, **kwargs)

    monkeypatch.setattr(Registry, "record_result", swallow_corrections)
    with pytest.raises(AssertionError):
        _proof_2(system)


# ================================================== 3. merge carries loops


def _proof_3(system: System) -> None:
    system.accepted(
        order("ORD-1", placer="PL1", filler="ACC1"),
        order("ORD-2", placer="PL2", filler="ACC2"),
        order("ORD-3", mrn=S_MRN_OTHER, placer="PL3", filler="ACC3"),
    )
    before = {loop.loop_id for loop in system.loops() if loop.mrn == S_MRN}
    assert len(before) == 2, f"the fixture did not open two loops on {S_MRN}: {before}"

    system.accepted(merge_message("A40-1", prior=S_MRN, surviving=S_MRN_SURVIVOR))

    moved = {loop.loop_id for loop in system.loops() if loop.mrn == S_MRN_SURVIVOR}
    assert moved == before, f"loops did not all move: {before} -> {moved}"
    assert [loop for loop in system.loops() if loop.mrn == S_MRN] == []
    assert system.in_state(LoopState.ORPHAN) == [], "a merge orphaned a loop"
    # The other patient is untouched, so "moved everything" is not "moved every
    # loop in the database".
    assert system.only(mrn=S_MRN_OTHER).state is LoopState.OPEN
    assert before <= system.queued_ids(), "a merged loop left the worklist"


def test_spec_3_merge_carries_every_open_loop_end_to_end(system):
    _proof_3(system)


def test_spec_3_can_fail(system, monkeypatch):
    """Move only the first loop -- the shape a `break` in the wrong place makes."""
    real = LoopStore.loops_for_mrn

    def only_the_first(self, mrn):
        return real(self, mrn)[:1]

    monkeypatch.setattr(LoopStore, "loops_for_mrn", only_the_first)
    with pytest.raises(AssertionError):
        _proof_3(system)


# ================================= 4. a loop cannot be stranded after a merge


def _proof_4(system: System) -> None:
    """The A40 lands first; the order for the retired MRN arrives after it."""
    system.accepted(merge_message("A40-1", prior=S_MRN, surviving=S_MRN_SURVIVOR))
    system.accepted(order("ORD-LATE", mrn=S_MRN, placer="PL9", filler="ACC9"))

    loop = system.only(placer_order_number="PL9")
    assert loop.mrn == S_MRN_SURVIVOR, (
        f"the late order opened on the retired identifier {loop.mrn!r}; it is "
        "clinically open and off the surviving patient's worklist"
    )
    assert loop.loop_id in system.queued_ids()
    # And the identifier the message actually carried is still on the record,
    # because an auditor reconciling against the sending system needs it.
    assert system.event_detail(loop.loop_id, "created").get("submitted_mrn") == S_MRN


def test_spec_4_a_late_order_on_a_retired_mrn_is_not_stranded(system):
    _proof_4(system)


def test_spec_4_can_fail(system, monkeypatch):
    """Resolve nothing. This is the whole bug the spec's paired tests 3 and 4
    exist to close, and it is one deleted line away at all times."""
    monkeypatch.setattr(LoopStore, "resolve_mrn", lambda self, mrn: mrn)
    with pytest.raises(AssertionError):
        _proof_4(system)


# ============================ 5. a result carrying the retired MRN still matches


def _proof_5(system: System) -> None:
    """Tier 3 on purpose: MRN, service code and date window.

    An exact-identifier tier would match whatever the PID said, so a pipeline
    that never resolved the alias would still pass. Tier 3 is the only tier
    whose evidence *is* the MRN, which is what makes this a proof that
    resolution happens before matching rather than only before loop creation.
    """
    system.accepted(order("ORD-1", placer="", filler=""))
    loop_id = system.only(state=LoopState.OPEN).loop_id
    system.accepted(merge_message("A40-1", prior=S_MRN, surviving=S_MRN_SURVIVOR))
    assert system.store.replay(loop_id).mrn == S_MRN_SURVIVOR

    # The RIS has not heard about the merge and sends the old identifier.
    system.accepted(result("RES-1", mrn=S_MRN, placer="", filler=""))

    assert system.in_state(LoopState.ORPHAN) == [], (
        "the result orphaned: the alias was not resolved before matching"
    )
    assert system.store.replay(loop_id).state is LoopState.RESULTED
    # The tier is read off the stored event, not passed in and read back.
    tier = system.event_detail(loop_id, "resulted").get("match_tier")
    assert tier == 3, f"expected a tier-3 attach on MRN evidence, got {tier!r}"


def test_spec_5_a_result_on_a_retired_mrn_matches_at_tier_3(system):
    _proof_5(system)


def test_spec_5_can_fail(system, monkeypatch):
    monkeypatch.setattr(LoopStore, "resolve_mrn", lambda self, mrn: mrn)
    with pytest.raises(AssertionError):
        _proof_5(system)


# ============================================== 6. circular merge is refused


def assert_no_identifier_resolves_to_itself(store: LoopStore) -> None:
    """The scan spec test 6 asks for, as a helper so it can be proven to fail."""
    offenders = [(prior, surviving) for prior, surviving in store.aliases()
                 if store.resolve_mrn(prior) == prior or prior == surviving]
    assert offenders == [], f"an identifier resolves to itself: {offenders}"


def _proof_6(system: System) -> None:
    system.accepted(order("ORD-1", placer="PL1", filler="ACC1"))
    loop_id = system.only(state=LoopState.OPEN).loop_id
    system.accepted(merge_message("A40-1", prior=S_MRN, surviving=S_MRN_SURVIVOR))

    aliases_before = sorted(system.store.aliases())
    owner_before = system.store.replay(loop_id).mrn
    assert aliases_before, "no alias was recorded, so there is no cycle to refuse"

    # B -> A closes the loop. Two independent things refuse it and both are
    # asserted, because either alone would let a regression in the other pass.
    #
    #   1. Ingest resolves both endpoints before the registry sees them, so the
    #      A40 arrives as "B merges into B" and takes the merge-into-itself
    #      no-op path. The engine is still answered; nothing changes.
    system.over_the_wire(
        merge_message("A40-2", prior=S_MRN_SURVIVOR, surviving=S_MRN)
    )
    assert sorted(system.store.aliases()) == aliases_before, "the alias table changed"
    assert system.store.replay(loop_id).mrn == owner_before, "a loop moved"

    #   2. The store refuses the cycle on its own, for a caller that reaches it
    #      without ingest's resolution -- a replay, an admin tool, the next
    #      caller. This is the guard the spec is really asking about, and
    #      without this half the proof above passes on a store with no cycle
    #      detection whatsoever.
    try:
        system.store.record_alias(
            S_MRN_SURVIVOR, S_MRN,
            established_at=datetime.now(timezone.utc), established_by="A40-2",
        )
    except store_module.CircularMergeError:
        pass
    else:
        # Raised rather than `pytest.raises`, so that every failure this proof
        # can produce is an AssertionError and the `_can_fail` companion has one
        # thing to catch.
        raise AssertionError(
            "the store recorded a cycle; both claims cannot hold and choosing "
            "between them strands every loop on the losing side"
        )

    assert sorted(system.store.aliases()) == aliases_before, "the refusal wrote something"
    assert system.store.replay(loop_id).mrn == owner_before, "a loop moved"
    assert_no_identifier_resolves_to_itself(system.store)


def test_spec_6_a_circular_merge_changes_nothing(system):
    _proof_6(system)


def test_spec_6_the_self_resolution_scan_can_fail(system):
    """The scan is the load-bearing assertion, so it is shown failing against a
    projection hand-corrupted into exactly the state a missed cycle produces."""
    system.accepted(order("ORD-1"), merge_message("A40-1", prior=S_MRN,
                                                  surviving=S_MRN_SURVIVOR))
    assert_no_identifier_resolves_to_itself(system.store)

    import sqlite3
    conn = sqlite3.connect(system.store.db_path)
    try:
        conn.execute("UPDATE mrn_aliases SET surviving_mrn = retired_mrn")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssertionError):
        assert_no_identifier_resolves_to_itself(system.store)


def _naive_apply_alias(cls, conn, retired_mrn, surviving_mrn, established_at,
                       established_by):
    """Record the pair the message named. No resolution of either endpoint, no
    compression, no cycle check -- the obvious implementation, and the one spec
    tests 6 and 11 both exist to rule out."""
    conn.execute(
        "INSERT OR REPLACE INTO mrn_aliases (retired_mrn, surviving_mrn, "
        "established_at, established_by) VALUES (?, ?, ?, ?)",
        (retired_mrn, surviving_mrn, established_at, established_by),
    )
    return retired_mrn, surviving_mrn


def test_spec_6_can_fail(system, monkeypatch):
    """Record the reverse alias unconditionally; the proof must catch the change."""
    monkeypatch.setattr(LoopStore, "_apply_alias", classmethod(_naive_apply_alias))
    with pytest.raises(AssertionError):
        _proof_6(system)


# =============== 7. loop creation refuses an MRN retired since ingest resolved it


def _proof_7(system: System, *, converges: bool) -> None:
    """The merge commits between ingest's resolution and the loop write.

    The window is real and narrow: ingest resolves once, then the A40 lands,
    then the write goes ahead against a value that is no longer current. What
    must never happen is a loop landing on the retired identifier -- it is
    clinically open and off the surviving patient's worklist, with an AA telling
    the engine the matter is settled.

    **Two outcomes are correct and the spec only names one.** The listener
    re-resolves and retries up to three times, so a single merge is absorbed and
    the loop lands on the *survivor* under an AA. Only a merge storm that never
    converges exhausts the retries, and then the refusal has to leave the
    process as an AE. Testing only the second would let the first regress into
    landing on the retired identifier, so both are asserted here.
    """
    acks = system.over_the_wire(order("ORD-1", placer="PL1", filler="ACC1"))

    landed = [loop for loop in system.loops() if loop.mrn == S_MRN]
    assert landed == [], f"a loop landed on the retired identifier: {landed}"

    if converges:
        assert "|AA|" in acks[0], acks[0]
        # The survivor is whatever the alias table now names, read back out of
        # the store rather than compared against a value this test invented.
        survivor = system.store.resolve_mrn(S_MRN)
        assert survivor != S_MRN, "no merge committed, so there was no race"
        assert system.only(placer_order_number="PL1").mrn == survivor, (
            "the retry recovered onto something other than the current survivor"
        )
        assert system.handler.mrn_reresolution_count == 1
    else:
        assert "|AE|" in acks[0], (
            f"the write was ACKed rather than refused: {acks[0]!r}. The engine "
            "will not retry and the loop does not exist"
        )
        assert system.loops() == [], "something landed despite the refusal"
        assert system.handler.mrn_retired_count == 1
        assert system.store.raw_count() == 1, (
            "the raw must be archived even though nothing applied"
        )


def _retire_before_open(system: System, monkeypatch, *, forever: bool) -> dict:
    """Commit a real merge after resolution and before the loop write.

    Real aliases through `record_alias`, not a stubbed `resolve_mrn`: the guard
    under test reads the same table the merge writes, and a stub proves only
    that the guard trusts its own stub.
    """
    real_open = Registry.open_loop
    fired = {"count": 0}

    def merge_then_open(self, *args, mrn, **kwargs):
        if forever or fired["count"] == 0:
            system.store.record_alias(
                mrn, f"{mrn}-NEXT{fired['count']}",
                established_at=datetime.now(timezone.utc),
                established_by="A40-RACE",
            )
        fired["count"] += 1
        return real_open(self, *args, mrn=mrn, **kwargs)

    monkeypatch.setattr(Registry, "open_loop", merge_then_open)
    return fired


@pytest.fixture()
def one_merge_mid_write(system, monkeypatch):
    return _retire_before_open(system, monkeypatch, forever=False)


@pytest.fixture()
def merge_storm_mid_write(system, monkeypatch):
    return _retire_before_open(system, monkeypatch, forever=True)


def test_spec_7_a_merge_mid_write_lands_on_the_survivor_never_the_retired_mrn(
    system, one_merge_mid_write
):
    _proof_7(system, converges=True)
    assert one_merge_mid_write["count"] == 2, "the retry never happened"


def test_spec_7_a_write_that_can_never_converge_is_refused(system, merge_storm_mid_write):
    _proof_7(system, converges=False)
    assert merge_storm_mid_write["count"] == 3, "the retry budget was not spent"


def test_spec_7_can_fail(system, merge_storm_mid_write, monkeypatch):
    """Blind the write-time re-check. The loop then lands on the identifier the
    merge retired, under an AA, which is the state this test exists to forbid."""
    monkeypatch.setattr(LoopStore, "resolve_mrn", lambda self, mrn: mrn)
    with pytest.raises(AssertionError):
        _proof_7(system, converges=False)


# ============================================== 8. the false-match gate, both halves


def _proof_8(pack: RulePack) -> None:
    """Spec test 8 and success criterion 4. Either half alone is passed by a
    broken system: zero false matches is perfect for a matcher that attaches
    nothing, and a coverage floor alone is met by one that attaches everything."""
    scored = replay(synthetic_corpus(), pack)

    assert scored.false_match_rate == 0.0, (
        f"false-match rate {scored.false_match_rate:.3f} at the configured floor "
        f"{pack.confidence_floor}: a result was attributed to an order it did "
        "not come from"
    )
    assert scored.auto_match_rate >= pack.min_auto_match_rate, (
        f"auto-match rate {scored.auto_match_rate:.3f} is below the pack's "
        f"configured minimum {pack.min_auto_match_rate:.3f}: the matcher has "
        "stopped resolving and the safety number reads perfect because of it"
    )
    ok, why = check_release_criteria(scored, pack)
    assert ok is True, why


def test_spec_8_the_signature_verified_shipped_pack_meets_both_halves(shipped_pack):
    """The pack that ships, loaded through `load_pack` rather than lifted out of
    pack.json as a dict. A gate that scores an unsigned body would let anyone
    justify a release by writing the numbers they wanted into a file."""
    _proof_8(shipped_pack)


def test_spec_8_can_fail_on_the_degenerate_matcher(shipped_pack):
    """A confidence floor above every tier: attaches nothing, false-match rate a
    perfect 0.000. This is the implementation the one-sided criterion rewards."""
    degenerate = replace(shipped_pack, confidence_floor=1.01)
    assert replay(synthetic_corpus(), degenerate).false_match_rate == 0.0, (
        "the degenerate pack must still score perfectly on the half that is "
        "not a gate, or this test is not showing what it claims"
    )
    with pytest.raises(AssertionError):
        _proof_8(degenerate)


def test_spec_8_can_fail_on_a_pack_that_manufactures_a_false_match(shipped_pack):
    """Widen the CT window past the corpus's four-day-late result and tier 3
    fires on an order it did not come from."""
    wide = replace(
        shipped_pack,
        date_windows_hours={**shipped_pack.date_windows_hours, "CT": 10_000},
    )
    assert replay(synthetic_corpus(), wide).false_match_rate > 0
    with pytest.raises(AssertionError):
        _proof_8(wide)


# =========================================== 9. CLOSED is unreachable in v1


def projection_rows(store: LoopStore) -> dict[str, dict]:
    """The `loops` table itself.

    `all_loops()` reads the projection for loop *ids* and then replays each one
    from `loop_events`, so a scan built on it cannot see the projection's own
    contents at all. That is good product behaviour and a trap for a test: an
    assertion phrased over `all_loops()` is an assertion about replay twice.
    """
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    try:
        conn.row_factory = sqlite3.Row
        return {row["loop_id"]: dict(row)
                for row in conn.execute("SELECT * FROM loops").fetchall()}
    finally:
        conn.close()


def assert_nothing_is_closed(store: LoopStore) -> None:
    """Both sources, because they can disagree and only one of them is the one a
    coordinator's worklist query filters on."""
    replayed = [loop.loop_id for loop in store.all_loops()
                if loop.state is LoopState.CLOSED]
    projected = [loop_id for loop_id, row in projection_rows(store).items()
                 if row["state"] == LoopState.CLOSED.value]
    assert replayed == [], f"loops replay into the state reserved for v2: {replayed}"
    assert projected == [], f"loops are recorded CLOSED in the projection: {projected}"


def _proof_9(system: System) -> None:
    """Every message type and every coordinator action, then the scan.

    Spec test 9 says no message, coordinator action or replay path reaches
    CLOSED. So the proof drives all three rather than asserting on a state
    table: a table is a claim about the code someone remembered to look at.
    """
    system.accepted(
        order("ORD-1", placer="PL1", filler="ACC1"),
        order("ORD-2", mrn=S_MRN_OTHER, placer="PL2", filler="ACC2"),
        result("RES-1", placer="PL1", filler="ACC1", obx11="F"),
        result("RES-ORPH", placer="NOPE", filler="NOPE", service="99999^MYSTERY^C4"),
        merge_message("A40-1", prior=S_MRN_OTHER, surviving=S_MRN_THIRD),
    )
    for loop in system.loops():
        for action, body in (
            ("acknowledge", {}),
            ("reverse_acknowledgement", {"reason": "wrong patient"}),
            ("dismiss", {"reason": "belongs to no order here"}),
        ):
            act(system, loop.loop_id, action, **body)

    assert len(system.loops()) >= 3, "the drive did not produce loops to scan"
    assert_nothing_is_closed(system.store)

    # The replay path, and the append path that feeds it.
    for loop in system.loops():
        assert system.store.replay(loop.loop_id).state is not LoopState.CLOSED
    with pytest.raises(Exception):
        system.store.append_event(
            LoopEvent(system.loops()[0].loop_id, "closed",
                      system.loops()[0].ordered_at or None, "C-CLOSED", {})
        )
    # And no word on the page offers it.
    assert "CLOSED" not in system.http.get("/worklist/").data.decode()


def test_spec_9_closed_is_unreachable_after_every_message_and_action(system):
    _proof_9(system)


def test_spec_9_the_closed_scan_can_fail(system):
    """The scan run against a projection hand-written into CLOSED."""
    system.accepted(order("ORD-1"))
    assert_nothing_is_closed(system.store)

    import sqlite3
    conn = sqlite3.connect(system.store.db_path)
    try:
        conn.execute("UPDATE loops SET state = ?", (LoopState.CLOSED.value,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssertionError):
        assert_nothing_is_closed(system.store)


# ======================================= 10. acknowledgement is reversible


def _fingerprint(events: list[LoopEvent]) -> list[tuple]:
    return [(e.event_type, e.control_id, json.dumps(e.detail, sort_keys=True))
            for e in events]


def _proof_10(system: System) -> None:
    system.accepted(order("ORD-1"), result("RES-1", obx11="F"))
    loop_id = system.only(state=LoopState.RESULTED).loop_id
    assert ack(system, loop_id).status_code < 400
    before = _fingerprint(system.events(loop_id))
    assert system.store.replay(loop_id).state is LoopState.ACKNOWLEDGED

    response = act(system, loop_id, "reverse_acknowledgement",
                   actor="coordinator-b", role="referral_coordinator",
                   reason="acknowledged the wrong loop")
    assert response.status_code < 400, response.data

    reversed_loop = system.store.replay(loop_id)
    assert reversed_loop.state is LoopState.RESULTED
    assert reversed_loop.ack_by == ""
    assert loop_id in system.queued_ids()

    after = system.events(loop_id)
    assert _fingerprint(after)[:len(before)] == before, (
        "a prior event was mutated; the log is not append-only"
    )
    assert len(after) == len(before) + 1, "the reversal appended more or less than one event"
    # The actor is recorded, and it is the second coordinator rather than the
    # one whose acknowledgement is being undone.
    assert after[-1].detail.get("reversed_by") == "coordinator-b"


def test_spec_10_acknowledgement_is_reversible_over_http(system):
    _proof_10(system)


def test_spec_10_can_fail(system, monkeypatch):
    """Undo by rewriting the acknowledgement instead of appending a reversal --
    the shape any 'just fix the row' implementation takes."""

    def mutate_instead(self, loop_id, actor, role, reason, control_id, **kwargs):
        import sqlite3
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute(
                "UPDATE loops SET state = ?, ack_by = '', ack_role = '', ack_at = '' "
                "WHERE loop_id = ?", (LoopState.RESULTED.value, loop_id),
            )

    monkeypatch.setattr(Registry, "reverse_acknowledgement", mutate_instead)
    with pytest.raises(AssertionError):
        _proof_10(system)


# ============================================== 11. alias chains compress


def _proof_11(system: System) -> None:
    """A -> B then B -> C must leave A pointing at C **in the table**.

    Asserting only that `resolve_mrn("A") == "C"` would be satisfied by chasing
    the chain at read time, which is the implementation the spec explicitly
    rules out: it is O(chain) on the ingest path and it is where a cycle turns
    into a hang rather than a refusal.
    """
    system.accepted(order("ORD-1", placer="PL1", filler="ACC1"))
    loop_id = system.only(state=LoopState.OPEN).loop_id

    system.accepted(merge_message("A40-1", prior=S_MRN, surviving=S_MRN_SURVIVOR))
    system.accepted(merge_message("A40-2", prior=S_MRN_SURVIVOR,
                                  surviving=S_MRN_THIRD))

    table = dict(system.store.aliases())
    assert table.get(S_MRN) == S_MRN_THIRD, (
        f"the table still records the intermediate hop: {S_MRN} -> "
        f"{table.get(S_MRN)!r}. Compression must happen at write time"
    )
    assert table.get(S_MRN_SURVIVOR) == S_MRN_THIRD
    assert system.store.resolve_mrn(S_MRN) == S_MRN_THIRD
    assert system.store.replay(loop_id).mrn == S_MRN_THIRD


def test_spec_11_alias_chains_compress_at_write_time(system):
    _proof_11(system)


def test_spec_11_can_fail(system, monkeypatch):
    """Record the hop and stop: correct at read time only, uncompressed on disk."""
    monkeypatch.setattr(LoopStore, "_apply_alias", classmethod(_naive_apply_alias))
    with pytest.raises(AssertionError):
        _proof_11(system)


# =========================== 12 & 13. no egress, no model calls -- the WHOLE suite


@pytest.mark.timeout(3600)
def test_spec_12_and_13_the_whole_suite_runs_under_both_guards():
    """Spec tests 12 and 13 as written: block non-loopback `socket.connect`,
    monkeypatch `anthropic` and `claude_cli` to raise, and the **full suite**
    passes.

    Two per-module tests already assert that one function opened no socket.
    Neither is this claim. They prove the paths somebody thought of are clean;
    the spec's claim is that every path is, including the ones nobody wrote a
    test for -- and that is only observable by arming the guards over
    everything and running it.

    Re-run in a subprocess because the guards have to be installed before
    collection, and because a guard armed in-process would be armed for the
    tests that come after it in this file only.

    **`-m "not docker"` is not a coverage gap, and adding those tests back would
    not close one.** Do not "fix" it. Both guards are `monkeypatch`-installed
    inside *this* pytest process: `socket.socket.connect` is rebound on this
    interpreter's socket module, and `spec_guards.MODEL_MODULES` are poisoned in
    this interpreter's `sys.modules`. A `docker build` or `docker run` is a
    child process with its own interpreter -- and, for the container, its own
    kernel namespace -- so neither guard is in force inside it and neither can
    observe what it does. Running `test_install_closure.py` here therefore adds
    exactly zero evidence for spec 12 or 13. What it did add was time and
    flakiness: `docker run` stalled past ten minutes more than once, and this
    inner run was twenty-two of the suite's thirty-eight minutes when a hang
    finally took the merge gate red. A safety proof is the last place to accept
    an unreliable step that proves nothing.

    Those tests still run in a normal invocation, where success criterion 6 is
    verified against the built image -- that is the only real proof of it, and
    nothing here weakens it.

    The `timeout(3600)` marker matches the `subprocess.run` budget below and
    exists because CI passes `--timeout=120`, which is the right per-test limit
    for every test but this one: this one *is* the suite, so a limit sized for a
    single test kills it every time and the failure looks like a hang rather than
    a misconfiguration. Exempting the one test that runs the others is narrower
    than raising the limit for all of them, which would be giving up the guard
    everywhere to accommodate one case.
    """
    if os.environ.get(spec_guards.ARMED_ENV):
        pytest.skip("already inside the guarded run; not recursing")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q",
         "-p", "tests.spec_guards",
         "-m", "not docker",
         "--deselect",
         "tests/test_spec_proofs.py::"
         "test_spec_12_and_13_the_whole_suite_runs_under_both_guards"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=3600,
        env={**os.environ, spec_guards.ARMED_ENV: "1"},
    )
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-25:])
    assert proc.returncode == 0, f"the guarded suite did not pass:\n{tail}"
    # Anti-vacuity: a run that collected nothing also reports no failures.
    assert " passed" in proc.stdout, tail
    passed = int(proc.stdout.split(" passed")[0].split()[-1])
    assert passed > 700, f"only {passed} tests ran under the guards:\n{tail}"


def test_spec_12_the_egress_guard_can_actually_fail():
    """A guard that never fires proves nothing about the suite that ran under
    it. Both directions: off-host refused, loopback still allowed."""
    with spec_guards.armed():
        with pytest.raises(spec_guards.EgressAttempted):
            socket.create_connection(("example.com", 80), timeout=5)
        with pytest.raises(spec_guards.EgressAttempted):
            socket.create_connection(("10.0.0.1", 443), timeout=5)

        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.create_connection(listener.getsockname(), timeout=5):
                pass
        finally:
            listener.close()

    # And the guard is off again, so it cannot leak into whatever runs next.
    # Skipped inside the guarded run, where the plugin armed the process before
    # collection and `armed()` correctly restores it to armed.
    if not os.environ.get(spec_guards.ARMED_ENV):
        assert socket.socket.connect.__name__ != "guarded_connect"


def test_spec_13_the_model_guard_can_actually_fail():
    """Every module in `spec_guards.MODEL_MODULES` must raise on use.

    The monorepo also listed `claude_cli`, and the interesting half of this test
    was that a reference bound before the guard was installed still raised --
    `healthcare_rag/__init__.py` imported the shim at package import time, so
    that was the realistic case. Nothing imports a model client here, so the
    prebound branch below is inert until one does. It stays because that is the
    case the poisoning in `_poison_in_place` exists for, and it would go silently
    untested otherwise.
    """
    assert spec_guards.MODEL_MODULES == ("anthropic",), (
        "a model client was added to the guard without a proof that it raises"
    )
    prebound = sys.modules.get("anthropic")

    with spec_guards.armed():
        import anthropic
        with pytest.raises(spec_guards.ModelCallAttempted):
            anthropic.Anthropic(api_key="not-a-key")

        if prebound is not None:
            with pytest.raises(spec_guards.ModelCallAttempted):
                prebound.Anthropic(api_key="not-a-key")

    # Restored, or the poisoned module leaks into every test after this one.
    # `is prebound` rather than a callability check because the module is absent
    # from sys.modules here, and putting a stub back would also read as callable.
    if not os.environ.get(spec_guards.ARMED_ENV):
        assert sys.modules.get("anthropic") is prebound


# ================================================ 14. no PHI in artifacts


def _drive_everything(system: System, caplog) -> None:
    """Real HL7 in, every coordinator action out, including the refused ones.

    Refused actions matter more than accepted ones here: a refusal message is
    composed from live state at the moment of failure, which is exactly how a
    reflected URL segment and a coordinator's free-text reason each reached an
    artifact earlier in this build.
    """
    caplog.set_level(logging.DEBUG)
    system.accepted(
        order("ORD-1", placer="PL1", filler="ACC1"),
        order("ORD-2", mrn=S_MRN_OTHER, placer="PL2", filler="ACC2"),
        result("RES-1", placer="PL1", filler="ACC1", obx11="F"),
        result("RES-P", mrn=S_MRN_OTHER, placer="PL2", filler="ACC2", obx11="P"),
        result("RES-ORPH", placer="NOPE", filler="NOPE",
               service="99999^MYSTERY^C4"),
        merge_message("A40-1", prior=S_MRN_OTHER, surviving=S_MRN_THIRD),
    )
    reason = f"belongs to {S_NAME}, kin {S_KIN}, guarantor {S_GUARANTOR}"
    for loop in system.loops():
        for action, body in (
            ("acknowledge", {}),
            ("dismiss", {"reason": reason}),
            ("reverse_acknowledgement", {"reason": reason}),
            ("attach", {"target": system.loops()[0].loop_id}),
            ("undo_match", {"reason": reason}),
        ):
            act(system, loop.loop_id, action, **body)
    # An identifier straight off the URL, which is the reflection path.
    system.http.post(f"/worklist/{S_MRN}/acknowledge",
                     json={"actor": "a", "role": "r"})
    system.registry.reverse_merge(S_MRN_OTHER, actor="admin", role="him",
                                  reason=reason, control_id="ADMIN-1")


def _artifacts(system: System, caplog) -> dict[str, str]:
    """Everything that leaves the building. Not the parser, not the store."""
    out: dict[str, str] = {
        "worklist HTML": system.http.get("/worklist/").data.decode(),
        "worklist JSON": system.http.get("/worklist/?format=json").data.decode(),
        "audit export": json.dumps(referral_audit_entries()),
        "label export": json.dumps(system.store.labels(), default=str),
        "eval report": format_report(replay(synthetic_corpus(), system.pack)),
    }
    for loop in system.loops():
        for action, body in (("acknowledge", {}),
                             ("dismiss", {"reason": "x"}),
                             ("reverse_acknowledgement", {"reason": "x"})):
            response = act(system, loop.loop_id, action, **body)
            out[f"{action} -> {response.status_code}"] = response.data.decode()
    out["logs"] = "\n".join(r.getMessage() for r in caplog.records)

    # The audit database's *bytes*, not the objects the wrapper built. A wrapper
    # that constructed clean events and then wrote something else would pass
    # every assertion made against its own return values.
    db = Path(audit_module.audit_db_path())
    for path in (db, Path(f"{db}-wal"), Path(f"{db}-journal")):
        if path.exists():
            out[f"audit db bytes ({path.name})"] = path.read_bytes().decode(
                "utf-8", errors="replace"
            )
    return out


def scan(artifacts: dict[str, str]) -> dict[str, list[str]]:
    return {
        name: sorted(label for label, value in SENTINELS.items() if value in text)
        for name, text in artifacts.items()
    }


def assert_clean(artifacts: dict[str, str]) -> None:
    leaks = {name: found for name, found in scan(artifacts).items() if found}
    assert leaks == {}, f"PHI sentinels left the building: {leaks}"


def _proof_14(system: System, caplog) -> None:
    _drive_everything(system, caplog)
    artifacts = _artifacts(system, caplog)

    # Anti-vacuity, three ways. A clean scan of nothing is not a clean scan.
    assert artifacts["logs"].strip(), "nothing was logged, so the log grep is empty"
    assert len(system.loops()) >= 3, "the drive produced almost no state"
    assert artifacts["audit export"].count("referral.") >= 8, (
        "the audit export returned nothing to be clean about"
    )
    assert any("audit db bytes" in name for name in artifacts), (
        "the audit database was never written, so its bytes prove nothing"
    )

    assert_clean(artifacts)


def test_spec_14_no_sentinel_reaches_any_artifact_end_to_end(system, caplog):
    """Sentinels planted in PID-3, PID-5, NK1-2, GT1-3 and both note channels,
    driven through the real listener, then grepped out of worklist HTML, worklist
    JSON, every action response, every log record, the label export, the eval
    report, the audit export **and the audit database's raw bytes**."""
    _proof_14(system, caplog)


def test_spec_14_the_store_still_holds_the_mrn(system, caplog):
    """The companion nobody thinks to write, and the one that stops the wrong fix.

    The store legitimately holds the MRN -- it is what matching resolves against
    and what a merge moves. Someone reading a red PHI test has an obvious lever:
    scrub the store. That turns a leak into a silently broken matcher, so the
    retention is asserted in the same file, right next to the leak scan.
    """
    _drive_everything(system, caplog)

    mrns = {loop.mrn for loop in system.loops()}
    assert S_MRN in mrns or S_MRN_THIRD in mrns, (
        f"no loop carries a planted MRN any more ({mrns}); if the PHI scan was "
        "made to pass by scrubbing the store, matching is now broken"
    )
    archive = "\n".join(system.store.raw_payloads())
    assert S_MRN in archive, "the raw archive must keep the message verbatim"
    assert S_KIN in archive and S_NOTE in archive, (
        "the archive is the evidence record; it is not an artifact and is not "
        "scrubbed"
    )


def test_spec_14_the_scan_can_actually_fail(system, caplog):
    """Every one of the six sentinels, planted into a cleared artifact one at a
    time. A scan that only ever detects the MRN would have missed the NK1 and
    GT1 leaks the spec names explicitly."""
    _drive_everything(system, caplog)
    artifacts = _artifacts(system, caplog)
    assert_clean(artifacts)

    for label, value in SENTINELS.items():
        contaminated = {"worklist HTML": artifacts["worklist HTML"] + value}
        assert scan(contaminated)["worklist HTML"] == [label]
        with pytest.raises(AssertionError):
            assert_clean(contaminated)


def test_spec_14_can_fail_on_a_real_leak(system, caplog, monkeypatch):
    """Not a string appended to a copy: an actual leak, through the renderer,
    reaching the actual HTML."""
    from referral_loop import worklist as worklist_module

    real_row = worklist_module._row

    def leaky(loop, now, pack, **kwargs):
        row = real_row(loop, now, pack, **kwargs)
        row["modality"] = loop.mrn
        return row

    monkeypatch.setattr(worklist_module, "_row", leaky)
    with pytest.raises(AssertionError):
        _proof_14(system, caplog)


# ============================================= 15. persist before ACK, on the wire


def _proof_15(system: System, monkeypatch) -> None:
    """The parse dies after the durable write; the message must survive it.

    `test_listener.py::test_raw_survives_process_death_mid_parse` kills a real
    OS process and is the stronger proof of the same property. This one adds the
    half that test cannot reach: what the *engine* is told over the wire when
    the transition does not land, which is the difference between a retry and a
    permanently lost result.
    """
    system.accepted(order("ORD-1", placer="PL1", filler="ACC1"))
    loop_id = system.only(state=LoopState.OPEN).loop_id

    real_process = MessageHandler._process
    exploded = {"count": 0}

    def die_after_the_write(self, control_id, text, peer, charge_refusal=None):
        if "ORU^R01" in text:
            exploded["count"] += 1
            raise store_module.StoreUnavailableError("killed mid-parse")
        return real_process(self, control_id, text, peer, charge_refusal)

    monkeypatch.setattr(MessageHandler, "_process", die_after_the_write)
    acks = system.over_the_wire(result("RES-1", placer="PL1", filler="ACC1"))

    assert exploded["count"] == 1, "the failure never fired"
    assert "|AA|" not in acks[0], (
        f"the engine was ACKed for a transition that did not land: {acks[0]!r}"
    )
    assert system.store.replay(loop_id).state is LoopState.OPEN

    # The raw is on disk regardless, verbatim, and replaying it reconstructs the
    # state that was lost.
    assert any("RES-1" in payload for payload in system.store.raw_payloads()), (
        "the message was not durably written before the parse"
    )
    monkeypatch.undo()
    system.accepted(result("RES-1", placer="PL1", filler="ACC1"))
    assert system.store.replay(loop_id).state is LoopState.RESULTED


def test_spec_15_a_transition_that_did_not_land_is_not_acked(system, monkeypatch):
    _proof_15(system, monkeypatch)


def test_spec_15_can_fail(system, monkeypatch):
    """ACK the message rather than the transition.

    The real flow runs untouched -- the parse still dies, the raw is still
    archived -- and only the code the engine is told changes. That is the
    realistic regression. Not "somebody removed the durable write", but "the
    failure path answers AA because arrival was mistaken for application",
    which silently turns a retryable failure into a lost result.
    """
    real_handle = MessageHandler.handle

    def always_aa(self, text):
        from referral_loop.listener import peek_control_id
        from referral_loop.mllp import build_ack
        real_handle(self, text)
        return build_ack(peek_control_id(text), "AA")

    monkeypatch.setattr(MessageHandler, "handle", always_aa)
    with pytest.raises(AssertionError):
        _proof_15(system, monkeypatch)


# =========================== 16. a retry under a new control id is not a transition


def _proof_16(system: System) -> None:
    system.accepted(order("ORD-1", placer="PL1", filler="ACC1"))
    loop_id = system.only(state=LoopState.OPEN).loop_id

    system.accepted(result("RES-1", placer="PL1", filler="ACC1", obx11="F"))
    after_first = [e.event_type for e in system.events(loop_id)]
    control_dupes = system.handler.duplicate_control_id_count
    content_dupes = system.handler.duplicate_content_key_count

    # Byte-identical apart from MSH-10: an interface engine retrying.
    system.accepted(result("RES-1-RETRY", placer="PL1", filler="ACC1", obx11="F"))

    assert [e.event_type for e in system.events(loop_id)] == after_first, (
        "the retry produced a second transition"
    )
    assert system.handler.duplicate_content_key_count == content_dupes + 1, (
        "a content-key duplicate was not counted"
    )
    assert system.handler.duplicate_control_id_count == control_dupes, (
        "a fresh MSH-10 was counted as an MSH-10 duplicate; the two signal "
        "different things and a retry configuration would be invisible"
    )

    # The other direction, because dedup that eats amendments is worse than the
    # double-counting it fixes.
    system.accepted(result("RES-2", placer="PL1", filler="ACC1", obx11="C",
                           observed_at=LATER_AT))
    assert len(system.events(loop_id)) == len(after_first) + 1, (
        "a genuine correction was swallowed by content dedup"
    )


def test_spec_16_a_retry_under_a_fresh_control_id_is_one_transition(system):
    _proof_16(system)


def test_spec_16_can_fail(system, monkeypatch):
    """Key the dedup on MSH-10 alone -- the obvious implementation, and the one
    that lets every engine retry double-count."""
    monkeypatch.setattr(
        "referral_loop.listener.content_key",
        lambda message, pack, *, mrn: None,
    )
    with pytest.raises(AssertionError):
        _proof_16(system)


# ==================================================== 17. pack tamper


def _pack_dir_with(tmp_path: Path, body: bytes, signature: bytes) -> Path:
    directory = tmp_path / "pack"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pack.json").write_bytes(body)
    (directory / "pack.sig").write_bytes(signature)
    return directory


def _shipped_bytes() -> tuple[bytes, bytes]:
    return ((SHIPPED_PACK_DIR / "pack.json").read_bytes(),
            (SHIPPED_PACK_DIR / "pack.sig").read_bytes())


def test_spec_17_the_untampered_shipped_pack_loads(tmp_path):
    """The control. Without it every refusal below is also produced by a
    `load_pack` that refuses everything."""
    body, signature = _shipped_bytes()
    pack = load_pack(_pack_dir_with(tmp_path, body, signature),
                     bytes.fromhex(SHIPPED_PUBKEY))
    assert pack.version, "the control loaded a pack with no version"


@pytest.mark.parametrize("region", ["confidence_floor", "field_map"])
def test_spec_17_one_mutated_byte_refuses_to_load(tmp_path, region):
    """Spec test 17, including the half it names explicitly: **a byte inside
    `field_map`**.

    A mapping change must be as tamper-evident as a threshold change, and it is
    the more dangerous of the two. A threshold that has been nudged shows up in
    the eval numbers. A field map redirected to a segment the parser will happily
    read is invisible to every metric the gate watches -- it does not make
    matching worse, it makes it answer a different question.

    The mutation is located by searching the signed bytes for the region's key,
    so this cannot silently start mutating a byte that is not in `field_map`
    because someone reordered the JSON.
    """
    body, signature = _shipped_bytes()
    needle = f'"{region}"'.encode()
    start = body.find(needle)
    assert start != -1, f"{region} is not in the shipped pack body"
    # A byte inside the region's *value*, past the key and its colon.
    target = start + len(needle) + 3
    assert target < len(body), "the region has no value to mutate"

    mutated = bytearray(body)
    mutated[target] ^= 0x01
    assert bytes(mutated) != body, "the mutation did not change a byte"

    with pytest.raises(PackVerificationError):
        load_pack(_pack_dir_with(tmp_path, bytes(mutated), signature),
                  bytes.fromhex(SHIPPED_PUBKEY))


def test_spec_17_a_tampered_field_map_would_otherwise_be_usable(tmp_path):
    """Why the field_map half is the dangerous one, made concrete.

    The tampered body is not corrupt: it parses, it validates, and it would
    build a `RulePack` that reads a different field. The signature is the only
    thing standing between it and a matcher quietly answering a different
    question, which is exactly why the spec asks for this byte specifically.
    """
    body, _ = _shipped_bytes()
    parsed = json.loads(body)
    parsed["field_map"]["filler_order_number"] = ["OBR-19"]
    redirected = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()

    assert json.loads(redirected)["field_map"]["filler_order_number"] == ["OBR-19"]
    with pytest.raises(PackVerificationError):
        load_pack(_pack_dir_with(tmp_path, redirected, _shipped_bytes()[1]),
                  bytes.fromhex(SHIPPED_PUBKEY))


# ============================================ 18. the field map drives matching


def _sign_pack(tmp_path: Path, body: dict) -> tuple[Path, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    packed = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    directory = tmp_path / f"pack-{len(list(tmp_path.iterdir()))}"
    directory.mkdir(parents=True)
    (directory / "pack.json").write_bytes(packed)
    (directory / "pack.sig").write_bytes(key.sign(packed))
    return directory, key.public_key().public_bytes_raw().hex()


def _system_on(tmp_path: Path, pack_dir: Path, pubkey: str, name: str) -> System:
    stack = boot(db_path=tmp_path / f"{name}.db", pack_dir=pack_dir,
                 public_key_hex=pubkey)
    app = create_app(store=stack.store, registry=stack.registry, pack=stack.pack)
    app.config["TESTING"] = True
    return System(stack, app.test_client())


@pytest.fixture()
def relocation(tmp_path, monkeypatch):
    """Two signed packs identical but for where the accession lives.

    The shipped pack already lists OBR-18 as a *fallback* candidate for the
    filler order number, so relocating the accession into it under the shipped
    pack matches without any pack change -- which would make this proof vacuous.
    Both packs here therefore name exactly one candidate, so the pack is the only
    thing that can explain the difference.
    """
    monkeypatch.setenv("PHI_MODE", "full")
    monkeypatch.setenv("PHI_ENCRYPTION_VERIFIED", "1")
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")

    body = json.loads((SHIPPED_PACK_DIR / "pack.json").read_bytes())
    at_obr3 = json.loads(json.dumps(body))
    at_obr3["field_map"]["filler_order_number"] = ["OBR-3"]
    at_obr18 = json.loads(json.dumps(body))
    at_obr18["field_map"]["filler_order_number"] = ["OBR-18"]
    return _sign_pack(tmp_path, at_obr3), _sign_pack(tmp_path, at_obr18)


def _tier_2_attach(system: System, *, accession_field: int) -> int | None:
    """Open a loop by accession only, then result on the accession only.

    No placer on either side, so tier 1 cannot fire and tier 2 is the only
    exact-identifier route to the loop. The tier comes back off the stored
    event -- never from an argument this function passed in.
    """
    system.accepted(order("ORD-1", placer="", filler="ACC-RELOCATED",
                          accession_field=accession_field))
    loops = system.in_state(LoopState.OPEN)
    assert len(loops) == 1, f"the order did not open exactly one loop: {loops}"
    loop_id = loops[0].loop_id

    system.accepted(result("RES-1", placer="", filler="ACC-RELOCATED",
                           service="88888^UNRELATED^C4",
                           accession_field=accession_field))
    if system.store.replay(loop_id).state is not LoopState.RESULTED:
        return None
    return system.event_detail(loop_id, "resulted").get("match_tier")


def _proof_18(tmp_path, relocation) -> None:
    """Move the accession OBR-3 -> OBR-18 in the fixture and in the pack; tier 2
    must still match, with no code change.

    Both halves are asserted, because only the pair proves anything. The
    relocated message under the *old* pack must MISS -- otherwise something other
    than the field map found it and "rules as data" is decorative.
    """
    (obr3_dir, obr3_key), (obr18_dir, obr18_key) = relocation

    baseline = _system_on(tmp_path, obr3_dir, obr3_key, "baseline")
    assert _tier_2_attach(baseline, accession_field=3) == 2, (
        "the baseline pack does not match its own fixture at tier 2"
    )

    relocated = _system_on(tmp_path, obr18_dir, obr18_key, "relocated")
    assert _tier_2_attach(relocated, accession_field=18) == 2, (
        "the accession moved to OBR-18 and only the pack changed, but tier 2 "
        "no longer fires: the field map is not what drives matching"
    )

    stale = _system_on(tmp_path, obr3_dir, obr3_key, "stale")
    assert _tier_2_attach(stale, accession_field=18) is None, (
        "the old pack found the relocated accession anyway, so the new pack is "
        "not what made the match"
    )


def test_spec_18_a_relocated_accession_still_matches_at_tier_2(tmp_path, relocation):
    _proof_18(tmp_path, relocation)


def test_spec_18_can_fail(tmp_path, relocation, monkeypatch):
    """Read OBR-3 from a constant instead of from the pack. This is the exact
    regression 'rules as data' claims cannot happen, and it is one hardcoded
    index away."""
    from referral_loop import matcher as matcher_module

    real = matcher_module.concept_value

    def hardcoded(message, pack, concept):
        if concept == "filler_order_number":
            return matcher_module.field_value(message, "OBR-3")
        return real(message, pack, concept)

    monkeypatch.setattr(matcher_module, "concept_value", hardcoded)
    with pytest.raises(AssertionError):
        _proof_18(tmp_path, relocation)


# ================================================ 19. state reconstruction


# The projection columns that carry loop state, and the Loop attribute each one
# materialises. `ordered_at` and the timestamps are excluded deliberately: they
# round-trip through ISO strings and comparing their text would test formatting
# rather than reconstruction.
_PROJECTED_FIELDS = {
    "state": lambda loop: loop.state.value,
    "mrn": lambda loop: loop.mrn,
    "placer_order_number": lambda loop: loop.placer_order_number,
    "filler_order_number": lambda loop: loop.filler_order_number,
    "service_code": lambda loop: loop.service_code,
    "modality": lambda loop: loop.modality,
    "ordering_provider": lambda loop: loop.ordering_provider,
    "ack_by": lambda loop: loop.ack_by,
    "ack_role": lambda loop: loop.ack_role,
}


def assert_every_loop_replays_to_its_projection(store: LoopStore) -> None:
    """Spec test 19 on **every** loop the system produced, against the real
    projection row.

    Two things had to be got right here and the first attempt got neither.

    `test_store.py::test_state_is_reconstructible_from_events_alone` appends
    three events it wrote itself and replays them, which proves `replay` works
    on the events the test chose. This proves it works on the events the
    *product* emits -- merges, corrections, attachments, reversals, dismissals,
    orphans -- which is the population an auditor would actually ask about.

    And the comparison reads the `loops` table with SQL rather than calling
    `all_loops()`. `all_loops()` takes loop *ids* from the projection and then
    replays each one, so a comparison phrased over it compares replay to replay
    and cannot fail. That is not hypothetical: it is what the first version of
    this function did, and it passed with the projection deliberately corrupted.
    """
    rows = projection_rows(store)
    assert rows, "there are no loops, so the reconstruction proves nothing"

    mismatches = []
    for loop_id, row in rows.items():
        rebuilt = store.replay(loop_id)
        differing = sorted(
            f"{column}: projection={row[column]!r} replay={project(rebuilt)!r}"
            for column, project in _PROJECTED_FIELDS.items()
            if (row[column] or "") != (project(rebuilt) or "")
        )
        if differing:
            mismatches.append((loop_id, differing))
    assert mismatches == [], (
        f"loops whose replay disagrees with the projection: {mismatches}"
    )


def _proof_19(system: System, caplog) -> None:
    _drive_everything(system, caplog)
    assert len(system.loops()) >= 3, "the drive produced almost no state"
    states = {loop.state for loop in system.loops()}
    assert len(states) >= 2, f"every loop is in the same state ({states})"

    assert_every_loop_replays_to_its_projection(system.store)

    # And the projection is genuinely derived rather than authoritative: drop it
    # entirely and rebuild from `loop_events` alone.
    import sqlite3
    before = {loop.loop_id: loop for loop in system.store.all_loops()}
    conn = sqlite3.connect(system.store.db_path)
    try:
        conn.execute("DELETE FROM loops")
        conn.commit()
    finally:
        conn.close()
    assert system.store.all_loops() == [], "the projection was not actually dropped"

    assert system.store.rebuild_projection() == len(before)
    after = {loop.loop_id: loop for loop in system.store.all_loops()}
    assert after == before, "the rebuilt projection is not the one that was lost"
    assert_every_loop_replays_to_its_projection(system.store)


def test_spec_19_every_loop_the_system_produced_replays_to_its_state(system, caplog):
    _proof_19(system, caplog)


def test_spec_19_can_fail(system, caplog, monkeypatch):
    """Materialise a projection that does not carry what the events say.

    The first version of this companion wrote a state straight into the `loops`
    row and was **healed** before the assertion ran: every later event on that
    loop re-materialises the whole row from the log, so the divergence was gone
    by the time anything looked. A break that repairs itself is not a break, and
    a can-fail check that a self-repairing break satisfies is not a check.

    So the break is in `_materialize` itself, where a divergence persists for as
    long as the code is wrong -- which is the shape a real one takes.
    """
    real = LoopStore._materialize

    def drop_a_column(self, loop_id, conn):
        real(self, loop_id, conn)
        conn.execute("UPDATE loops SET modality = '' WHERE loop_id = ?", (loop_id,))

    monkeypatch.setattr(LoopStore, "_materialize", drop_a_column)
    with pytest.raises(AssertionError):
        _proof_19(system, caplog)


def test_spec_19_the_comparison_can_fail_on_a_corrupted_projection(system, caplog):
    """The narrower version of the same check, and the one that would have caught
    the vacuous first draft: corrupt one column in the `loops` table and the
    comparison must go red without any code being broken."""
    _drive_everything(system, caplog)
    assert_every_loop_replays_to_its_projection(system.store)

    import sqlite3
    conn = sqlite3.connect(system.store.db_path)
    try:
        conn.execute("UPDATE loops SET modality = 'ZZWRONG'")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssertionError):
        assert_every_loop_replays_to_its_projection(system.store)


# ======================================================= success criterion 1


def test_criterion_1_a_synthetic_stream_reaches_a_populated_worklist(system, caplog):
    """Criterion 1 as one chain: synthetic HL7 in over a real socket, a populated
    worklist out, with both guards armed for the duration.

    The guards are the point. Criteria 1's three clauses are usually verified in
    three different places and then reported as one sentence; here the stream
    that populates the worklist is the same stream running under the egress and
    model guards, so the sentence is a single observation.
    """
    caplog.set_level(logging.DEBUG)
    with spec_guards.armed():
        _criterion_1_body(system)


def _criterion_1_body(system: System) -> None:
    system.accepted(
        order("ORD-1", placer="PL1", filler="ACC1"),
        order("ORD-2", mrn=S_MRN_OTHER, placer="PL2", filler="ACC2"),
        order("ORD-3", mrn=S_MRN_THIRD, placer="PL3", filler="ACC3"),
        result("RES-1", placer="PL1", filler="ACC1", obx11="F"),
        result("RES-2", mrn=S_MRN_OTHER, placer="PL2", filler="ACC2", obx11="P"),
        result("RES-ORPH", placer="NOPE", filler="NOPE",
               service="99999^MYSTERY^C4"),
    )

    queues = system.queues()
    populated = {name: len(rows) for name, rows in queues.items() if rows}
    assert len(populated) >= 3, (
        f"the stream did not populate the worklist across queues: {populated}"
    )
    assert sum(populated.values()) == 4, populated
    html = system.http.get("/worklist/").data.decode()
    assert "<table" in html and "L-" in html, "the page rendered no rows"

    # Zero model calls and zero non-loopback connections: the guards were armed
    # for every line above, and either would have raised rather than recorded.
    # Asserted by exercising them rather than by inspecting that they are
    # installed -- "the guard object is in place" is the shape of an assertion
    # that cannot fail.
    with pytest.raises(spec_guards.EgressAttempted):
        socket.create_connection(("example.com", 80), timeout=5)
    with pytest.raises(spec_guards.ModelCallAttempted):
        sys.modules["anthropic"].Anthropic
