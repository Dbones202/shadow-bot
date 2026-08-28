"""Media requests: `/request_movie`, `/request_tv`, `/media allow|revoke|list`.

Requests go through a Radarr/Sonarr `lookup` search so the requester can
confirm they picked the right title before anything is added — no separate
IMDb API call is needed, since both apps' lookup responses already carry an
`imdbId`. The allowlist and the completion notifications are both global
rather than per-guild: Donovan runs one Radarr/Sonarr pair, one owner id
(`MEDIA_OWNER_ID`) gates who can be added to it, and that gate is deliberately
not `bot_owner_ids` — see config.py's docstring on the field.

The daily poller mirrors the pacing pattern GamesCog already uses for its round
clock: state lives in `media_requests`, not in memory, so a restart never loses
track of what is still pending.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from shadow_bot.db import media as media_db
from shadow_bot.db.models import MediaRequest
from shadow_bot.domain.media import MediaCandidate, movie_is_downloaded, series_is_downloaded
from shadow_bot.services.radarr import RadarrClient
from shadow_bot.services.sonarr import SonarrClient

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

#: How often the completion poller checks Radarr/Sonarr. Donovan explicitly
#: asked for once a day rather than anything tighter — these downloads take
#: hours regardless, and a daily check is plenty responsive for a notification
#: that isn't time-critical.
POLL_HOURS = 24

MAX_RESULTS = 5


class MediaSearchError(RuntimeError):
    """Radarr/Sonarr isn't configured, or the request to it failed."""


class ResultButton(discord.ui.Button):
    def __init__(
        self, cog: MediaCog, candidate: MediaCandidate, media_type: str, index: int
    ) -> None:
        super().__init__(
            label=candidate.display_title[:80],
            style=discord.ButtonStyle.secondary
            if candidate.already_in_library
            else discord.ButtonStyle.primary,
            disabled=candidate.already_in_library,
            row=index // 5,
        )
        self._cog = cog
        self._candidate = candidate
        self._media_type = media_type

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cog.handle_selection(interaction, self._candidate, self._media_type)


class ResultView(discord.ui.View):
    """Search results are only good for a few minutes — after that Radarr's
    picture of "already in library" may be stale, so the view times out and the
    buttons stop working rather than silently double-requesting something."""

    def __init__(self, cog: MediaCog, candidates: list[MediaCandidate], media_type: str) -> None:
        super().__init__(timeout=180)
        for index, candidate in enumerate(candidates[:MAX_RESULTS]):
            self.add_item(ResultButton(cog, candidate, media_type, index))


