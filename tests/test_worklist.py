"""The coordinator worklist: queues, sorting, actions, and what leaves the building.

Three properties are load-bearing here and each is asserted against the rendered
artifact rather than against an intermediate:

  * **PHI.** Spec test 14 plants sentinels in PID/NK1/GT1 and note segments and
    asserts zero occurrences in worklist HTML, logs, exports and audit entries.
    The tests below plant an MRN sentinel through the real registry and grep the
    HTML, the JSON, and captured log records -- and then deliberately break the
    renderer to prove the grep can actually fail.
  * **The claim.** ACKNOWLEDGED means a coordinator confirmed the result belongs
    to this loop. It does not mean a clinician read it, and no word on the page
    may suggest otherwise.
  * **The queues.** open_loops() alone is not the worklist. A loop reopened by a
    correction sits in RESULTED with its acknowledgement cleared and open_loops()
    does not select it -- the population most needing a human.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from referral_loop import worklist as worklist_module
from referral_loop.errors import StoreUnavailableError
from referral_loop.events import LoopState
from referral_loop.registry import Registry
from referral_loop.store import LoopStore
from referral_loop.worklist import (
    create_app,
    create_blueprint,
    make_worklist_server,
)
from tests._pack import PACK

# Planted where a real PID-3 lands. If any of these strings reaches HTML, JSON or
# a log record, the product has an egress problem, not a formatting problem.
MRN_SENTINEL = "ZZSENTINELMRN0001"
NOTE_SENTINEL = "ZZSENTINELNOTE"


@pytest.fixture()
def accepted(monkeypatch):
    """The site has accepted the shipped staleness thresholds (spec q3)."""
    monkeypatch.setenv("REFERRAL_THRESHOLDS_ACCEPTED", "1")


@pytest.fixture()
def stack(tmp_path, accepted):
    store = LoopStore(tmp_path / "loops.db")
    registry = Registry(store)
    app = create_app(store=store, registry=registry, pack=PACK)
    app.config["TESTING"] = True
    client = app.test_client()
    # This client stands in for the coordinator's browser, so it sends what one
    # sends: an Origin matching the Host the test client defaults to. Without it
    # every form post below would be indistinguishable from a cross-site one --
    # which is the point of the host/origin tests, and would be an accident here.
    client.environ_base["HTTP_ORIGIN"] = "http://localhost"
    return client, registry, store


@pytest.fixture()
def http(stack):
    return stack[0]


@pytest.fixture()
def registry(stack):
    return stack[1]


# ---------------------------------------------------------------- helpers

def _resulted(registry, *, mrn=MRN_SENTINEL, modality="CT", obx11="F", ordered_at=None, loop_id=None):
    loop_id = registry.open_loop(
        mrn=mrn, modality=modality, control_id=f"C-{loop_id or modality}-1",
        ordered_at=ordered_at, loop_id=loop_id,
    )
    registry.record_result(loop_id, obx11=obx11, control_id=f"C-{loop_id}-2")
    return loop_id


def _acknowledged(registry, **kwargs):
    loop_id = _resulted(registry, **kwargs)
    registry.acknowledge(loop_id, actor="coordinator-a", role="referral_coordinator",
                         control_id="C-ACK")
    return loop_id


def _queues(http, query=""):
    response = http.get(f"/worklist/?format=json{query}")
    assert response.status_code == 200, response.data
    return response.get_json()["queues"]


def _ids(rows):
    return [r["loop_id"] for r in rows]


# ---------------------------------------------------------------- queues

def test_a_loop_awaiting_a_result_is_on_the_worklist(http, registry):
    loop_id = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C1")
    assert _ids(_queues(http)["awaiting_result"]) == [loop_id]
    assert loop_id.encode() in http.get("/worklist/").data


def test_a_resulted_loop_moves_to_the_acknowledgement_queue(http, registry):
    loop_id = _resulted(registry)
    queues = _queues(http)
    assert _ids(queues["awaiting_result"]) == []
    assert _ids(queues["awaiting_acknowledgement"]) == [loop_id]


def test_a_loop_reopened_by_a_correction_is_still_on_a_queue(http, registry):
    """open_loops() does not select RESULTED, so this loop was invisible to every
    accessor until resulted_unacknowledged() existed. It is the population most
    needing a human: an acknowledgement was cleared by safety rule 2."""
    loop_id = _acknowledged(registry)
    registry.record_result(loop_id, obx11="C", control_id="C-CORR")
    assert registry.get(loop_id).state is LoopState.RESULTED

    queues = _queues(http)
    assert _ids(queues["awaiting_result"]) == []
    assert _ids(queues["awaiting_acknowledgement"]) == [loop_id]


def test_orphans_have_their_own_queue(http, registry):
    orphan_id = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL,
                                detail={"modality": "CT", "match_tier": 5})
    queues = _queues(http)
    assert _ids(queues["orphans"]) == [orphan_id]
    assert _ids(queues["awaiting_result"]) == []
    assert _ids(queues["awaiting_acknowledgement"]) == []


def test_terminal_loops_are_on_no_queue(http, registry):
    """ACKNOWLEDGED, CANCELLED and DISMISSED are all terminal for v1. A worklist
    that kept showing them is a queue that only grows."""
    acked = _acknowledged(registry, modality="CT")

    cancelled = registry.open_loop(mrn=MRN_SENTINEL, modality="MG", control_id="C-CANCEL-1")
    registry.cancel(cancelled, control_id="C-CANCEL-2")

    dismissed = registry.orphan(control_id="C-ORU2", mrn=MRN_SENTINEL, detail={"modality": "MG"})
    registry.dismiss_orphan(dismissed, actor="a", role="referral_coordinator",
                            reason="another facility")

    every_id = [r["loop_id"] for rows in _queues(http).values() for r in rows]
    assert acked not in every_id
    assert cancelled not in every_id
    assert dismissed not in every_id
    assert every_id == []


# ---------------------------------------------------------------- sorting

def test_staleness_is_the_primary_sort(http, registry):
    now = datetime.now(timezone.utc)
    # PACK: CT threshold 4h, _default 336h.
    fresh = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C-F",
                               ordered_at=now - timedelta(hours=1))
    stale = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C-S",
                               ordered_at=now - timedelta(hours=40))
    very_stale = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C-V",
                                    ordered_at=now - timedelta(hours=400))

    rows = _queues(http)["awaiting_result"]
    assert _ids(rows) == [very_stale, stale, fresh]
    assert [r["is_stale"] for r in rows] == [True, True, False]


def test_an_unknown_order_time_outranks_every_stale_loop(http, registry, store_bypass):
    """staleness_ratio returns +inf for a loop with no ordered_at, deliberately:
    a data-quality gap must fail toward visibility, not sit at the quiet bottom."""
    now = datetime.now(timezone.utc)
    very_stale = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C-V",
                                    ordered_at=now - timedelta(hours=4000))
    unknown = store_bypass("L-noclock", {"mrn": MRN_SENTINEL, "modality": "CT", "ordered_at": ""})

    rows = _queues(http)["awaiting_result"]
    assert _ids(rows) == [unknown, very_stale]
    assert rows[0]["staleness_ratio"] is None, "maximally stale is JSON null, never Infinity"
    assert rows[0]["is_stale"] is True
    assert rows[0]["age_basis"] == "unknown"


def test_the_sort_is_deterministic_for_equally_stale_loops(http, registry):
    at = datetime.now(timezone.utc) - timedelta(hours=40)
    ids = sorted(registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id=f"C{i}",
                                    ordered_at=at, loop_id=f"L-tie-{i}")
                 for i in range(4))
    assert _ids(_queues(http)["awaiting_result"]) == ids


def test_an_orphan_is_aged_from_when_it_arrived(http, stack):
    """Found by mutation: an orphan has no order date -- a result nobody ordered
    has nothing to measure from -- so without the arrival clock every orphan
    showed a blank age and the queue lost the one number that tells a coordinator
    it is being allowed to grow."""
    from referral_loop.events import LoopEvent

    _, _, store = stack
    arrived = datetime.now(timezone.utc) - timedelta(hours=30)
    store.append_event(LoopEvent("O-old", "orphaned", arrived, "C-ORU",
                                 {"mrn": MRN_SENTINEL, "modality": "CT"}))

    row = _queues(http)["orphans"][0]
    assert row["age_basis"] == "arrived"
    assert 29.5 < row["age_hours"] < 30.5, row["age_hours"]


def test_age_breaks_a_tie_between_two_equally_stale_loops(http, registry):
    """Found by mutation: equal ratios fell back to loop id, so of two loops the
    same multiple past threshold the one waiting eleven weeks longer could sort
    below the one waiting eight hours. PACK: CT threshold 4h, _default 336h, so
    these two ratios are both 2.0 on very different clocks."""
    now = datetime.now(timezone.utc)
    quick = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C-Q",
                               loop_id="L-a-quick", ordered_at=now - timedelta(hours=8))
    slow = registry.open_loop(mrn=MRN_SENTINEL, modality="MG", control_id="C-S",
                              loop_id="L-z-slow", ordered_at=now - timedelta(hours=672))

    rows = _queues(http)["awaiting_result"]
    assert [r["staleness_ratio"] for r in rows] == [2.0, 2.0], "the tie was not actually a tie"
    assert _ids(rows) == [slow, quick], "loop id decided it, not age"


def test_json_never_emits_a_non_finite_number(http, registry, store_bypass):
    """Infinity is not valid JSON. A consumer parsing strictly would fail on the
    single row the sort exists to surface."""
    store_bypass("L-noclock", {"mrn": MRN_SENTINEL, "modality": "CT", "ordered_at": ""})
    body = http.get("/worklist/?format=json").data.decode()
    assert "Infinity" not in body and "NaN" not in body


# ---------------------------------------------------------------- thresholds gate

def test_unaccepted_thresholds_produce_a_legible_refusal_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    store = LoopStore(tmp_path / "loops.db")
    registry = Registry(store)
    registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C1")
    app = create_app(store=store, registry=registry, pack=PACK)
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.get("/worklist/")
    assert response.status_code == 503
    body = response.data.decode()
    assert "REFERRAL_THRESHOLDS_ACCEPTED" in body
    assert "staleness_hours" in body
    assert "Traceback" not in body

    payload = client.get("/worklist/?format=json").get_json()
    assert payload["error_type"] == "ThresholdsNotAcceptedError"
    assert "REFERRAL_THRESHOLDS_ACCEPTED" in payload["error"]


def test_an_empty_worklist_still_refuses_when_thresholds_are_unaccepted(tmp_path, monkeypatch):
    """Found by mutation: removing the explicit gate from the view left every
    other test passing, because each row's staleness call gates on its own. With
    no loops there are no rows, so a fresh install would serve a normal-looking
    empty worklist and only start refusing once the first loop arrived -- the
    operator learning about the gate at the worst possible moment."""
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    store = LoopStore(tmp_path / "loops.db")
    app = create_app(store=store, registry=Registry(store), pack=PACK)
    app.config["TESTING"] = True

    assert store.all_loops() == []
    assert app.test_client().get("/worklist/").status_code == 503
    assert app.test_client().get("/worklist/?format=json").status_code == 503


def test_a_store_failure_is_a_503_that_echoes_nothing_the_store_said(stack, monkeypatch, caplog):
    """A store error's message is composed from whatever SQLite said, and both
    the response and the log line are artifacts test 14 greps. The type is
    logged; the text is not."""
    caplog.set_level(logging.DEBUG)
    http, _, store = stack
    monkeypatch.setattr(
        store, "open_loops",
        lambda *a, **k: (_ for _ in ()).throw(
            StoreUnavailableError(f"disk full while reading {MRN_SENTINEL}")),
    )
    response = http.get("/worklist/?format=json")
    assert response.status_code == 503
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert MRN_SENTINEL not in response.data.decode()
    assert MRN_SENTINEL not in logs
    assert "StoreUnavailableError" in logs


def test_an_unset_acceptance_env_var_does_not_disable_a_safety_action(tmp_path, monkeypatch):
    """The gate is about a staleness *claim*. Acknowledgement makes none, and
    wedging the safety actions behind an unrelated env var would be worse than
    the unsorted page it prevents."""
    monkeypatch.delenv("REFERRAL_THRESHOLDS_ACCEPTED", raising=False)
    store = LoopStore(tmp_path / "loops.db")
    registry = Registry(store)
    loop_id = _resulted(registry)
    app = create_app(store=store, registry=registry, pack=PACK)
    app.config["TESTING"] = True

    response = app.test_client().post(
        f"/worklist/{loop_id}/acknowledge",
        json={"actor": "coordinator-a", "role": "referral_coordinator"},
    )
    assert response.status_code == 200
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


# ---------------------------------------------------------------- acknowledge

def test_acknowledging_a_final_result_records_the_match(http, registry):
    loop_id = _resulted(registry)
    response = http.post(f"/worklist/{loop_id}/acknowledge",
                         json={"actor": "coordinator-a", "role": "referral_coordinator"})
    assert response.status_code == 200
    assert response.get_json()["state"] == "ACKNOWLEDGED"
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED
    assert _ids(_queues(http)["awaiting_acknowledgement"]) == []


def test_acknowledging_a_preliminary_is_refused_with_a_renderable_4xx(http, registry):
    """Safety rule 1. A radiology prelim that later corrects is the malpractice
    scenario; auto-resolving on it would make the tool the cause."""
    loop_id = _resulted(registry, obx11="P")
    response = http.post(f"/worklist/{loop_id}/acknowledge",
                         json={"actor": "coordinator-a", "role": "referral_coordinator"})
    assert response.status_code == 409
    payload = response.get_json()
    assert "preliminary" in payload["detail"].lower()
    assert registry.get(loop_id).state is LoopState.RESULTED
    assert _ids(_queues(http)["awaiting_acknowledgement"]) == [loop_id]


def test_acknowledging_twice_is_refused(http, registry):
    loop_id = _acknowledged(registry)
    response = http.post(f"/worklist/{loop_id}/acknowledge",
                         json={"actor": "coordinator-b", "role": "referral_coordinator"})
    assert response.status_code == 409
    assert "ACKNOWLEDGED" in response.get_json()["error"]
    assert registry.get(loop_id).ack_by == "coordinator-a"


def test_acknowledging_an_open_loop_is_refused(http, registry):
    loop_id = registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C1")
    response = http.post(f"/worklist/{loop_id}/acknowledge",
                         json={"actor": "a", "role": "referral_coordinator"})
    assert response.status_code == 409
    # No result has arrived, so the preliminary explanation would be a lie.
    assert "preliminary" not in response.get_json().get("detail", "").lower()


def test_an_unknown_loop_is_404_not_500(http):
    response = http.post("/worklist/L-does-not-exist/acknowledge",
                         json={"actor": "a", "role": "referral_coordinator"})
    assert response.status_code == 404


# ---------------------------------------------------------------- reversal

def test_a_coordinator_can_undo_their_own_acknowledgement(http, registry):
    """Spec rule 4. Rule 2 recovers the machine's error; this recovers the human's."""
    loop_id = _acknowledged(registry)
    response = http.post(f"/worklist/{loop_id}/reverse_acknowledgement",
                         json={"actor": "coordinator-a", "role": "referral_coordinator",
                               "reason": "acknowledged the wrong loop"})
    assert response.status_code == 200
    assert registry.get(loop_id).state is LoopState.RESULTED
    assert _ids(_queues(http)["awaiting_acknowledgement"]) == [loop_id]


def test_the_page_s_own_undo_form_target_works(http, registry):
    """The reversal needs a loop id the row forms cannot supply, so the page
    posts to /undo with one in the body. Untested, that form is a dead button on
    the only route back from a wrong acknowledgement."""
    loop_id = _acknowledged(registry)
    assert b'action="/worklist/undo"' in http.get("/worklist/").data

    response = http.post("/worklist/undo", data={
        "loop_id": loop_id, "actor": "coordinator-a",
        "role": "referral_coordinator", "reason": "acknowledged the wrong loop"})
    assert response.status_code == 200
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_undo_without_a_loop_id_is_a_400(http):
    assert http.post("/worklist/undo", data={"actor": "a", "role": "r",
                                             "reason": "x"}).status_code == 400


def test_undo_on_an_unknown_loop_is_a_404(http):
    response = http.post("/worklist/undo", data={
        "loop_id": "L-nope", "actor": "a", "role": "r", "reason": "x"})
    assert response.status_code == 404


def test_reversing_a_loop_that_was_never_acknowledged_is_refused(http, registry):
    loop_id = _resulted(registry)
    response = http.post(f"/worklist/{loop_id}/reverse_acknowledgement",
                         json={"actor": "a", "role": "referral_coordinator", "reason": "oops"})
    assert response.status_code == 409


def test_a_reversal_without_a_reason_is_refused(http, registry):
    loop_id = _acknowledged(registry)
    response = http.post(f"/worklist/{loop_id}/reverse_acknowledgement",
                         json={"actor": "a", "role": "referral_coordinator"})
    assert response.status_code == 400
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


# ---------------------------------------------------------------- dismissal

def test_an_orphan_can_be_dismissed_with_a_reason(http, registry):
    orphan_id = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
    response = http.post(f"/worklist/{orphan_id}/dismiss",
                         json={"actor": "coordinator-a", "role": "referral_coordinator",
                               "reason": "misrouted from another facility"})
    assert response.status_code == 200
    assert registry.get(orphan_id).state is LoopState.DISMISSED
    assert _ids(_queues(http)["orphans"]) == []


def test_dismissing_a_non_orphan_is_refused(http, registry):
    loop_id = _resulted(registry)
    response = http.post(f"/worklist/{loop_id}/dismiss",
                         json={"actor": "a", "role": "referral_coordinator", "reason": "no"})
    assert response.status_code == 409
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_dismissal_is_terminal(http, registry):
    orphan_id = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
    body = {"actor": "a", "role": "referral_coordinator", "reason": "another facility"}
    assert http.post(f"/worklist/{orphan_id}/dismiss", json=body).status_code == 200
    assert http.post(f"/worklist/{orphan_id}/dismiss", json=body).status_code == 409


# ---------------------------------------------------------------- input validation

@pytest.mark.parametrize("action, body", [
    ("acknowledge", {"actor": "", "role": "referral_coordinator"}),
    ("acknowledge", {"actor": "   ", "role": "referral_coordinator"}),
    ("acknowledge", {"actor": "a", "role": " \t "}),
    ("acknowledge", {"role": "referral_coordinator"}),
    ("acknowledge", {"actor": 7, "role": "referral_coordinator"}),
    ("reverse_acknowledgement", {"actor": "a", "role": "r", "reason": "  "}),
    ("reverse_acknowledgement", {"actor": "a", "role": "r"}),
    ("dismiss", {"actor": "a", "role": "r", "reason": "\n\n"}),
    ("dismiss", {"actor": " ", "role": "r", "reason": "why"}),
])
def test_blank_or_missing_attribution_is_a_400(http, registry, action, body):
    loop_id = _acknowledged(registry)
    orphan_id = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
    target = orphan_id if action == "dismiss" else loop_id
    response = http.post(f"/worklist/{target}/{action}", json=body)
    assert response.status_code == 400, response.data


def test_a_missing_body_is_a_400_not_a_500(http, registry):
    loop_id = _acknowledged(registry)
    assert http.post(f"/worklist/{loop_id}/reverse_acknowledgement").status_code == 400


def test_an_absurdly_long_reason_is_refused(http, registry):
    loop_id = _acknowledged(registry)
    response = http.post(f"/worklist/{loop_id}/reverse_acknowledgement",
                         json={"actor": "a", "role": "r", "reason": "x" * 5000})
    assert response.status_code == 400
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_attribution_is_stored_stripped(http, registry):
    loop_id = _resulted(registry)
    http.post(f"/worklist/{loop_id}/acknowledge",
              json={"actor": "  coordinator-a  ", "role": " referral_coordinator "})
    loop = registry.get(loop_id)
    assert loop.ack_by == "coordinator-a"
    assert loop.ack_role == "referral_coordinator"


def test_a_form_post_works_too(http, registry):
    """A coordinator uses the page, not curl. The forms in the HTML must post
    something the endpoints accept."""
    loop_id = _resulted(registry)
    response = http.post(f"/worklist/{loop_id}/acknowledge",
                         data={"actor": "coordinator-a", "role": "referral_coordinator"})
    assert response.status_code == 200


# ---------------------------------------------------------------- merges

def test_recent_merges_are_surfaced(http, registry):
    """Spec section 4 decision 1: a coordinator must see a merge happen rather
    than infer it from work that quietly stopped appearing."""
    registry.open_loop(mrn="MRN-RETIRED", modality="CT", control_id="C1")
    registry.merge_patient("MRN-RETIRED", "MRN-SURVIVING", control_id="A40-1")

    payload = http.get("/worklist/?format=json").get_json()
    assert len(payload["recent_merges"]) == 1
    merge = payload["recent_merges"][0]
    assert merge["event_type"] == "established"
    assert merge["recorded_by"] == "A40-1"
    assert "merge" in http.get("/worklist/").data.decode().lower()


def test_the_merge_feed_is_newest_first_and_bounded(http, registry):
    """Found by mutation. Oldest-first means the merge that just happened is at
    the bottom of an unbounded list, which is the same as not surfacing it; and
    unbounded means the notice becomes a log and stops being read."""
    for i in range(25):
        registry.merge_patient(f"MRN-OLD-{i:02d}", f"MRN-NEW-{i:02d}", control_id=f"A40-{i:02d}")

    merges = http.get("/worklist/?format=json").get_json()["recent_merges"]
    assert len(merges) == 20, "the feed is unbounded"
    assert merges[0]["recorded_by"] == "A40-24", "oldest first: the newest merge is buried"
    assert merges[-1]["recorded_by"] == "A40-05"
    ids = [m["alias_event_id"] for m in merges]
    assert ids == sorted(ids, reverse=True)


def test_a_merge_is_surfaced_without_either_identifier(http, registry):
    """The merge is the event a coordinator needs to see; the two MRNs are not."""
    registry.merge_patient("ZZSENTINELRETIRED", "ZZSENTINELSURVIVING", control_id="A40-1")
    for body in (http.get("/worklist/").data, http.get("/worklist/?format=json").data):
        assert b"ZZSENTINELRETIRED" not in body
        assert b"ZZSENTINELSURVIVING" not in body


# ---------------------------------------------------------------- injection

# The probe these tests plant to prove Jinja did not evaluate hostile input, and
# the string that appears if and only if it did.
#
# It used to be `{{ 7*7 }}` / `"49"`, asserted absent from the whole document,
# and that assertion collided with the document by construction: `open_loop`
# mints ids as `L-<12 random hex chars>`, so roughly one rendered loop in
# twenty-five carries a literal "49" in its id, and the ages and ratios on the
# page are decimal too. The suite went red on `assert '49' not in body` with
# nothing wrong with the product.
#
# The whole document is the right scope -- an evaluated expression can surface
# anywhere the value is interpolated, not only in the cell it was planted in --
# so the fix is to the marker rather than the scope. Two properties make a
# collision impossible rather than merely unlikely:
#
#   * the concatenation. `"JINJA" ~ "EVALUATED"` yields JINJAEVALUATED only when
#     Jinja evaluates it; the un-evaluated source text does not contain that
#     substring, because the `" ~ "` sits in the middle of it.
#   * the alphabet. J, N, U and V are outside hex, so no random loop id can
#     contain the marker, and neither can an age, a ratio or a count.
JINJA_PROBE = '{{ "JINJA" ~ "EVALUATED" }}'
JINJA_PROBE_EVALUATED = "JINJAEVALUATED"


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>",
    "L-\"><img src=x onerror=alert(1)>",
    JINJA_PROBE,
    "{% raise %}",
    "L-'--",
])
def test_a_hostile_loop_id_is_escaped_not_executed(http, registry, hostile):
    from markupsafe import escape

    registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C1", loop_id=hostile)
    body = http.get("/worklist/").data.decode()

    # No tag the hostile id tried to open exists in the document. The template
    # itself contains no img and no script, so any occurrence came from the row.
    assert "<script" not in body.lower()
    assert "<img" not in body.lower()
    # Jinja never evaluated it.
    assert JINJA_PROBE_EVALUATED not in body
    # ...and the row is still rendered, escaped exactly, rather than dropped:
    # a row silently missing from a worklist is this product's failure mode.
    assert str(escape(hostile)) in body


def test_jinja_syntax_in_a_modality_is_not_evaluated(http, registry):
    from markupsafe import escape

    registry.open_loop(mrn=MRN_SENTINEL, modality=JINJA_PROBE, control_id="C1")
    body = http.get("/worklist/").data.decode()
    assert JINJA_PROBE_EVALUATED not in body
    assert JINJA_PROBE in body or str(escape(JINJA_PROBE)) in body


@pytest.mark.parametrize("action, setup", [("dismiss", "orphan"), ("reverse_acknowledgement", "ack")])
def test_a_free_text_reason_reaches_no_artifact_at_all(http, registry, caplog, action, setup):
    """Reasons are free text a coordinator types. Free text is the channel PHI
    leaks through -- the same shape that leaked through an unbounded free-text field in an earlier system.
    It stays in the append-only log, where the audit needs it, and off the page,
    off the JSON, off the action's own response, and out of every log record.

    The response and the log records were added after mutation testing: echoing
    the reason back in the 200 body and in the log line survived the first suite,
    because the only assertion looked at the worklist GET."""
    caplog.set_level(logging.DEBUG)
    if setup == "orphan":
        target = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
        stored_key = "dismissed_reason"
    else:
        target = _acknowledged(registry)
        stored_key = "reversed_reason"

    hostile_reason = f"<script>alert(1)</script> {NOTE_SENTINEL}"
    posted = http.post(f"/worklist/{target}/{action}",
                       json={"actor": "a", "role": "r", "reason": hostile_reason})
    assert posted.status_code == 200

    stored = [e for e in registry.store.events_for(target) if stored_key in e.detail]
    assert stored[-1].detail[stored_key] == hostile_reason, "the audit still has it"

    artifacts = {
        "action response": posted.data.decode(),
        "html": http.get("/worklist/").data.decode(),
        "json": http.get("/worklist/?format=json").data.decode(),
        "logs": "\n".join(r.getMessage() for r in caplog.records),
    }
    for name, text in artifacts.items():
        assert NOTE_SENTINEL not in text, f"the reason reached {name}"
        assert "<script>alert" not in text, f"the reason reached {name}"


# ---------------------------------------------------------------- PHI

def _every_artifact(http, caplog):
    """HTML, JSON and every log record emitted while producing them."""
    html = http.get("/worklist/").data.decode()
    js = http.get("/worklist/?format=json").data.decode()
    logs = "\n".join(r.getMessage() for r in caplog.records)
    return {"html": html, "json": js, "logs": logs}


def _assert_no_sentinel(artifacts, sentinel=MRN_SENTINEL):
    leaked = [name for name, text in artifacts.items() if sentinel in text]
    assert leaked == [], f"{sentinel} leaked into {leaked}"


def test_no_mrn_sentinel_reaches_html_json_or_logs(http, registry, caplog):
    caplog.set_level(logging.DEBUG)
    registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C1")
    _resulted(registry, mrn=MRN_SENTINEL, modality="MG")
    registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "US"})
    _acknowledged(registry, mrn=MRN_SENTINEL, modality="XR")

    _assert_no_sentinel(_every_artifact(http, caplog))


def test_no_sentinel_reaches_a_4xx_response_or_its_log(http, registry, caplog):
    """Error paths are where a message composed from live state gets echoed back."""
    caplog.set_level(logging.DEBUG)
    prelim = _resulted(registry, obx11="P", modality="CT")
    acked = _acknowledged(registry, modality="MG")
    orphan = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "US"})

    bodies = [
        http.post(f"/worklist/{prelim}/acknowledge",
                  json={"actor": "a", "role": "r"}).data.decode(),
        http.post(f"/worklist/{acked}/acknowledge", json={"actor": "a", "role": "r"}).data.decode(),
        http.post(f"/worklist/{prelim}/reverse_acknowledgement",
                  json={"actor": "a", "role": "r", "reason": "x"}).data.decode(),
        http.post(f"/worklist/{prelim}/dismiss",
                  json={"actor": "a", "role": "r", "reason": "x"}).data.decode(),
        http.post(f"/worklist/{orphan}/acknowledge", json={"actor": "a", "role": "r"}).data.decode(),
        http.post("/worklist/L-nope/acknowledge", json={"actor": "a", "role": "r"}).data.decode(),
    ]
    artifacts = {f"response[{i}]": b for i, b in enumerate(bodies)}
    artifacts["logs"] = "\n".join(r.getMessage() for r in caplog.records)
    _assert_no_sentinel(artifacts)


def test_the_phi_harness_can_actually_detect_a_leak(http, registry, caplog, monkeypatch):
    """A grep that has never failed proves nothing. Break the renderer on purpose
    and assert the same assertion that guards the real page now fails."""
    caplog.set_level(logging.DEBUG)
    registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C1")

    _assert_no_sentinel(_every_artifact(http, caplog))  # clean to start with

    real_row = worklist_module._row

    def leaky(loop, now, pack, **kwargs):
        row = real_row(loop, now, pack, **kwargs)
        row["modality"] = loop.mrn          # the MRN, straight into the template
        return row

    monkeypatch.setattr(worklist_module, "_row", leaky)

    artifacts = _every_artifact(http, caplog)
    assert MRN_SENTINEL in artifacts["html"], "the leak did not even reach the HTML"
    assert MRN_SENTINEL in artifacts["json"]
    with pytest.raises(AssertionError):
        _assert_no_sentinel(artifacts)


_E2E_SENTINELS = {
    "PID_MRN": "ZZSENTINELMRN",
    "PID_NAME": "ZZSENTINELNAME",
    "NK1_NAME": "ZZSENTINELKIN",
    "GT1_NAME": "ZZSENTINELGUARANTOR",
    "NTE_TEXT": "ZZSENTINELNOTE",
}

_E2E_ORDER = (
    "MSH|^~\\&|EHR|HOSP|RIS|HOSP|20260725080000||ORM^O01|ORD001|P|2.5.1\r"
    "PID|1||ZZSENTINELMRN^^^HOSP^MR||ZZSENTINELNAME^JANE||19800101|F\r"
    "NK1|1|ZZSENTINELKIN^JOHN|SPO|555 ELM ST\r"
    "GT1|1||ZZSENTINELGUARANTOR^JOHN|||555 ELM ST\r"
    "ORC|NW|PLACER1\r"
    "OBR|1|PLACER1||71260^CT CHEST^C4|||20260725080000\r"
)
_E2E_RESULT = (
    "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725120000||ORU^R01|SENT001|P|2.5.1\r"
    "PID|1||ZZSENTINELMRN^^^HOSP^MR||ZZSENTINELNAME^JANE||19800101|F\r"
    "NK1|1|ZZSENTINELKIN^JOHN|SPO|555 ELM ST\r"
    "GT1|1||ZZSENTINELGUARANTOR^JOHN|||555 ELM ST\r"
    "OBR|1|PLACER1|FILLER1|71260^CT CHEST^C4|||20260725100000\r"
    "OBX|1|TX|71260^CT CHEST^C4||ZZSENTINELNOTE||||||F\r"
    "NTE|1||ZZSENTINELNOTE\r"
)
_E2E_UNMATCHED = (
    "MSH|^~\\&|LAB|HOSP|EHR|HOSP|20260725130000||ORU^R01|SENT002|P|2.5.1\r"
    "PID|1||ZZSENTINELMRN9^^^HOSP^MR||ZZSENTINELNAME^JANE||19800101|F\r"
    "NK1|1|ZZSENTINELKIN^JOHN|SPO\r"
    "OBR|1|NOSUCHPLACER|NOSUCHFILLER|99999^MYSTERY^C4|||20260725130000\r"
    "OBX|1|TX|99999^MYSTERY^C4||ZZSENTINELNOTE||||||F\r"
)


def test_real_hl7_in_and_no_sentinel_out(tmp_path, accepted, caplog):
    """Spec test 14, end to end rather than through a hand-built Loop.

    Real messages carrying planted identifiers in PID, NK1, GT1 and a note go in
    through the listener; every artifact the worklist produces is then grepped --
    HTML, JSON, each action response including the refused ones, and every log
    record emitted along the way. Asserting on the parser instead would prove
    only that the parser is careful today.
    """
    from referral_loop.listener import MessageHandler

    caplog.set_level(logging.DEBUG)
    store = LoopStore(tmp_path / "loops.db")
    reg = Registry(store)
    handler = MessageHandler(store=store, registry=reg, pack=PACK)
    for message in (_E2E_ORDER, _E2E_RESULT, _E2E_UNMATCHED):
        assert "|AA|" in handler.handle(message)

    states = sorted(loop.state for loop in store.all_loops())
    assert states == sorted([LoopState.RESULTED, LoopState.ORPHAN]), "the fixtures did not exercise both queues"

    app = create_app(store=store, registry=reg, pack=PACK)
    app.config["TESTING"] = True
    client = app.test_client()

    artifacts = {
        "html": client.get("/worklist/").data.decode(),
        "json": client.get("/worklist/?format=json").data.decode(),
    }
    for loop in store.all_loops():
        for action, body in (("acknowledge", {"actor": "a", "role": "r"}),
                             ("dismiss", {"actor": "a", "role": "r", "reason": "x"}),
                             ("reverse_acknowledgement", {"actor": "a", "role": "r", "reason": "x"})):
            response = client.post(f"/worklist/{loop.loop_id}/{action}", json=body)
            artifacts[f"{action} -> {response.status_code}"] = response.data.decode()
    artifacts["logs"] = "\n".join(r.getMessage() for r in caplog.records)

    leaks = {
        name: sorted(k for k, v in _E2E_SENTINELS.items() if v in text)
        for name, text in artifacts.items()
    }
    assert not any(leaks.values()), f"sentinels left the building: { {k: v for k, v in leaks.items() if v} }"

    # And the same scan, run against a deliberately contaminated copy of the
    # very artifact it just cleared, so "clean" is a result rather than a scan
    # that cannot fail.
    contaminated = artifacts["html"] + _E2E_SENTINELS["PID_MRN"]
    assert [k for k, v in _E2E_SENTINELS.items() if v in contaminated] == ["PID_MRN"]


def test_the_row_builder_emits_only_allowlisted_keys(accepted):
    """An allowlist asserted as a set, so adding a field is a deliberate act."""
    from referral_loop.events import Loop

    loop = Loop(loop_id="L1", mrn=MRN_SENTINEL, state=LoopState.OPEN, modality="CT",
                service_code="71260", ordering_provider="DRSENTINEL",
                placer_order_number="PLACER-SENTINEL", filler_order_number="FILLER-SENTINEL",
                ordered_at=datetime.now(timezone.utc))
    row = worklist_module._row(loop, datetime.now(timezone.utc), PACK)
    assert set(row) == {"loop_id", "state", "modality", "age_hours", "age_basis",
                        "is_stale", "staleness_ratio"}
    assert MRN_SENTINEL not in repr(row)


# ---------------------------------------------------------------- the claim

FORBIDDEN_WORDS = ("closed", "close ", "resolved", "resolve", "complete", "completed",
                   "reviewed by", "signed off", "dispositioned")


def test_the_page_never_claims_a_clinical_disposition(http, registry):
    """A tool reporting every loop handled while no clinician saw a result is
    section 1's failure with a dashboard asserting it did not happen."""
    _resulted(registry)
    registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
    registry.open_loop(mrn=MRN_SENTINEL, modality="MG", control_id="C9")

    body = http.get("/worklist/").data.decode().lower()
    offenders = [w for w in FORBIDDEN_WORDS if w in body]
    assert offenders == [], f"the page implies clinical disposition: {offenders}"


