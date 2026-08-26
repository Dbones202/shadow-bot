from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.games import anonymise_participants
from shadow_bot.db.models import (
    EconomyAccount,
    RoleCollectionCooldown,
    RoleIncomeRule,
)


async def delete_member_economy(session: AsyncSession, guild_id: int, user_id: int) -> int:
    """Remove everything the bot holds about a member who has left.

    Balances, ledger entries and cooldowns are deleted outright by the cascade
    from `economy_accounts`. Game history is the one exception: those rows are
    **anonymised first**, because deleting them would silently rewrite games that
    already finished — the member's kills would vanish and their victims would be
    left without a killer.

    Anonymising is not a weaker promise. The account link, the user id and the
    stored name are all removed, so nothing that survives points back at the
    person; what remains is the shape of a game that happened.

    Order matters. The anonymisation has to run before the account is deleted:
    afterwards the `ON DELETE SET NULL` would already have cleared `account_id`,
    and the query that finds these rows joins through the guild, so the user id
    would be left behind.

    Returns the number of game rows anonymised, so the caller can log it.
    """
    anonymised = await anonymise_participants(session, guild_id, user_id)
    await session.execute(
        delete(EconomyAccount).where(
            EconomyAccount.guild_id == guild_id,
            EconomyAccount.user_id == user_id,
        )
    )
    return anonymised


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
