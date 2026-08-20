"""Deciding what a member may collect from their income roles, and when.

Pure logic: given the rules attached to the roles a member currently holds,
their existing cooldowns, and the current time, work out what is payable now
and when the rest come back.

Kept free of ORM objects and Discord types so the awkward parts — partial
eligibility, never-collected roles, cooldowns that expire mid-command — are
testable directly.

Two rules from ECONOMY_SPEC.md are encoded here:

* `/collect` takes **everything** currently eligible in one action, and reports
  the next availability of the roles that were not.
* Missed windows **do not accumulate**. A member who ignores a 12-hour income
  for three days collects one payout, not six. The next window is measured from
  the moment of collection, not from when the previous one expired.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class IncomeOpportunity:
    """One income role a member holds, with whatever cooldown applies to them."""

    role_id: int
    payout: int
    cooldown_seconds: int
    #: When this member may next collect. ``None`` means never collected, or the
    #: cooldown was cleared because they lost and regained the role — either way
    #: they are immediately eligible.
    available_at: datetime | None = None

    def is_ready(self, now: datetime) -> bool:
        return self.available_at is None or self.available_at <= now


@dataclass(frozen=True, slots=True)
class CollectedIncome:
    role_id: int
    payout: int
    next_available_at: datetime


@dataclass(frozen=True, slots=True)
class WaitingIncome:
    role_id: int
    payout: int
    available_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    collected: tuple[CollectedIncome, ...]
    waiting: tuple[WaitingIncome, ...]

    @property
    def total(self) -> int:
        return sum(item.payout for item in self.collected)

    @property
    def collected_anything(self) -> bool:
        return bool(self.collected)

    @property
    def next_available_at(self) -> datetime | None:
        """The soonest a waiting role becomes collectable, if any are waiting."""
        if not self.waiting:
            return None
        return min(item.available_at for item in self.waiting)


def plan_collection(opportunities: list[IncomeOpportunity], *, now: datetime) -> CollectionPlan:
    """Split a member's income roles into what pays now and what does not.

    The returned plan is advisory — the caller applies it inside a transaction.
    Separating the decision from the write keeps this testable and means the
    same reasoning drives both the payout and the message shown to the member.
    """
    collected: list[CollectedIncome] = []
    waiting: list[WaitingIncome] = []

    for opportunity in opportunities:
        if opportunity.is_ready(now):
            collected.append(
                CollectedIncome(
                    role_id=opportunity.role_id,
                    payout=opportunity.payout,
                    # Measured from now, not from the expired window — this is
                    # what stops missed windows accumulating.
                    next_available_at=now + timedelta(seconds=opportunity.cooldown_seconds),
                )
            )
        else:
            assert opportunity.available_at is not None  # is_ready covers None
            waiting.append(
                WaitingIncome(
                    role_id=opportunity.role_id,
                    payout=opportunity.payout,
                    available_at=opportunity.available_at,
                )
            )

    # Biggest payouts first when collected; soonest first when waiting — both
    # are what a member actually wants to see at the top of the list.
    collected.sort(key=lambda item: item.payout, reverse=True)
    waiting.sort(key=lambda item: item.available_at)
    return CollectionPlan(collected=tuple(collected), waiting=tuple(waiting))
