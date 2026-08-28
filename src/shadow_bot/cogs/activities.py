"""Active income: `/work`, `/crime`, `/steal`, `/slut`, plus `/activity` config.

Every number is configured per guild by an administrator — cooldown, success
chance, reward range, fine range. Nothing is hardcoded, and an activity does
nothing until it has been set up, so a fresh server has no accidental economy.

Narration (M9) comes from the same place Hungry Games reads it: `bot.narration_defaults`
(bundled or `EVENTS_DIR`, see `domain.narration.load_event_library`), layered with this
guild's own lines via `cogs._narration.guild_library`. Until `work.md` / `crime.md` /
`steal.md` / `slut.md` have lines for a given outcome, `NarrationLibrary.pick` falls back
to the plain sentence this cog always used, so today's behaviour is unchanged.
"""

from __future__ import annotations

import logging
import random
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from shadow_bot.cogs._narration import guild_library
from shadow_bot.db import activities as activity_db
from shadow_bot.db import economy
from shadow_bot.db.models import GuildSettings
from shadow_bot.domain.activities import Activity, ActivityError, describe_chance
from shadow_bot.domain.amounts import MAX_AMOUNT, AmountError, CurrencyStyle, format_money
from shadow_bot.domain.amounts import parse_amount as parse
from shadow_bot.domain.authority import has_admin_permission, is_economy_admin
from shadow_bot.domain.durations import (
    DurationError,
    format_duration,
    parse_duration,
    relative_timestamp,
)
from shadow_bot.domain.narration import NarrationLibrary

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot


def _display_name(user: discord.Member | discord.User) -> str:
    """A guild nickname where there is one, the account name otherwise."""
    return getattr(user, "display_name", user.name)

LOGGER = logging.getLogger(__name__)

#: Not because anyone will realistically attack a Discord economy, but because
#: Mersenne Twister's state is reconstructible from its output and SystemRandom
#: costs nothing here.
_RNG = random.SystemRandom()

ACTIVITY_CHOICES = [
    app_commands.Choice(name=member.value, value=member.value) for member in Activity
]


def _parse_chance(raw: str) -> Decimal:
    """Read a success chance written as `65`, `65%`, or `0.65`.

    Values above 1 are read as percentages, which is how people write them.
    """
    text = raw.strip().rstrip("%").strip()
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ActivityError(f"`{raw.strip()}` is not a percentage I understand.") from exc
    if value > 1:
        value /= 100
    return value


