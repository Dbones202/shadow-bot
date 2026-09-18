"""Database operations for the store and member inventories (M11).

Purchases lock the buyer's account the same way economy.py's money-moving
functions do, then the item row too — its stock can be decremented by two
buyers at once. Both locks are taken in a fixed order, account then item, so
two concurrent purchases of *different* items by different members can never
deadlock against each other.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.economy import get_or_create_account
from shadow_bot.db.models import EconomyAccount, InventoryItem, LedgerEntry, StoreItem
from shadow_bot.domain.store import (
    StoreError,
    apply_purchase,
    require_valid_price,
    require_valid_stock,
)


async def get_item(session: AsyncSession, guild_id: int, name: str) -> StoreItem | None:
    """Exact, case-insensitive lookup by name — callers pair this with
    autocomplete rather than fuzzy matching, so a member never buys the
    "wrong" item because two names partially matched."""
    return (
        await session.execute(
            select(StoreItem).where(StoreItem.guild_id == guild_id, StoreItem.name.ilike(name))
        )
    ).scalar_one_or_none()


async def list_items(
    session: AsyncSession, guild_id: int, *, enabled_only: bool = True
) -> Sequence[StoreItem]:
    stmt = select(StoreItem).where(StoreItem.guild_id == guild_id).order_by(StoreItem.name)
    if enabled_only:
        stmt = stmt.where(StoreItem.enabled.is_(True))
    return (await session.execute(stmt)).scalars().all()


async def create_item(
    session: AsyncSession,
    guild_id: int,
    *,
    name: str,
    description: str,
    price: int,
    stock: int | None,
    created_by: int,
    effect_type: str = "none",
    effect_data: dict | None = None,
) -> StoreItem:
    """``effect_type``/``effect_data`` are accepted so this function does not
    need to change again once a future milestone gives items real effects —
    no command passes anything but the defaults today, and nothing reads
    them back yet."""
    require_valid_price(price)
    require_valid_stock(stock)
    item = StoreItem(
        guild_id=guild_id,
        name=name,
        description=description,
        price=price,
        stock=stock,
        created_by=created_by,
        effect_type=effect_type,
        effect_data=effect_data or {},
    )
    session.add(item)
    return item


async def set_item_fields(
    item: StoreItem,
    *,
    price: int | None = None,
    stock_unlimited: bool | None = None,
    stock: int | None = None,
    description: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Apply whichever fields were provided.

    ``stock_unlimited=True`` clears stock to null; otherwise ``stock`` (if
    given) sets a finite count. Passing both prefers ``stock_unlimited``.
    """
    if price is not None:
        require_valid_price(price)
        item.price = price
    if description is not None:
        item.description = description
    if enabled is not None:
        item.enabled = enabled
    if stock_unlimited:
        item.stock = None
    elif stock is not None:
        require_valid_stock(stock)
        item.stock = stock


async def get_inventory(session: AsyncSession, account_id: uuid.UUID) -> Sequence[InventoryItem]:
    return (
        (
            await session.execute(
                select(InventoryItem)
                .join(StoreItem, StoreItem.id == InventoryItem.item_id)
                .where(InventoryItem.account_id == account_id)
                .order_by(StoreItem.name)
            )
        )
        .scalars()
        .all()
    )


async def purchase(
    session: AsyncSession, guild_id: int, user_id: int, item_id: uuid.UUID, quantity: int
) -> tuple[EconomyAccount, StoreItem, InventoryItem]:
    """Buy ``quantity`` of an item, locking the buyer's account and the item
    row (in that order) before checking price and stock, so two concurrent
    purchases can never both spend a member's last coin or the item's last
    unit of stock.

    Raises ``StoreError`` (from `domain.store`) for anything the buyer should
    see as a plain message.
    """
    account = await get_or_create_account(session, guild_id, user_id, lock=True)
    item = (
        await session.execute(select(StoreItem).where(StoreItem.id == item_id).with_for_update())
    ).scalar_one_or_none()
    if item is None or item.guild_id != guild_id or not item.enabled:
        raise StoreError("That item is not available.")

    existing = (
        await session.execute(
            select(InventoryItem).where(
                InventoryItem.account_id == account.id, InventoryItem.item_id == item.id
            )
        )
    ).scalar_one_or_none()
    owned = existing.quantity if existing else 0

    result = apply_purchase(
        cash=account.cash, price=item.price, quantity=quantity, stock=item.stock, owned=owned
    )

    account.cash = result.remaining_cash
    if item.stock is not None:
        item.stock = result.remaining_stock

    if existing is None:
        existing = InventoryItem(account_id=account.id, item_id=item.id, quantity=0)
        session.add(existing)
    existing.quantity += quantity

    session.add(
        LedgerEntry(
            correlation_id=uuid.uuid4(),
            guild_id=guild_id,
            subject_account_id=account.id,
            actor_account_id=account.id,
            category="store_purchase",
            cash_delta=-result.total_cost,
            attempted_amount=result.total_cost,
            applied_amount=result.total_cost,
            details={"item_id": str(item.id), "item_name": item.name, "quantity": quantity},
        )
    )
    return account, item, existing
