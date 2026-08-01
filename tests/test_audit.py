"""The immutable audit trail, and the one thing it must never contain.

Spec section 3 routes referral audit through `guardrails/immutable_audit.py`,
and spec test 14 asserts PHI sentinels appear zero times in "worklist HTML,
logs, exports, **and audit entries**". Until this task there were no audit
entries, so that last clause passed while asserting nothing about anything.

Three properties are load-bearing here, and each is asserted against the
artifact rather than an intermediate:

  * **The rows exist.** Every human action that changes clinical-facing state
    appends exactly one row per attempt, refusals included.
  * **The file on disk is clean.** The sentinel grep reads the SQLite file's
    bytes, not the `GuardrailAuditEvent` objects the wrapper built -- and then
    the same grep is run against a row deliberately written past the wrapper, so
    "clean" is a result rather than a scan that cannot fail.
  * **The action outranks the audit.** An unwritable audit database logs loudly
    and drops the row; it never stops a coordinator acknowledging a result.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from healthcare_rag.referral_loop import audit
from healthcare_rag.referral_loop.audit import (
    AuditAction,
    AuditScope,
    RefusalCode,
    audited,
    referral_audit_entries,
)
from healthcare_rag.referral_loop.errors import (
    CircularMergeError,
    PackVerificationError,
    ReferralLoopError,
)
from healthcare_rag.referral_loop.pack import load_pack
from healthcare_rag.referral_loop.registry import Registry
from healthcare_rag.referral_loop.store import LoopStore
from healthcare_rag.referral_loop.worklist import create_app
from tests.referral_loop.test_matcher import PACK
from tests.referral_loop.test_pack import PACK as PACK_JSON
from tests.referral_loop.test_pack import _write_pack
from tests.referral_loop.test_worklist import (
    _E2E_ORDER,
    _E2E_RESULT,
    _E2E_SENTINELS,
    _E2E_UNMATCHED,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

MRN_SENTINEL = "ZZSENTINELMRN0001"
REASON_SENTINEL = "ZZSENTINELREASON"


# ------------------------------------------------------------------- fixtures

@pytest.fixture()
def stack(tmp_path):
    store = LoopStore(tmp_path / "loops.db")
    return store, Registry(store)


@pytest.fixture()
def registry(stack):
    return stack[1]


def _resulted(registry, *, mrn=MRN_SENTINEL, modality="CT", obx11="F") -> str:
    loop_id = registry.open_loop(mrn=mrn, modality=modality, control_id="C-ORM")
    registry.record_result(loop_id, obx11=obx11, control_id="C-ORU")
    return loop_id


def _acknowledged(registry, **kw) -> str:
    loop_id = _resulted(registry, **kw)
    registry.acknowledge(loop_id, actor="coordinator-a", role="referral_coordinator",
                         control_id="C-ACK")
    return loop_id


def _rows(action: AuditAction | None = None) -> list[dict]:
    entries = referral_audit_entries()
    if action is None:
        return entries
    return [r for r in entries if json.loads(r["detail"])["action"] == action.value]


def _detail(row: dict) -> dict:
    return json.loads(row["detail"])


# --------------------------------------------------------- what gets audited

def test_an_acknowledgement_appends_exactly_one_row(registry):
    loop_id = _resulted(registry)
    assert _rows() == [], "nothing before the action; the message path is not audited"

    registry.acknowledge(loop_id, actor="Ada Coordinator", role="referral_coordinator",
                         control_id="C-ACK")

    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert _detail(row)["action"] == "referral.acknowledged"
    assert row["outcome"] == "success"
    # immutable_audit's own vocabulary. A consumer of the guardrail stack
    # filters denials on this column, not on `outcome`, so it has to be right.
    assert row["event_type"] == "write"
    assert row["resource_type"] == "referral_loop"
    assert row["resource_id"] == loop_id
    assert row["actor"] == "Ada Coordinator"
    assert _detail(row)["actor_role"] == "referral_coordinator"
    # The literal, not `audit.REFERRAL_TENANT_ID` -- comparing the row against
    # the constant that produced it is a test supplying its own answer, and it
    # survived a mutation that made the constant `socket.gethostname()`.
    assert row["tenant_id"] == "referral-loop-single-site"


def test_the_tenant_constant_identifies_no_site(registry):
    """v1 is a single-site install and `tenant_isolation` is deliberately not
    imported (spec sections 3 and 11), but GuardrailAuditEvent requires a
    tenant_id. Derived from the hostname or the install path it would be
    site-identifying, and it would read as a tenancy boundary this build does
    not test. It is a literal placeholder that says so."""
    import getpass
    import socket

    _acknowledged(registry)
    tenant = _rows()[0]["tenant_id"]

    assert tenant == "referral-loop-single-site"
    for leaky in (socket.gethostname(), getpass.getuser(), str(REPO_ROOT)):
        assert leaky.lower() not in tenant.lower()


def test_a_refused_acknowledgement_on_a_preliminary_read_is_recorded(registry):
    """The event a risk officer asks about by name. An audit that only records
    successes cannot answer them, and `ReferralLoopError` alone cannot say which
    of acknowledge's two refusals fired."""
    loop_id = _resulted(registry, obx11="P")

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="a", role="r", control_id="C-ACK")

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    # A refusal filed under event_type "write" is invisible to anyone querying
    # the guardrail stack for denials, which is who asks about this event.
    assert rows[0]["event_type"] == "deny"
    assert rows[0]["resource_id"] == loop_id
    assert _detail(rows[0])["refusal"] == RefusalCode.PRELIMINARY_NOT_ACKNOWLEDGEABLE.value


