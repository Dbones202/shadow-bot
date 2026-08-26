"""The Hungry Games: `/hungrygames start|join|status|cancel`.

Rounds are paced over real time by a background loop rather than resolved in
one message, because watching it unfold is the point. That means state lives in
the database — a restart mid-game resumes from where it left off instead of
stranding everyone's entry fees.

Narration comes from `domain.narration`: bundled defaults, overridden per guild
by rows in `flavor_texts`. The logic in `domain.games` never sees a word of it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from shadow_bot.db import economy, game_stats
from shadow_bot.db import games as game_db
from shadow_bot.db.models import FlavorText, Game, GuildSettings
from shadow_bot.domain import cards
from shadow_bot.domain.amounts import MAX_AMOUNT, AmountError, CurrencyStyle, format_money
from shadow_bot.domain.amounts import parse_amount as parse
from shadow_bot.domain.authority import has_admin_permission, is_economy_admin
from shadow_bot.domain.banking import BankingError
from shadow_bot.domain.durations import (
    DurationError,
    format_duration,
    parse_duration,
    relative_timestamp,
)
from shadow_bot.domain.games import EventKind, RoundPlan
from shadow_bot.domain.narration import (
    NarrationError,
    NarrationLibrary,
    NarrationSession,
    render,
)
from shadow_bot.domain.narration import parse as parse_narration

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

_RNG = random.SystemRandom()

#: How often the loop looks for work. Rounds are scheduled in the database, so
#: this only bounds how late a round can be, not how fast the game runs.
TICK_SECONDS = 5
#: Fallback gap between rounds when a guild has no setting yet. The real value
#: lives on the game row, copied from guild settings when the game is created.
ROUND_SECONDS = 15

CATEGORY = "hungrygames"

#: Bounds on the signup window. Long enough to gather people, short enough
#: that a forgotten game does not sit open for a day.
MIN_SIGNUP_SECONDS = 30
MAX_SIGNUP_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class StyleRules:
    """What a game's chosen style means for the text, as plain values.

    Snapshotted out of the ORM before the session closes.
    """

    style: str
    winner: str | None
    killed_by: str | None
    killed_self: str | None


@dataclass(frozen=True, slots=True)
class StartParams:
    """Everything /hungrygames start settled before the game row exists."""

    entry_fee: int
    seeded_pot: int
    min_players: int
    signup_seconds: int
    round_seconds: int
    style: str


class OutcomesModal(discord.ui.Modal, title="Set the outcomes"):
    """Prompts for the three consequences an organizer-defined game promises.

    Discord allows five inputs per modal and these are three, so it fits without
    splitting across steps. Whatever is typed here goes straight into the signup
    embed, so nobody enters without having read what they are agreeing to.
    """

    killed_by = discord.ui.TextInput(
        label="If you are killed by another tribute",
        placeholder="e.g. You owe your killer a favour of their choosing.",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )
    killed_self = discord.ui.TextInput(
        label="If the arena gets you",
        placeholder="e.g. Post your most embarrassing photo.",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )
    winner = discord.ui.TextInput(
        label="If you win",
        placeholder="e.g. Pick what happens at the next game night.",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, cog: GamesCog, params: StartParams, settings: GuildSettings) -> None:
        super().__init__()
        self._cog = cog
        self._params = params
        self._settings = settings

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog._open_signups(
            interaction,
            self._params,
            self._settings,
            outcomes=(str(self.winner), str(self.killed_by), str(self.killed_self)),
        )


class JoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hg:join:(?P<game_id>[0-9a-fA-F-]{36})",
):
    """The Enter button on a signup message.

    A `DynamicItem` rather than a plain view because the button has to keep
    working after a restart. The game id lives in the `custom_id` and is parsed
    back out on click, so nothing has to be held in memory between restarts and
    no view registry has to be rebuilt at boot — the same reasoning that put
    game state in the database in the first place.
    """

    def __init__(self, game_id: uuid.UUID) -> None:
        super().__init__(
            discord.ui.Button(
                label="Enter the arena",
                style=discord.ButtonStyle.success,
                emoji="\N{CROSSED SWORDS}",
                custom_id=f"hg:join:{game_id}",
            )
        )
        self.game_id = game_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(uuid.UUID(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("GamesCog")
        if cog is None:  # cog unloaded mid-game; fail politely rather than 500
            await interaction.response.send_message(
                "Games are unavailable right now.", ephemeral=True
            )
            return
        await cog.handle_join(interaction, self.game_id)


class SignupView(discord.ui.View):
    """Holds the join button. No timeout: signups are ended by the game clock,
    not by Discord expiring the view."""

    def __init__(self, game_id: uuid.UUID) -> None:
        super().__init__(timeout=None)
        self.add_item(JoinButton(game_id))


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
        #: One narration session per guild, alive for the length of one game.
        self._narrators: dict[int, NarrationSession] = {}

    async def cog_load(self) -> None:
        # Registers the button template so clicks on messages posted before this
        # restart still resolve. Without it an old signup message's button
        # silently does nothing.
        self.bot.add_dynamic_items(JoinButton)
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

    def _forfeit(self, rules: StyleRules, key: str, values: dict[str, str], narrator) -> str:
        """The consequence line attached to an elimination, if the style has one.

        * `standard` — none at all. Narration and pot, nothing owed afterwards.
        * `random_tasks` — drawn from the event files, different every time.
        * `organizer_defined` — the fixed text the organizer typed at start,
          which everyone already read in the signup embed before joining.
        """
        if rules.style == "standard":
            return ""
        if rules.style == "organizer_defined":
            template = rules.killed_by if key == "forfeit_kill" else rules.killed_self
            return render(template, values) if template else ""
        return narrator.pick(CATEGORY, key, values)

    def _narrate(
        self,
        plan: RoundPlan,
        narrator: NarrationSession,
        guild: discord.Guild | None,
        rules: StyleRules,
        *,
        hidden: bool = False,
    ) -> list[str]:
        """Turn a round plan into the lines that get posted.

        `hidden` wraps names in Discord spoiler bars so the round has to be
        clicked to be read. It fires rarely and at random — see `_advance`.
        """

        def name(user_id: int) -> str:
            shown = self._name(guild, user_id)
            return f"||{shown}||" if hidden else shown

        lines: list[str] = []
        for event in plan.events:
            values = {"tribute": name(event.subject.user_id)}
            if event.victim is not None:
                values["victim"] = name(event.victim.user_id)
            # {killer} reads better than {tribute} in a kill line, and both are
            # accepted so older event files keep working.
            values["killer"] = values["tribute"]

            if event.kind is EventKind.KILL:
                lines.append(
                    narrator.pick(
                        CATEGORY, "kill", values, fallback="{tribute} eliminates {victim}."
                    )
                )
                forfeit = self._forfeit(rules, "forfeit_kill", values, narrator)
                if forfeit:
                    lines.append(f"  ↳ *{forfeit}*")
            elif event.kind is EventKind.DEATH:
                lines.append(
                    narrator.pick(CATEGORY, "death", values, fallback="{tribute} is eliminated.")
                )
                forfeit = self._forfeit(rules, "forfeit_death", values, narrator)
                if forfeit:
                    lines.append(f"  ↳ *{forfeit}*")
            else:
                lines.append(
                    narrator.pick(CATEGORY, "survive", values, fallback="{tribute} survives.")
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

    @group.command(name="start", description="Open signups for a game")
    @app_commands.describe(
        entry_fee="Cost to enter. Entry fees fund the pot and create no new currency.",
        signup="How long signups stay open, e.g. 5m, 30s. Defaults to the server setting.",
        seed="Optional prize added on top. This creates new currency.",
        min_players="Fewest tributes needed, or the game is cancelled and fees refunded.",
        style="How eliminations are handled. Defaults to the server setting.",
        round_time="Gap between rounds, e.g. 15s, 1m. Defaults to the server setting.",
    )
    @app_commands.choices(
        style=[
            app_commands.Choice(name="Standard - narration and pot only", value="standard"),
            app_commands.Choice(
                name="Random tasks - forfeits drawn at random", value="random_tasks"
            ),
            app_commands.Choice(
                name="Organizer defined - you set the outcomes", value="organizer_defined"
            ),
        ]
    )
    async def start(
        self,
        interaction: discord.Interaction,
        entry_fee: str = "0",
        signup: str | None = None,
        seed: str = "0",
        min_players: int = game_db.MIN_PLAYERS_FLOOR,
        style: app_commands.Choice[str] | None = None,
        round_time: str | None = None,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "You need Administrator permission to start a game.", ephemeral=True
            )
            return
        settings = await self._settings(interaction)
        if settings is None:
            return

        chosen_style = style.value if style else settings.game_default_style
        try:
            fee = (
                0
                if entry_fee.strip() in {"0", ""}
                else parse(entry_fee, available=MAX_AMOUNT, what="charge")
            )
            seeded = (
                0 if seed.strip() in {"0", ""} else parse(seed, available=MAX_AMOUNT, what="seed")
            )
            window = parse_duration(signup) if signup else settings.game_signup_seconds
            gap = parse_duration(round_time) if round_time else settings.game_round_seconds
        except (AmountError, DurationError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        params = StartParams(
            entry_fee=fee,
            seeded_pot=seeded,
            min_players=min_players,
            signup_seconds=window,
            round_seconds=gap,
            style=chosen_style,
        )

        # An organizer-defined game needs three pieces of text, and a modal is
        # both the natural way to prompt and the only way to get multi-line
        # boxes. Sending the modal *is* the interaction response, so the game is
        # created from the modal's submit rather than here.
        if chosen_style == "organizer_defined":
            await interaction.response.send_modal(OutcomesModal(self, params, settings))
            return

        await self._open_signups(interaction, params, settings)

    async def _open_signups(
        self,
        interaction: discord.Interaction,
        params: StartParams,
        settings: GuildSettings,
        outcomes: tuple[str, str, str] | None = None,
    ) -> None:
        """Create the game and post the signup embed. Shared by both start paths."""
        assert interaction.guild_id is not None
        winner, killed_by, killed_self = outcomes or (None, None, None)
        try:
            async with self.bot.database.sessions.begin() as session:
                game = await game_db.create_game(
                    session,
                    interaction.guild_id,
                    channel_id=interaction.channel_id or 0,
                    created_by=interaction.user.id,
                    entry_fee=params.entry_fee,
                    seeded_pot=params.seeded_pot,
                    min_players=params.min_players,
                    signup_seconds=params.signup_seconds,
                    round_seconds=params.round_seconds,
                    style=params.style,
                    outcome_winner=winner,
                    outcome_killed_by=killed_by,
                    outcome_killed_self=killed_self,
                )
                closes = game.signup_closes_at
                game_id = game.id
        except game_db.GameError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        money = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(
            title=settings.game_name,
            description="Signups are open. Enter with `/hungrygames join`.",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name="Entry fee",
            value=format_money(params.entry_fee, money) if params.entry_fee else "Free",
            inline=True,
        )
        if params.seeded_pot:
            embed.add_field(
                name="Starting pot", value=format_money(params.seeded_pot, money), inline=True
            )
        embed.add_field(name="Closes", value=relative_timestamp(closes), inline=True)

        # Everyone reads the terms before entering, which is the whole point of
        # asking the organizer for them up front.
        if outcomes:
            embed.add_field(name="If you are killed", value=killed_by or "-", inline=False)
            embed.add_field(name="If the arena gets you", value=killed_self or "-", inline=False)
            embed.add_field(name="If you win", value=winner or "-", inline=False)
        elif params.style == "standard":
            embed.add_field(
                name="Style", value="Standard - no forfeits, just the pot.", inline=False
            )

        embed.add_field(name="Tributes (0)", value="-", inline=False)
        embed.set_footer(text=f"At least {params.min_players} tributes, or fees are refunded.")
        await interaction.response.send_message(embed=embed, view=SignupView(game_id))

        # Remembered so the button can be disabled and the roster finalised when
        # signups close. Best-effort: losing it costs a tidy-up, not the game.
        try:
            sent = await interaction.original_response()
        except discord.HTTPException:
            sent = None
        if sent is not None:
            async with self.bot.database.sessions.begin() as session:
                stored = await session.get(Game, game_id)
                if stored is not None:
                    stored.signup_message_id = sent.id
        LOGGER.info(
            "game_created guild=%s by=%s fee=%s seed=%s style=%s round=%ss",
            interaction.guild_id,
            interaction.user.id,
            params.entry_fee,
            params.seeded_pot,
            params.style,
            params.round_seconds,
        )

    @group.command(name="join", description="Enter the current game")
    async def join(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            game = await game_db.active_game(session, interaction.guild_id)
        if game is None or game.status != "signup":
            await interaction.response.send_message(
                "No game is open for signups right now.", ephemeral=True
            )
            return
        await self.handle_join(interaction, game.id)

    async def handle_join(self, interaction: discord.Interaction, game_id: uuid.UUID) -> None:
        """Enter a tribute. Shared by the button and the slash command.

        The reply is ephemeral either way — a public line per joiner buries the
        signup message in a busy channel. The roster on that message is updated
        instead, so the count everyone can see stays live.
        """
        assert interaction.guild_id is not None
        settings = await self._settings(interaction)
        if settings is None:
            return

        try:
            async with self.bot.database.sessions.begin() as session:
                game = await session.get(Game, game_id)
                if game is None or game.guild_id != interaction.guild_id:
                    await interaction.response.send_message(
                        "That game is no longer available.", ephemeral=True
                    )
                    return
                if game.status != "signup":
                    await interaction.response.send_message(
                        "Signups for that game have closed.", ephemeral=True
                    )
                    return
                await game_db.join_game(
                    session,
                    game,
                    interaction.user.id,
                    display_name=interaction.user.display_name,
                )
                fee = game.entry_fee
                roster = [p.display_name for p in await game_db.participants(session, game.id)]
        except (game_db.GameError, BankingError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        money = CurrencyStyle.from_settings(settings)
        paid = f" for {format_money(fee, money)}" if fee else ""
        await interaction.response.send_message(
            f"You have entered the arena{paid}. **{len(roster)}** tribute(s) so far.",
            ephemeral=True,
        )
        await self._refresh_roster(interaction.message, roster)

    async def _refresh_roster(self, message: discord.Message | None, roster: list[str]) -> None:
        """Rewrite the tribute list on the signup message.

        Best-effort: the message may have been deleted, or this may be a slash
        command with no message attached. Failing to update a roster must never
        stop somebody from having joined.
        """
        if message is None or not message.embeds:
            return
        embed = message.embeds[0]
        shown = ", ".join(roster[:20]) + (f" +{len(roster) - 20} more" if len(roster) > 20 else "")
        for index, field in enumerate(embed.fields):
            if field.name and field.name.startswith("Tributes"):
                embed.set_field_at(
                    index, name=f"Tributes ({len(roster)})", value=shown or "-", inline=False
                )
                break
        else:
            embed.add_field(name=f"Tributes ({len(roster)})", value=shown or "-", inline=False)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            LOGGER.debug("Could not refresh the signup roster", exc_info=True)

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

    def _narrator(self, guild_id: int, library: NarrationLibrary) -> NarrationSession:
        """One narration session per game, so lines do not repeat within it.

        Kept in memory rather than persisted: after a restart a game resumes and
        starts its memory over, which at worst allows one repeat. Storing used
        lines would be a table's worth of machinery for a cosmetic guarantee.
        """
        return self._narrators.setdefault(guild_id, NarrationSession(library))

    async def _advance(self, game_id) -> None:
        now = datetime.now(UTC)
        async with self.bot.database.sessions.begin() as session:
            game = await session.get(Game, game_id, with_for_update=True)
            if game is None:
                return
            guild_id, channel_id, status = game.guild_id, game.channel_id, game.status
            # Captured as plain values: attributes expire when the session
            # commits, so reading them off the ORM object afterwards would
            # trigger a lazy refresh on a closed session.
            rules = StyleRules(
                style=game.style,
                winner=game.outcome_winner,
                killed_by=game.outcome_killed_by,
                killed_self=game.outcome_killed_self,
            )

            signup_message_id = game.signup_message_id
            game_id = game.id

            if status == "signup":
                started = await game_db.close_signups(session, game, now=now)
                joined = await game_db.participants(session, game.id)
                entrants = len(joined)
                payload = ("started" if started else "cancelled", entrants, None)
            elif status == "running":
                outcome = await game_db.run_round(session, game, rng=_RNG, now=now)
                payload = ("round", 0, outcome)
            else:
                return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            LOGGER.warning("Game %s has no reachable channel %s", game_id, channel_id)
            return

        guild = getattr(channel, "guild", None)
        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, guild_id)
        game_label = settings.game_name if settings else "Hungry Games"
        spoiler_percent = settings.game_spoiler_percent if settings else 10
        card_mode = settings.battle_cards if settings else "round"

        # Signups are over either way, so retire the button rather than leaving
        # it looking live on an old message.
        if payload[0] in {"started", "cancelled"} and signup_message_id:
            await self._close_signup_message(channel, signup_message_id)

        library = await self._library(guild_id)
        narrator = self._narrator(guild_id, library)
        kind, entrants, outcome = payload

        if kind == "cancelled":
            self._narrators.pop(guild_id, None)
            await channel.send(
                f"The {game_label} were called off — only {entrants} tribute(s) entered. "
                "Entry fees have been refunded."
            )
            return
        if kind == "started":
            self._narrators[guild_id] = NarrationSession(library)
            await channel.send(f"**The {game_label} begin.** {entrants} tributes enter the arena.")
            return

        assert outcome is not None
        # Occasionally hide the whole round so people have to click for it. Rare
        # on purpose: an always-spoilered game is a chore to read, a surprise one
        # is the point.
        hidden = _RNG.random() < (max(0, min(spoiler_percent, 100)) / 100)
        lines = self._narrate(outcome.plan, narrator, guild, rules, hidden=hidden)
        embed = discord.Embed(
            title=f"Round {outcome.round_number}",
            description="\n".join(lines) or "Nothing happens.",
            color=discord.Color.dark_red(),
        )

        card = await self._build_card(
            outcome, guild, game_label, card_mode, game_id, spoiler=hidden
        )
        # A spoilered attachment only shows its cover when it stands on its own.
        # Pulling it into an embed renders it immediately and the spoiler is
        # lost, which would defeat the point of hiding the round.
        if card is not None and not hidden:
            embed.set_image(url=f"attachment://{card.filename}")

        if not outcome.finished:
            embed.set_footer(text=f"{len(outcome.plan.survivors)} still standing.")
            await self._post(channel, embed, card)
            return

        await self._post(channel, embed, card)
        self._narrators.pop(guild_id, None)

        style = CurrencyStyle.from_settings(settings) if settings else None
        pot_text = format_money(outcome.pot, style) if style else str(outcome.pot)

        winner_name = (
            self._name(guild, outcome.winner_user_id) if outcome.winner_user_id else "nobody"
        )
        values = {"winner": winner_name, "pot": pot_text, "game": game_label}
        title = f"{game_label} #{outcome.game_number}" if outcome.game_number else game_label
        final = discord.Embed(
            title=f"{title} — it is over",
            description=narrator.pick(CATEGORY, "winner", values, fallback="{winner} wins {pot}."),
            color=discord.Color.gold(),
        )

        # The winner's spoils follow the same style rule as the forfeits.
        if rules.style == "organizer_defined" and rules.winner:
            final.add_field(name="Spoils", value=render(rules.winner, values), inline=False)
        elif rules.style == "random_tasks":
            reward = narrator.pick(CATEGORY, "reward_winner", values)
            if reward:
                final.add_field(name="Spoils", value=reward, inline=False)

        recap = self._standings_text(outcome, guild)
        if recap:
            final.add_field(name="Final standings", value=recap, inline=False)
        await channel.send(embed=final)
        LOGGER.info(
            "game_complete guild=%s number=%s winner=%s pot=%s rounds=%s style=%s",
            guild_id,
            outcome.game_number,
            outcome.winner_user_id,
            outcome.pot,
            outcome.round_number,
            rules.style,
        )

    async def _avatar(self, guild: discord.Guild | None, user_id: int) -> bytes | None:
        """Fetch one avatar, small. Failure is not an error — the card degrades."""
        member = guild.get_member(user_id) if guild else None
        if member is None:
            return None
        try:
            return await member.display_avatar.replace(size=256, format="png").read()
        except (discord.HTTPException, discord.NotFound):
            return None

    async def _build_card(
        self,
        outcome,
        guild: discord.Guild | None,
        game_label: str,
        mode: str,
        game_id,
        *,
        spoiler: bool,
    ) -> discord.File | None:
        """Render this round's eliminations, or None if cards are switched off.

        Wrapped so that nothing here can take a round down: if rendering or
        fetching fails the round still posts, just without a picture. The cost
        is measured either way and rolled onto the game row, because the whole
        point of shipping this separately is being able to answer "what is this
        costing me" a month from now.
        """
        if mode == "off":
            return None

        eliminations = [
            event
            for event in outcome.plan.events
            if event.kind in (EventKind.KILL, EventKind.DEATH)
        ]
        if not eliminations:
            return None

        started = time.perf_counter()
        try:
            # One fetch per face, cached by Discord's own CDN layer. Awaited
            # sequentially: a round has at most six faces and gather() here buys
            # milliseconds at the cost of burst-rate-limit risk.
            pairs: list[tuple[cards.Fighter, cards.Fighter | None]] = []
            for event in eliminations[:3]:
                if event.kind is EventKind.KILL and event.victim is not None:
                    killer = cards.Fighter(
                        name=self._name(guild, event.subject.user_id),
                        avatar=await self._avatar(guild, event.subject.user_id),
                    )
                    victim = cards.Fighter(
                        name=self._name(guild, event.victim.user_id),
                        avatar=await self._avatar(guild, event.victim.user_id),
                        defeated=True,
                    )
                    pairs.append((killer, victim))
                else:
                    pairs.append(
                        (
                            cards.Fighter(
                                name=self._name(guild, event.subject.user_id),
                                avatar=await self._avatar(guild, event.subject.user_id),
                                defeated=True,
                            ),
                            None,
                        )
                    )
            fetch_ms = (time.perf_counter() - started) * 1000
            result = await asyncio.to_thread(
                cards.render_duel, game_label, outcome.round_number, pairs
            )
        except Exception:
            LOGGER.exception("Battle card rendering failed; posting the round without one")
            return None

        LOGGER.info(
            "battle_card game=%s round=%s faces=%s fetch_ms=%.0f render_ms=%.0f bytes=%s",
            game_id,
            outcome.round_number,
            result.faces,
            fetch_ms,
            result.render_ms,
            result.bytes_written,
        )
        try:
            async with self.bot.database.sessions.begin() as session:
                game = await session.get(Game, game_id)
                if game is not None:
                    game.cards_rendered += 1
                    game.card_render_ms += int(result.render_ms + fetch_ms)
                    game.card_bytes += result.bytes_written
        except Exception:
            LOGGER.exception("Could not record battle card cost")

        return discord.File(io.BytesIO(result.png), filename="round.png", spoiler=spoiler)

    @staticmethod
    async def _post(channel, embed: discord.Embed, card: discord.File | None) -> None:
        """Send a round, with its card if there is one."""
        if card is None:
            await channel.send(embed=embed)
        else:
            await channel.send(embed=embed, file=card)

    async def _close_signup_message(self, channel, message_id: int) -> None:
        """Strip the join button off the signup message once signups end."""
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(view=None)
        except discord.HTTPException:
            LOGGER.debug("Could not retire the signup button", exc_info=True)

    def _standings_text(self, outcome, guild: discord.Guild | None) -> str:
        """Top placements, capped so a thirty-player game still fits an embed."""
        if not outcome.standings:
            return ""
        ordered = sorted(outcome.standings.items(), key=lambda pair: pair[1])
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        rows = [
            f"{medals.get(place, f'{place}.')} {self._name(guild, user_id)}"
            for user_id, place in ordered[:10]
        ]
        if len(ordered) > 10:
            rows.append(f"…and {len(ordered) - 10} more")
        return "\n".join(rows)


class GameStatsCog(commands.Cog):
    """`/leaderboard` and `/gamestats` — open to everyone.

    Top-level commands rather than subcommands of `/hungrygames`, because they
    read across every game a server has ever finished rather than acting on the
    one in progress.
    """

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    def _label(self, guild: discord.Guild | None, row) -> str:
        member = guild.get_member(row.user_id) if guild else None
        return member.display_name if member else row.display_name

    @staticmethod
    def _format(value: float, unit: str, money: CurrencyStyle | None) -> str:
        if unit == "":  # winnings, which are money
            return format_money(int(value), money) if money else str(int(value))
        shown = f"{value:g}"
        return f"{shown}{unit}" if unit == "%" else f"{shown} {unit}"

    @app_commands.command(name="leaderboard", description="Game leaderboards for this server")
    @app_commands.describe(stat="Which board to show")
    @app_commands.choices(
        stat=[
            app_commands.Choice(name=definition.label, value=definition.key)
            for definition in game_stats.STATS
        ]
    )
    @app_commands.guild_only()
    async def leaderboard(
        self, interaction: discord.Interaction, stat: app_commands.Choice[str] | None = None
    ) -> None:
        assert interaction.guild_id is not None
        key = stat.value if stat else "wins"
        definition = game_stats.BY_KEY[key]

        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, interaction.guild_id)
            rows = await game_stats.leaderboard(session, interaction.guild_id, key)

        game_label = settings.game_name if settings else "Hungry Games"
        money = CurrencyStyle.from_settings(settings) if settings else None

        if not rows:
            note = (
                f" Needs at least {game_stats.MIN_GAMES_FOR_RATE} games played."
                if key == "win_rate"
                else ""
            )
            await interaction.response.send_message(
                f"No {game_label} results yet.{note}", ephemeral=True
            )
            return

        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = [
            f"{medals.get(i, f'{i + 1}.')} **{self._label(interaction.guild, row)}** — "
            f"{self._format(row.value, definition.unit, money)}"
            for i, row in enumerate(rows)
        ]
        embed = discord.Embed(
            title=f"{game_label} — {definition.label}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Completed games only. Members who left are not listed.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="gamestats", description="A member's game record")
    @app_commands.describe(member="Whose record to show. Defaults to you.")
    @app_commands.guild_only()
    async def gamestats(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        assert interaction.guild_id is not None
        target = member or interaction.user

        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, interaction.guild_id)
            stats = await game_stats.member_stats(session, interaction.guild_id, target.id)

        game_label = settings.game_name if settings else "Hungry Games"
        if not stats.games:
            await interaction.response.send_message(
                f"{target.display_name} has not played a {game_label} yet.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"{target.display_name} — {game_label}", color=discord.Color.dark_gold()
        )
        embed.add_field(name="Games", value=str(stats.games), inline=True)
        embed.add_field(name="Wins", value=str(stats.wins), inline=True)
        embed.add_field(
            name="Win rate",
            value=(
                f"{stats.win_rate}%"
                if stats.win_rate is not None
                else f"—  (needs {game_stats.MIN_GAMES_FOR_RATE} games)"
            ),
            inline=True,
        )
        embed.add_field(name="Kills", value=str(stats.kills), inline=True)
        embed.add_field(name="Killed by others", value=str(stats.times_killed), inline=True)
        embed.add_field(name="Lost to the arena", value=str(stats.self_deaths), inline=True)
        embed.add_field(name="Best finish", value=f"#{stats.best_placement}", inline=True)
        embed.add_field(
            name="Average finish", value=str(stats.average_placement or "—"), inline=True
        )
        await interaction.response.send_message(embed=embed)


class GameConfigCog(commands.Cog):
    """Per-guild game settings.

    A separate top-level group rather than subcommands of `/hungrygames`,
    because Discord only honours `default_member_permissions` on top-level
    commands. As its own group it can genuinely be hidden from members, which
    `/hungrygames start` cannot be without also hiding `join`.
    """

    group = app_commands.Group(
        name="gameconfig",
        description="Configure games for this server",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    @group.command(name="show", description="Show the current game settings")
    async def show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            settings = await economy.get_settings(session, interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "This server has no economy yet. Run `/setup` first.", ephemeral=True
            )
            return

        embed = discord.Embed(title="Game settings", color=discord.Color.blurple())
        embed.add_field(name="Name", value=settings.game_name, inline=True)
        embed.add_field(
            name="Round gap", value=format_duration(settings.game_round_seconds), inline=True
        )
        embed.add_field(
            name="Signup window", value=format_duration(settings.game_signup_seconds), inline=True
        )
        embed.add_field(name="Default style", value=settings.game_default_style, inline=True)
        embed.add_field(
            name="Spoiler chance", value=f"{settings.game_spoiler_percent}%", inline=True
        )
        embed.add_field(name="Battle cards", value=settings.battle_cards, inline=True)
        embed.set_footer(
            text="The command is always /hungrygames - Discord command names are the same "
            "on every server, so only the displayed name changes."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="set", description="Change one or more game settings")
    @app_commands.describe(
        name="What this server calls the game, e.g. Kaos Pit. Display only.",
        round_time="Gap between rounds, e.g. 15s, 1m.",
        signup="How long signups stay open by default, e.g. 5m.",
        default_style="Style used when the organizer does not pick one.",
        spoiler_percent="Chance a round posts hidden behind a spoiler (0-100).",
        battle_cards="Round images: off, one per round, or one per duel.",
    )
    @app_commands.choices(
        default_style=[
            app_commands.Choice(name="Standard", value="standard"),
            app_commands.Choice(name="Random tasks", value="random_tasks"),
            app_commands.Choice(name="Organizer defined", value="organizer_defined"),
        ],
        battle_cards=[
            app_commands.Choice(name="Off - text only", value="off"),
            app_commands.Choice(name="One card per round", value="round"),
            app_commands.Choice(name="One card per duel", value="kill"),
        ],
    )
    async def set_(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        round_time: str | None = None,
        signup: str | None = None,
        default_style: app_commands.Choice[str] | None = None,
        spoiler_percent: int | None = None,
        battle_cards: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if not is_economy_admin(
            interaction.user.id,
            guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
            app_owner_ids=self.bot.settings.bot_owner_ids,
            has_administrator=has_admin_permission(interaction.user),
        ):
            await interaction.response.send_message(
                "You do not have permission to change game settings.", ephemeral=True
            )
            return

        changes: list[str] = []
        try:
            gap = parse_duration(round_time) if round_time else None
            window = parse_duration(signup) if signup else None
        except DurationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if gap is not None and not (game_db.MIN_ROUND_SECONDS <= gap <= game_db.MAX_ROUND_SECONDS):
            await interaction.response.send_message(
                f"Round gap must be between {game_db.MIN_ROUND_SECONDS}s and "
                f"{game_db.MAX_ROUND_SECONDS}s.",
                ephemeral=True,
            )
            return
        if window is not None and not MIN_SIGNUP_SECONDS <= window <= MAX_SIGNUP_SECONDS:
            await interaction.response.send_message(
                f"Signup window must be between {format_duration(MIN_SIGNUP_SECONDS)} and "
                f"{format_duration(MAX_SIGNUP_SECONDS)}.",
                ephemeral=True,
            )
            return
        if spoiler_percent is not None and not 0 <= spoiler_percent <= 100:
            await interaction.response.send_message(
                "Spoiler chance must be between 0 and 100.", ephemeral=True
            )
            return
        if name is not None and not 1 <= len(name.strip()) <= 64:
            await interaction.response.send_message(
                "The game name must be between 1 and 64 characters.", ephemeral=True
            )
            return

        async with self.bot.database.sessions.begin() as session:
            settings = await economy.get_settings(session, interaction.guild_id)
            if settings is None:
                await interaction.response.send_message(
                    "This server has no economy yet. Run `/setup` first.", ephemeral=True
                )
                return
            if name is not None:
                settings.game_name = name.strip()
                changes.append(f"name → **{settings.game_name}**")
            if gap is not None:
                settings.game_round_seconds = gap
                changes.append(f"round gap → **{format_duration(gap)}**")
            if window is not None:
                settings.game_signup_seconds = window
                changes.append(f"signup window → **{format_duration(window)}**")
            if default_style is not None:
                settings.game_default_style = default_style.value
                changes.append(f"default style → **{default_style.value}**")
            if spoiler_percent is not None:
                settings.game_spoiler_percent = spoiler_percent
                changes.append(f"spoiler chance → **{spoiler_percent}%**")
            if battle_cards is not None:
                settings.battle_cards = battle_cards.value
                changes.append(f"battle cards → **{battle_cards.value}**")

        if not changes:
            await interaction.response.send_message(
                "Nothing to change — pass at least one option.", ephemeral=True
            )
            return

        # Games already in flight keep the pacing they were created with, so a
        # change here never re-paces a game people are currently watching.
        await interaction.response.send_message(
            "Updated: " + ", ".join(changes) + ".\nAny game already running keeps its own pacing.",
            ephemeral=True,
        )
        LOGGER.info(
            "gameconfig guild=%s by=%s changes=%s",
            interaction.guild_id,
            interaction.user.id,
            len(changes),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GamesCog(bot))  # type: ignore[arg-type]
    await bot.add_cog(GameConfigCog(bot))  # type: ignore[arg-type]
    await bot.add_cog(GameStatsCog(bot))  # type: ignore[arg-type]
