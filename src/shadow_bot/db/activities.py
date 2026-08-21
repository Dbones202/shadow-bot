"""Database operations for activities — configuration, cooldowns, and attempts.

`attempt` locks the accounts it touches before reading balances, for the same
reason every other money-moving function does: without it, two commands racing
can both read the same balance and the second write silently discards the first.
Steal locks both participants in a consistent order so two members robbing each
other simultaneously cannot deadlock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.economy import _ledger, get_or_create_account
from shadow_bot.db.models import ActivityCooldown, ActivityRule, EconomyAccount
from shadow_bot.domain.activities import (
    Activity,
    ActivityError,
    ActivityOutcome,
    ActivitySettings,
    resolve_activity,
    validate_settings,
)
from shadow_bot.domain.fines import apply_fine


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """Everything the command needs to describe what happened."""

    outcome: ActivityOutcome
    #: What a failed attempt actually collected — a member at their floor pays
    #: less than the fine demanded, and the message should say so.
    collected: int = 0
    uncollected: int = 0
    cash: int = 0
    bank: int = 0
    next_available_at: datetime | None = None


class OnCooldown(ActivityError):
    """Raised when a member tries again too soon."""

    def __init__(self, available_at: datetime) -> None:
        super().__init__("That activity is still on cooldown.")
        self.available_at = available_at


def to_settings(rule: ActivityRule) -> ActivitySettings:
    return ActivitySettings(
        key=Activity(rule.activity_key),
        enabled=rule.enabled,
        cooldown_seconds=rule.cooldown_seconds,
        success_chance=Decimal(rule.success_chance),
        success_min=rule.success_min,
        success_max=rule.success_max,
        fine_min=rule.fine_min,
        fine_max=rule.fine_max,
    )


async def get_rule(session: AsyncSession, guild_id: int, activity: Activity) -> ActivityRule | None:
    return await session.get(ActivityRule, (guild_id, activity.value))


async def list_rules(session: AsyncSession, guild_id: int) -> list[ActivityRule]:
    return list(
        (
            await session.execute(
                select(ActivityRule)
                .where(ActivityRule.guild_id == guild_id)
                .order_by(ActivityRule.activity_key)
            )
        )
        .scalars()
        .all()
    )


async def upsert_rule(
    session: AsyncSession,
    guild_id: int,
    activity: Activity,
    *,
    cooldown_seconds: int,
    success_chance: Decimal,
    success_min: int,
    success_max: int,
    fine_min: int,
    fine_max: int,
    enabled: bool = True,
) -> tuple[ActivityRule, bool]:
    """Configure an activity. Returns ``(rule, created)``.

    Validated before writing so an administrator gets an explanation rather
    than a database CHECK constraint surfacing as a generic failure.
    """
    validate_settings(
        success_chance=success_chance,
        success_min=success_min,
        success_max=success_max,
        fine_min=fine_min,
        fine_max=fine_max,
    )
    if cooldown_seconds <= 0:
        raise ActivityError("Cooldown must be longer than zero.")

    rule = await get_rule(session, guild_id, activity)
    created = rule is None
    if rule is None:
        rule = ActivityRule(guild_id=guild_id, activity_key=activity.value)
        session.add(rule)

    rule.cooldown_seconds = cooldown_seconds
    rule.success_chance = success_chance
    rule.success_min = success_min
    rule.success_max = success_max
    rule.fine_min = fine_min
    rule.fine_max = fine_max
    rule.enabled = enabled
    await session.flush()
    return rule, created


async def set_enabled(
    session: AsyncSession, guild_id: int, activity: Activity, enabled: bool
) -> bool:
    """Turn an activity on or off. Returns False if it was never configured."""
    rule = await get_rule(session, guild_id, activity)
    if rule is None:
        return False
    rule.enabled = enabled
    return True


async def _cooldown(
    session: AsyncSession, account_id: uuid.UUID, activity: Activity
) -> datetime | None:
    row = await session.get(ActivityCooldown, (account_id, activity.value))
    return row.available_at if row else None


async def attempt(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    activity: Activity,
    *,
    chance_roll: Decimal,
    amount_roll: float,
    cash_floor: int,
    bank_floor: int,
    target_id: int | None = None,
    now: datetime | None = None,
) -> AttemptResult:
    """Run one attempt at an activity and apply its consequences.

    Rolls are supplied by the caller so the resolution stays deterministic and
    testable — see `domain.activities`.
    """
    now = now or datetime.now(UTC)

    rule = await get_rule(session, guild_id, activity)
    if rule is None or not rule.enabled:
        raise ActivityError(
            f"`/{activity.value}` is not set up on this server yet. "
            f"An administrator can enable it with `/activity set`."
        )
    settings = to_settings(rule)

    if activity.targets_a_member:
        if target_id is None:
            raise ActivityError(f"`/{activity.value}` needs someone to target.")
        if target_id == user_id:
            raise ActivityError("You cannot steal from yourself.")
        # Lock both accounts in a consistent order so two members targeting each
        # other at the same moment cannot deadlock.
        for uid in sorted({user_id, target_id}):
            await get_or_create_account(session, guild_id, uid)
        rows = (
            await session.execute(
                select(EconomyAccount)
                .where(
                    EconomyAccount.guild_id == guild_id,
                    EconomyAccount.user_id.in_([user_id, target_id]),
                )
                .order_by(EconomyAccount.user_id)
                .with_for_update()
            )
        ).scalars()
        accounts = {a.user_id: a for a in rows}
        account, target = accounts[user_id], accounts[target_id]
    else:
        account = await get_or_create_account(session, guild_id, user_id, lock=True)
        target = None

    available_at = await _cooldown(session, account.id, activity)
    if available_at is not None and available_at > now:
        raise OnCooldown(available_at)

    outcome = resolve_activity(
        settings,
        chance_roll=chance_roll,
        amount_roll=amount_roll,
        target_cash=target.cash if target else None,
    )

    correlation_id = uuid.uuid4()
    collected = uncollected = 0

    if outcome.succeeded:
        account.cash += outcome.amount
        if target is not None and outcome.amount:
            target.cash -= outcome.amount
            session.add(
                _ledger(
                    guild_id=guild_id,
                    correlation_id=correlation_id,
                    subject=target,
                    category=f"{activity.value}_loss",
                    cash_delta=-outcome.amount,
                    actor=account,
                    counterparty=account,
                    amount=outcome.amount,
                )
            )
        if outcome.amount:
            session.add(
                _ledger(
                    guild_id=guild_id,
                    correlation_id=correlation_id,
                    subject=account,
                    category=f"{activity.value}_success",
                    cash_delta=outcome.amount,
                    counterparty=target,
                    amount=outcome.amount,
                    details={"activity": activity.value},
                )
            )
    elif outcome.amount:
        # A failed attempt is fined, drawing cash to its floor and then bank to
        # its floor — the behaviour tested in domain/fines.py since day one and
        # only now actually used.
        result = apply_fine(
            cash=account.cash,
            bank=account.bank,
            amount=outcome.amount,
            cash_floor=cash_floor,
            bank_floor=bank_floor,
        )
        account.cash, account.bank = result.cash, result.bank
        collected, uncollected = result.collected, result.uncollected
        session.add(
            _ledger(
                guild_id=guild_id,
                correlation_id=correlation_id,
                subject=account,
                category=f"{activity.value}_fine",
                cash_delta=-result.cash_taken,
                bank_delta=-result.bank_taken,
                amount=outcome.amount,
                details={
                    "activity": activity.value,
                    "attempted": outcome.amount,
                    "collected": collected,
                    "uncollected": uncollected,
                },
            )
        )

    next_available_at = now + timedelta(seconds=settings.cooldown_seconds)
    await session.execute(
        pg_insert(ActivityCooldown)
        .values(
            account_id=account.id,
            activity_key=activity.value,
            available_at=next_available_at,
        )
        .on_conflict_do_update(
            index_elements=["account_id", "activity_key"],
            set_={"available_at": next_available_at},
        )
    )

    return AttemptResult(
        outcome=outcome,
        collected=collected,
        uncollected=uncollected,
        cash=account.cash,
        bank=account.bank,
        next_available_at=next_available_at,
    )
