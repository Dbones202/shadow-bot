"""Integration tests for the store and inventory against a real PostgreSQL
database. Skipped unless TEST_DATABASE_URL is set — see test_economy_db.py's
module docstring for how to run these locally.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shadow_bot.db import economy
from shadow_bot.db import store as store_db
from shadow_bot.db.models import GuildSettings, LedgerEntry
from shadow_bot.domain.store import StoreError

TEST_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set"),
    pytest.mark.asyncio,
]

GUILD = 999_000_000_000_000_002
OWNER = 999_000_000_000_000_009
ALICE = 111_000_000_000_000_011
BOB = 222_000_000_000_000_022


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_URL, pool_size=10, max_overflow=10)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker.begin() as session:
        await session.execute(
            text(
                "TRUNCATE ledger_entries, inventory_items, store_items, economy_accounts, "
                "guild_settings CASCADE"
            )
        )
        session.add(GuildSettings(guild_id=GUILD, economy_enabled=True))

    yield maker
    await engine.dispose()


async def _grant_cash(maker, user_id: int, cash: int) -> None:
    async with maker.begin() as session:
        account = await economy.get_or_create_account(session, GUILD, user_id, lock=True)
        account.cash = cash


async def _add_item(maker, *, name: str, price: int, stock: int | None = None):
    async with maker.begin() as session:
        item = await store_db.create_item(
            session,
            GUILD,
            name=name,
            description="",
            price=price,
            stock=stock,
            created_by=OWNER,
        )
        await session.flush()
        return item.id


async def test_purchase_deducts_cash_and_adds_inventory(sessions) -> None:
    await _grant_cash(sessions, ALICE, 1_000)
    item_id = await _add_item(sessions, name="Hat", price=100)

    async with sessions.begin() as session:
        account, item, inventory = await store_db.purchase(session, GUILD, ALICE, item_id, 2)
        assert account.cash == 800
        assert inventory.quantity == 2
        assert item.name == "Hat"


async def test_purchase_twice_accumulates_quantity_in_one_row(sessions) -> None:
    await _grant_cash(sessions, ALICE, 1_000)
    item_id = await _add_item(sessions, name="Hat", price=100)

    async with sessions.begin() as session:
        await store_db.purchase(session, GUILD, ALICE, item_id, 1)
    async with sessions.begin() as session:
        await store_db.purchase(session, GUILD, ALICE, item_id, 2)

    async with sessions() as session:
        account = await economy.get_or_create_account(session, GUILD, ALICE)
        rows = await store_db.get_inventory(session, account.id)
    assert len(rows) == 1
    assert rows[0].quantity == 3


async def test_purchase_rejects_insufficient_cash(sessions) -> None:
    await _grant_cash(sessions, ALICE, 50)
    item_id = await _add_item(sessions, name="Hat", price=100)

    with pytest.raises(StoreError):
        async with sessions.begin() as session:
            await store_db.purchase(session, GUILD, ALICE, item_id, 1)

    async with sessions() as session:
        account = await economy.get_or_create_account(session, GUILD, ALICE)
        assert account.cash == 50


async def test_purchase_decrements_finite_stock(sessions) -> None:
    await _grant_cash(sessions, ALICE, 1_000)
    item_id = await _add_item(sessions, name="Limited", price=10, stock=2)

    async with sessions.begin() as session:
        await store_db.purchase(session, GUILD, ALICE, item_id, 2)

    async with sessions() as session:
        item = await store_db.get_item(session, GUILD, "Limited")
        assert item.stock == 0

    await _grant_cash(sessions, BOB, 1_000)
    with pytest.raises(StoreError):
        async with sessions.begin() as session:
            await store_db.purchase(session, GUILD, BOB, item_id, 1)


async def test_purchase_writes_a_ledger_entry(sessions) -> None:
    await _grant_cash(sessions, ALICE, 1_000)
    item_id = await _add_item(sessions, name="Hat", price=100)

    async with sessions.begin() as session:
        await store_db.purchase(session, GUILD, ALICE, item_id, 1)

    async with sessions() as session:
        stmt = select(LedgerEntry).where(LedgerEntry.category == "store_purchase")
        entries = (await session.execute(stmt)).scalars().all()
    assert len(entries) == 1
    assert entries[0].cash_delta == -100


async def test_disabled_item_cannot_be_bought(sessions) -> None:
    await _grant_cash(sessions, ALICE, 1_000)
    item_id = await _add_item(sessions, name="Hat", price=100)

    async with sessions.begin() as session:
        item = await store_db.get_item(session, GUILD, "Hat")
        await store_db.set_item_fields(item, enabled=False)

    with pytest.raises(StoreError):
        async with sessions.begin() as session:
            await store_db.purchase(session, GUILD, ALICE, item_id, 1)


async def test_get_item_is_case_insensitive(sessions) -> None:
    await _add_item(sessions, name="Golden Hat", price=100)
    async with sessions() as session:
        assert await store_db.get_item(session, GUILD, "golden hat") is not None


async def test_set_item_fields_unlimited_stock_clears_finite_stock(sessions) -> None:
    await _add_item(sessions, name="Limited", price=10, stock=5)

    async with sessions.begin() as session:
        item = await store_db.get_item(session, GUILD, "Limited")
        await store_db.set_item_fields(item, stock_unlimited=True)

    async with sessions() as session:
        item = await store_db.get_item(session, GUILD, "Limited")
        assert item.stock is None
