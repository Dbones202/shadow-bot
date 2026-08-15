from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from discord_economy_bot.db.models import (
    EconomyAccount,
    RoleCollectionCooldown,
    RoleIncomeRule,
)


async def delete_member_economy(session: AsyncSession, guild_id: int, user_id: int) -> None:
    await session.execute(
        delete(EconomyAccount).where(
            EconomyAccount.guild_id == guild_id,
            EconomyAccount.user_id == user_id,
        )
    )


async def reset_lost_role_cooldowns(
    session: AsyncSession, guild_id: int, user_id: int, lost_role_ids: set[int]
) -> None:
    if not lost_role_ids:
        return

    account_id = select(EconomyAccount.id).where(
        EconomyAccount.guild_id == guild_id,
        EconomyAccount.user_id == user_id,
    )
    rule_ids = select(RoleIncomeRule.id).where(
        RoleIncomeRule.guild_id == guild_id,
        RoleIncomeRule.role_id.in_(lost_role_ids),
    )
    await session.execute(
        delete(RoleCollectionCooldown).where(
            RoleCollectionCooldown.account_id.in_(account_id),
            RoleCollectionCooldown.role_rule_id.in_(rule_ids),
        )
    )