class ActivitiesCog(commands.Cog):
    group = app_commands.Group(
        name="activity",
        description="Administrator tools for active income",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    async def _settings(self, interaction: discord.Interaction) -> GuildSettings | None:
        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "This server has no economy yet. An administrator can create one with `/setup`.",
                ephemeral=True,
            )
        return settings

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return is_economy_admin(
            interaction.user.id,
            guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
            app_owner_ids=self.bot.settings.bot_owner_ids,
            has_administrator=has_admin_permission(interaction.user),
        )

    # --- Configuration --------------------------------------------------------

    @group.command(name="set", description="Configure an activity's odds, rewards and fines")
    @app_commands.describe(
        activity="Which activity to configure",
        cooldown="How often it may be attempted, e.g. 1h, 30m, 1d",
        success_chance="Chance of success, e.g. 65 or 65% or 0.65",
        reward_min="Smallest payout on success",
        reward_max="Largest payout on success",
        fine_min="Smallest fine on failure",
        fine_max="Largest fine on failure",
    )
    @app_commands.choices(activity=ACTIVITY_CHOICES)
    async def set_activity(
        self,
        interaction: discord.Interaction,
        activity: app_commands.Choice[str],
        cooldown: str,
        success_chance: str,
        reward_min: str,
        reward_max: str,
        fine_min: str,
        fine_max: str,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to configure activities.", ephemeral=True
            )
            return
        settings = await self._settings(interaction)
        if settings is None:
            return

        key = Activity(activity.value)
        try:
            seconds = parse_duration(cooldown)
            chance = _parse_chance(success_chance)
            numbers = {
                name: parse(value, available=MAX_AMOUNT, what="set")
                for name, value in (
                    ("reward_min", reward_min),
                    ("reward_max", reward_max),
                    ("fine_min", fine_min),
                    ("fine_max", fine_max),
                )
            }
        except (DurationError, AmountError, ActivityError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        assert interaction.guild_id is not None
        try:
            async with self.bot.database.sessions.begin() as session:
                _, created = await activity_db.upsert_rule(
                    session,
                    interaction.guild_id,
                    key,
                    cooldown_seconds=seconds,
                    success_chance=chance,
                    success_min=numbers["reward_min"],
                    success_max=numbers["reward_max"],
                    fine_min=numbers["fine_min"],
                    fine_max=numbers["fine_max"],
                )
        except ActivityError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(
            title=f"`/{key.value}` {'configured' if created else 'updated'}",
            color=discord.Color.green(),
        )
        embed.add_field(name="Success chance", value=describe_chance(chance), inline=True)
        embed.add_field(name="Cooldown", value=format_duration(seconds), inline=True)
        embed.add_field(
            name="Reward",
            value=f"{format_money(numbers['reward_min'], style)} – "
            f"{format_money(numbers['reward_max'], style)}",
            inline=False,
        )
        embed.add_field(
            name="Fine on failure",
            value=f"{format_money(numbers['fine_min'], style)} – "
            f"{format_money(numbers['fine_max'], style)}",
            inline=False,
        )
        if key.targets_a_member:
            embed.set_footer(
                text="Steal is capped at the target's cash — you cannot take what is not there."
            )
        await interaction.response.send_message(embed=embed)
        LOGGER.info(
            "activity_configured guild=%s activity=%s chance=%s cooldown=%s",
            interaction.guild_id,
            key.value,
            chance,
            seconds,
        )

    @group.command(name="enable", description="Turn an activity on")
    @app_commands.choices(activity=ACTIVITY_CHOICES)
    async def enable(
        self, interaction: discord.Interaction, activity: app_commands.Choice[str]
    ) -> None:
        await self._toggle(interaction, Activity(activity.value), True)

    @group.command(name="disable", description="Turn an activity off")
    @app_commands.choices(activity=ACTIVITY_CHOICES)
    async def disable(
        self, interaction: discord.Interaction, activity: app_commands.Choice[str]
    ) -> None:
        await self._toggle(interaction, Activity(activity.value), False)

    async def _toggle(
        self, interaction: discord.Interaction, activity: Activity, enabled: bool
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to configure activities.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            existed = await activity_db.set_enabled(
                session, interaction.guild_id, activity, enabled
            )

        if not existed:
            await interaction.response.send_message(
                f"`/{activity.value}` has not been configured yet — use `/activity set` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"`/{activity.value}` is now **{'enabled' if enabled else 'disabled'}**."
        )

    @group.command(name="list", description="Show how every activity is configured")
    async def list_activities(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction)
        if settings is None:
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            rules = await activity_db.list_rules(session, interaction.guild_id)

        style = CurrencyStyle.from_settings(settings)
        configured = {rule.activity_key: rule for rule in rules}

        embed = discord.Embed(title="Activities", color=discord.Color.blurple())
        for key in Activity:
            rule = configured.get(key.value)
            if rule is None:
                # Show unconfigured activities too, so it is obvious what exists
                # and what is simply not set up yet.
                embed.add_field(name=f"/{key.value}", value="*not configured*", inline=False)
                continue
            state = "enabled" if rule.enabled else "**disabled**"
            embed.add_field(
                name=f"/{key.value} — {state}",
                value=(
                    f"{describe_chance(Decimal(rule.success_chance))} success, "
                    f"every {format_duration(rule.cooldown_seconds)}\n"
                    f"Reward {format_money(rule.success_min, style)} – "
                    f"{format_money(rule.success_max, style)}\n"
                    f"Fine {format_money(rule.fine_min, style)} – "
                    f"{format_money(rule.fine_max, style)}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Member commands ------------------------------------------------------

    @app_commands.command(name="work", description="Work for an honest wage")
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction) -> None:
        await self._attempt(interaction, Activity.WORK)

    @app_commands.command(name="crime", description="Attempt a crime. Riskier, and fined if caught")
    @app_commands.guild_only()
    async def crime(self, interaction: discord.Interaction) -> None:
        await self._attempt(interaction, Activity.CRIME)

    @app_commands.command(name="slut", description="Earn on the side. Riskier, and fined if caught")
    @app_commands.guild_only()
    async def slut(self, interaction: discord.Interaction) -> None:
        await self._attempt(interaction, Activity.SLUT)

    @app_commands.command(name="steal", description="Steal cash from another member")
    @app_commands.describe(member="Who to steal from")
    @app_commands.guild_only()
    async def steal(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if member.bot:
            await interaction.response.send_message("Bots do not hold accounts.", ephemeral=True)
            return
        await self._attempt(interaction, Activity.STEAL, target=member)

    async def _attempt(
        self,
        interaction: discord.Interaction,
        activity: Activity,
        *,
        target: discord.Member | None = None,
    ) -> None:
        settings = await self._settings(interaction)
        if settings is None:
            return
        if not settings.economy_enabled:
            await interaction.response.send_message(
                "The economy is currently disabled on this server.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        style = CurrencyStyle.from_settings(settings)

        try:
            async with self.bot.database.sessions.begin() as session:
                result = await activity_db.attempt(
                    session,
                    interaction.guild_id,
                    interaction.user.id,
                    activity,
                    chance_roll=Decimal(str(_RNG.random())),
                    amount_roll=_RNG.random(),
                    cash_floor=settings.cash_floor,
                    bank_floor=settings.bank_floor,
                    target_id=target.id if target else None,
                )
        except activity_db.OnCooldown as exc:
            await interaction.response.send_message(
                f"`/{activity.value}` is not ready yet — try again "
                f"{relative_timestamp(exc.available_at)}.",
                ephemeral=True,
            )
            return
        except ActivityError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        library = await guild_library(self.bot, interaction.guild_id)
        embed = self._render(activity, result, style, target, interaction.user, library)
        embed.set_footer(
            text=f"Cash {format_money(result.cash, style)} · "
            f"Bank {format_money(result.bank, style)}"
        )
        await interaction.response.send_message(embed=embed)
        LOGGER.info(
            "activity guild=%s user=%s activity=%s success=%s amount=%s collected=%s",
            interaction.guild_id,
            interaction.user.id,
            activity.value,
            result.outcome.succeeded,
            result.outcome.amount,
            result.collected,
        )

    @staticmethod
    def _render(
        activity: Activity,
        result: activity_db.AttemptResult,
        style: CurrencyStyle,
        target: discord.Member | None,
        actor: discord.Member | discord.User,
        library: NarrationLibrary,
    ) -> discord.Embed:
        """Build the result embed, narrated where a line is configured.

        `library.pick` is what does the work here: it renders a random
        configured line when one exists for `(activity, outcome)`, and
        otherwise renders `fallback` — the exact sentence this cog always
        used — so behaviour is unchanged until `work.md` / `crime.md` /
        `steal.md` / `slut.md` actually have lines for a given outcome.
        """
        outcome = result.outcome
        category = activity.value
        values = {"user": _display_name(actor), "currency": style.plural}
        if target is not None:
            values["target"] = _display_name(target)

        if outcome.succeeded and outcome.target_was_empty:
            fallback = (
                f"You got into {target.mention}'s pockets and found them empty."
                if target
                else "There was nothing to take."
            )
            description = library.pick(category, "empty", values, fallback=fallback)
            return discord.Embed(
                title="Nothing to take",
                description=description,
                color=discord.Color.greyple(),
            )

        if outcome.succeeded:
            values["amount"] = format_money(outcome.amount, style)
            fallback = (
                f"You took **{format_money(outcome.amount, style)}** from {target.mention}."
                if target
                else f"You earned **{format_money(outcome.amount, style)}**."
            )
            description = library.pick(category, "success", values, fallback=fallback)
            if outcome.capped:
                description += "\nThat was everything they had."
            return discord.Embed(
                title=f"{activity.value.capitalize()} paid off",
                description=description,
                color=discord.Color.green(),
            )

        values["amount"] = format_money(result.collected, style)
        fallback = f"You were fined **{format_money(result.collected, style)}**."
        description = library.pick(category, "failure", values, fallback=fallback)
        embed = discord.Embed(
            title="Caught",
            description=description,
            color=discord.Color.red(),
        )
        if result.uncollected:
            # Say so rather than letting the number quietly not match the fine.
            embed.add_field(
                name="Could not collect",
                value=f"{format_money(result.uncollected, style)} — you hit your balance floor.",
                inline=False,
            )
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ActivitiesCog(bot))  # type: ignore[arg-type]
