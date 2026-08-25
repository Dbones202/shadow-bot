"""The Hungry Games: `/hungrygames start|join|status|cancel`.

Rounds are paced over real time by a background loop rather than resolved in
one message, because watching it unfold is the point. That means state lives in
the database — a restart mid-game resumes from where it left off instead of
stranding everyone's entry fees.

Narration comes from `domain.narration`: bundled defaults, overridden per guild
by rows in `flavor_texts`. The logic in `domain.games` never sees a word of it.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from importlib.resources import files
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from shadow_bot.db import economy
from shadow_bot.db import games as game_db
from shadow_bot.db.models import FlavorText, Game, GuildSettings
from shadow_bot.domain.amounts import MAX_AMOUNT, AmountError, CurrencyStyle, format_money
from shadow_bot.domain.amounts import parse_amount as parse
from shadow_bot.domain.authority import has_admin_permission, is_economy_admin
from shadow_bot.domain.banking import BankingError
from shadow_bot.domain.durations import DurationError, parse_duration, relative_timestamp
from shadow_bot.domain.games import EventKind, RoundPlan
from shadow_bot.domain.narration import NarrationError, NarrationLibrary
from shadow_bot.domain.narration import parse as parse_narration

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

_RNG = random.SystemRandom()

#: How often the loop looks for work. Rounds are scheduled in the database, so
#: this only bounds how late a round can be, not how fast the game runs.
TICK_SECONDS = 5
#: Gap between rounds once a game is running.
ROUND_SECONDS = 15

CATEGORY = "hungrygames"


def _load_defaults() -> dict[tuple[str, str], list[str]]:
    """Read the bundled narration. A broken file must not stop the bot booting."""
    try:
        text = (files("shadow_bot") / "data" / "narration" / "default.txt").read_text(
            encoding="utf-8"
        )
        return parse_narration(text)
    except (OSError, NarrationError):
        LOGGER.exception("Could not load bundled narration; falling back to plain messages")
        return {}


DEFAULT_NARRATION = _load_defaults()


class GamesCog(commands.Cog):
    group = app_commands.Group(
        name="hungrygames",
        description="Run a Hungry Games event",
        guild_only=True,
        # Deliberately not gated to administrators. Discord only honours
        # default_member_permissions on *top-level* commands — subcommands
        # inherit from the group — and `join`/`status` must stay open to
        # everyone. `start` and `cancel` therefore enforce authority in code
        # instead; a member can see them but cannot run them.
    )

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # --- Narration ------------------------------------------------------------

    async def _library(self, guild_id: int) -> NarrationLibrary:
        """Bundled defaults, with this guild's own lines taking precedence."""
        overrides: dict[tuple[str, str], list[str]] = {}
        async with self.bot.database.sessions() as session:
            rows = (
                (await session.execute(select(FlavorText).where(FlavorText.guild_id == guild_id)))
                .scalars()
                .all()
            )
        for row in rows:
            overrides.setdefault((row.activity_key.lower(), row.outcome.lower()), []).append(
                row.text
            )
        return NarrationLibrary(defaults=DEFAULT_NARRATION, overrides=overrides)

    def _name(self, guild: discord.Guild | None, user_id: int) -> str:
        member = guild.get_member(user_id) if guild else None
        return member.display_name if member else f"<@{user_id}>"

    def _narrate(
        self, plan: RoundPlan, library: NarrationLibrary, guild: discord.Guild | None
    ) -> list[str]:
        lines: list[str] = []
        for event in plan.events:
            values = {"tribute": self._name(guild, event.subject.user_id)}
            if event.victim is not None:
                values["victim"] = self._name(guild, event.victim.user_id)

            if event.kind is EventKind.KILL:
                lines.append(
                    library.pick(
                        CATEGORY, "kill", values, fallback="{tribute} eliminates {victim}."
                    )
                )
                forfeit = library.pick(CATEGORY, "forfeit_kill", values)
                if forfeit:
                    lines.append(f"  ↳ *{forfeit}*")
            elif event.kind is EventKind.DEATH:
                lines.append(
                    library.pick(CATEGORY, "death", values, fallback="{tribute} is eliminated.")
                )
                forfeit = library.pick(CATEGORY, "forfeit_death", values)
                if forfeit:
                    lines.append(f"  ↳ *{forfeit}*")
            else:
                lines.append(
                    library.pick(CATEGORY, "survive", values, fallback="{tribute} survives.")
                )
        return lines

    # --- Commands -------------------------------------------------------------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        # Single place to widen later: capability grants would plug in here, and
        # `economy_role_permissions.per_action_limit` is already the right shape
        # for bounding how large a seed a delegated role may set.
        return is_economy_admin(
            interaction.user.id,
            guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
            app_owner_ids=self.bot.settings.bot_owner_ids,
            has_administrator=has_admin_permission(interaction.user),
        )

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

    @group.command(name="start", description="Open signups for a Hungry Games event")
    @app_commands.describe(
        entry_fee="Cost to enter. Entry fees fund the pot and create no new currency.",
        signup="How long signups stay open, e.g. 2m, 30s",
        seed="Optional prize added on top. This creates new currency.",
        min_players="Fewest tributes needed, or the game is cancelled and fees refunded.",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        entry_fee: str = "0",
        signup: str = "2m",
        seed: str = "0",
        min_players: int = 2,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to start a game.", ephemeral=True
            )
            return
        settings = await self._settings(interaction)
        if settings is None:
            return

        try:
            fee = (
                0
                if entry_fee.strip() in {"0", ""}
                else parse(entry_fee, available=MAX_AMOUNT, what="charge")
            )
            seeded = (
                0 if seed.strip() in {"0", ""} else parse(seed, available=MAX_AMOUNT, what="seed")
            )
            window = parse_duration(signup)
        except (AmountError, DurationError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        assert interaction.guild_id is not None
        try:
            async with self.bot.database.sessions.begin() as session:
                game = await game_db.create_game(
                    session,
                    interaction.guild_id,
                    channel_id=interaction.channel_id or 0,
                    created_by=interaction.user.id,
                    entry_fee=fee,
                    seeded_pot=seeded,
                    min_players=min_players,
                    signup_seconds=window,
                )
                closes = game.signup_closes_at
        except game_db.GameError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(
            title="The Hungry Games",
            description="Signups are open. Enter with `/hungrygames join`.",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name="Entry fee",
            value=format_money(fee, style) if fee else "Free",
            inline=True,
        )
        if seeded:
            embed.add_field(name="Starting pot", value=format_money(seeded, style), inline=True)
        embed.add_field(name="Closes", value=relative_timestamp(closes), inline=True)
        embed.set_footer(text=f"At least {min_players} tributes, or fees are refunded.")
        await interaction.response.send_message(embed=embed)
        LOGGER.info(
            "game_created guild=%s by=%s fee=%s seed=%s",
            interaction.guild_id,
            interaction.user.id,
            fee,
            seeded,
        )

    @group.command(name="join", description="Enter the current game")
    async def join(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction)
        if settings is None:
            return

        assert interaction.guild_id is not None
        try:
            async with self.bot.database.sessions.begin() as session:
                game = await game_db.active_game(session, interaction.guild_id)
                if game is None or game.status != "signup":
                    await interaction.response.send_message(
                        "No game is open for signups right now.", ephemeral=True
                    )
                    return
                await game_db.join_game(session, game, interaction.user.id)
                fee = game.entry_fee
                total = len(await game_db.participants(session, game.id))
        except (game_db.GameError, BankingError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        style = CurrencyStyle.from_settings(settings)
        paid = f" for {format_money(fee, style)}" if fee else ""
        await interaction.response.send_message(
            f"{interaction.user.mention} enters the arena{paid}. **{total}** so far."
        )

    @group.command(name="status", description="Show the current game")
    async def status(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction)
        if settings is None:
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            game = await game_db.active_game(session, interaction.guild_id)
            if game is None:
                await interaction.response.send_message(
                    "No game is running. An administrator can start one with `/hungrygames start`.",
                    ephemeral=True,
                )
                return
            entrants = await game_db.participants(session, game.id)
            pot = await game_db.pot_for(session, game)

        style = CurrencyStyle.from_settings(settings)
        alive = [p for p in entrants if p.alive]
        embed = discord.Embed(title="The Hungry Games", color=discord.Color.dark_gold())
        embed.add_field(name="Status", value=game.status, inline=True)
        embed.add_field(name="Pot", value=format_money(pot, style), inline=True)
        if game.status == "signup":
            embed.add_field(
                name="Closes", value=relative_timestamp(game.signup_closes_at), inline=True
            )
            embed.add_field(name="Entered", value=str(len(entrants)), inline=False)
        else:
            embed.add_field(name="Round", value=str(game.round_number), inline=True)
            embed.add_field(
                name=f"Still standing ({len(alive)})",
                value=", ".join(self._name(interaction.guild, p.user_id) for p in alive[:30])
                or "nobody",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="cancel", description="Cancel the current game and refund entry fees")
    async def cancel(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to cancel a game.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            game = await game_db.active_game(session, interaction.guild_id)
            if game is None:
                await interaction.response.send_message("No game is running.", ephemeral=True)
                return
            refunded = await game_db.cancel_game(session, game, reason="cancelled_by_admin")

        await interaction.response.send_message(
            f"Game cancelled. {len(refunded)} entry fee(s) refunded."
        )

    # --- Background pacing ----------------------------------------------------

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self) -> None:
        """Close signups and run rounds that have come due.

        Each game is handled in its own transaction with `FOR UPDATE SKIP LOCKED`,
        so a slow channel post cannot hold up other guilds' games.
        """
        try:
            async with self.bot.database.sessions() as session:
                due = await game_db.due_games(session)
                ids = [g.id for g in due]
        except Exception:
            LOGGER.exception("Could not look for due games")
            return

        for game_id in ids:
            try:
                await self._advance(game_id)
            except Exception:
                LOGGER.exception("Game %s failed to advance", game_id)

    @tick.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _advance(self, game_id) -> None:
        now = datetime.now(UTC)
        async with self.bot.database.sessions.begin() as session:
            game = await session.get(Game, game_id, with_for_update=True)
            if game is None:
                return
            guild_id, channel_id, status = game.guild_id, game.channel_id, game.status

            if status == "signup":
                started = await game_db.close_signups(
                    session, game, round_seconds=ROUND_SECONDS, now=now
                )
                entrants = len(await game_db.participants(session, game.id))
                payload = ("started" if started else "cancelled", entrants, None)
            elif status == "running":
                outcome = await game_db.run_round(
                    session, game, rng=_RNG, round_seconds=ROUND_SECONDS, now=now
                )
                payload = ("round", 0, outcome)
            else:
                return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            LOGGER.warning("Game %s has no reachable channel %s", game_id, channel_id)
            return

        guild = getattr(channel, "guild", None)
        library = await self._library(guild_id)
        kind, entrants, outcome = payload

        if kind == "cancelled":
            await channel.send(
                f"The Hungry Games were called off — only {entrants} tribute(s) entered. "
                "Entry fees have been refunded."
            )
            return
        if kind == "started":
            await channel.send(f"**The Hungry Games begin.** {entrants} tributes enter the arena.")
            return

        assert outcome is not None
        lines = self._narrate(outcome.plan, library, guild)
        embed = discord.Embed(
            title=f"Round {outcome.round_number}",
            description="\n".join(lines) or "Nothing happens.",
            color=discord.Color.dark_red(),
        )
        if not outcome.finished:
            embed.set_footer(text=f"{len(outcome.plan.survivors)} still standing.")
            await channel.send(embed=embed)
            return

        await channel.send(embed=embed)

        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, guild_id)
        style = CurrencyStyle.from_settings(settings) if settings else None
        pot_text = format_money(outcome.pot, style) if style else str(outcome.pot)

        values = {
            "winner": self._name(guild, outcome.winner_user_id)
            if outcome.winner_user_id
            else "nobody",
            "pot": pot_text,
        }
        final = discord.Embed(
            title="The Hungry Games are over",
            description=library.pick(CATEGORY, "winner", values, fallback="{winner} wins {pot}."),
            color=discord.Color.gold(),
        )
        reward = library.pick(CATEGORY, "reward_winner", values)
        if reward:
            final.add_field(name="Spoils", value=reward, inline=False)
        await channel.send(embed=final)
        LOGGER.info(
            "game_complete guild=%s winner=%s pot=%s rounds=%s",
            guild_id,
            outcome.winner_user_id,
            outcome.pot,
            outcome.round_number,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GamesCog(bot))  # type: ignore[arg-type]