class MediaCog(commands.Cog):
    group = app_commands.Group(
        name="media",
        description="Manage who can request movies and TV shows",
        guild_only=True,
        # Every subcommand here is owner-gated in code (_is_owner), the same
        # way GamesCog gates `start`/`cancel` — Discord's default permission
        # system has no concept of "one specific user id".
    )

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    # --- Authority --------------------------------------------------------

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        owner_id = self.bot.settings.media_owner_id
        return owner_id is not None and interaction.user.id == owner_id

    def _radarr(self) -> RadarrClient:
        settings = self.bot.settings
        if not (settings.radarr_url and settings.radarr_api_key):
            raise MediaSearchError("Radarr is not configured.")
        return RadarrClient(
            self.bot.http_session, base_url=settings.radarr_url, api_key=settings.radarr_api_key
        )

    def _sonarr(self) -> SonarrClient:
        settings = self.bot.settings
        if not (settings.sonarr_url and settings.sonarr_api_key):
            raise MediaSearchError("Sonarr is not configured.")
        return SonarrClient(
            self.bot.http_session, base_url=settings.sonarr_url, api_key=settings.sonarr_api_key
        )

    # --- /media allow|revoke|list ------------------------------------------

    @group.command(name="allow", description="Let a user run /request_movie and /request_tv")
    @app_commands.describe(user="The user to grant access to")
    async def allow(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can change who is allowed to request media.", ephemeral=True
            )
            return
        async with self.bot.database.sessions.begin() as session:
            await media_db.allow_user(
                session, user_id=user.id, username=str(user), added_by=interaction.user.id
            )
        await interaction.response.send_message(
            f"{user.mention} can now request movies and TV shows."
        )

    @group.command(name="revoke", description="Remove a user's ability to request media")
    @app_commands.describe(user="The user to remove access from")
    async def revoke(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can change who is allowed to request media.", ephemeral=True
            )
            return
        async with self.bot.database.sessions.begin() as session:
            removed = await media_db.revoke_user(session, user_id=user.id)
        if removed:
            await interaction.response.send_message(f"{user.mention} can no longer request media.")
        else:
            await interaction.response.send_message(
                f"{user.mention} wasn't on the list.", ephemeral=True
            )

    @group.command(name="list", description="Show who can request movies and TV shows")
    async def list_allowed(self, interaction: discord.Interaction) -> None:
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the bot owner can view the media request allowlist.", ephemeral=True
            )
            return
        async with self.bot.database.sessions() as session:
            entries = await media_db.list_allowed(session)
        if not entries:
            await interaction.response.send_message("Nobody is on the list yet.", ephemeral=True)
            return
        lines = [f"- {entry.username or entry.user_id} (<@{entry.user_id}>)" for entry in entries]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # --- /request_movie, /request_tv ---------------------------------------

    @app_commands.command(name="request_movie", description="Search for and request a movie")
    @app_commands.describe(title="What to search for")
    async def request_movie(self, interaction: discord.Interaction, title: str) -> None:
        await self._search(interaction, title, media_type="movie")

    @app_commands.command(name="request_tv", description="Search for and request a TV show")
    @app_commands.describe(title="What to search for")
    async def request_tv(self, interaction: discord.Interaction, title: str) -> None:
        await self._search(interaction, title, media_type="tv")

    async def _search(
        self, interaction: discord.Interaction, title: str, *, media_type: str
    ) -> None:
        async with self.bot.database.sessions() as session:
            allowed = await media_db.is_allowed(session, interaction.user.id)
        if not allowed:
            await interaction.response.send_message(
                "You're not on the list of people who can request movies or TV shows.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = self._radarr() if media_type == "movie" else self._sonarr()
            candidates = await client.search(title)
        except MediaSearchError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            LOGGER.exception("Media search failed for %r (%s)", title, media_type)
            await interaction.followup.send(
                "That search failed. Try again in a moment.", ephemeral=True
            )
            return

        if not candidates:
            await interaction.followup.send(f"No results for **{title}**.", ephemeral=True)
            return

        embeds = []
        for candidate in candidates[:MAX_RESULTS]:
            embed = discord.Embed(title=candidate.display_title, url=candidate.imdb_url)
            if candidate.poster_url:
                embed.set_thumbnail(url=candidate.poster_url)
            if candidate.already_in_library:
                embed.description = "Already in the library."
            embeds.append(embed)

        await interaction.followup.send(
            content=f"Results for **{title}** — pick one:",
            embeds=embeds,
            view=ResultView(self, candidates, media_type),
            ephemeral=True,
        )

    async def handle_selection(
        self, interaction: discord.Interaction, candidate: MediaCandidate, media_type: str
    ) -> None:
        async with self.bot.database.sessions() as session:
            allowed = await media_db.is_allowed(session, interaction.user.id)
        if not allowed:
            await interaction.response.send_message(
                "You're no longer on the list of people who can request media.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = self.bot.settings
        try:
            if media_type == "movie":
                client = self._radarr()
                added = await client.add(
                    candidate,
                    quality_profile_id=settings.radarr_quality_profile_id,
                    root_folder=settings.radarr_root_folder,
                )
            else:
                client = self._sonarr()
                added = await client.add(
                    candidate,
                    quality_profile_id=settings.sonarr_quality_profile_id,
                    root_folder=settings.sonarr_root_folder,
                )
        except Exception:
            LOGGER.exception("Adding %s (%s) failed", candidate.title, media_type)
            await interaction.followup.send(
                "That request failed to reach Radarr/Sonarr. Try again in a moment.", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            await media_db.create_request(
                session,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                requested_by=interaction.user.id,
                media_type=media_type,
                external_id=int(added["id"]),
                tmdb_id=candidate.tmdb_id,
                tvdb_id=candidate.tvdb_id,
                imdb_id=candidate.imdb_id,
                title=candidate.title,
                year=candidate.year,
            )

        await interaction.followup.send(
            f"Requested **{candidate.display_title}**. "
            f"I'll reply in this channel once it's downloaded.",
            ephemeral=True,
        )

    # --- Background pacing ---------------------------------------------------

    @tasks.loop(hours=POLL_HOURS)
    async def poll(self) -> None:
        settings = self.bot.settings
        if not (
            (settings.radarr_url and settings.radarr_api_key)
            or (settings.sonarr_url and settings.sonarr_api_key)
        ):
            return  # neither app configured — nothing to poll

        try:
            async with self.bot.database.sessions() as session:
                pending = list(await media_db.pending_requests(session))
        except Exception:
            LOGGER.exception("Could not look for pending media requests")
            return

        for request in pending:
            try:
                await self._check_one(request)
            except Exception:
                LOGGER.exception("Failed to check media request %s", request.id)

    @poll.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_one(self, request: MediaRequest) -> None:
        settings = self.bot.settings
        if request.media_type == "movie":
            if not (settings.radarr_url and settings.radarr_api_key):
                return
            record = await self._radarr().get_movie(request.external_id)
            downloaded = movie_is_downloaded(record)
        else:
            if not (settings.sonarr_url and settings.sonarr_api_key):
                return
            record = await self._sonarr().get_series(request.external_id)
            downloaded = series_is_downloaded(record)

        if not downloaded:
            return

        async with self.bot.database.sessions.begin() as session:
            row = await session.get(MediaRequest, request.id)
            if row is None or row.status != "pending":
                return
            await media_db.mark_downloaded(session, row)
            channel_id, requested_by, title = row.channel_id, row.requested_by, row.title

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                LOGGER.warning("Could not reach channel %s to notify about %s", channel_id, title)
                return
        await channel.send(f"<@{requested_by}> **{title}** has finished downloading.")


async def setup(bot: EconomyBot) -> None:
    await bot.add_cog(MediaCog(bot))
