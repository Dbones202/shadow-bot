from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from shadow_bot.config import Settings
from shadow_bot.db.session import Database
from shadow_bot.logging_config import configure_logging

LOGGER = logging.getLogger(__name__)

#: Every cog the bot loads at startup. Keep this list authoritative — the import
#: smoke test in tests/test_extensions.py walks it, so a bad module path fails
#: the test suite instead of crashing the running bot.
EXTENSIONS: tuple[str, ...] = (
    "shadow_bot.cogs.health",
    "shadow_bot.cogs.member_lifecycle",
    "shadow_bot.cogs.setup",
    "shadow_bot.cogs.economy",
    "shadow_bot.cogs.admin",
)


class EconomyBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        intents = discord.Intents.default()
        intents.members = settings.enable_members_intent
        intents.message_content = settings.enable_message_content_intent
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.database = database

    async def setup_hook(self) -> None:
        await self.database.check_connection()

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            LOGGER.info("Loaded extension %s", extension)

        if self.settings.test_guild_id:
            guild = discord.Object(id=self.settings.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info("Synced %s command(s) to test guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            LOGGER.info("Synced %s global command(s)", len(synced))

    async def close(self) -> None:
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        if self.user is not None:
            LOGGER.info(
                "Ready as %s (%s) in %s guild(s)", self.user, self.user.id, len(self.guilds)
            )


async def _tree_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = "You do not have permission to use that command here."
        LOGGER.info("Rejected command from %s: %s", interaction.user.id, error)
    else:
        message = "That command could not be completed. The error has been logged."
        LOGGER.exception("Application command failed", exc_info=error)

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def run_bot() -> None:
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    bot = EconomyBot(settings, database)
    bot.tree.error(_tree_error)
    async with bot:
        await bot.start(settings.discord_token)