def test_a_refusal_on_the_wrong_state_is_recorded_as_a_different_refusal(registry):
    loop_id = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C-ORM")

    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="a", role="r", control_id="C-ACK")

    assert _detail(_rows()[0])["refusal"] == RefusalCode.WRONG_STATE.value


def test_an_acknowledgement_attributed_to_nobody_is_recorded_as_refused(registry):
    loop_id = _resulted(registry)
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(loop_id, actor="", role="", control_id="C-ACK")

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    # No name was given, so the audit says so rather than inventing one.
    assert rows[0]["actor"] == audit.SYSTEM_ACTOR
    assert _detail(rows[0])["refusal"] == "ReferralLoopError"


def test_a_reversal_records_that_a_reason_was_given_and_never_the_reason(registry):
    loop_id = _acknowledged(registry)

    registry.reverse_acknowledgement(
        loop_id, actor="Bo Coordinator", role="referral_coordinator",
        reason=f"wrong loop, this belongs to {REASON_SENTINEL}", control_id="C-UNDO",
    )

    rows = _rows(AuditAction.ACKNOWLEDGEMENT_REVERSED)
    assert len(rows) == 1
    assert _detail(rows[0])["reason_recorded"] is True
    # The reason itself is in loop_events, where the audit needs it and nothing
    # renders it, and nowhere in the audit row.
    assert REASON_SENTINEL not in json.dumps(rows[0])
    stored = [e for e in registry.store.events_for(loop_id) if "reversed_reason" in e.detail]
    assert REASON_SENTINEL in stored[-1].detail["reversed_reason"], "the event log still has it"


def test_a_dismissal_and_its_refusal_are_both_recorded(registry):
    orphan = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
    not_an_orphan = _resulted(registry)

    registry.dismiss_orphan(orphan, actor="a", role="r", reason=REASON_SENTINEL,
                            control_id="C-DIS")
    with pytest.raises(ReferralLoopError):
        registry.dismiss_orphan(not_an_orphan, actor="a", role="r", reason=REASON_SENTINEL,
                                control_id="C-DIS")

    rows = _rows(AuditAction.ORPHAN_DISMISSED)
    assert [r["outcome"] for r in rows] == ["success", "denied"]
    assert [r["resource_id"] for r in rows] == [orphan, not_an_orphan]
    # reason_recorded is a claim about the event log, so it is False when the
    # dismissal never landed there.
    assert [_detail(r)["reason_recorded"] for r in rows] == [True, False]
    assert REASON_SENTINEL not in json.dumps(rows)


def test_a_patient_merge_records_the_size_and_neither_identifier(registry):
    a = registry.open_loop(mrn="ZZRETIRED001", modality="CT", control_id="C1")
    b = registry.open_loop(mrn="ZZRETIRED001", modality="MG", control_id="C2")

    moved = registry.merge_patient("ZZRETIRED001", "ZZSURVIVING9", control_id="A40-1")
    assert sorted(moved) == sorted([a, b]), "the fixture did not actually move loops"

    rows = _rows(AuditAction.PATIENT_MERGED)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["resource_type"] == "referral_patient_merge"
    assert rows[0]["resource_id"] == "patient-merge"
    assert _detail(rows[0])["loops_moved"] == 2
    assert rows[0]["actor"] == audit.ENGINE_ACTOR
    blob = json.dumps(rows[0])
    assert "ZZRETIRED001" not in blob and "ZZSURVIVING9" not in blob


