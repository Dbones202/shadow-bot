"""Administrative currency commands: `/economy add` and `/economy remove`.

Creating and removing currency is the only way money enters or leaves a guild.
Access is currently limited to the guild owner and the application owner — the
delegated capability system in `domain.permissions` is a later milestone, and
until it is wired up this cog deliberately grants nothing to anyone else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from shadow_bot.db import economy
from shadow_bot.db.models import GuildSettings
from shadow_bot.domain.amounts import MAX_AMOUNT, AmountError, CurrencyStyle, format_money
from shadow_bot.domain.amounts import parse_amount as parse
from shadow_bot.domain.authority import Authority, authority_of
from shadow_bot.domain.banking import BankingError

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

DESTINATIONS = [
    app_commands.Choice(name="cash", value="cash"),
    app_commands.Choice(name="bank", value="bank"),
]


class AdminEconomyCog(commands.Cog):
    """Grouped under `/economy` so member commands stay at the top level."""

    group = app_commands.Group(
        name="economy",
        description="Owner tools for managing currency",
        guild_only=True,
    )

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    async def _authorise(
        self, interaction: discord.Interaction
    ) -> tuple[GuildSettings, Authority] | None:
        """Check configuration and standing, replying if either fails."""
        assert interaction.guild_id is not None

        standing = authority_of(
            interaction.user.id,
            guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
            app_owner_ids=self.bot.settings.bot_owner_ids,
        )
        if standing is Authority.NONE:
            await interaction.response.send_message(
                "Only the server owner can create or remove currency.", ephemeral=True
            )
            return None

        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "This server has no economy yet. Run `/setup` first.", ephemeral=True
            )
            return None
        return settings, standing

    @staticmethod
    def _reject_bot(member: discord.Member) -> str | None:
        return "Bots do not hold accounts." if member.bot else None

    @group.command(name="add", description="Create currency into a member's balance")
    @app_commands.describe(
        member="Who receives it",
        amount="How much to create",
        destination="Cash or bank. Defaults to cash.",
    )
    @app_commands.choices(destination=DESTINATIONS)
    async def add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: str,
        destination: app_commands.Choice[str] | None = None,
    ) -> None:
        authorised = await self._authorise(interaction)
        if authorised is None:
            return
        settings, standing = authorised

        if problem := self._reject_bot(member):
            await interaction.response.send_message(problem, ephemeral=True)
            return

        # Creating currency has no balance to draw from, so the only ceiling is
        # what the column can hold.
        try:
            value = parse(amount, available=MAX_AMOUNT, what="create")
        except AmountError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        where = destination.value if destination else "cash"
        assert interaction.guild_id is not None

        async with self.bot.database.sessions.begin() as session:
            try:
                account = await economy.grant_currency(
                    session,
                    interaction.guild_id,
                    actor_id=interaction.user.id,
                    target_id=member.id,
                    amount=value,
                    destination=where,
                    authority=standing.value,
                )
            except BankingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            cash, bank = account.cash, account.bank

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(
            title="Currency created",
            description=f"Added **{format_money(value, style)}** to {member.mention}'s {where}.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Cash", value=format_money(cash, style), inline=True)
        embed.add_field(name="Bank", value=format_money(bank, style), inline=True)
        await interaction.response.send_message(embed=embed)
        LOGGER.info(
            "currency_created guild=%s actor=%s target=%s amount=%s dest=%s authority=%s",
            interaction.guild_id,
            interaction.user.id,
            member.id,
            value,
            where,
            standing.value,
        )

    @group.command(name="remove", description="Remove currency from a member's balance")
    @app_commands.describe(
        member="Whose balance to reduce",
        amount="How much to remove",
        source="Cash or bank. Defaults to cash.",
    )
    @app_commands.choices(source=DESTINATIONS)
    async def remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        authorised = await self._authorise(interaction)
        if authorised is None:
            return
        settings, standing = authorised

        if problem := self._reject_bot(member):
            await interaction.response.send_message(problem, ephemeral=True)
            return

        try:
            value = parse(amount, available=MAX_AMOUNT, what="remove")
        except AmountError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        where = source.value if source else "cash"
        floor = settings.cash_floor if where == "cash" else settings.bank_floor
        assert interaction.guild_id is not None

        async with self.bot.database.sessions.begin() as session:
            try:
                account, removed, uncollected = await economy.remove_currency(
                    session,
                    interaction.guild_id,
                    actor_id=interaction.user.id,
                    target_id=member.id,
                    amount=value,
                    source=where,
                    floor=floor,
                    authority=standing.value,
                )
            except BankingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            cash, bank = account.cash, account.bank

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(
            title="Currency removed",
            description=f"Removed **{format_money(removed, style)}** from "
            f"{member.mention}'s {where}.",
            color=discord.Color.orange() if uncollected else discord.Color.green(),
        )
        if uncollected:
            # Say so explicitly. A silent partial removal looks like the command
            # took a different amount than it was given.
            embed.add_field(
                name="Could not remove",
                value=f"{format_money(uncollected, style)} — the {where} floor "
                f"({format_money(floor, style)}) was reached.",
                inline=False,
            )
        embed.add_field(name="Cash", value=format_money(cash, style), inline=True)
        embed.add_field(name="Bank", value=format_money(bank, style), inline=True)
        await interaction.response.send_message(embed=embed)
        LOGGER.info(
            "currency_removed guild=%s actor=%s target=%s removed=%s uncollected=%s "
            "source=%s authority=%s",
            interaction.guild_id,
            interaction.user.id,
            member.id,
            removed,
            uncollected,
            where,
            standing.value,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminEconomyCog(bot))  # type: ignore[arg-type]