def test_the_page_states_what_acknowledgement_does_and_does_not_mean(http):
    body = http.get("/worklist/").data.decode().lower()
    assert "belongs to this loop" in body
    assert "clinician" in body


def test_the_json_view_carries_the_same_claim(http):
    payload = http.get("/worklist/?format=json").get_json()
    claim = payload["acknowledgement_means"].lower()
    assert "belongs to this loop" in claim
    assert "clinician" in claim
    assert not [w for w in FORBIDDEN_WORDS if w in claim]


def test_closed_is_not_offered_as_an_action(http, registry):
    loop_id = _resulted(registry)
    assert http.post(f"/worklist/{loop_id}/close", json={"actor": "a", "role": "r"}).status_code == 404
    assert b"CLOSED" not in http.get("/worklist/").data


# ---------------------------------------------------------------- the page itself

def test_the_page_references_no_external_resource(http, registry):
    """No egress in v1 includes the browser's. A CDN font on this page would make
    a hospital's PHI-adjacent screen phone home on every load."""
    registry.open_loop(mrn=MRN_SENTINEL, modality="CT", control_id="C1")
    body = http.get("/worklist/").data.decode()
    assert not re.search(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", body)


def test_a_get_on_an_action_is_405(http, registry):
    loop_id = _resulted(registry)
    assert http.get(f"/worklist/{loop_id}/acknowledge").status_code == 405


# ------------------------------------------------- host and origin validation

def _raw(http):
    """A client carrying none of the headers a same-origin browser would send.

    The `http` fixture stands in for the coordinator's browser and therefore
    sends `Origin`. An attacker's page, an old browser and a curl script do not
    all send the same things, so every test below builds the exact header set it
    is about rather than inheriting one.
    """
    return http.application.test_client()


def test_a_rebound_host_header_cannot_read_the_queue(http, registry):
    """H5. The bind is loopback and the attack does not care.

    evil.com is served with a short TTL and re-pointed at 127.0.0.1 after the
    page loads. The browser's origin is still evil.com, so the same-origin policy
    lets the page *read* the response -- every loop id, state and modality in all
    three queues. The only thing that distinguishes this request from a
    coordinator's is the Host header it carries.
    """
    loop_id = _resulted(registry)
    response = _raw(http).get("http://evil.com/worklist/?format=json")
    assert response.status_code == 403
    assert loop_id.encode() not in response.data


def test_a_hostname_that_merely_resolves_to_loopback_is_refused(http, registry):
    """The rebinding family, not one domain. localtest.me and *.nip.io resolve to
    127.0.0.1 by design and need no DNS trickery at all."""
    _resulted(registry)
    assert _raw(http).get("http://localtest.me:5057/worklist/").status_code == 403
    assert _raw(http).get("http://127.0.0.1.nip.io:5057/worklist/").status_code == 403


def test_a_request_with_no_host_header_is_refused(http, registry):
    """HTTP/1.0 has no required Host. Werkzeug then falls back to SERVER_NAME,
    which is the socket -- always loopback here -- so a check reading
    `request.host` would pass anything that simply omitted the header."""
    from werkzeug.test import EnvironBuilder, run_wsgi_app

    _resulted(registry)
    # Driven as raw WSGI: the test client insists on a Host header, which is the
    # assumption this test exists to refuse to make.
    environ = EnvironBuilder("/worklist/?format=json").get_environ()
    environ.pop("HTTP_HOST", None)
    assert environ["SERVER_NAME"] == "localhost"  # the fallback that would pass
    _, status, _ = run_wsgi_app(http.application, environ)
    assert status.startswith("403")


def test_a_loopback_host_is_accepted_on_whatever_port_it_was_given(http, registry):
    """The port is not part of the defence. A browser puts in the Host header
    whatever port the URL named, and an attacker who names a loopback host in the
    URL gets a response the same-origin policy will not let their page read."""
    _resulted(registry)
    for url in ("http://127.0.0.1:5057/worklist/", "http://localhost:8080/worklist/",
                "http://[::1]:9999/worklist/", "http://localhost/worklist/"):
        assert _raw(http).get(url).status_code == 200, url


def test_a_cross_origin_acknowledge_leaves_the_loop_untouched(http, registry):
    """The write half of H5, and the reason a 403 is not the assertion that
    matters: what must be true is that the loop was not acknowledged by a
    coordinator who does not exist."""
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        json={"actor": "Dr Nobody", "role": "referral_coordinator"},
        headers={"Origin": "http://evil.com"},
    )
    assert response.status_code == 403
    assert registry.get(loop_id).state is LoopState.RESULTED
    assert not registry.get(loop_id).ack_by  # nobody, not "Dr Nobody"


