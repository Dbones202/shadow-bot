"""Member-facing balance commands: `/balance`, `/deposit`, `/withdraw`, `/pay`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from shadow_bot.db import economy
from shadow_bot.db.models import GuildSettings
from shadow_bot.domain.amounts import AmountError, CurrencyStyle, format_money, parse_amount
from shadow_bot.domain.banking import BankingError, spendable

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

NOT_CONFIGURED = "This server has no economy yet. The server owner can create one with `/setup`."
DISABLED = "The economy is currently disabled on this server."


class EconomyCog(commands.Cog):
    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    async def _settings_or_reply(self, interaction: discord.Interaction) -> GuildSettings | None:
        """Return the guild's settings, or explain why the command cannot run.

        Both branches respond to the interaction, so callers only need to check
        for ``None`` and return.
        """
        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, interaction.guild_id)

        if settings is None:
            await interaction.response.send_message(NOT_CONFIGURED, ephemeral=True)
            return None
        if not settings.economy_enabled:
            await interaction.response.send_message(DISABLED, ephemeral=True)
            return None
        return settings

    @app_commands.command(name="balance", description="Show cash and bank balances")
    @app_commands.describe(member="Whose balance to show. Defaults to you.")
    @app_commands.guild_only()
    async def balance(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        settings = await self._settings_or_reply(interaction)
        if settings is None:
            return

        target = member or interaction.user
        if isinstance(target, discord.Member) and target.bot:
            await interaction.response.send_message("Bots do not hold accounts.", ephemeral=True)
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            account = await economy.get_or_create_account(session, interaction.guild_id, target.id)
            cash, bank = account.cash, account.bank

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(
            title=f"{target.display_name}'s balance", color=discord.Color.blurple()
        )
        embed.add_field(name="Cash", value=format_money(cash, style), inline=True)
        embed.add_field(name="Bank", value=format_money(bank, style), inline=True)
        embed.add_field(name="Total", value=format_money(cash + bank, style), inline=False)
        if cash < 0 or bank < 0:
            embed.set_footer(text="Negative balances come from fines and must be paid off.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="deposit", description="Move cash into your bank")
    @app_commands.describe(amount="An amount, or `all` / `half`")
    @app_commands.guild_only()
    async def deposit(self, interaction: discord.Interaction, amount: str) -> None:
        await self._move(interaction, amount, action="deposit")

    @app_commands.command(name="withdraw", description="Move banked funds into cash")
    @app_commands.describe(amount="An amount, or `all` / `half`")
    @app_commands.guild_only()
    async def withdraw(self, interaction: discord.Interaction, amount: str) -> None:
        await self._move(interaction, amount, action="withdraw")

    async def _move(
        self, interaction: discord.Interaction, raw_amount: str, *, action: str
    ) -> None:
        """Shared body for deposit and withdraw — they differ only in direction."""
        settings = await self._settings_or_reply(interaction)
        if settings is None:
            return

        assert interaction.guild_id is not None
        guild_id, user_id = interaction.guild_id, interaction.user.id
        style = CurrencyStyle.from_settings(settings)

        async with self.bot.database.sessions.begin() as session:
            account = await economy.get_or_create_account(session, guild_id, user_id, lock=True)
            source = account.cash if action == "deposit" else account.bank

            try:
                amount = parse_amount(raw_amount, available=spendable(source), what=action)
            except AmountError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            try:
                if action == "deposit":
                    await economy.deposit(session, guild_id, user_id, amount)
                else:
                    await economy.withdraw(session, guild_id, user_id, amount)
            except BankingError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            cash, bank = account.cash, account.bank

        verb = "Deposited" if action == "deposit" else "Withdrew"
        embed = discord.Embed(
            title=f"{verb} {format_money(amount, style)}",
            color=discord.Color.green(),
        )
        embed.add_field(name="Cash", value=format_money(cash, style), inline=True)
        embed.add_field(name="Bank", value=format_money(bank, style), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="pay", description="Send cash to another member")
    @app_commands.describe(member="Who to pay", amount="An amount, or `all` / `half`")
    @app_commands.guild_only()
    async def pay(
        self, interaction: discord.Interaction, member: discord.Member, amount: str
    ) -> None:
        settings = await self._settings_or_reply(interaction)
        if settings is None:
            return

        if member.bot:
            await interaction.response.send_message("Bots do not hold accounts.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "Paying yourself would not achieve much.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        style = CurrencyStyle.from_settings(settings)

        async with self.bot.database.sessions.begin() as session:
            sender = await economy.get_or_create_account(
                session, guild_id, interaction.user.id, lock=True
            )
            try:
                value = parse_amount(amount, available=spendable(sender.cash), what="send")
            except AmountError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            try:
                sender_account, _ = await economy.pay(
                    session, guild_id, interaction.user.id, member.id, value
                )
            except (BankingError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            remaining = sender_account.cash

        embed = discord.Embed(
            description=f"{interaction.user.mention} paid {member.mention} "
            f"**{format_money(value, style)}**",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Your cash: {format_money(remaining, style)}")
        # Public on purpose: the recipient should see it, and a visible transfer
        # log is part of how a server economy stays honest.
        await interaction.response.send_message(embed=embed)
        LOGGER.info(
            "pay guild=%s from=%s to=%s amount=%s",
            guild_id,
            interaction.user.id,
            member.id,
            value,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot))  # type: ignore[arg-type]
