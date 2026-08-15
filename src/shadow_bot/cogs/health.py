from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands


class HealthCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot and database connection")
    async def ping(self, interaction: discord.Interaction) -> None:
        started = time.perf_counter()
        try:
            await self.bot.database.check_connection()  # type: ignore[attr-defined]
            database_status = "Connected"
        except Exception:
            database_status = "Unavailable"

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        gateway_ms = round(self.bot.latency * 1000)
        color = discord.Color.green() if database_status == "Connected" else discord.Color.red()
        embed = discord.Embed(title="Bot status", color=color)
        embed.add_field(name="Discord", value=f"Connected ({gateway_ms} ms)", inline=False)
        embed.add_field(
            name="PostgreSQL", value=f"{database_status} ({elapsed_ms} ms)", inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HealthCog(bot))