def test_a_cross_origin_form_post_cannot_dismiss_an_orphan(http, registry):
    """The CORS-simple case: a plain <form method="post"> needs no preflight, so
    nothing in the browser asks this server's permission before sending it.
    Dismissal is terminal, which is what makes this the worst of the three."""
    orphan_id = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{orphan_id}/dismiss",
        data={"actor": "Dr Nobody", "role": "coordinator", "reason": "not ours"},
        headers={"Origin": "http://evil.com"},
    )
    assert response.status_code == 403
    assert registry.get(orphan_id).state is LoopState.ORPHAN


def test_a_cross_site_undo_match_is_refused_on_sec_fetch_site_alone(http, registry):
    """undo_match detaches a real result and re-orphans it. A browser that sends
    Sec-Fetch-Site says cross-site plainly; no Origin comparison is needed."""
    loop_id = _acknowledged(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/undo_match",
        data={"actor": "Dr Nobody", "role": "coordinator", "reason": "not ours"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_a_same_site_request_is_not_treated_as_same_origin(http, registry):
    """A sibling host under the same registrable domain is a different origin and
    has no business posting here."""
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        json={"actor": "a", "role": "r"},
        headers={"Sec-Fetch-Site": "same-site"},
    )
    assert response.status_code == 403
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_a_null_origin_is_refused(http, registry):
    """A sandboxed iframe and a file:// page both post with Origin: null. Neither
    is the coordinator's page, and `null` matches no host."""
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        json={"actor": "a", "role": "r"}, headers={"Origin": "null"},
    )
    assert response.status_code == 403
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_a_form_post_with_no_same_origin_evidence_at_all_is_refused(http, registry):
    """The classic hole, and the reason the Origin check alone is not enough.

    A browser old enough to send neither Sec-Fetch-Site nor an Origin on a
    cross-site form submission -- IE11, Firefox before 70 -- would otherwise walk
    straight through. What such a request cannot do is set a Content-Type outside
    the CORS-simple set, so form-encoded with no evidence fails closed.
    """
    orphan_id = registry.orphan(control_id="C-ORU", mrn=MRN_SENTINEL, detail={"modality": "CT"})
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{orphan_id}/dismiss",
        data={"actor": "Dr Nobody", "role": "coordinator", "reason": "not ours"},
    )
    assert response.status_code == 403
    assert registry.get(orphan_id).state is LoopState.ORPHAN


