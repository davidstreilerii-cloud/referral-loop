"""Retention is configured, not assumed (spec section 6).

Raw messages and resolved loops both need a purge policy the hospital sets.
There is no default and there is not going to be one: an unset retention period
means the site has not made the decision yet, and guessing one on their behalf
would hold PHI past whatever their policy actually allows -- or delete it before
it. This is the same posture as the confidence floor in section 7 and the
threshold acceptance gate in section 12, applied to the one operation in this
subsystem that destroys data: decline rather than guess.

Which states are purgeable
--------------------------
Only terminal ones. An open loop is never deleted by age, and getting that set
wrong deletes a clinically open loop -- the failure this entire product exists
to prevent, caused by the housekeeping bolted onto it.

v1 has four terminal states (spec section 4):

  * ``ACKNOWLEDGED`` -- a coordinator confirmed this result belongs to this
    loop. Terminal for v1, and *reopenable*: safety rule 2 returns it to
    ``RESULTED`` when a correction arrives. That is not a reason to exclude it,
    because a reopened loop is no longer ACKNOWLEDGED when it is replayed here.
    It is the reason age is measured from the newest event.
  * ``CANCELLED`` -- the expectation was withdrawn. The failure matrix sends a
    result arriving for a cancelled loop to the orphan queue rather than
    reopening it, so nothing brings this state back.
  * ``DISMISSED`` -- a coordinator judged an orphan unattachable. Terminal for
    orphans that belong to no loop here.
  * ``ATTACHED`` -- a coordinator attached an orphan to the real loop. Terminal
    and off every queue.

``ORPHAN`` is **not** terminal and is not in the set. It is a result awaiting a
human -- the state exists precisely so an unmatched result is visible as
unplaced -- and deleting one by age deletes a result nobody ever looked at.
An orphan queue that ages itself out is a tracker that reports all-clear by
forgetting.

``CLOSED`` is not in the set either. It is reserved for v2 and unreachable in
v1, so a ``loops`` row carrying it can only have come from outside this version,
and v1 must hold no opinion about deleting a claim it refuses to make.

What retention does not touch
-----------------------------
``labels``
    Carry no identifier by design -- no MRN, no accession, no actor, no free
    text, day resolution only -- so no clinical retention clock applies to them.
    They are also the evidence spec section 7's release gate vetoes on, and a
    deletable label is a gate that can be passed by deleting the evidence. The
    honest consequence is stated rather than hidden: a label whose loop has been
    purged can no longer be reconstructed into an eval case, and
    ``eval.corpus_from_site`` already skips and counts exactly that.

``mrn_alias_events`` / ``mrn_aliases``
    An alias records a permanent fact -- these identifiers are one patient.
    Ageing one out silently re-strands every loop on the retired identifier,
    which is the invisible-open-loop failure section 4 designs against, arriving
    on a retention timer. Section 4 already refuses expiry for this reason; a
    purge is expiry wearing a different name.

``applied_messages``
    The idempotency ledger. Deleting a row re-arms double application of any
    message the engine redelivers under an old control id, and it is the
    smallest table in the file.

The audit trail
    Never touched, and this module never opens that database to delete from it.
    ``guardrails/immutable_audit.py`` blocks deletion at its own authorizer, and
    audit retention is governed by a different and usually longer policy than
    clinical data. It is written *to*: a delete path over PHI that leaves no
    record of having run is the first thing an auditor asks about, and after it
    runs the evidence it would have left is exactly what is gone.

Retention and the eval archive pull in opposite directions
----------------------------------------------------------
Section 7 evaluates a pack revision by replaying the raw archive. A raw
retention period shorter than the interval between pack releases means the
corpus a release is justified against has aged out. Nothing here can resolve
that -- both are site decisions -- so the purge logs the tension rather than
picking a side, and ``corpus_from_site`` counts what it could not rebuild.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .audit import SYSTEM_ACTOR, SYSTEM_ROLE, AuditAction, AuditScope, audited
from .errors import ReferralLoopError
from .events import LoopState
from .store import LoopStore

logger = logging.getLogger(__name__)

RAW_DAYS_ENV = "REFERRAL_RAW_RETENTION_DAYS"
RESOLVED_DAYS_ENV = "REFERRAL_RESOLVED_RETENTION_DAYS"

# What the implementation plan called this variable, before spec section 4 split
# ACKNOWLEDGED from CLOSED. Refused by name rather than read: letting a site
# configure retention under the name of a state this version refuses to enter
# would be the weaker claim wearing the stronger one's name a second time.
_V2_NAMED_ENV = "REFERRAL_CLOSED_RETENTION_DAYS"

PURGEABLE_STATES: tuple[LoopState, ...] = (
    LoopState.ACKNOWLEDGED,
    LoopState.CANCELLED,
    LoopState.DISMISSED,
    LoopState.ATTACHED,
)

# Derived, not written out a second time. A state added to LoopState later lands
# in neither tuple until somebody decides which, and test_the_purgeable_set_
# partitions_every_state fails -- rather than the new state defaulting into the
# deletable half because a literal list was not updated.
NEVER_PURGEABLE_STATES: tuple[LoopState, ...] = tuple(
    state for state in LoopState if state not in PURGEABLE_STATES
)

_PURGEABLE_VALUES: tuple[str, ...] = tuple(state.value for state in PURGEABLE_STATES)

# One day is the shortest policy this will accept.
#
# Zero is refused with the negatives, and deliberately. It means "keep nothing":
# it destroys the replay archive section 7 evaluates a pack against, and deletes
# a loop in the same second it resolves -- before the coordinator who resolved it
# could look at their own work. It is also indistinguishable from a placeholder
# somebody typed intending to come back to it, and the two readings differ by
# the entire archive.
MIN_DAYS = 1

# A hundred years. Past it, `timedelta` overflows and the purge would die with
# an OverflowError naming nothing; refusing at configuration time names the
# variable instead. A century is not a retention period anyone means.
MAX_DAYS = 36525


def _checked(name: str, value: object) -> int:
    """A retention period, or a refusal naming the variable that carries it."""
    if isinstance(value, bool) or not isinstance(value, int):
        # bool before int: True is an int and would configure a one-day policy.
        raise ReferralLoopError(
            f"{name} must be a whole number of days; got {value!r}. Retention is a site "
            "policy decision and this refuses to interpret it."
        )
    if value < MIN_DAYS:
        raise ReferralLoopError(
            f"{name} must be at least {MIN_DAYS} day; got {value}. A period of zero or less "
            "means keep nothing: it destroys the replay archive a rule-pack revision is "
            "evaluated against, and deletes a resolved loop in the same second it resolves."
        )
    if value > MAX_DAYS:
        raise ReferralLoopError(
            f"{name} must be at most {MAX_DAYS} days (a century); got {value}."
        )
    return value


@dataclass(frozen=True)
class RetentionPolicy:
    """Two periods, both stated by the site. No defaults, on either.

    Validated in ``__post_init__`` rather than in ``from_env``, because
    ``from_env`` is not the only door: the CLI reads the environment, tests and
    any future config loader construct the dataclass directly, and validation
    that only guards one of them is decoration.
    """

    raw_days: int
    resolved_days: int

    def __post_init__(self) -> None:
        _checked(RAW_DAYS_ENV, self.raw_days)
        _checked(RESOLVED_DAYS_ENV, self.resolved_days)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "RetentionPolicy":
        import os

        env = os.environ if env is None else env
        raw = (env.get(RAW_DAYS_ENV) or "").strip()
        resolved = (env.get(RESOLVED_DAYS_ENV) or "").strip()

        if not raw or not resolved:
            hint = ""
            if (env.get(_V2_NAMED_ENV) or "").strip():
                hint = (
                    f" {_V2_NAMED_ENV} is set and is not read: CLOSED is reserved for v2 and "
                    f"unreachable in v1, so the period is named for the state v1 actually "
                    f"reaches. Rename it to {RESOLVED_DAYS_ENV}."
                )
            raise ReferralLoopError(
                "Retention is a site policy decision with no safe default, and half a policy "
                f"is not one. Set both {RAW_DAYS_ENV} (how long the raw HL7 archive is kept) "
                f"and {RESOLVED_DAYS_ENV} (how long a resolved loop is kept after its last "
                f"event), in whole days.{hint}"
            )

        return cls(raw_days=_as_int(RAW_DAYS_ENV, raw), resolved_days=_as_int(RESOLVED_DAYS_ENV, resolved))


def _as_int(name: str, text: str) -> int:
    """Strict. `int()` accepts "  30\\n" and "+30"; nothing else is a period."""
    if not text.isdigit():
        raise ReferralLoopError(
            f"{name} must be a whole number of days written in digits; got {text!r}."
        )
    try:
        return int(text)
    except ValueError as exc:  # pragma: no cover - isdigit already excludes it
        raise ReferralLoopError(f"{name} is not a whole number of days: {text!r}") from exc


def purge(
    store: LoopStore,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    reclaim: bool = True,
    actor: str = SYSTEM_ACTOR,
    role: str = SYSTEM_ROLE,
) -> dict:
    """Delete aged raw messages and aged terminal loops. Open loops are untouchable.

    Returns a report of counts. Every value is an int or a bool -- no loop id,
    no MRN, no control id -- because the report is logged and written to the
    audit trail, and this codebase does not open a new channel for identifiers
    to leave through in the module whose job is removing them.

    **A dry run writes no audit row.** The counts it produces are what a purge
    *would* delete, and a row carrying them is indistinguishable from a row
    describing a purge that did. This trail records destruction of clinical
    records; a preview destroyed nothing, and an auditor reading `loops_deleted:
    412` has to be able to conclude that 412 loops are gone. `nullcontext` keeps
    the two paths structurally identical so the real one cannot be edited around.
    """
    now = datetime.now(timezone.utc) if now is None else store._as_utc(now)

    recorder = (
        nullcontext(AuditScope()) if dry_run
        else audited(AuditAction.RETENTION_PURGED, actor=actor, role=role)
    )
    with recorder as scope:
        report = store.purge_retention(
            raw_cutoff=now - timedelta(days=policy.raw_days),
            loop_cutoff=now - timedelta(days=policy.resolved_days),
            purgeable_states=_PURGEABLE_VALUES,
            dry_run=dry_run,
            reclaim=reclaim,
        )
        report["dry_run"] = dry_run
        scope.raw_deleted = report["raw_deleted"]
        scope.loops_deleted = report["loops_deleted"]
        scope.raw_retention_days = policy.raw_days
        scope.resolved_retention_days = policy.resolved_days

    logger.info(
        "Retention purge (%s): raw_days=%d resolved_days=%d %s",
        "dry run" if dry_run else "applied", policy.raw_days, policy.resolved_days, report,
    )
    if report["raw_deleted"]:
        logger.info(
            "The raw archive is what a rule-pack revision is replayed against (spec section "
            "7). %d message(s) older than %d days are now gone from it, and any labeled case "
            "that needed them can no longer be reconstructed.",
            report["raw_deleted"], policy.raw_days,
        )
    return report
