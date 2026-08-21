"""Role income: `/income add|remove|list` for owners, `/collect` for members.

This is the first way currency enters an economy without an owner typing a
command. Rules attach a payout and a cooldown to a Discord role; members holding
that role collect on their own schedule.

Losing the role clears its cooldown (handled in `cogs.member_lifecycle`), so
regaining a role grants immediate eligibility rather than resuming a part-served
wait.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from shadow_bot.db import economy, income
from shadow_bot.db.models import GuildSettings
from shadow_bot.domain.amounts import MAX_AMOUNT, AmountError, CurrencyStyle, format_money
from shadow_bot.domain.amounts import parse_amount as parse
from shadow_bot.domain.authority import has_admin_permission, is_economy_admin
from shadow_bot.domain.durations import (
    DurationError,
    format_duration,
    parse_duration,
    relative_timestamp,
)

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

NOT_CONFIGURED = "This server has no economy yet. The server owner can create one with `/setup`."


class IncomeCog(commands.Cog):
    group = app_commands.Group(
        name="income",
        description="Administrator tools for role-based income",
        guild_only=True,
        # Group-wide, so `list` is administrator-visible too. Members see their
        # own earning roles and cooldowns through /collect, which is open to all.
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    async def _settings(self, interaction: discord.Interaction) -> GuildSettings | None:
        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(NOT_CONFIGURED, ephemeral=True)
        return settings

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return is_economy_admin(
            interaction.user.id,
            guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
            app_owner_ids=self.bot.settings.bot_owner_ids,
            has_administrator=has_admin_permission(interaction.user),
        )

    # --- Owner configuration --------------------------------------------------

    @group.command(name="add", description="Attach income to a role, or update it")
    @app_commands.describe(
        role="The role that earns",
        payout="How much each collection pays",
        cooldown="How often it may be collected, e.g. 12h, 1d, 30m",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        payout: str,
        cooldown: str,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to configure role income.", ephemeral=True
            )
            return
        settings = await self._settings(interaction)
        if settings is None:
            return

        if role.is_default():
            # @everyone would pay every member, which is almost never intended
            # and makes the income indistinguishable from a global drip.
            await interaction.response.send_message(
                "`@everyone` cannot earn income — every member holds it. "
                "Create a dedicated role instead.",
                ephemeral=True,
            )
            return
        if role.managed:
            await interaction.response.send_message(
                f"{role.mention} is managed by an integration and cannot be assigned "
                "to members normally.",
                ephemeral=True,
            )
            return

        try:
            amount = parse(payout, available=MAX_AMOUNT, what="pay")
        except AmountError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            seconds = parse_duration(cooldown)
        except DurationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            _, created = await income.upsert_rule(
                session,
                interaction.guild_id,
                role_id=role.id,
                payout=amount,
                cooldown_seconds=seconds,
            )

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(
            title="Role income added" if created else "Role income updated",
            description=f"{role.mention} earns **{format_money(amount, style)}** "
            f"every **{format_duration(seconds)}**.",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Members with this role can collect with /collect.")
        await interaction.response.send_message(embed=embed)
        LOGGER.info(
            "income_rule_set guild=%s role=%s payout=%s cooldown=%s created=%s",
            interaction.guild_id,
            role.id,
            amount,
            seconds,
            created,
        )

    @group.command(name="remove", description="Remove a role's income")
    @app_commands.describe(role="The role to stop paying")
    async def remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to configure role income.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            removed = await income.delete_rule(session, interaction.guild_id, role.id)

        if not removed:
            await interaction.response.send_message(
                f"{role.mention} has no income to remove.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"{role.mention} no longer earns income. Existing cooldowns for it were cleared."
        )
        LOGGER.info("income_rule_removed guild=%s role=%s", interaction.guild_id, role.id)

    @group.command(name="list", description="Show every role that earns income")
    async def list_income(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction)
        if settings is None:
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            rules = await income.list_rules(session, interaction.guild_id)

        if not rules:
            await interaction.response.send_message(
                "No roles earn income yet. The server owner can add one with `/income add`.",
                ephemeral=True,
            )
            return

        style = CurrencyStyle.from_settings(settings)
        lines = []
        for rule in rules:
            role = interaction.guild.get_role(rule.role_id) if interaction.guild else None
            # A rule can outlive its role if the role is deleted in Discord.
            # Show it rather than hiding it, so the owner knows to clean it up.
            name = role.mention if role else f"*deleted role {rule.role_id}*"
            status = "" if rule.enabled else " — disabled"
            lines.append(
                f"{name} — **{format_money(rule.payout, style)}** "
                f"every {format_duration(rule.cooldown_seconds)}{status}"
            )

        embed = discord.Embed(
            title="Role income",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Member collection ----------------------------------------------------

    @app_commands.command(name="collect", description="Collect income from your roles")
    @app_commands.guild_only()
    async def collect(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction)
        if settings is None:
            return
        if not settings.economy_enabled:
            await interaction.response.send_message(
                "The economy is currently disabled on this server.", ephemeral=True
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        role_ids = [role.id for role in member.roles]

        async with self.bot.database.sessions.begin() as session:
            plan = await income.collect(session, interaction.guild_id, member.id, role_ids)
            balance = (
                await economy.get_or_create_account(session, interaction.guild_id, member.id)
            ).cash

        style = CurrencyStyle.from_settings(settings)

        if not plan.collected and not plan.waiting:
            await interaction.response.send_message(
                "None of your roles earn income. Ask the server owner about `/income add`.",
                ephemeral=True,
            )
            return

        if not plan.collected:
            soonest = plan.next_available_at
            embed = discord.Embed(
                title="Nothing to collect yet",
                description=f"Your next income is ready {relative_timestamp(soonest)}."
                if soonest
                else "Nothing is ready yet.",
                color=discord.Color.orange(),
            )
        else:
            embed = discord.Embed(
                title=f"Collected {format_money(plan.total, style)}",
                color=discord.Color.green(),
            )
            embed.add_field(
                name="From",
                value="\n".join(
                    f"{self._role_name(interaction, item.role_id)} — "
                    f"{format_money(item.payout, style)}"
                    for item in plan.collected
                ),
                inline=False,
            )
            embed.set_footer(text=f"Your cash: {format_money(balance, style)}")

        if plan.waiting:
            embed.add_field(
                name="Still cooling down",
                value="\n".join(
                    f"{self._role_name(interaction, item.role_id)} — "
                    f"{format_money(item.payout, style)} {relative_timestamp(item.available_at)}"
                    for item in plan.waiting[:10]
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        if plan.collected:
            LOGGER.info(
                "income_collected guild=%s user=%s total=%s roles=%s",
                interaction.guild_id,
                member.id,
                plan.total,
                [item.role_id for item in plan.collected],
            )

    @staticmethod
    def _role_name(interaction: discord.Interaction, role_id: int) -> str:
        role = interaction.guild.get_role(role_id) if interaction.guild else None
        return role.mention if role else f"role {role_id}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(IncomeCog(bot))  # type: ignore[arg-type]