def test_a_json_post_with_no_browser_headers_still_works(http, registry):
    """The deliberate exception, and the line the whole rule rests on: a
    cross-site request a browser will send without asking permission first cannot
    carry Content-Type: application/json. Anything that does was either preflighted
    -- and this server answers no preflight -- or is not a browser at all.

    Non-browser local callers are unprotected here by design; there is no
    authentication. This keeps a local script working rather than pretending
    blocking it would have bought something.
    """
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        json={"actor": "coordinator-a", "role": "referral_coordinator"},
    )
    assert response.status_code == 200
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_a_get_is_not_origin_checked(http, registry):
    """Reads are bounded by Host, not by Origin. A coordinator arriving from a
    bookmark or the address bar sends no Origin at all, so an Origin rule on GET
    would either reject every ordinary page load or allow a missing one -- and a
    cross-origin read of a loopback URL is already unreadable to the page that
    asked for it."""
    loop_id = _resulted(registry)
    response = _raw(http).get("http://127.0.0.1:5057/worklist/?format=json",
                              headers={"Origin": "http://evil.com"})
    assert response.status_code == 200
    assert loop_id in _ids(response.get_json()["queues"]["awaiting_acknowledgement"])


def test_the_coordinator_s_own_browser_still_reads_and_acts(http, registry):
    """The negative control, end to end and with the headers a real browser sends
    on its own page's form: the queue renders and the acknowledgement lands."""
    loop_id = _resulted(registry)
    browser = _raw(http)
    page = browser.get("http://127.0.0.1:5057/worklist/",
                       headers={"Sec-Fetch-Site": "none"})
    assert page.status_code == 200
    assert loop_id.encode() in page.data

    acted = browser.post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        data={"actor": "coordinator-a", "role": "referral_coordinator"},
        headers={"Origin": "http://127.0.0.1:5057", "Sec-Fetch-Site": "same-origin"},
    )
    assert acted.status_code == 200, acted.data
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_a_route_that_redirects_before_matching_is_still_gated(http, registry):
    """The blueprint hook is not enough, and this is the request that proves it.

    Flask runs a blueprint's before_request only once a rule in that blueprint has
    matched. `/worklist` without the trailing slash matches nothing: routing
    raises RequestRedirect first, so a gate registered only on the blueprint never
    runs -- and Werkzeug builds the redirect target out of the Host header, which
    put the caller's own string into the Location header and the HTML body.
    """
    _resulted(registry)
    response = _raw(http).get(f"http://{MRN_SENTINEL}.evil.com/worklist")
    assert response.status_code == 403
    assert MRN_SENTINEL not in response.headers.get("Location", "")
    assert MRN_SENTINEL not in response.data.decode()


