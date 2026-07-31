"""The coordinator worklist. A Flask blueprint, localhost only.

This is the product's entire user surface. Everything upstream of it -- framing,
parsing, the state machine, the matcher, the alias table -- exists to populate
three queues correctly.

What the page claims, and what it refuses to claim
--------------------------------------------------
`ACKNOWLEDGED` means **a coordinator confirmed this result belongs to this
loop**. That is identifier work, and it is exactly what a referral coordinator
can support. It does not mean a clinician competent to act on an abnormal
finding has read it; nothing in v1 observes that, and `CLOSED` is reserved for
v2 and unreachable (spec section 4).

So no word on this page is "closed", "resolved", "complete", or anything else
implying clinical disposition -- enforced by a test that greps the rendered HTML
for those words, not by care. The reason is the product's own failure mode one
layer up: a tool reporting every loop handled while no clinician saw a result is
section 1's scenario with a dashboard asserting it did not happen.

Three queues, because two would hide a population
-------------------------------------------------
`open_loops()` selects OPEN and SCHEDULED. A loop reopened by a corrected result
sits in RESULTED with its acknowledgement cleared, so `open_loops()` does not
return it -- and that population is the one most needing a human. The page
therefore queries `open_loops()` **and** `resulted_unacknowledged()`, plus
`loops_in_states([ORPHAN])` for results nobody ordered.

Sorting
-------
Staleness is the primary sort: it is the thing the product exists to surface.
`staleness_ratio` returns +inf for a loop with no known order time, which sorts
it above every stale loop -- a data-quality gap fails toward visibility. Both
staleness functions refuse to answer until the site has accepted the shipped
thresholds (spec q3); the queue views turn that into a 503 naming the
environment variable, because an unsorted worklist that looked normal would be a
worse failure than a legible refusal. The safety *actions* are not gated: they
make no staleness claim, and wedging them behind an unrelated env var would be
worse than the page they belong to being unavailable.

What leaves the building
------------------------
The store legitimately holds the MRN -- matching and merges are identifier
arithmetic and cannot work without it. The artifacts this module produces are a
different question, and spec test 14 asks it: sentinels planted in PID, NK1, GT1
and note segments must appear zero times in worklist HTML, logs, exports and
audit entries.

`_row` is therefore an allowlist, asserted as a set by a test, and it renders
loop id, state, modality, age and staleness. Not MRN, not patient name, not note
text, not the placer/filler order numbers or the ordering provider -- those are
on the `Loop` object and would be genuinely useful to a coordinator, and they are
still left off, because "useful" is how a field ends up on a screen that a
screenshot then leaves the building on. Widening it is a decision for a pilot
site, made once, with the test updated in the same commit.

Free-text reasons are held to the same rule. A coordinator types them, so they
are the unbounded free-text channel: they stay in the append-only event log where the
audit needs them, and reach no artifact. That also means they have no XSS
surface at all rather than an escaped one.

Log records are an artifact too. Nothing here logs a `str(exc)` from a store or
registry failure, because `MrnRetiredError` and the alias errors compose their
messages from MRNs; the exception *type* and the loop id are logged instead.

What the loopback bind does and does not bound
----------------------------------------------
`make_worklist_server` refuses a non-loopback bind outright rather than warning
like the MLLP listener does -- ingress from an interface engine on another host
is a real deployment, an unauthenticated coordinator queue reachable from the
ward network is not.

That refusal bounds *which interface* accepts a connection. It bounds nothing
about who can drive the browser that makes one, and an earlier version of this
docstring claimed otherwise. DNS rebinding is the counterexample: a page on
evil.com whose A record is re-pointed to 127.0.0.1 after it loads can fetch this
server from the coordinator's own workstation, and because the page's origin is
still evil.com the same-origin policy lets it *read* every queue. A plain
cross-site `<form method="post">` needs no rebinding at all -- it is a
CORS-simple request, so no preflight ever asks this server's permission, and
`_payload` accepts `request.form`.

So `create_blueprint` installs a `before_request` gate. It is not belt-and-braces
alongside the bind check; it is the only thing standing between this page and a
browser being used as a confused deputy, and the two checks answer different
questions:

  * **Host must be loopback**, on any port. Under rebinding the browser puts the
    attacker's hostname in Host -- that is unavoidable for them, because naming a
    loopback host in the URL is what makes the response unreadable to their page.
    A request with no Host header at all is refused rather than allowed: Werkzeug
    falls back to SERVER_NAME, which is always this socket, so a check reading
    `request.host` would let anything that simply omitted the header through.
    This closes the read. It also means nothing here may ever emit a CORS header.
  * **State-changing methods must prove same origin.** An `Origin`, if sent, must
    name the same host:port the request was addressed to. Otherwise
    `Sec-Fetch-Site` must say `same-origin` or `none` (`none` is a user-initiated
    navigation, which no page can forge). With neither header the request is
    refused unless it is `application/json`; that carve-out is reachable only
    when `Origin` is absent, and every browser mechanism that can put a JSON body
    on a cross-site request is a CORS request and therefore sends one. GET and
    HEAD are not Origin-checked: an ordinary page load from a bookmark carries no
    Origin, and the Host rule already closes the read.

The cost of that last rule, stated where it will be found: **the coordinator's
own page stops working on a browser that sends neither header** -- IE11, Firefox
before 70. Its forms post url-encoded, so a same-origin acknowledgement from one
of those is refused exactly as a forged one is. The gate cannot tell them apart,
and this is the direction it was told to fail in. A site still on such a browser
needs the fix to be a trusted-origin list, not a loosened default.

What is still unprotected
-------------------------
There is still **no authentication**. Any user logged into this workstation, and
any process running on it, can read every queue and take every action -- and will
be recorded as whatever name and role they typed. The gate above stops a remote
web page from driving the coordinator's browser; it stops nobody who is already
on the host. That is a real gap, not a bounded one.

The answer to it is still an authenticating reverse proxy, and this gate admits
one on configuration alone. The proxy must rewrite Host to the loopback name and
port it forwards to, and then do either of:

  * forward the browser's `Sec-Fetch-Site`, stripping `Origin`; or
  * rewrite `Origin` to match the Host it forwards.

Both are one `proxy_set_header` line, and neither weakens the gate: a proxy that
strips `Origin` still forwards `Sec-Fetch-Site: cross-site` on a genuinely
cross-site post, and one that rewrites `Origin` still cannot manufacture a
`Sec-Fetch-Site` the browser did not send. Pinned by tests, because this
paragraph is a claim about behaviour and the first draft of it was wrong: it said
fronting this page needed a code change, which was never true.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from urllib.parse import urlsplit

import jinja2
from flask import Blueprint, Flask, jsonify, request

from .errors import (
    LoopNotFoundError,
    ReferralLoopError,
    StoreUnavailableError,
    ThresholdsNotAcceptedError,
)
from .events import Loop, LoopState
from .pack import RulePack
from .registry import Registry
from .staleness import age, is_stale, require_thresholds_accepted, staleness_ratio
from .store import LoopStore

logger = logging.getLogger(__name__)

_LOOPBACK = ("127.0.0.1", "::1", "localhost")

# Methods that change nothing. Everything else has to prove same origin; see the
# module docstring for why GET is deliberately not in that set.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The only Sec-Fetch-Site values a page on this origin, or a user typing the URL,
# can produce. `same-site` is a sibling host under one registrable domain, which
# for a loopback service means somebody else's; it is refused with `cross-site`.
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})

# The control id written on a coordinator's action. Not a message, so there is no
# MSH-10 -- and the event log should say so rather than carry an empty string
# that reads as "we forgot".
CONTROL_ID = "WORKLIST"

# A coordinator's name and role are typed into a form; a reason is free text.
# Bounded so a paste of a whole chart note cannot be committed to an append-only
# log by accident.
_MAX_ATTRIBUTION = 200
_MAX_REASON = 2000

# How many merges to surface. Enough that a coordinator sees one happen during
# their shift, few enough that the section stays a notice rather than a log.
_RECENT_MERGES = 20

# The sentence the whole state split exists to make true. Rendered on the page
# and returned in the JSON view so an integrator reading the API sees the same
# claim a coordinator does.
ACKNOWLEDGEMENT_MEANS = (
    "Acknowledged means a coordinator confirmed this result belongs to this loop "
    "— an identifier match. It does not mean a clinician has read the finding; "
    "nothing in this version observes that."
)

PRELIMINARY_RULE = (
    "A preliminary read may not be acknowledged. A preliminary that later corrects "
    "to a finding is the scenario this rule exists for, so the loop stays on the "
    "queue until a final or corrected read arrives."
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _row(loop: Loop, now: datetime, pack: RulePack, *, arrived_at: datetime | None = None) -> dict:
    """The allowlist. Only these keys reach the template or the JSON view.

    `age_hours` measures from `ordered_at` where there is one -- the same clock
    staleness uses -- and from the record's first event otherwise, which is what
    an orphan has (a result nobody ordered has no order date). `age_basis` says
    which, so a duration is never silently comparing two different things.

    `staleness_ratio` is a finite float or None. None means maximally stale: no
    known order time, or a non-positive threshold. It is not `float("inf")`,
    because `Infinity` is not valid JSON and a strict consumer would fail on
    exactly the row the sort exists to surface.
    """
    ratio = staleness_ratio(loop, now, pack)
    if loop.ordered_at is not None:
        hours: float | None = age(loop, now).total_seconds() / 3600
        basis = "ordered"
    elif arrived_at is not None:
        hours = max(0.0, (_as_utc(now) - _as_utc(arrived_at)).total_seconds() / 3600)
        basis = "arrived"
    else:
        hours = None
        basis = "unknown"
    return {
        "loop_id": loop.loop_id,
        "state": loop.state.value,
        "modality": loop.modality,
        "age_hours": None if hours is None else round(hours, 1),
        "age_basis": basis,
        "is_stale": is_stale(loop, now, pack),
        "staleness_ratio": None if ratio == float("inf") else round(ratio, 3),
    }


def _sort_key(row: dict) -> tuple:
    """Staleness first, then age, then loop id so the order is reproducible.

    A None ratio is maximally stale and must sort above every finite one, so it
    maps to -inf here rather than to 0.0 -- which is what it would collapse to
    under a naive `-(ratio or 0)` and would bury the row the ratio was made
    non-finite to surface.
    """
    ratio = row["staleness_ratio"]
    return (
        float("-inf") if ratio is None else -ratio,
        -(row["age_hours"] or 0.0),
        row["loop_id"],
    )


_TEMPLATE_SOURCE = """<!doctype html>
<title>Referral worklist</title>
<style>
 body { font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 1.5rem;
        color: #16202a; background: #fff; }
 h1 { font-size: 1.4rem; margin-bottom: .2rem; }
 h2 { font-size: 1.05rem; margin: 1.8rem 0 .2rem; }
 .meta { color: #5b6b7a; font-size: .85rem; margin: 0 0 .8rem; }
 .claim { border-left: 3px solid #2f6f9f; background: #f2f7fb; padding: .6rem .8rem;
          font-size: .88rem; margin: .8rem 0 0; }
 .hint { color: #5b6b7a; font-size: .82rem; margin: .1rem 0 .5rem; }
 table { border-collapse: collapse; width: 100%; font-size: .9rem; }
 th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #e3e8ec;
          vertical-align: middle; }
 th { font-weight: 600; color: #40505e; font-size: .8rem; text-transform: uppercase;
      letter-spacing: .03em; }
 tr.stale td { background: #fff5f4; }
 .badge { font-size: .72rem; padding: .08rem .35rem; border-radius: 3px;
          background: #e8edf1; color: #40505e; }
 .badge.stale { background: #f6d2cd; color: #7d2419; }
 .empty { color: #5b6b7a; font-size: .88rem; font-style: italic; padding: .4rem 0; }
 form { display: inline; }
 input { font: inherit; font-size: .82rem; padding: .12rem .3rem; width: 8rem; }
 button { font: inherit; font-size: .82rem; padding: .12rem .5rem; }
</style>
<h1>Referral worklist</h1>
<p class="meta">rule pack {{ pack_version }} &middot; generated {{ generated_at }}
   &middot; localhost only</p>
<p class="claim">{{ acknowledgement_means }}</p>

{% macro rows(queue, action, action_label, needs_reason) -%}
{% if queue %}
<table>
 <tr><th>Loop</th><th>State</th><th>Modality</th><th>Age (h)</th><th>Staleness</th>
     <th>{% if action %}Action{% endif %}</th></tr>
 {% for row in queue %}
 <tr class="{{ 'stale' if row.is_stale else '' }}">
  <td>{{ row.loop_id }}</td>
  <td>{{ row.state }}</td>
  <td>{{ row.modality or '&mdash;'|safe }}</td>
  <td>{% if row.age_hours is none %}&mdash;{% else %}{{ '%.1f'|format(row.age_hours) }}{% endif %}
      <span class="badge">{{ row.age_basis }}</span></td>
  <td>{% if row.is_stale %}<span class="badge stale">STALE</span>{% endif %}
      {% if row.staleness_ratio is not none %}{{ '%.2f'|format(row.staleness_ratio) }}&times;
      {% elif row.is_stale %}no order time{% endif %}</td>
  <td>{% if action %}
   <form method="post" action="{{ base }}/{{ row.loop_id|urlencode }}/{{ action }}">
    <input name="actor" placeholder="your name" required>
    <input name="role" placeholder="your role" required>
    {% if needs_reason %}<input name="reason" placeholder="reason (required)" required>{% endif %}
    <button type="submit">{{ action_label }}</button>
   </form>
  {% endif %}</td>
 </tr>
 {% endfor %}
</table>
{% else %}<p class="empty">Nothing here.</p>{% endif %}
{%- endmacro %}

<h2>Awaiting a result &mdash; {{ queues.awaiting_result|length }}</h2>
<p class="hint">Ordered or scheduled, nothing back yet. Sorted by how far past its
 per-modality threshold each one is; a loop with no known order time ranks above
 every stale loop rather than below them.</p>
{{ rows(queues.awaiting_result, none, none, false) }}

<h2>Awaiting acknowledgement &mdash; {{ queues.awaiting_acknowledgement|length }}</h2>
<p class="hint">A result arrived and nobody has confirmed it belongs to this loop yet.
 A loop returns here whenever a corrected result supersedes an earlier read, or when
 a coordinator undoes their own acknowledgement.</p>
{{ rows(queues.awaiting_acknowledgement, 'acknowledge', 'Acknowledge match', false) }}

<h2>Orphan results &mdash; {{ queues.orphans|length }}</h2>
<p class="hint">A result with no matching order. Attach it to the order it belongs to,
 or dismiss it if it belongs to no order here &mdash; misrouted from another facility,
 or a feed misconfiguration. Dismissal is permanent and needs a reason.</p>
{{ rows(queues.orphans, 'dismiss', 'Dismiss orphan', true) }}

<h2>Recent patient merges &mdash; {{ recent_merges|length }}</h2>
<p class="hint">A merge moves loops onto the surviving record. It is shown here so a
 merge is something you see happen, rather than something you infer from work that
 quietly stopped appearing. Patient identifiers are deliberately not shown.</p>
{% if recent_merges %}
<table>
 <tr><th>#</th><th>Event</th><th>Recorded at</th><th>Recorded by</th></tr>
 {% for merge in recent_merges %}
 <tr><td>{{ merge.alias_event_id }}</td><td>{{ merge.event_type }}</td>
     <td>{{ merge.recorded_at }}</td><td>{{ merge.recorded_by }}</td></tr>
 {% endfor %}
</table>
{% else %}<p class="empty">No merges recorded.</p>{% endif %}

<h2>To attach an orphan to the order it belongs to</h2>
<p class="hint">Enter the orphan's id and the id of the loop that ordered the study. The
 result is applied to that loop exactly as if it had matched on the wire &mdash; so a
 preliminary read still cannot be acknowledged, and the orphan leaves this queue while
 staying in the record.</p>
<form method="post" action="{{ base }}/attach">
 <input name="orphan_id" placeholder="orphan id" required>
 <input name="target_loop_id" placeholder="attach to loop id" required>
 <input name="actor" placeholder="your name" required>
 <input name="role" placeholder="your role" required>
 <button type="submit">Attach orphan</button>
</form>

<h2>To undo a match</h2>
<p class="hint">If a result was attached to the wrong loop, undo it here. This is not the
 same as undoing an acknowledgement: that withdraws your confirmation and leaves the
 result where it is, while this detaches the result. The loop goes back to awaiting one
 and the result returns to the orphan queue so it can be put where it belongs.</p>
<form method="post" action="{{ base }}/undo_match">
 <input name="loop_id" placeholder="loop id" required>
 <input name="actor" placeholder="your name" required>
 <input name="role" placeholder="your role" required>
 <input name="reason" placeholder="reason (required)" required>
 <button type="submit">Undo match</button>
</form>

<h2>To undo an acknowledgement</h2>
<p class="hint">If you acknowledged the wrong loop, undo it here. The loop returns to
 the acknowledgement queue and both the original entry and the undo stay in the
 record.</p>
<form method="post" action="{{ base }}/undo">
 <input name="loop_id" placeholder="loop id" required>
 <input name="actor" placeholder="your name" required>
 <input name="role" placeholder="your role" required>
 <input name="reason" placeholder="reason (required)" required>
 <button type="submit">Undo acknowledgement</button>
</form>
"""

_REFUSAL_SOURCE = """<!doctype html>
<title>Referral worklist unavailable</title>
<style>
 body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 44rem; color: #16202a; }
 h1 { font-size: 1.2rem; } code { background: #f0f3f5; padding: .05rem .3rem; }
 p { line-height: 1.5; }
</style>
<h1>Referral worklist unavailable</h1>
<p>{{ error }}</p>
<p>The queues are not shown because staleness is their primary sort, and an
 unsorted worklist that looked normal would hide the one thing this page exists to
 surface. Acknowledging, undoing an acknowledgement and dismissing an orphan still
 work &mdash; none of them makes a staleness claim.</p>
"""

# An environment of our own rather than Flask's, with autoescape pinned on
# explicitly. Flask's default is derived from a template filename, and this
# module has no files -- relying on how that function treats a None filename
# would make XSS protection here a property of a Flask internal.
_ENV = jinja2.Environment(autoescape=True, undefined=jinja2.StrictUndefined)
_TEMPLATE = _ENV.from_string(_TEMPLATE_SOURCE)
_REFUSAL = _ENV.from_string(_REFUSAL_SOURCE)


def _text(payload, name: str, limit: int) -> str:
    """A required, non-blank, bounded string. Raises _BadInput, which is a 400.

    Whitespace-only is empty. An action attributed to "   " answers none of the
    questions the attribution exists to answer, and the registry would accept it:
    its own guard is `if not actor`, and a space is truthy.
    """
    value = payload.get(name)
    if not isinstance(value, str):
        raise _BadInput(f"{name} is required and must be text")
    value = value.strip()
    if not value:
        raise _BadInput(f"{name} is required; a blank {name} attributes this to nobody")
    if len(value) > limit:
        raise _BadInput(f"{name} is longer than {limit} characters")
    return value


class _BadInput(Exception):
    """A malformed request, not a refused one. 400, never 409."""


def _is_loopback_host(raw: str) -> bool:
    """Is this Host header naming this machine by a name only this machine has?

    The port is parsed but not compared against anything. A browser copies into
    Host whatever port the URL named, and the operator may run this on any port,
    so there is nothing to compare it to -- `create_blueprint` does not know the
    bind port, and coupling it to one would put the check back where the bind
    check already is. The port is also not what the attack turns on: an attacker
    who names 127.0.0.1 in the URL to satisfy a port rule gets a response their
    page may not read. It is parsed only so that a Host of `localhost:evil` is
    not read as the host `localhost`.
    """
    host = raw.strip().lower()
    if host.startswith("["):  # [::1], with or without a port
        closed = host.find("]")
        if closed == -1:
            return False
        name, rest = host[1:closed], host[closed + 1:]
        if rest and not rest.startswith(":"):
            return False
        port = rest[1:]
    else:
        name, _, port = host.partition(":")
    if port and not port.isdigit():
        return False
    return name in _LOOPBACK


def _is_same_origin(origin: str, host: str) -> bool:
    """Does this Origin name the very host:port the request was addressed to?

    Compared against the Host header rather than against `_LOOPBACK`, because
    `http://localhost:5057` and `http://127.0.0.1:5057` are genuinely different
    origins and no browser will mix them. `Origin: null` -- a sandboxed iframe, a
    file:// page -- parses to an empty netloc and so matches nothing.

    The scheme is part of an origin and this server speaks http, so `http` is the
    only one that can be same-origin with it. `https://127.0.0.1:5057` is a
    different origin that nothing here serves; accepting it would widen the rule
    for a deployment that does not exist.
    """
    parts = urlsplit(origin.strip())
    return parts.scheme == "http" and parts.netloc.lower() == host.strip().lower()


def _payload():
    return request.get_json(silent=True) or request.form or {}


def _refuse_a_request_this_page_did_not_originate():
    """Host and Origin validation. See the module docstring for the threat.

    Registered twice, on the app in `create_app` and on the blueprint in
    `create_blueprint`, because the two placements cover different things and
    neither covers the other:

      * A blueprint's `before_request` runs only once a rule *in that blueprint*
        has matched. It therefore never runs for a routing failure -- and
        `GET /worklist`, without the trailing slash, is one: routing raises
        RequestRedirect before matching anything, and Werkzeug builds the
        redirect target out of the Host header. Registered only on the blueprint,
        this gate let that request through and reflected the caller's Host into
        both the Location header and the redirect body. The same held for the
        404s and for `/static/<path:filename>`, which `Flask(__name__)` registers
        and the blueprint does not.
      * The app hook only exists on the app `create_app` builds. Keeping the
        blueprint hook means the gate travels with the routes: an app assembled
        by hand that registers this blueprint still cannot mount the queues
        without it.

    Running twice on one request is harmless and deliberate. This reads request
    headers and returns either None or a response; it holds no state and changes
    none, so the second call reaches the same answer as the first. When the app
    hook refuses, Flask short-circuits and the blueprint hook never runs at all.

    The bind check in `make_worklist_server` is a third control answering a
    different question again, and does not stand in for this one.

    Nothing caller-controlled is echoed or logged. The Host and the Origin are
    attacker-chosen strings, and a response body and a log record are two of the
    four artifacts spec test 14 greps -- reflecting a refused Host would make
    this gate the leak it exists to prevent.
    """
    host = request.headers.get("Host")
    if host is None or not _is_loopback_host(host):
        logger.warning("Worklist request refused: Host is not a loopback name")
        return jsonify({"error": "This service answers on loopback names only"}), 403

    if request.method in _SAFE_METHODS:
        return None

    origin = request.headers.get("Origin")
    if origin is not None:
        if _is_same_origin(origin, host):
            return None
        logger.warning("Worklist action refused: cross-origin request")
        return jsonify({"error": "Cross-origin requests may not change anything"}), 403

    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        if site.strip().lower() in _SAME_ORIGIN_FETCH_SITES:
            return None
        logger.warning("Worklist action refused: cross-site request")
        return jsonify({"error": "Cross-origin requests may not change anything"}), 403

    # Neither header, so this is an old browser or not a browser at all. What
    # makes the carve-out below safe is not the CORS-simple list -- that list has
    # had exceptions -- but the reachability of this branch: it is reachable only
    # when Origin is absent, and every browser mechanism that can put a JSON body
    # on a cross-site request is a CORS request, which always carries one. So a
    # cross-site JSON post cannot arrive here at all; anything that does is a
    # local caller, which this page does not authenticate anyway.
    if request.is_json:
        return None
    logger.warning("Worklist action refused: no evidence the request is same-origin")
    return jsonify({
        "error": "A state-changing request must carry an Origin, or be application/json"
    }), 403


def create_blueprint(store: LoopStore, registry: Registry, pack: RulePack) -> Blueprint:
    bp = Blueprint("worklist", __name__, url_prefix="/worklist")
    base = "/worklist"

    # The gate. Also registered on the app in `create_app`, which is what covers
    # the requests that never match a rule here; see the function's own docstring.
    bp.before_request(_refuse_a_request_this_page_did_not_originate)

    # ------------------------------------------------------------ error handlers

    @bp.errorhandler(ThresholdsNotAcceptedError)
    def _thresholds(exc: ThresholdsNotAcceptedError):
        # Operator-legible, not a stack trace: the message names the environment
        # variable and the pack key, which is the whole of what the operator has
        # to do about it.
        logger.warning("Worklist refused: staleness thresholds not accepted by the site")
        if request.args.get("format") == "json":
            return jsonify({"error": str(exc), "error_type": type(exc).__name__}), 503
        return _REFUSAL.render(error=str(exc)), 503

    @bp.errorhandler(_BadInput)
    def _bad_input(exc: _BadInput):
        return jsonify({"error": str(exc)}), 400

    @bp.errorhandler(LoopNotFoundError)
    def _not_found(exc: LoopNotFoundError):
        """404, and deliberately no echo of what was asked for.

        Found by probing Task 15 and fixed in the same commit. This body used to
        return `request.view_args["loop_id"]`, which on this route is a segment
        the caller controls -- a coordinator's browser can POST to
        /worklist/<anything>/acknowledge. Reflecting it made an HTTP response,
        one of the four artifacts spec test 14 greps, echo whatever was in the
        URL: a sentinel planted in PID came straight back out. `_refused` may
        still echo the id because it is only reachable once the loop replayed,
        so by then it is one this system minted.

        Not logged either, for the same reason: logs are the second of those
        four artifacts. An operator wanting to know which id was asked for has
        the request log of whatever is in front of this, and a caller who typed
        the id already has it.
        """
        logger.info("Worklist action refused: no such loop")
        return jsonify({"error": "No such loop"}), 404

    @bp.errorhandler(StoreUnavailableError)
    def _unavailable(exc: StoreUnavailableError):
        # The type and nothing else. A store error's message is composed from
        # whatever SQLite said, and log records are an artifact test 14 greps.
        logger.error("Worklist store read failed (%s)", type(exc).__name__)
        return jsonify({"error": "The loop store is unavailable"}), 503

    # ------------------------------------------------------------------- queues

    @bp.get("/")
    def index():
        # Called before the three scans rather than left to the first row: the
        # gate is about whether this page may make a staleness claim at all, and
        # failing after the work is done is a slower way to say the same thing.
        require_thresholds_accepted()
        now = datetime.now(timezone.utc)

        awaiting_result = sorted(
            (_row(loop, now, pack) for loop in store.open_loops()), key=_sort_key
        )
        awaiting_ack = sorted(
            (_row(loop, now, pack) for loop in store.resulted_unacknowledged()), key=_sort_key
        )
        # Orphans have no order date -- a result nobody ordered has nothing to
        # measure from -- so their clock is the record's first event. One extra
        # indexed read per orphan, paid only on a queue whose whole problem is
        # that it must not be allowed to grow.
        orphans = sorted(
            (_row(loop, now, pack, arrived_at=_arrived_at(store, loop))
             for loop in store.loops_in_states([LoopState.ORPHAN])),
            key=_sort_key,
        )

        queues = {
            "awaiting_result": awaiting_result,
            "awaiting_acknowledgement": awaiting_ack,
            "orphans": orphans,
        }
        merges = _recent_merges(store)
        generated_at = now.isoformat(timespec="seconds")

        if request.args.get("format") == "json":
            return jsonify({
                "generated_at": generated_at,
                "pack_version": pack.version,
                "acknowledgement_means": ACKNOWLEDGEMENT_MEANS,
                "queues": queues,
                "recent_merges": merges,
            })
        return _TEMPLATE.render(
            base=base,
            queues=queues,
            recent_merges=merges,
            pack_version=pack.version,
            generated_at=generated_at,
            acknowledgement_means=ACKNOWLEDGEMENT_MEANS,
        )

    # ------------------------------------------------------------------ actions

    @bp.post("/<path:loop_id>/acknowledge")
    def acknowledge(loop_id: str):
        """Record that a coordinator confirmed the match. Never a clinical claim."""
        payload = _payload()
        actor = _text(payload, "actor", _MAX_ATTRIBUTION)
        role = _text(payload, "role", _MAX_ATTRIBUTION)
        try:
            registry.acknowledge(loop_id, actor=actor, role=role, control_id=CONTROL_ID)
        except LoopNotFoundError:
            # A subclass of ReferralLoopError, so it has to be re-raised ahead of
            # the broad clause below or a missing loop becomes a 409 "refused"
            # rather than a 404 "no such thing" -- and _refused would then call
            # registry.get on the same absent loop.
            raise
        except ReferralLoopError as exc:
            return _refused(registry, loop_id, exc, explain_preliminary=True)
        logger.info("Loop %s acknowledged via the worklist", loop_id)
        return jsonify({
            "loop_id": loop_id,
            "state": registry.get(loop_id).state.value,
            "acknowledgement_means": ACKNOWLEDGEMENT_MEANS,
        })

    @bp.post("/<path:loop_id>/reverse_acknowledgement")
    def reverse_acknowledgement(loop_id: str):
        """Spec rule 4. Rule 2 recovers the machine's error; this recovers the human's.

        A coordinator who acknowledged the wrong loop previously had no way back:
        that loop stayed resolved while the real one stayed open and unwatched.
        """
        payload = _payload()
        actor = _text(payload, "actor", _MAX_ATTRIBUTION)
        role = _text(payload, "role", _MAX_ATTRIBUTION)
        reason = _text(payload, "reason", _MAX_REASON)
        try:
            registry.reverse_acknowledgement(
                loop_id, actor=actor, role=role, reason=reason, control_id=CONTROL_ID
            )
        except LoopNotFoundError:
            raise
        except ReferralLoopError as exc:
            return _refused(registry, loop_id, exc)
        # No reason in the log line: it is free text a human typed, which is the
        # channel PHI leaks through. It is in the append-only event log, where
        # the audit needs it and nothing renders it.
        logger.warning("Acknowledgement on loop %s reversed via the worklist", loop_id)
        return jsonify({"loop_id": loop_id, "state": registry.get(loop_id).state.value})

    @bp.post("/<path:loop_id>/dismiss")
    def dismiss(loop_id: str):
        """Terminal for an orphan that belongs to no loop here. Never automatic."""
        payload = _payload()
        actor = _text(payload, "actor", _MAX_ATTRIBUTION)
        role = _text(payload, "role", _MAX_ATTRIBUTION)
        reason = _text(payload, "reason", _MAX_REASON)
        try:
            registry.dismiss_orphan(
                loop_id, actor=actor, role=role, reason=reason, control_id=CONTROL_ID
            )
        except LoopNotFoundError:
            raise
        except ReferralLoopError as exc:
            return _refused(registry, loop_id, exc)
        logger.warning("Orphan %s dismissed via the worklist", loop_id)
        return jsonify({"loop_id": loop_id, "state": registry.get(loop_id).state.value})

    @bp.post("/<path:loop_id>/attach")
    def attach(loop_id: str):
        """A coordinator says this orphan belongs to that loop. Spec section 5.

        The result is applied through the registry's ordinary result path, so
        safety rule 1 still holds: attaching a preliminary read leaves the target
        RESULTED and unacknowledgeable. Nothing about that is this route's doing
        and nothing here may weaken it.
        """
        payload = _payload()
        target = _text(payload, "target_loop_id", _MAX_ATTRIBUTION)
        actor = _text(payload, "actor", _MAX_ATTRIBUTION)
        role = _text(payload, "role", _MAX_ATTRIBUTION)
        try:
            registry.attach_orphan(
                loop_id, target, actor=actor, role=role, control_id=CONTROL_ID
            )
        except LoopNotFoundError:
            # Answered here rather than re-raised, unlike every other action on
            # this page, and for a reason specific to taking two ids: the shared
            # 404 handler names the one in the URL, so a coordinator who mistyped
            # the *target* would be told their orphan does not exist. Still a
            # 404 and never the 409 below, which is what re-raising exists to
            # guarantee.
            #
            # Neither id is echoed. Both are caller-supplied here, and a response
            # body is one of the artifacts spec test 14 greps -- reflecting
            # whatever was posted would make this route the leak.
            logger.info("Worklist attach refused: one of the two loop ids does not exist")
            return jsonify({
                "error": "No such loop; check both the orphan id and the target loop id"
            }), 404
        except ReferralLoopError as exc:
            return _refused(registry, loop_id, exc)
        logger.info("Orphan %s attached to loop %s via the worklist", loop_id, target)
        return jsonify({
            "orphan_id": loop_id,
            "target_loop_id": target,
            "state": registry.get(target).state.value,
            "acknowledgement_means": ACKNOWLEDGEMENT_MEANS,
        })

    @bp.post("/<path:loop_id>/undo_match")
    def undo_match(loop_id: str):
        """A coordinator says the matcher attached the wrong result.

        Distinct from reverse_acknowledgement, which withdraws a human's
        confirmation and leaves the result attached. This detaches the result:
        the loop goes back to awaiting one and the result returns to the orphan
        queue so it can be put where it belongs.
        """
        payload = _payload()
        actor = _text(payload, "actor", _MAX_ATTRIBUTION)
        role = _text(payload, "role", _MAX_ATTRIBUTION)
        reason = _text(payload, "reason", _MAX_REASON)
        try:
            orphan_id = registry.undo_match(
                loop_id, actor=actor, role=role, reason=reason, control_id=CONTROL_ID
            )
        except LoopNotFoundError:
            raise
        except ReferralLoopError as exc:
            return _refused(registry, loop_id, exc)
        # No reason in the log line: free text a human typed, the channel PHI
        # leaks through. It is in the append-only event log and nowhere else.
        logger.warning("Match on loop %s undone via the worklist", loop_id)
        return jsonify({
            "loop_id": loop_id,
            "state": registry.get(loop_id).state.value,
            "detached_to": orphan_id,
        })

    @bp.post("/undo")
    def undo():
        """The page's own form target for a reversal, which needs a loop id field."""
        loop_id = _text(_payload(), "loop_id", _MAX_ATTRIBUTION)
        return reverse_acknowledgement(loop_id)

    @bp.post("/attach")
    def attach_form():
        """The page's own form target, which carries the orphan id in the body."""
        return attach(_text(_payload(), "orphan_id", _MAX_ATTRIBUTION))

    @bp.post("/undo_match")
    def undo_match_form():
        return undo_match(_text(_payload(), "loop_id", _MAX_ATTRIBUTION))

    return bp


def _refused(registry: Registry, loop_id: str, exc: ReferralLoopError, *,
             explain_preliminary: bool = False):
    """A refused action is a 409 the UI can render, never a 500.

    The registry's messages for these three actions are composed from the loop
    id, the state name and an OBX-11 code -- all allowlisted, none identifying --
    so echoing one is safe and is far more use to a coordinator than a generic
    string. The PHI proof asserts that rather than trusting it.

    `explain_preliminary` adds the safety rule in words, but only when it is
    actually the rule that fired. `_ACKNOWLEDGEABLE_FROM` is exactly {RESULTED},
    so a refusal on a RESULTED loop can only have come from the result-status
    allowlist; on any other state it came from the state check, and attaching the
    preliminary explanation there would be a confident lie.
    """
    body = {"error": str(exc), "loop_id": loop_id}
    if explain_preliminary:
        try:
            if registry.get(loop_id).state is LoopState.RESULTED:
                body["detail"] = PRELIMINARY_RULE
        except ReferralLoopError:  # pragma: no cover - the get above already ran
            pass
    logger.info("Worklist action on loop %s refused (%s)", loop_id, type(exc).__name__)
    return jsonify(body), 409


def _arrived_at(store: LoopStore, loop: Loop) -> datetime | None:
    """When this record first appeared. The only clock an orphan has."""
    events = store.events_for(loop.loop_id)
    return events[0].occurred_at if events else None


def _recent_merges(store: LoopStore) -> list[dict]:
    """Merge activity, most recent first, with both identifiers left off.

    Spec section 4 decision 1 asks that a coordinator *see a merge happen* rather
    than infer it from work that stopped appearing. What makes that true is that
    a merge is visible and timed -- not which two MRNs it named, which is the one
    part of the alias log that must not leave the building.
    """
    rows = store.alias_events()[-_RECENT_MERGES:]
    return [
        {
            "alias_event_id": row["alias_event_id"],
            "event_type": row["event_type"],
            "recorded_at": row["established_at"],
            # The MSH-10 of the ADT^A40, or the control id of an administrative
            # reversal. A message identifier, not a person.
            "recorded_by": row["established_by"],
        }
        for row in reversed(rows)
    ]


def create_app(store: LoopStore, registry: Registry, pack: RulePack) -> Flask:
    app = Flask(__name__)
    # Before the blueprint's own copy, and covering what that one cannot: routing
    # failures, and the /static route Flask registers here rather than there.
    app.before_request(_refuse_a_request_this_page_did_not_originate)
    app.register_blueprint(create_blueprint(store, registry, pack))
    return app


def make_worklist_server(
    store: LoopStore,
    registry: Registry,
    pack: RulePack,
    host: str = "127.0.0.1",
    port: int = 5057,
):
    """A bound WSGI server for the worklist. Loopback by default and by refusal.

    The MLLP listener warns on a non-loopback bind rather than refusing, because
    an interface engine genuinely lives on another host. This page does not have
    that excuse: it has no authentication, it is specified as a localhost surface
    (spec sections 2 and 3), and a v1 pilot that exposed it to the ward network
    would be exposing a PHI-adjacent queue with nothing in front of it. Refusing
    is recoverable in one line of config; the alternative is not.
    """
    if host not in _LOOPBACK:
        raise ReferralLoopError(
            f"Refusing to bind the coordinator worklist to {host!r}. It has no "
            "authentication and v1 specifies it as a localhost surface; put it behind "
            "an authenticating reverse proxy on this host instead."
        )
    from werkzeug.serving import make_server

    app = create_app(store, registry, pack)
    return make_server(host, port, app)
