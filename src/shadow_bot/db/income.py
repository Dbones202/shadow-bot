"""Database operations for role income and collection.

Rule management is straightforward CRUD. `collect` is the interesting one: it
locks the member's account, decides what is payable using the pure planner in
`domain.income`, then applies the result and stamps new cooldowns — all inside
one transaction, so a member spamming `/collect` cannot be paid twice for the
same window.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.economy import _ledger, get_or_create_account
from shadow_bot.db.models import RoleCollectionCooldown, RoleIncomeRule
from shadow_bot.domain.income import CollectionPlan, IncomeOpportunity, plan_collection


async def list_rules(session: AsyncSession, guild_id: int) -> list[RoleIncomeRule]:
    return list(
        (
            await session.execute(
                select(RoleIncomeRule)
                .where(RoleIncomeRule.guild_id == guild_id)
                .order_by(RoleIncomeRule.payout.desc())
            )
        )
        .scalars()
        .all()
    )


async def upsert_rule(
    session: AsyncSession,
    guild_id: int,
    *,
    role_id: int,
    payout: int,
    cooldown_seconds: int,
) -> tuple[RoleIncomeRule, bool]:
    """Create or update a role's income. Returns ``(rule, created)``.

    Configuring the same role twice updates it rather than failing on the
    unique constraint — an owner adjusting a payout should not have to remove
    the rule first.
    """
    existing = (
        await session.execute(
            select(RoleIncomeRule).where(
                RoleIncomeRule.guild_id == guild_id,
                RoleIncomeRule.role_id == role_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.payout = payout
        existing.cooldown_seconds = cooldown_seconds
        existing.enabled = True
        return existing, False

    rule = RoleIncomeRule(
        id=uuid.uuid4(),
        guild_id=guild_id,
        role_id=role_id,
        payout=payout,
        cooldown_seconds=cooldown_seconds,
        enabled=True,
    )
    session.add(rule)
    await session.flush()
    return rule, True


async def delete_rule(session: AsyncSession, guild_id: int, role_id: int) -> bool:
    """Remove a role's income rule. Returns whether anything was removed.

    Cooldown rows referencing the rule are removed by the cascade on
    `role_collection_cooldowns.role_rule_id`.
    """
    result = await session.execute(
        delete(RoleIncomeRule).where(
            RoleIncomeRule.guild_id == guild_id,
            RoleIncomeRule.role_id == role_id,
        )
    )
    return bool(result.rowcount)


async def collect(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    role_ids: Sequence[int],
    *,
    now: datetime | None = None,
) -> CollectionPlan:
    """Pay out every income role the member currently holds and may collect.

    ``role_ids`` comes from Discord rather than the database — a member's roles
    are Discord's truth, and rules for roles they no longer hold must not pay.

    Returns the plan that was applied, so the caller can render exactly what
    happened without recomputing it.
    """
    now = now or datetime.now(UTC)
    account = await get_or_create_account(session, guild_id, user_id, lock=True)

    if not role_ids:
        return plan_collection([], now=now)

    rules = list(
        (
            await session.execute(
                select(RoleIncomeRule).where(
                    RoleIncomeRule.guild_id == guild_id,
                    RoleIncomeRule.role_id.in_(list(role_ids)),
                    RoleIncomeRule.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rules:
        return plan_collection([], now=now)

    cooldowns = {
        row.role_rule_id: row.available_at
        for row in (
            await session.execute(
                select(RoleCollectionCooldown).where(
                    RoleCollectionCooldown.account_id == account.id,
                    RoleCollectionCooldown.role_rule_id.in_([r.id for r in rules]),
                )
            )
        )
        .scalars()
        .all()
    }

    by_role = {rule.role_id: rule for rule in rules}
    plan = plan_collection(
        [
            IncomeOpportunity(
                role_id=rule.role_id,
                payout=rule.payout,
                cooldown_seconds=rule.cooldown_seconds,
                available_at=cooldowns.get(rule.id),
            )
            for rule in rules
        ],
        now=now,
    )

    if not plan.collected:
        return plan

    account.cash += plan.total
    correlation_id = uuid.uuid4()

    for item in plan.collected:
        rule = by_role[item.role_id]
        await session.execute(
            pg_insert(RoleCollectionCooldown)
            .values(
                account_id=account.id,
                role_rule_id=rule.id,
                available_at=item.next_available_at,
            )
            .on_conflict_do_update(
                index_elements=["account_id", "role_rule_id"],
                set_={"available_at": item.next_available_at},
            )
        )
        # One ledger entry per role, sharing a correlation id. A single summed
        # entry would make "how much has this role paid out?" unanswerable.
        session.add(
            _ledger(
                guild_id=guild_id,
                correlation_id=correlation_id,
                subject=account,
                category="role_income",
                cash_delta=item.payout,
                amount=item.payout,
                details={"role_id": str(item.role_id), "rule_id": str(rule.id)},
            )
        )

    return plan
