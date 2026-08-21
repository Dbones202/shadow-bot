"""Guild configuration: the `/setup` wizard and `/settings`.

Nothing else in the economy can run until a guild has a `guild_settings` row,
because `economy_accounts` carries a foreign key to it. This cog is therefore
the entry point for every new server.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from shadow_bot.db.economy import get_settings
from shadow_bot.db.models import GuildSettings
from shadow_bot.domain.amounts import CurrencyStyle, format_money
from shadow_bot.domain.authority import has_admin_permission, is_economy_admin
from shadow_bot.domain.validation import (
    SettingError,
    validate_currency_name,
    validate_currency_symbol,
    validate_timezone,
)

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    "currency_name": "coin",
    "currency_name_plural": "coins",
    "currency_symbol": "🪙",
    "timezone": "UTC",
}


class SetupModal(discord.ui.Modal, title="Economy setup"):
    """Collects the four things a guild must decide before money exists."""

    currency_name: discord.ui.TextInput = discord.ui.TextInput(
        label="Currency name (singular)",
        placeholder="coin",
        max_length=50,
    )
    currency_name_plural: discord.ui.TextInput = discord.ui.TextInput(
        label="Currency name (plural)",
        placeholder="coins",
        max_length=50,
    )
    currency_symbol: discord.ui.TextInput = discord.ui.TextInput(
        label="Symbol or emoji",
        placeholder="🪙  or  <:gold:123456789012345678>",
        max_length=100,
    )
    timezone: discord.ui.TextInput = discord.ui.TextInput(
        label="Timezone (IANA name)",
        placeholder="America/Denver",
        max_length=64,
    )

    def __init__(self, cog: EconomySetupCog, existing: GuildSettings | None) -> None:
        super().__init__()
        source = existing or DEFAULTS
        self.cog = cog
        self.currency_name.default = _value(source, "currency_name")
        self.currency_name_plural.default = _value(source, "currency_name_plural")
        self.currency_symbol.default = _value(source, "currency_symbol")
        self.timezone.default = _value(source, "timezone")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Validate everything before reporting, so the owner sees every problem
        # at once rather than rediscovering the form one field at a time.
        errors: list[str] = []
        cleaned: dict[str, str] = {}

        for field, validator, label in (
            ("currency_name", validate_currency_name, "Currency name"),
            ("currency_name_plural", validate_currency_name, "Plural name"),
            ("currency_symbol", validate_currency_symbol, "Symbol"),
            ("timezone", validate_timezone, "Timezone"),
        ):
            raw = getattr(self, field).value
            try:
                if validator is validate_currency_name:
                    cleaned[field] = validator(raw, field=label)
                else:
                    cleaned[field] = validator(raw)
            except SettingError as exc:
                errors.append(f"**{label}** — {exc}")

        if errors:
            await interaction.response.send_message(
                "I could not save those settings:\n" + "\n".join(f"• {e}" for e in errors),
                ephemeral=True,
            )
            return

        assert interaction.guild_id is not None
        await self.cog.save(interaction.guild_id, cleaned)

        style = CurrencyStyle(
            symbol=cleaned["currency_symbol"],
            singular=cleaned["currency_name"],
            plural=cleaned["currency_name_plural"],
        )
        embed = discord.Embed(
            title="Economy configured",
            description=f"New members start with {format_money(0, style)}.",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Currency",
            value=f"{cleaned['currency_symbol']} "
            f"{cleaned['currency_name']} / {cleaned['currency_name_plural']}",
            inline=False,
        )
        embed.add_field(name="Timezone", value=cleaned["timezone"], inline=False)
        embed.set_footer(text="Members can now use /balance, /deposit, /withdraw and /pay.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        LOGGER.info("Guild %s configured its economy", interaction.guild_id)


def _value(source: GuildSettings | dict[str, str], field: str) -> str:
    if isinstance(source, dict):
        return source[field]
    return str(getattr(source, field))


class EconomySetupCog(commands.Cog):
    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    async def save(self, guild_id: int, values: dict[str, str]) -> None:
        async with self.bot.database.sessions.begin() as session:
            settings = await session.get(GuildSettings, guild_id)
            if settings is None:
                settings = GuildSettings(guild_id=guild_id)
                session.add(settings)
            for field, value in values.items():
                setattr(settings, field, value)
            settings.economy_enabled = True

    @app_commands.command(name="setup", description="Configure this server's economy")
    @app_commands.guild_only()
    # Visible to administrators by default. This controls who Discord *shows*
    # the command to; the is_economy_admin check below is the actual gate.
    @app_commands.default_permissions(administrator=True)
    async def setup_command(self, interaction: discord.Interaction) -> None:
        if not is_economy_admin(
            interaction.user.id,
            guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
            app_owner_ids=self.bot.settings.bot_owner_ids,
            has_administrator=has_admin_permission(interaction.user),
        ):
            await interaction.response.send_message(
                "You need Administrator permission to configure the economy.",
                ephemeral=True,
            )
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            existing = await get_settings(session, interaction.guild_id)

        await interaction.response.send_modal(SetupModal(self, existing))

    @app_commands.command(name="settings", description="Show this server's economy settings")
    @app_commands.guild_only()
    async def settings_command(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            settings = await get_settings(session, interaction.guild_id)

        if settings is None:
            await interaction.response.send_message(
                "This server has no economy yet. The server owner can create one with `/setup`.",
                ephemeral=True,
            )
            return

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(title="Economy settings", color=discord.Color.blurple())
        embed.add_field(
            name="Currency",
            value=f"{settings.currency_symbol} "
            f"{settings.currency_name} / {settings.currency_name_plural}",
            inline=False,
        )
        embed.add_field(name="Timezone", value=settings.timezone, inline=False)
        embed.add_field(
            name="Balance floors",
            value=f"Cash {format_money(settings.cash_floor, style)}\n"
            f"Bank {format_money(settings.bank_floor, style)}",
            inline=False,
        )
        embed.add_field(
            name="Status",
            value="Enabled" if settings.economy_enabled else "Disabled",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomySetupCog(bot))  # type: ignore[arg-type]