def test_a_circular_merge_is_recorded_as_refused(registry):
    registry.merge_patient("ZZA", "ZZB", control_id="A40-1")
    with pytest.raises(CircularMergeError):
        registry.merge_patient("ZZB", "ZZA", control_id="A40-2")

    rows = _rows(AuditAction.PATIENT_MERGED)
    assert [r["outcome"] for r in rows] == ["success", "denied"]
    assert _detail(rows[1])["refusal"] == "CircularMergeError"


def test_an_alias_reversal_is_recorded_with_its_actor(registry):
    loop_id = registry.open_loop(mrn="ZZRETIRED001", modality="CT", control_id="C1")
    registry.merge_patient("ZZRETIRED001", "ZZSURVIVING9", control_id="A40-1")

    carried = registry.reverse_merge(
        "ZZRETIRED001", actor="Cy Admin", role="him_supervisor",
        reason=f"registration error {REASON_SENTINEL}", control_id="ADMIN-1",
    )
    assert carried == [loop_id], "the fixture did not actually carry a loop back"

    rows = _rows(AuditAction.MERGE_REVERSED)
    assert len(rows) == 1
    assert rows[0]["actor"] == "Cy Admin"
    assert _detail(rows[0])["loops_moved"] == 1
    assert _detail(rows[0])["reason_recorded"] is True
    assert REASON_SENTINEL not in json.dumps(rows[0])


def test_the_pack_in_force_at_boot_is_recorded(tmp_path):
    pubkey = _write_pack(tmp_path, PACK_JSON)
    pack = load_pack(tmp_path, pubkey)

    rows = _rows(AuditAction.PACK_LOADED)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["resource_type"] == "referral_rule_pack"
    assert rows[0]["resource_id"] == pack.version == "1.0.0"
    assert _detail(rows[0])["pack_version"] == "1.0.0"
    assert rows[0]["actor"] == audit.SYSTEM_ACTOR


def test_a_refused_pack_is_recorded_without_its_path(tmp_path):
    """"The site refused to boot on a pack that failed verification" is the more
    interesting of the two facts about what was running."""
    pubkey = _write_pack(tmp_path, PACK_JSON, corrupt=True)
    with pytest.raises(PackVerificationError):
        load_pack(tmp_path, pubkey)

    rows = _rows(AuditAction.PACK_LOADED)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["resource_id"] == audit.UNKNOWN_VERSION
    assert _detail(rows[0])["refusal"] == "PackVerificationError"
    assert str(tmp_path) not in json.dumps(rows[0])


def test_inbound_messages_are_not_audited(tmp_path):
    """The raw archive already holds every message verbatim and durably. A second
    exportable copy doubles the PHI footprint for no added assurance, and would
    break this module's whole claim that no audit value is message-derived."""
    from healthcare_rag.referral_loop.listener import MessageHandler

    store = LoopStore(tmp_path / "loops.db")
    handler = MessageHandler(store=store, registry=Registry(store), pack=PACK)
    for message in (_E2E_ORDER, _E2E_RESULT, _E2E_UNMATCHED):
        assert "|AA|" in handler.handle(message)

    assert len(store.all_loops()) == 2, "the fixture did not actually do any work"
    assert _rows() == []


# --------------------------------------------------- arbitrary text, and how not

def test_a_loop_id_this_system_never_minted_is_not_echoed(registry):
    """A browser can POST to /worklist/<anything>/acknowledge, so the refusal
    path receives a caller-controlled string. Recording it verbatim would hand
    a database designed to be immutable whatever was in the URL."""
    with pytest.raises(ReferralLoopError):
        registry.acknowledge(MRN_SENTINEL, actor="a", role="r", control_id="C")

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["resource_id"] == audit.UNMINTED
    assert MRN_SENTINEL not in json.dumps(rows[0])


def test_the_same_hostile_loop_id_through_the_real_http_surface(tmp_path):
    """Through Flask rather than the registry, because the URL is the actual
    channel and `<path:loop_id>` accepts a great deal."""
    store = LoopStore(tmp_path / "loops.db")
    app = create_app(store=store, registry=Registry(store), pack=PACK)
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.post(f"/worklist/{MRN_SENTINEL}/acknowledge",
                           json={"actor": "a", "role": "r"})
    assert response.status_code == 404, response.data

    assert MRN_SENTINEL not in json.dumps(_rows())