@pytest.mark.parametrize("path", [
    "/nope",                  # routing produces a 404, matching no blueprint rule
    "/static/anything.css",   # registered by Flask(__name__), not by the blueprint
    "/worklist",              # routing produces a 308 before any rule matches
])
def test_the_gate_covers_requests_that_match_no_blueprint_rule(http, registry, path):
    """Everything the app answers, not merely everything the blueprint answers."""
    _resulted(registry)
    assert _raw(http).get(f"http://evil.com{path}").status_code == 403


def test_the_gate_travels_onto_an_app_that_only_registers_the_blueprint(stack):
    """The other half: mounting the queues somewhere else must not shed the gate.

    This is why the hook stays on the blueprint as well as on the app -- an app
    built by hand rather than by create_app still gets it.
    """
    from flask import Flask

    _, registry, store = stack
    other = Flask("some_other_host_app")
    other.register_blueprint(create_blueprint(store, registry, PACK))
    client = other.test_client()

    assert client.get("http://evil.com/worklist/?format=json").status_code == 403
    assert client.get("http://127.0.0.1:5057/worklist/?format=json").status_code == 200


def test_a_loopback_request_that_redirects_still_redirects(http, registry):
    """The negative control for the gate running earlier: a coordinator who types
    the URL without the trailing slash must still be sent to the page."""
    _resulted(registry)
    response = _raw(http).get("http://127.0.0.1:5057/worklist")
    assert response.status_code == 308
    assert response.headers["Location"].endswith("/worklist/")


