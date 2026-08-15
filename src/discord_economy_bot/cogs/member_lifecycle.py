from __future__ import annotations

import logging

import discord
from discord.ext import commands

from discord_economy_bot.db.member_cleanup import (
    delete_member_economy,
    reset_lost_role_cooldowns,
)

LOGGER = logging.getLogger(__name__)


class MemberLifecycleCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        async with self.bot.database.sessions.begin() as session:  # type: ignore[attr-defined]
            await delete_member_economy(session, payload.guild_id, payload.user.id)
        LOGGER.info(
            "Deleted economy data for departed member %s/%s", payload.guild_id, payload.user.id
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        before_ids = {role.id for role in before.roles}
        after_ids = {role.id for role in after.roles}
        lost_role_ids = before_ids - after_ids
        if not lost_role_ids:
            return
        async with self.bot.database.sessions.begin() as session:  # type: ignore[attr-defined]
            await reset_lost_role_cooldowns(session, after.guild.id, after.id, lost_role_ids)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberLifecycleCog(bot))