def test_a_minted_loop_id_is_kept_because_it_is_the_join_an_auditor_needs(registry):
    """The complement of the test above: `unminted` must not be what every row
    says, or the audit would be uselessly safe."""
    loop_id = _acknowledged(registry)
    assert re.match(r"^L-[0-9a-f]{12}$", loop_id)
    assert _rows(AuditAction.ACKNOWLEDGED)[0]["resource_id"] == loop_id


def test_the_scope_a_caller_holds_accepts_no_field_of_its_own():
    """`AuditScope` is the only writable surface inside an audited block. If a
    caller could set an attribute of their own, `detail` would be a free-text
    channel again -- which is the shape this whole module exists to close.

    The set is pinned rather than merely checked for prose, so widening it is a
    deliberate edit here. Task 16 added the four retention slots; every one of
    them holds an integer, which is what makes the widening provably safe."""
    scope = AuditScope()
    with pytest.raises(AttributeError):
        scope.mrn = MRN_SENTINEL
    assert set(AuditScope.__slots__) == {
        "loops_moved", "pack_version", "refusal",
        "raw_deleted", "loops_deleted", "raw_retention_days", "resolved_retention_days",
    }


def test_a_detail_key_outside_the_allowlist_is_dropped_rather_than_written(caplog, monkeypatch):
    """The allowlist is enforced at write time, not by review: a contributor who
    adds a key without adding it to `_DETAIL_KEYS` gets a dropped row and an
    ERROR, never a quiet widening of what the audit may contain."""
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(audit, "_DETAIL_KEYS", frozenset({"action"}))
    before = audit.write_failures()

    with audited(AuditAction.ACKNOWLEDGED, loop_id="L-0123456789ab", actor="a", role="r"):
        pass

    assert _rows() == []
    assert audit.write_failures() == before + 1
    assert "ValueError" in caplog.text


def test_a_pasted_note_in_the_actor_field_is_bounded_and_single_lined(registry):
    """The actor is the one field carrying text a human typed, because an audit
    that cannot say who acted is not an audit. It is bounded and collapsed so a
    pasted chart note cannot land in it whole."""
    loop_id = _resulted(registry)
    pasted = "Ada\n\n" + ("x" * 500)

    registry.acknowledge(loop_id, actor=pasted, role="r " * 100, control_id="C")

    row = _rows(AuditAction.ACKNOWLEDGED)[0]
    assert len(row["actor"]) == audit._MAX_ACTOR
    assert "\n" not in row["actor"] and row["actor"].startswith("Ada x")
    assert len(_detail(row)["actor_role"]) <= audit._MAX_ROLE


def test_a_pack_version_of_the_wrong_shape_is_not_echoed():
    with audited(AuditAction.PACK_LOADED, actor="s", role="system") as scope:
        scope.pack_version = f"1.0.0 {MRN_SENTINEL}\n{'x' * 300}"

    row = _rows(AuditAction.PACK_LOADED)[0]
    assert row["resource_id"] == audit.UNKNOWN_VERSION
    assert MRN_SENTINEL not in json.dumps(row)


# ------------------------------------------------------- PHI, on the disk file

def _audit_files() -> list[Path]:
    """The database and any sidecar journal SQLite may have left beside it."""
    db = Path(audit.audit_db_path())
    return [p for p in (db, Path(f"{db}-wal"), Path(f"{db}-journal")) if p.exists()]


def _sentinels_on_disk(sentinels: dict[str, str]) -> dict[str, list[str]]:
    """Which sentinels appear in the audit database's *bytes*.

    The file rather than the objects the wrapper built: a wrapper that
    constructed clean `GuardrailAuditEvent`s and then wrote something else would
    pass every assertion made against its own return values.
    """
    found: dict[str, list[str]] = {}
    for path in _audit_files():
        blob = path.read_bytes()
        hits = sorted(name for name, value in sentinels.items() if value.encode() in blob)
        if hits:
            found[path.name] = hits
    return found


