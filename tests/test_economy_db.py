"""Integration tests for balance movement against a real PostgreSQL database.

These are skipped unless ``TEST_DATABASE_URL`` is set. A dedicated variable —
rather than reusing ``DATABASE_URL`` — means running the suite on a machine
configured for the live bot can never point these at production data. The
tests truncate tables between cases.

To run locally against a throwaway database:

    TEST_DATABASE_URL=postgresql+psycopg://shadow_bot:pw@127.0.0.1:5432/shadow_bot \\
        python -m pytest tests/test_economy_db.py

The concurrency test is the reason this file exists. Row locking cannot be
verified with mocks: the failure mode it guards against — two transfers reading
the same balance and the second overwriting the first — only appears when real
transactions contend for real rows.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shadow_bot.db import economy
from shadow_bot.db.models import AuditEvent, EconomyAccount, GuildSettings, LedgerEntry
from shadow_bot.domain.banking import BankingError

TEST_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set"),
    pytest.mark.asyncio,
]

GUILD = 999_000_000_000_000_001
ALICE = 111_000_000_000_000_001
BOB = 222_000_000_000_000_002


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_URL, pool_size=10, max_overflow=10)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker.begin() as session:
        await session.execute(
            text("TRUNCATE audit_events, ledger_entries, economy_accounts, guild_settings CASCADE")
        )
        session.add(GuildSettings(guild_id=GUILD, economy_enabled=True))

    yield maker
    await engine.dispose()


async def _balances(maker, user_id: int) -> tuple[int, int]:
    async with maker() as session:
        account = (
            await session.execute(
                select(EconomyAccount).where(
                    EconomyAccount.guild_id == GUILD, EconomyAccount.user_id == user_id
                )
            )
        ).scalar_one()
        return account.cash, account.bank


async def _grant(maker, user_id: int, cash: int) -> None:
    async with maker.begin() as session:
        account = await economy.get_or_create_account(session, GUILD, user_id, lock=True)
        account.cash = cash


async def test_account_is_created_with_zero_balances(sessions) -> None:
    async with sessions.begin() as session:
        account = await economy.get_or_create_account(session, GUILD, ALICE)
        assert (account.cash, account.bank) == (0, 0)


async def test_get_or_create_is_idempotent(sessions) -> None:
    async with sessions.begin() as session:
        first = await economy.get_or_create_account(session, GUILD, ALICE)
        first_id = first.id
    async with sessions.begin() as session:
        second = await economy.get_or_create_account(session, GUILD, ALICE)
        assert second.id == first_id

    async with sessions() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(EconomyAccount)
                .where(EconomyAccount.user_id == ALICE)
            )
        ).scalar_one()
    assert count == 1


async def test_concurrent_account_creation_yields_one_row(sessions) -> None:
    """Two commands from the same member arriving together must not duplicate.

    The unique constraint plus ON CONFLICT DO NOTHING is what makes this safe.
    """

    async def create() -> uuid.UUID:
        async with sessions.begin() as session:
            account = await economy.get_or_create_account(session, GUILD, ALICE)
            return account.id

    ids = await asyncio.gather(*(create() for _ in range(8)), return_exceptions=True)
    successful = [i for i in ids if isinstance(i, uuid.UUID)]
    assert successful, f"every attempt failed: {ids}"
    assert len(set(successful)) == 1


async def test_deposit_then_withdraw_round_trips(sessions) -> None:
    await _grant(sessions, ALICE, 1_000)

    async with sessions.begin() as session:
        await economy.deposit(session, GUILD, ALICE, 600)
    assert await _balances(sessions, ALICE) == (400, 600)

    async with sessions.begin() as session:
        await economy.withdraw(session, GUILD, ALICE, 250)
    assert await _balances(sessions, ALICE) == (650, 350)


async def test_deposit_beyond_cash_is_refused(sessions) -> None:
    await _grant(sessions, ALICE, 100)
    with pytest.raises(BankingError):
        async with sessions.begin() as session:
            await economy.deposit(session, GUILD, ALICE, 101)
    assert await _balances(sessions, ALICE) == (100, 0)


async def test_payment_moves_cash_and_records_both_sides(sessions) -> None:
    await _grant(sessions, ALICE, 1_000)

    async with sessions.begin() as session:
        await economy.pay(session, GUILD, ALICE, BOB, 400)

    assert await _balances(sessions, ALICE) == (600, 0)
    assert await _balances(sessions, BOB) == (400, 0)

    async with sessions() as session:
        entries = (
            (
                await session.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.category.in_(["pay_sent", "pay_received"])
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(entries) == 2
    assert len({e.correlation_id for e in entries}) == 1, "both sides share one correlation id"
    assert sum(e.cash_delta for e in entries) == 0, "a transfer creates no money"


async def test_ledger_records_every_movement(sessions) -> None:
    await _grant(sessions, ALICE, 500)
    async with sessions.begin() as session:
        await economy.deposit(session, GUILD, ALICE, 200)
    async with sessions.begin() as session:
        await economy.withdraw(session, GUILD, ALICE, 100)

    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    LedgerEntry.category, LedgerEntry.cash_delta, LedgerEntry.bank_delta
                ).order_by(LedgerEntry.created_at)
            )
        ).all()

    assert [r[0] for r in rows] == ["deposit", "withdraw"]
    assert rows[0][1:] == (-200, 200)
    assert rows[1][1:] == (100, -100)


async def test_concurrent_payments_cannot_create_money(sessions) -> None:
    """The test this whole file exists for.

    Alice has 500 and ten simultaneous transfers of 100 are attempted. Exactly
    five can succeed. Without `SELECT ... FOR UPDATE` several transactions read
    a stale balance, all decide they can afford it, and the last write wins —
    Bob ends up with more than Alice ever had.
    """
    await _grant(sessions, ALICE, 500)

    async def send() -> bool:
        try:
            async with sessions.begin() as session:
                await economy.pay(session, GUILD, ALICE, BOB, 100)
            return True
        except BankingError:
            return False

    results = await asyncio.gather(*(send() for _ in range(10)), return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"unexpected errors: {failures}"

    alice_cash, _ = await _balances(sessions, ALICE)
    bob_cash, _ = await _balances(sessions, BOB)

    assert sum(1 for r in results if r is True) == 5
    assert alice_cash == 0
    assert bob_cash == 500
    assert alice_cash + bob_cash == 500, "money was created or destroyed"
    assert alice_cash >= 0, "sender was overdrafted"


async def test_concurrent_deposits_do_not_lose_updates(sessions) -> None:
    """Ten deposits of 50 from 500 cash must land as exactly 500 banked."""
    await _grant(sessions, ALICE, 500)

    async def deposit() -> None:
        async with sessions.begin() as session:
            await economy.deposit(session, GUILD, ALICE, 50)

    await asyncio.gather(*(deposit() for _ in range(10)))
    assert await _balances(sessions, ALICE) == (0, 500)


async def test_paying_yourself_is_rejected(sessions) -> None:
    await _grant(sessions, ALICE, 100)
    with pytest.raises(ValueError, match="yourself"):
        async with sessions.begin() as session:
            await economy.pay(session, GUILD, ALICE, ALICE, 10)


async def test_emoji_currency_survives_a_round_trip(sessions) -> None:
    """Guards the UTF8 encoding decision made during deployment.

    On a SQL_ASCII database this is where mojibake would first appear.
    """
    async with sessions.begin() as session:
        settings = await session.get(GuildSettings, GUILD)
        settings.currency_symbol = "🪙"
        settings.currency_name = "dubloon"
        settings.currency_name_plural = "dubloons"

    async with sessions() as session:
        reloaded = await session.get(GuildSettings, GUILD)
        assert reloaded.currency_symbol == "🪙"
        assert reloaded.currency_name == "dubloon"


# --- Administrative currency operations ---------------------------------------

ADMIN = 333_000_000_000_000_003


async def test_grant_creates_currency_and_audits_it(sessions) -> None:
    async with sessions.begin() as session:
        await economy.grant_currency(session, GUILD, actor_id=ADMIN, target_id=ALICE, amount=5_000)
    assert await _balances(sessions, ALICE) == (5_000, 0)

    async with sessions() as session:
        events = (await session.execute(select(AuditEvent))).scalars().all()
        entries = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.category == "admin_grant")
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 1
    assert events[0].event_type == "currency_created"
    assert events[0].succeeded is True
    assert events[0].details["amount"] == 5_000
    assert events[0].details["authority"] == "guild_owner"
    assert len(entries) == 1 and entries[0].cash_delta == 5_000


async def test_grant_into_bank(sessions) -> None:
    async with sessions.begin() as session:
        await economy.grant_currency(
            session, GUILD, actor_id=ADMIN, target_id=ALICE, amount=750, destination="bank"
        )
    assert await _balances(sessions, ALICE) == (0, 750)


async def test_grant_rejects_an_unknown_destination(sessions) -> None:
    with pytest.raises(ValueError, match="cash or bank"):
        async with sessions.begin() as session:
            await economy.grant_currency(
                session, GUILD, actor_id=ADMIN, target_id=ALICE, amount=1, destination="mattress"
            )


async def test_removal_stops_at_the_floor_and_records_the_shortfall(sessions) -> None:
    await _grant(sessions, ALICE, 100)

    async with sessions.begin() as session:
        _, removed, uncollected = await economy.remove_currency(
            session, GUILD, actor_id=ADMIN, target_id=ALICE, amount=2_000, floor=-1_000
        )

    assert (removed, uncollected) == (1_100, 900)
    assert await _balances(sessions, ALICE) == (-1_000, 0)

    async with sessions() as session:
        event = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.event_type == "currency_removed")
                )
            )
            .scalars()
            .one()
        )
    assert event.details["attempted"] == 2_000
    assert event.details["removed"] == 1_100
    assert event.details["uncollected"] == 900


async def test_removal_that_takes_nothing_still_audits(sessions) -> None:
    """An owner who sees no balance change needs a record explaining why."""
    await _grant(sessions, ALICE, 0)
    async with sessions.begin() as session:
        account = await economy.get_or_create_account(session, GUILD, ALICE, lock=True)
        account.cash = -1_000

    async with sessions.begin() as session:
        _, removed, uncollected = await economy.remove_currency(
            session, GUILD, actor_id=ADMIN, target_id=ALICE, amount=500, floor=-1_000
        )

    assert (removed, uncollected) == (0, 500)
    async with sessions() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.event_type == "currency_removed")
                )
            )
            .scalars()
            .all()
        )
        ledger = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.category == "admin_remove")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1, "the attempt is audited even though nothing moved"
    assert ledger == [], "no ledger entry for a movement that did not happen"


async def test_actor_outside_the_guild_leaves_no_phantom_account(sessions) -> None:
    """An application owner acting in support may not be a member of the guild.

    Creating an account for them would put someone in the economy who never
    joined it, so the audit record simply carries no actor account.
    """
    async with sessions.begin() as session:
        await economy.grant_currency(
            session, GUILD, actor_id=ADMIN, target_id=ALICE, amount=100, authority="app_owner"
        )

    async with sessions() as session:
        accounts = (await session.execute(select(EconomyAccount.user_id))).scalars().all()
        event = (await session.execute(select(AuditEvent))).scalars().one()

    assert ADMIN not in accounts
    assert event.actor_account_id is None
    assert event.details["actor_user_id"] == str(ADMIN)
    assert event.details["authority"] == "app_owner"


async def test_granted_currency_can_then_be_paid_and_banked(sessions) -> None:
    """End-to-end: money enters circulation, moves, and is conserved."""
    async with sessions.begin() as session:
        await economy.grant_currency(session, GUILD, actor_id=ADMIN, target_id=ALICE, amount=1_000)
    async with sessions.begin() as session:
        await economy.pay(session, GUILD, ALICE, BOB, 400)
    async with sessions.begin() as session:
        await economy.deposit(session, GUILD, BOB, 400)

    alice = await _balances(sessions, ALICE)
    bob = await _balances(sessions, BOB)
    assert alice == (600, 0)
    assert bob == (0, 400)
    assert sum(alice) + sum(bob) == 1_000, "only the granted amount exists"