def test_an_https_origin_on_this_http_server_is_not_same_origin(http, registry):
    """The scheme is part of an origin. Nothing serves this page over TLS, so an
    https Origin naming the same host:port is a different origin than the one the
    coordinator's page has -- and widening the rule to admit it would be widening
    it for a deployment that does not exist."""
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        json={"actor": "a", "role": "r"},
        headers={"Origin": "https://127.0.0.1:5057"},
    )
    assert response.status_code == 403
    assert registry.get(loop_id).state is LoopState.RESULTED


@pytest.mark.parametrize("proxy_headers", [
    # The proxy forwards the browser's Sec-Fetch-Site and strips Origin.
    {"Sec-Fetch-Site": "same-origin"},
    # The proxy rewrites Origin to the loopback upstream it forwards to.
    {"Origin": "http://127.0.0.1:5057"},
])
def test_an_authenticating_proxy_gets_through_on_configuration_alone(
        http, registry, proxy_headers):
    """The module docstring says fronting this page needs only a Host rewrite plus
    one of these two headers. That is a claim about behaviour, so it is asserted
    rather than written down -- the previous draft of that paragraph said a code
    change was required and was simply wrong."""
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        data={"actor": "coordinator-a", "role": "referral_coordinator"},
        headers=proxy_headers,
    )
    assert response.status_code == 200, response.data
    assert registry.get(loop_id).state is LoopState.ACKNOWLEDGED


