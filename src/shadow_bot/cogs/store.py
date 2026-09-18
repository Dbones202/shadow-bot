"""Store and inventory commands: `/store list|buy|additem|edititem|removeitem`,
plus the member-facing `/inventory` (M11).

`/store` stays visible to everyone — like `/hungrygames`, Discord's
`default_member_permissions` only controls a *group's* visibility, and hiding
the group would hide `list`/`buy` from the members who need them.
`additem`/`edititem`/`removeitem` check economy admin authority in code
instead, the same pattern GamesCog uses for `start`/`cancel`.

v1 items are cosmetic/inventory-only: a purchase moves money and adds a row
to `inventory_items`, nothing else. See the roadmap doc for what a "grants a
role" item would need on top of this.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from shadow_bot.db import economy
from shadow_bot.db import store as store_db
from shadow_bot.db.models import GuildSettings
from shadow_bot.domain.amounts import CurrencyStyle, format_money
from shadow_bot.domain.authority import Authority, authority_of, has_admin_permission
from shadow_bot.domain.store import StoreError

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot

LOGGER = logging.getLogger(__name__)

NOT_CONFIGURED = "This server has no economy yet. The server owner can create one with `/setup`."
DISABLED = "The economy is currently disabled on this server."
ADMIN_ONLY = "You need economy admin authority to manage the store."


class StoreCog(commands.Cog):
    group = app_commands.Group(
        name="store", description="Buy items with your server's currency", guild_only=True
    )

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    # --- Shared guards -----------------------------------------------------

    async def _settings_or_reply(self, interaction: discord.Interaction) -> GuildSettings | None:
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

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        standing = authority_of(
            interaction.user.id,
            guild_owner_id=interaction.guild.owner_id if interaction.guild else None,
            app_owner_ids=self.bot.settings.bot_owner_ids,
            has_administrator=has_admin_permission(interaction.user),
        )
        return standing is not Authority.NONE

    # --- Autocomplete --------------------------------------------------------

    async def _buyable_item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._item_autocomplete(interaction, current, enabled_only=True)

    async def _any_item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._item_autocomplete(interaction, current, enabled_only=False)

    async def _item_autocomplete(
        self, interaction: discord.Interaction, current: str, *, enabled_only: bool
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        async with self.bot.database.sessions() as session:
            items = await store_db.list_items(
                session, interaction.guild_id, enabled_only=enabled_only
            )
        needle = current.lower()
        return [
            app_commands.Choice(name=i.name, value=i.name)
            for i in items
            if needle in i.name.lower()
        ][:25]

    # --- Everyone ------------------------------------------------------------

    @group.command(name="list", description="Show what's for sale")
    async def list_items(self, interaction: discord.Interaction) -> None:
        settings = await self._settings_or_reply(interaction)
        if settings is None:
            return
        assert interaction.guild_id is not None
        async with self.bot.database.sessions() as session:
            items = await store_db.list_items(session, interaction.guild_id)

        if not items:
            await interaction.response.send_message("Nothing is for sale here yet.", ephemeral=True)
            return

        style = CurrencyStyle.from_settings(settings)
        embed = discord.Embed(title="Store", color=discord.Color.blurple())
        for item in items:
            stock_note = "" if item.stock is None else f" — {item.stock:,} left"
            value = f"{format_money(item.price, style)}{stock_note}"
            if item.description:
                value += f"\n{item.description}"
            embed.add_field(name=item.name, value=value, inline=False)
        await interaction.response.send_message(embed=embed)

    @group.command(name="buy", description="Buy an item")
    @app_commands.describe(item="What to buy", quantity="How many. Defaults to 1.")
    @app_commands.autocomplete(item=_buyable_item_autocomplete)
    async def buy(
        self,
        interaction: discord.Interaction,
        item: str,
        quantity: app_commands.Range[int, 1] = 1,
    ) -> None:
        settings = await self._settings_or_reply(interaction)
        if settings is None:
            return
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        style = CurrencyStyle.from_settings(settings)

        async with self.bot.database.sessions() as session:
            record = await store_db.get_item(session, guild_id, item)
        if record is None or not record.enabled:
            await interaction.response.send_message(
                f"No item called **{item}** is for sale.", ephemeral=True
            )
            return

        async with self.bot.database.sessions.begin() as session:
            try:
                account, bought_item, inventory = await store_db.purchase(
                    session, guild_id, interaction.user.id, record.id, quantity
                )
            except StoreError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            cash = account.cash
            owned = inventory.quantity
            name = bought_item.name

        embed = discord.Embed(title=f"Bought {quantity:,}x {name}", color=discord.Color.green())
        embed.add_field(name="You now own", value=f"{owned:,}", inline=True)
        embed.add_field(name="Cash left", value=format_money(cash, style), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        LOGGER.info(
            "store_purchase guild=%s user=%s item=%s quantity=%s",
            guild_id,
            interaction.user.id,
            name,
            quantity,
        )

    # --- Administrator -------------------------------------------------------

    @group.command(name="additem", description="Add a new item to the store")
    @app_commands.describe(
        name="Item name",
        price="Price in this server's currency",
        description="Shown under the item in /store list",
        stock="Limited quantity available. Leave unset for unlimited.",
    )
    async def additem(
        self,
        interaction: discord.Interaction,
        name: str,
        price: app_commands.Range[int, 0],
        description: str = "",
        stock: app_commands.Range[int, 0] | None = None,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY, ephemeral=True)
            return
        settings = await self._settings_or_reply(interaction)
        if settings is None:
            return

        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            existing = await store_db.get_item(session, interaction.guild_id, name)
            if existing is not None:
                await interaction.response.send_message(
                    f"**{name}** already exists — use `/store edititem` to change it.",
                    ephemeral=True,
                )
                return
            await store_db.create_item(
                session,
                interaction.guild_id,
                name=name,
                description=description,
                price=price,
                stock=stock,
                created_by=interaction.user.id,
            )

        style = CurrencyStyle.from_settings(settings)
        stock_note = "" if stock is None else f", {stock:,} in stock"
        await interaction.response.send_message(
            f"Added **{name}** — {format_money(price, style)}{stock_note}."
        )

    @group.command(name="edititem", description="Change an existing store item")
    @app_commands.describe(
        name="Item to edit",
        price="New price. Leave unset to keep it.",
        description="New description. Leave unset to keep it.",
        stock="New stock count. Leave unset to keep it.",
        unlimited_stock="Set true to make stock unlimited again.",
        enabled="Show or hide the item in /store list and /store buy.",
    )
    @app_commands.autocomplete(name=_any_item_autocomplete)
    async def edititem(
        self,
        interaction: discord.Interaction,
        name: str,
        price: app_commands.Range[int, 0] | None = None,
        description: str | None = None,
        stock: app_commands.Range[int, 0] | None = None,
        unlimited_stock: bool | None = None,
        enabled: bool | None = None,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY, ephemeral=True)
            return
        assert interaction.guild_id is not None

        async with self.bot.database.sessions.begin() as session:
            record = await store_db.get_item(session, interaction.guild_id, name)
            if record is None:
                await interaction.response.send_message(
                    f"No item called **{name}**.", ephemeral=True
                )
                return
            await store_db.set_item_fields(
                record,
                price=price,
                stock_unlimited=unlimited_stock,
                stock=stock,
                description=description,
                enabled=enabled,
            )
            item_name = record.name

        await interaction.response.send_message(f"Updated **{item_name}**.")

    @group.command(name="removeitem", description="Take an item off the store")
    @app_commands.describe(name="Item to remove")
    @app_commands.autocomplete(name=_any_item_autocomplete)
    async def removeitem(self, interaction: discord.Interaction, name: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY, ephemeral=True)
            return
        assert interaction.guild_id is not None
        async with self.bot.database.sessions.begin() as session:
            record = await store_db.get_item(session, interaction.guild_id, name)
            if record is None:
                await interaction.response.send_message(
                    f"No item called **{name}**.", ephemeral=True
                )
                return
            # Disabled, not deleted — members may already own it, and deleting
            # the row would cascade their inventory rows away with it.
            record.enabled = False
            item_name = record.name

        await interaction.response.send_message(
            f"**{item_name}** is no longer for sale. Anyone who already owns it keeps it."
        )


class InventoryCog(commands.Cog):
    """Separate from StoreCog only so `/inventory` stays a top-level command
    rather than `/store inventory` — the other member-facing commands
    (`/balance`, `/collect`) are all top-level too."""

    def __init__(self, bot: EconomyBot) -> None:
        self.bot = bot

    @app_commands.command(name="inventory", description="Show what you've bought from the store")
    @app_commands.describe(member="Whose inventory to show. Defaults to you.")
    @app_commands.guild_only()
    async def inventory(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        assert interaction.guild_id is not None
        target = member or interaction.user
        if isinstance(target, discord.Member) and target.bot:
            await interaction.response.send_message("Bots do not hold accounts.", ephemeral=True)
            return

        async with self.bot.database.sessions() as session:
            account = await economy.get_or_create_account(session, interaction.guild_id, target.id)
            rows = await store_db.get_inventory(session, account.id)
            all_items = await store_db.list_items(session, interaction.guild_id, enabled_only=False)
            names_by_id = {i.id: i.name for i in all_items}

        if not rows:
            await interaction.response.send_message(
                f"{target.display_name} hasn't bought anything yet.", ephemeral=True
            )
            return

        lines = [
            f"- {names_by_id.get(row.item_id, 'an item that no longer exists')} x{row.quantity:,}"
            for row in rows
        ]
        embed = discord.Embed(
            title=f"{target.display_name}'s inventory",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: EconomyBot) -> None:
    await bot.add_cog(StoreCog(bot))
    await bot.add_cog(InventoryCog(bot))