def _drive_every_audited_action(tmp_path, caplog):
    """Real HL7 in, every coordinator action out, including the refused ones."""
    from healthcare_rag.referral_loop.listener import MessageHandler

    store = LoopStore(tmp_path / "loops.db")
    reg = Registry(store)
    handler = MessageHandler(store=store, registry=reg, pack=PACK)
    for message in (_E2E_ORDER, _E2E_RESULT, _E2E_UNMATCHED):
        assert "|AA|" in handler.handle(message)

    app = create_app(store=store, registry=reg, pack=PACK)
    app.config["TESTING"] = True
    client = app.test_client()

    reason = f"belongs to {_E2E_SENTINELS['PID_NAME']}, kin {_E2E_SENTINELS['NK1_NAME']}"
    for loop in store.all_loops():
        for action, body in (
            ("acknowledge", {"actor": "a", "role": "r"}),
            ("dismiss", {"actor": "a", "role": "r", "reason": reason}),
            ("reverse_acknowledgement", {"actor": "a", "role": "r", "reason": reason}),
        ):
            client.post(f"/worklist/{loop.loop_id}/{action}", json=body)

    # A merge and its reversal, on the identifiers the messages carried.
    reg.merge_patient(_E2E_SENTINELS["PID_MRN"], "ZZSURVIVOR", control_id="A40-1")
    reg.reverse_merge(_E2E_SENTINELS["PID_MRN"], actor="admin", role="him", reason=reason,
                      control_id="ADMIN-1")
    # And a hostile loop id straight off the URL.
    client.post(f"/worklist/{_E2E_SENTINELS['PID_MRN']}/acknowledge",
                json={"actor": "a", "role": "r"})
    return store


def test_no_sentinel_reaches_the_audit_database_on_disk(tmp_path, caplog):
    """Spec test 14's fourth artifact, which had nothing to assert against until
    this task. Sentinels are planted in PID, NK1, GT1 and a note segment, driven
    through the listener and every worklist action, and then the audit file's
    bytes are grepped."""
    caplog.set_level(logging.DEBUG)
    _drive_every_audited_action(tmp_path, caplog)

    assert len(_rows()) >= 8, "the drive did not actually produce audit rows"
    assert _sentinels_on_disk(_E2E_SENTINELS) == {}


