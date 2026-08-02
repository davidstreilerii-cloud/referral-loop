"""The referral lifecycle, and the separate lifecycle of an artifact that matched nothing.

The existing LoopState in events.py has nine members and conflates the two: ORPHAN,
DISMISSED and ATTACHED are not states a referral can be in, they are the life of an
inbound document that found no referral to attach to. Design spec section 6.5 records
why they are split here and what it buys.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReferralState(str, Enum):
    """Ordered by the path a referral normally walks, exits last.

    Deliberately absent: a CLOSED member. The existing machine reserves one and refuses
    it at the store; the guarantee that replaces it lives on the transition, not the
    state -- a move into RECONCILED whose assertion source is the system rather than a
    human is refused. That is a stronger rule than an unreachable enum member, because it
    survives someone making the member reachable.
    """

    DRAFT = "draft"
    SENT = "sent"
    RECEIVED = "received"
    ACCEPTED = "accepted"
    SCHEDULED = "scheduled"
    SEEN = "seen"
    DOCUMENTED = "documented"
    RECONCILED = "reconciled"

    DECLINED = "declined"
    CANCELLED = "cancelled"
    AGED_OUT = "aged-out"


class ArtifactState(str, Enum):
    """An inbound document, and whether anyone has decided what it belongs to.

    Both exits are terminal. There is no route back to UNMATCHED: re-opening a decision
    means a new artifact record referencing the same content hash, so the coordinator's
    original judgement stays in the log rather than being overwritten.
    """

    UNMATCHED = "unmatched"
    ATTACHED = "attached"
    DISMISSED = "dismissed"


@dataclass(frozen=True)
class Hold:
    """Suspension, carried alongside the state rather than replacing it.

    FHIR's Task.status has an on-hold value, which is where this projects -- but
    projecting to it discards which state the referral was held *from*, and that is the
    distinction the aging agent escalates on ("accepted but never scheduled" is not
    "scheduled but never seen"). So the underlying state is preserved here and the
    projection reconstructs both halves.
    """

    reason: str
    actor: str