def test_a_proxy_that_strips_origin_still_refuses_a_genuine_cross_site_post(http, registry):
    """The other half of that claim: the first proxy shape leans on a header the
    browser sets itself, so stripping Origin does not hand an attacker the page."""
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        data={"actor": "Dr Nobody", "role": "coordinator"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_the_coordinator_s_own_page_breaks_on_a_browser_sending_neither_header(
        http, registry):
    """The stated cost of the rule, pinned so it cannot be quietly forgotten.

    This is the *same-origin* acknowledgement a coordinator's own click produces
    on IE11 or Firefox before 70: url-encoded, no Origin, no Sec-Fetch-Site. The
    gate cannot tell it from a forged one and refuses it. Asserted here so the
    docstring's warning is a fact about the code rather than a note about it.
    """
    loop_id = _resulted(registry)
    response = _raw(http).post(
        f"http://127.0.0.1:5057/worklist/{loop_id}/acknowledge",
        data={"actor": "coordinator-a", "role": "referral_coordinator"},
    )
    assert response.status_code == 403
    assert registry.get(loop_id).state is LoopState.RESULTED


def test_the_refusal_echoes_nothing_the_caller_sent(http, registry, caplog):
    """A 403 body and its log record are artifacts too, and the Host and Origin
    are caller-controlled text. Spec test 14 greps both."""
    _resulted(registry)
    with caplog.at_level(logging.INFO):
        response = _raw(http).get(f"http://{MRN_SENTINEL}.evil.com/worklist/?format=json")
        forged = _raw(http).post(
            "http://127.0.0.1:5057/worklist/L-000000000000/dismiss",
            json={"actor": "a", "role": "r", "reason": "x"},
            headers={"Origin": f"http://{MRN_SENTINEL}.evil.com"},
        )
    assert response.status_code == 403 and forged.status_code == 403
    assert MRN_SENTINEL not in response.data.decode()
    assert MRN_SENTINEL not in forged.data.decode()
    assert MRN_SENTINEL not in caplog.text


# ---------------------------------------------------------------- binding

def test_the_server_binds_loopback_without_being_told_to(tmp_path, accepted):
    """The trap this test exists to avoid: supplying host="127.0.0.1" and then
    asserting the server bound 127.0.0.1. Nothing is supplied here -- the default
    is the thing under test, and the assertion reads the socket, not the argument."""
    store = LoopStore(tmp_path / "loops.db")
    server = make_worklist_server(store, Registry(store), PACK, port=0)
    try:
        assert server.server_address[0] in ("127.0.0.1", "::1")
    finally:
        server.server_close()


def test_a_non_loopback_bind_is_refused(tmp_path, accepted):
    """The MLLP listener warns, because ingress from an interface engine on
    another host is a real deployment. A coordinator worklist with no
    authentication in front of it is not."""
    store = LoopStore(tmp_path / "loops.db")
    with pytest.raises(Exception) as exc:
        make_worklist_server(store, Registry(store), PACK, host="0.0.0.0", port=0)
    assert "0.0.0.0" in str(exc.value)


def test_a_loopback_bind_may_be_asked_for_explicitly(tmp_path, accepted):
    store = LoopStore(tmp_path / "loops.db")
    server = make_worklist_server(store, Registry(store), PACK, host="127.0.0.1", port=0)
    try:
        assert server.server_address[1] != 0
    finally:
        server.server_close()


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def store_bypass(stack):
    """Append an event directly, so a loop with no ordered_at can be constructed.

    registry.open_loop defaults ordered_at to now precisely so a loop can never
    age silently -- which means the +inf branch of staleness_ratio is only
    reachable through a restored or foreign-written log. That branch is on the
    worklist's primary sort key, so it is exercised here rather than assumed.
    """
    from referral_loop.events import LoopEvent

    _, _, store = stack

    def _append(loop_id: str, detail: dict) -> str:
        store.append_event(
            LoopEvent(loop_id, "created", datetime.now(timezone.utc), "C-BYPASS", detail)
        )
        return loop_id

    return _append