def test_the_disk_grep_can_actually_detect_a_leak(tmp_path, caplog):
    """A PHI test that cannot fail is the failure mode this task exists to
    prevent. The same scan that just cleared the file is run again after a row
    carrying a sentinel is written straight past the wrapper -- which is exactly
    what a future contributor calling `log_guardrail_event` directly would do."""
    caplog.set_level(logging.DEBUG)
    _drive_every_audited_action(tmp_path, caplog)
    assert _sentinels_on_disk(_E2E_SENTINELS) == {}, "not clean before the leak is planted"

    module = audit._module()
    module.log_guardrail_event(
        module.GuardrailAuditEvent(
            event_type="write",
            resource_type="referral_loop",
            resource_id="L-000000000000",
            tenant_id=audit.REFERRAL_TENANT_ID,
            actor="a",
            outcome="success",
            # The free-text channel, used exactly as it would be by accident.
            detail=json.dumps({"reason": f"belongs to {_E2E_SENTINELS['NK1_NAME']}"}),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )

    assert _sentinels_on_disk(_E2E_SENTINELS) == {
        Path(audit.audit_db_path()).name: ["NK1_NAME"]
    }


def test_the_export_an_auditor_reads_is_clean_too(tmp_path, caplog):
    """The file is one artifact; `referral_audit_entries` is the other, and it is
    the one that actually leaves the building."""
    caplog.set_level(logging.DEBUG)
    _drive_every_audited_action(tmp_path, caplog)

    blob = json.dumps(referral_audit_entries())
    leaked = sorted(k for k, v in _E2E_SENTINELS.items() if v in blob)
    assert leaked == []
    assert blob.count("referral.") >= 8, "the export returned nothing to be clean about"


def test_no_sentinel_reaches_a_log_record_on_the_merge_and_reversal_paths(registry, caplog):
    """Found by probing this task, fixed in the same commit, kept honest here.

    Spec test 14 names logs as one of its four artifacts, and the E2E proof in
    test_worklist greps `caplog` -- but it never reverses a merge, so four log
    lines on the identity paths were never covered. `store.reverse_alias` logged
    both MRNs *and* the coordinator's free-text reason at WARNING, which is the
    unbounded free-text channel arriving through a door nothing was watching.
    """
    caplog.set_level(logging.DEBUG)
    reason = f"registration error, see {_E2E_SENTINELS['PID_NAME']}"

    registry.open_loop(mrn=_E2E_SENTINELS["PID_MRN"], modality="CT", control_id="C1")
    registry.merge_patient(_E2E_SENTINELS["PID_MRN"], "ZZSURVIVOR", control_id="A40-1")
    registry.merge_patient(_E2E_SENTINELS["PID_MRN"], "ZZSURVIVOR", control_id="A40-2")
    registry.merge_patient("ZZSAME", "ZZSAME", control_id="A40-3")
    registry.reverse_merge(_E2E_SENTINELS["PID_MRN"], actor="admin", role="him",
                           reason=reason, control_id="ADMIN-1")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text.strip(), "nothing was logged, so the grep would pass vacuously"
    leaked = sorted(k for k, v in _E2E_SENTINELS.items() if v in text)
    assert leaked == [], f"identifiers reached a log record: {leaked}"
    assert "registration error" not in text, "the free-text reason reached a log record"
    # And the operator can still see that each thing happened.
    assert "itself" in text and "reversed" in text


def test_the_export_returns_referral_rows_and_nothing_else(registry):
    """`referral_audit_entries` is the export an auditor is handed. The database
    is shared with the rest of the guardrail stack, and handing over another
    subsystem's PHI-adjacent rows because they happened to be in the same file
    would be a disclosure this subsystem never made."""
    _acknowledged(registry)

    module = audit._module()
    module.log_guardrail_event(
        module.GuardrailAuditEvent(
            event_type="read", resource_type="patient_record", resource_id="P-999",
            tenant_id=audit.REFERRAL_TENANT_ID, actor="another-subsystem",
            outcome="success", detail=json.dumps({"note": MRN_SENTINEL}),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )

    assert len(module.export_audit_trail()) == 2, "the foreign row was not written"
    exported = referral_audit_entries()
    assert [r["resource_type"] for r in exported] == ["referral_loop"]
    assert MRN_SENTINEL not in json.dumps(exported)


def test_the_audit_logger_never_puts_a_database_message_in_a_log_record(caplog, monkeypatch):
    """Log records are an artifact spec test 14 greps, and a sqlite3 message
    carries the database path. The exception type goes in the log, never
    `str(exc)`."""
    caplog.set_level(logging.ERROR)

    def explode(_event):
        raise RuntimeError(f"disk full while writing {MRN_SENTINEL}")

    monkeypatch.setattr(audit._module(), "log_guardrail_event", explode)

    with audited(AuditAction.ACKNOWLEDGED, loop_id="L-0123456789ab", actor="a", role="r"):
        pass

    assert caplog.records, "nothing was logged at all"
    assert MRN_SENTINEL not in caplog.text
    assert "RuntimeError" in caplog.text


# ------------------------------------------------------------ failure policy

def test_an_unwritable_audit_does_not_stop_a_coordinator(registry, caplog, monkeypatch):
    """The decision this module turns on. A `loop_events` write that fails must
    block, because the clinical fact would be lost. An audit write that fails
    loses a duplicate -- actor, role, reason and time are already durable in
    `loop_events` -- and failing closed would wedge the worklist, manufacturing
    the unfollowed-up result the product exists to prevent."""
    caplog.set_level(logging.ERROR)
    loop_id = _resulted(registry)
    before = audit.write_failures()

    def explode(_event):
        raise OSError("audit volume is full")

    monkeypatch.setattr(audit._module(), "log_guardrail_event", explode)

    registry.acknowledge(loop_id, actor="a", role="r", control_id="C-ACK")

    from healthcare_rag.referral_loop.events import LoopState
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED, "the action was blocked"
    assert audit.write_failures() == before + 1
    assert "OSError" in caplog.text and "dropped" in caplog.text


def test_a_failing_init_does_not_stop_a_coordinator_either(registry, caplog, monkeypatch):
    """Two places can fail, and only one of them was obvious."""
    caplog.set_level(logging.ERROR)
    loop_id = _resulted(registry)
    audit._initialised_for = None
    monkeypatch.setattr(audit._module(), "init_audit_db",
                        lambda: (_ for _ in ()).throw(PermissionError("read-only volume")))

    registry.acknowledge(loop_id, actor="a", role="r", control_id="C-ACK")

    from healthcare_rag.referral_loop.events import LoopState
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED
    assert "PermissionError" in caplog.text


def test_a_transient_failure_does_not_disable_auditing_for_the_process(registry, monkeypatch):
    """The init cache is cleared on failure. Caching a failed init would mean one
    bad moment at startup silently turns the audit off until a restart."""
    first = _resulted(registry, modality="CT")
    second = _resulted(registry, modality="MG")
    audit._initialised_for = None

    calls = {"n": 0}
    real_init = audit._module().init_audit_db

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        real_init()

    monkeypatch.setattr(audit._module(), "init_audit_db", flaky)

    registry.acknowledge(first, actor="a", role="r", control_id="C1")
    assert _rows() == []

    registry.acknowledge(second, actor="a", role="r", control_id="C2")
    assert len(_rows()) == 1, "auditing stayed off after a transient failure"


def test_the_action_is_recorded_only_after_it_committed(registry, monkeypatch):
    """A row saying `success` for an append that raised would be worse than no
    row: the audit would be evidence for something that did not happen."""
    loop_id = _resulted(registry)
    monkeypatch.setattr(registry.store, "append_event",
                        lambda _e: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        registry.acknowledge(loop_id, actor="a", role="r", control_id="C")

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["event_type"] == "deny"
    assert _detail(rows[0])["refusal"] == "OSError"


# ------------------------------------------------------- init and the db file

def test_init_is_idempotent_and_creates_the_database_on_a_fresh_install(tmp_path):
    """`AUDIT_DB` lives under a `data/` directory that a fresh install does not
    have. `init_audit_db` makedirs it; asserted here rather than assumed, because
    the failure would only show up on the first install."""
    fresh = tmp_path / "brand" / "new" / "data" / "audit_trail.db"
    assert not fresh.parent.exists()

    audit.set_audit_db(fresh)
    with audited(AuditAction.PACK_LOADED, actor="s", role="system") as scope:
        scope.pack_version = "1.0.0"
    assert fresh.exists()

    audit._initialised_for = None  # force a second init over an existing schema
    with audited(AuditAction.PACK_LOADED, actor="s", role="system") as scope:
        scope.pack_version = "1.0.1"

    assert [r["resource_id"] for r in _rows()] == ["1.0.0", "1.0.1"]


def test_init_runs_once_per_path_not_once_per_write(registry, monkeypatch):
    audit._initialised_for = None
    calls = {"n": 0}
    real_init = audit._module().init_audit_db

    def counted():
        calls["n"] += 1
        real_init()

    monkeypatch.setattr(audit._module(), "init_audit_db", counted)

    for modality in ("CT", "MG", "US"):
        registry.acknowledge(_resulted(registry, modality=modality), actor="a", role="r",
                             control_id="C")

    assert len(_rows()) == 3
    assert calls["n"] == 1


def test_the_default_audit_database_is_the_guardrail_stacks_own():
    """Spec section 3: referral audit routes through immutable_audit, which
    "already owns its own append-only database". Not a new file beside the loop
    store, and not `rag_growth.db` -- `db.py` is excluded.

    The conftest redirects the live path, so this reads the value captured
    before any redirect rather than the patched one.
    """
    from tests.referral_loop.conftest import INSTALLED_AUDIT_DB

    installed = Path(INSTALLED_AUDIT_DB).resolve()
    assert installed == (REPO_ROOT / "data" / "audit_trail.db").resolve()
    assert installed.name != "rag_growth.db"


# ---------------------------------------------------------------- immutability

def test_a_referral_audit_row_cannot_be_updated_or_deleted(registry):
    """The property spec section 3 says the audit database already has. Asserted
    on a row this subsystem wrote, because "the module blocks it" and "our rows
    are blocked" are different claims."""
    import sqlite3

    _acknowledged(registry)
    assert len(_rows()) == 1

    conn = sqlite3.connect(audit.audit_db_path())
    try:
        for statement in ("UPDATE audit_events SET actor = 'someone else'",
                          "DELETE FROM audit_events"):
            with pytest.raises(sqlite3.Error):
                conn.execute(statement)
                conn.commit()
    finally:
        conn.close()

    assert len(_rows()) == 1
    assert _rows()[0]["actor"] == "coordinator-a"


# ---------------------------------------------------------------- concurrency

def test_two_coordinators_acting_at_once_produce_two_rows(registry):
    """`immutable_audit` serialises on its own `_db_lock`, and the registry on
    its own; nothing coordinates the two. Two coordinators clearing their queues
    at the same second is the ordinary case, not the exotic one."""
    loops = [_resulted(registry, modality=m) for m in ("CT", "MG", "US", "XR", "NM", "CR")]
    barrier = threading.Barrier(len(loops))
    errors: list[BaseException] = []

    def clear(loop_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            registry.acknowledge(loop_id, actor=f"coordinator-{loop_id[-2:]}", role="r",
                                 control_id="C")
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=clear, args=(loop_id,)) for loop_id in loops]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    rows = _rows(AuditAction.ACKNOWLEDGED)
    assert sorted(r["resource_id"] for r in rows) == sorted(loops)
    assert audit.write_failures() == 0


# ------------------------------------------------- how the module is loaded

def test_the_audit_database_path_is_not_computed_from_a_parent_repo_layout():
    """The vendored module inherited a path built by walking up two directories from
    healthcare_rag/guardrails/. In this repo that walk lands outside the package, so the
    path must come from configuration or a package-relative default, never from ``..``."""
    from referral_loop import immutable_audit

    source = Path(immutable_audit.__file__).read_text(encoding="utf-8")
    assert '".."' not in source, "the audit DB path still walks up out of the package"
    assert os.path.isabs(immutable_audit.AUDIT_DB), immutable_audit.AUDIT_DB


_PROBE = r"""
import json, sys
from healthcare_rag.referral_loop import audit
audit.set_audit_db(sys.argv[1])
with audit.audited(audit.AuditAction.PACK_LOADED, actor="s", role="system") as scope:
    scope.pack_version = "1.0.0"
assert audit.write_failures() == 0, "the probe did not manage to write a row"
print(json.dumps(sorted(m for m in sys.modules if "guardrails" in m)))
"""


def _probe(tmp_path, prelude: str = "") -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", prelude + _PROBE, str(tmp_path / "probe.db")],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        pytest.fail(f"probe failed (exit {proc.returncode}):\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_auditing_does_not_pull_in_tenant_isolation(tmp_path):
    """The behavioural form of the import-closure test. `test_import_closure`
    imports the package and looks at `sys.modules`, which cannot see a lazy
    import; this actually writes an audit row and then looks.

    Spec section 3 keeps `tenant_isolation` out because "importing an unexercised
    isolation control would suggest a guarantee the build does not test", and
    `healthcare_rag/guardrails/__init__.py` re-exports it -- so importing the one
    permitted module the ordinary way would settle that question the wrong way.
    """
    loaded = _probe(tmp_path)
    assert "healthcare_rag.guardrails.immutable_audit" in loaded
    assert "healthcare_rag.guardrails.tenant_isolation" not in loaded
    assert "healthcare_rag.guardrails" not in loaded


def test_one_module_object_whichever_import_happens_first(tmp_path):
    """Two module objects would mean two `_db_lock`s over one file, which is
    worse than the problem being avoided. Asserted in both orders, in a clean
    interpreter, because in-process the answer depends on what ran before."""
    check = (
        "import sys\n"
        "from healthcare_rag.referral_loop import audit\n"
        "from healthcare_rag.guardrails import immutable_audit as pkg\n"
        "assert audit._module() is pkg, 'two module objects over one database'\n"
        "assert audit._module()._db_lock is pkg._db_lock\n"
    )
    _probe(tmp_path, prelude=check)
    _probe(tmp_path, prelude=(
        "from healthcare_rag.guardrails import immutable_audit as pkg\n"
        "from healthcare_rag.referral_loop import audit\n"
        "assert audit._module() is pkg, 'two module objects over one database'\n"
    ))


def test_the_suite_never_writes_to_the_installed_audit_database():
    """The conftest redirect, asserted rather than trusted. The installed
    database is append-only, so a row written to it by a test could not be
    removed afterwards -- the isolation has to hold on the first write."""
    installed = (REPO_ROOT / "data" / "audit_trail.db").resolve()
    assert Path(audit.audit_db_path()).resolve() != installed


def test_the_redirect_survives_a_test_calling_monkeypatch_undo(registry, monkeypatch):
    """The regression guard for a leak this task actually caused.

    pytest hands every fixture and the test body the *same* function-scoped
    monkeypatch instance, and six tests in this package call `monkeypatch.undo()`
    to take their own patch off mid-test. When the conftest used monkeypatch too,
    that undo reverted the audit redirect as well, and three merge tests then
    wrote to the installed database -- three rows per suite run, unremovable,
    because the thing they were writing to is append-only.
    """
    monkeypatch.setattr(audit, "REFERRAL_TENANT_ID", "irrelevant")
    monkeypatch.undo()

    installed = (REPO_ROOT / "data" / "audit_trail.db").resolve()
    assert Path(audit.audit_db_path()).resolve() != installed

    registry.merge_patient("ZZA", "ZZB", control_id="A40-1")
    assert len(_rows(AuditAction.PATIENT_MERGED)) == 1
