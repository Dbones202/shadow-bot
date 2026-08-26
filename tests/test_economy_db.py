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
import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shadow_bot.db import activities as activity_db
from shadow_bot.db import economy, income
from shadow_bot.db import games as game_db
from shadow_bot.db.models import (
    AuditEvent,
    EconomyAccount,
    Game,
    GameEvent,
    GameParticipant,
    GuildSettings,
    LedgerEntry,
    RoleCollectionCooldown,
)
from shadow_bot.domain.activities import Activity, ActivityError
from shadow_bot.domain.banking import BankingError

TEST_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set"),
    pytest.mark.asyncio,
]

GUILD = 999_000_000_000_000_001
ALICE = 111_000_000_000_000_001
BOB = 222_000_000_000_000_002
CAROL = 444_000_000_000_000_004


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_URL, pool_size=10, max_overflow=10)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker.begin() as session:
        await session.execute(
            text(
                "TRUNCATE audit_events, ledger_entries, game_events, game_participants, games, "
                "activity_cooldowns, activity_rules, role_collection_cooldowns, "
                "role_income_rules, economy_accounts, guild_settings CASCADE"
            )
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


# --- Role income --------------------------------------------------------------

ROLE_A = 444_000_000_000_000_004
ROLE_B = 555_000_000_000_000_005


async def _rule(sessions, role_id: int, payout: int, cooldown: int):
    async with sessions.begin() as session:
        rule, _ = await income.upsert_rule(
            session, GUILD, role_id=role_id, payout=payout, cooldown_seconds=cooldown
        )
        return rule.id


async def test_collect_pays_every_held_income_role(sessions) -> None:
    await _rule(sessions, ROLE_A, 300, 3_600)
    await _rule(sessions, ROLE_B, 200, 3_600)

    async with sessions.begin() as session:
        plan = await income.collect(session, GUILD, ALICE, [ROLE_A, ROLE_B])

    assert plan.total == 500
    assert await _balances(sessions, ALICE) == (500, 0)


async def test_collect_ignores_roles_the_member_does_not_hold(sessions) -> None:
    """Discord is the truth for role membership, not the rules table."""
    await _rule(sessions, ROLE_A, 300, 3_600)
    await _rule(sessions, ROLE_B, 200, 3_600)

    async with sessions.begin() as session:
        plan = await income.collect(session, GUILD, ALICE, [ROLE_A])

    assert plan.total == 300


async def test_second_collect_within_the_cooldown_pays_nothing(sessions) -> None:
    await _rule(sessions, ROLE_A, 300, 3_600)

    async with sessions.begin() as session:
        await income.collect(session, GUILD, ALICE, [ROLE_A])
    async with sessions.begin() as session:
        plan = await income.collect(session, GUILD, ALICE, [ROLE_A])

    assert plan.total == 0
    assert len(plan.waiting) == 1
    assert await _balances(sessions, ALICE) == (300, 0)


async def test_collect_again_after_the_cooldown_expires(sessions) -> None:
    await _rule(sessions, ROLE_A, 300, 3_600)
    async with sessions.begin() as session:
        await income.collect(session, GUILD, ALICE, [ROLE_A])

    later = datetime.now(UTC) + timedelta(seconds=3_601)
    async with sessions.begin() as session:
        plan = await income.collect(session, GUILD, ALICE, [ROLE_A], now=later)

    assert plan.total == 300
    assert await _balances(sessions, ALICE) == (600, 0)


async def test_concurrent_collects_pay_once(sessions) -> None:
    """The double-collect race: ten simultaneous /collect must pay one payout.

    Without the account row lock, several transactions read "no cooldown", all
    decide the member is eligible, and the payout multiplies.

    The account is created up front on purpose. If it does not exist yet, every
    transaction serialises behind the first one's INSERT ... ON CONFLICT while
    the database decides who creates the row — which hides the race and makes
    this test pass even with the lock removed. Verified by deleting
    `with_for_update()` and watching this fail.
    """
    await _rule(sessions, ROLE_A, 300, 3_600)
    await _grant(sessions, ALICE, 0)  # pre-create so nothing serialises on insert

    async def attempt() -> int:
        async with sessions.begin() as session:
            plan = await income.collect(session, GUILD, ALICE, [ROLE_A])
            return plan.total

    results = await asyncio.gather(*(attempt() for _ in range(10)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"unexpected errors: {errors}"

    cash, _ = await _balances(sessions, ALICE)
    assert cash == 300, f"expected a single payout, got {cash}"
    assert sum(r for r in results if isinstance(r, int)) == 300


async def test_collect_writes_one_ledger_entry_per_role(sessions) -> None:
    await _rule(sessions, ROLE_A, 300, 3_600)
    await _rule(sessions, ROLE_B, 200, 7_200)

    async with sessions.begin() as session:
        await income.collect(session, GUILD, ALICE, [ROLE_A, ROLE_B])

    async with sessions() as session:
        entries = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.category == "role_income")
                )
            )
            .scalars()
            .all()
        )

    assert len(entries) == 2
    assert len({e.correlation_id for e in entries}) == 1
    assert sum(e.cash_delta for e in entries) == 500
    assert {e.details["role_id"] for e in entries} == {str(ROLE_A), str(ROLE_B)}


async def test_updating_a_rule_does_not_duplicate_it(sessions) -> None:
    first = await _rule(sessions, ROLE_A, 300, 3_600)
    second = await _rule(sessions, ROLE_A, 900, 7_200)
    assert first == second

    async with sessions() as session:
        rules = await income.list_rules(session, GUILD)
    assert len(rules) == 1
    assert rules[0].payout == 900


async def test_removing_a_rule_clears_its_cooldowns(sessions) -> None:
    """Otherwise an orphaned cooldown would linger and confuse a re-added rule."""
    await _rule(sessions, ROLE_A, 300, 3_600)
    async with sessions.begin() as session:
        await income.collect(session, GUILD, ALICE, [ROLE_A])

    async with sessions.begin() as session:
        assert await income.delete_rule(session, GUILD, ROLE_A) is True

    async with sessions() as session:
        remaining = (await session.execute(select(RoleCollectionCooldown))).scalars().all()
    assert remaining == []


async def test_removing_a_rule_that_does_not_exist_reports_false(sessions) -> None:
    async with sessions.begin() as session:
        assert await income.delete_rule(session, GUILD, ROLE_A) is False


async def test_collect_with_no_roles_is_harmless(sessions) -> None:
    async with sessions.begin() as session:
        plan = await income.collect(session, GUILD, ALICE, [])
    assert plan.total == 0 and plan.waiting == ()


# --- Activities ---------------------------------------------------------------

WORK = Activity.WORK
STEAL = Activity.STEAL


async def _configure(
    sessions,
    activity=WORK,
    *,
    chance="1",
    reward=(100, 100),
    fine=(50, 50),
    cooldown=3_600,
    enabled=True,
):
    async with sessions.begin() as session:
        await activity_db.upsert_rule(
            session,
            GUILD,
            activity,
            cooldown_seconds=cooldown,
            success_chance=Decimal(chance),
            success_min=reward[0],
            success_max=reward[1],
            fine_min=fine[0],
            fine_max=fine[1],
            enabled=enabled,
        )


async def _try(
    sessions, activity=WORK, *, user=ALICE, chance_roll="0", amount_roll=0.0, target=None, now=None
):
    async with sessions.begin() as session:
        return await activity_db.attempt(
            session,
            GUILD,
            user,
            activity,
            chance_roll=Decimal(chance_roll),
            amount_roll=amount_roll,
            cash_floor=-1_000,
            bank_floor=-10_000,
            target_id=target,
            now=now,
        )


async def test_unconfigured_activity_is_refused(sessions) -> None:
    with pytest.raises(ActivityError, match="not set up"):
        await _try(sessions)


async def test_disabled_activity_is_refused(sessions) -> None:
    await _configure(sessions, enabled=False)
    with pytest.raises(ActivityError, match="not set up"):
        await _try(sessions)


async def test_successful_work_pays_and_sets_a_cooldown(sessions) -> None:
    await _configure(sessions)
    result = await _try(sessions)
    assert result.outcome.succeeded
    assert await _balances(sessions, ALICE) == (100, 0)
    assert result.next_available_at is not None


async def test_second_attempt_within_the_cooldown_is_refused(sessions) -> None:
    await _configure(sessions)
    await _try(sessions)
    with pytest.raises(activity_db.OnCooldown):
        await _try(sessions)
    assert await _balances(sessions, ALICE) == (100, 0), "the refused attempt paid nothing"


async def test_attempt_after_the_cooldown_expires(sessions) -> None:
    await _configure(sessions)
    await _try(sessions)
    later = datetime.now(UTC) + timedelta(seconds=3_601)
    await _try(sessions, now=later)
    assert await _balances(sessions, ALICE) == (200, 0)


async def test_failure_fines_using_the_tested_fine_logic(sessions) -> None:
    await _configure(sessions, chance="0", fine=(500, 500))
    await _grant(sessions, ALICE, 800)
    result = await _try(sessions, chance_roll="0.5")
    assert not result.outcome.succeeded
    assert result.collected == 500
    assert await _balances(sessions, ALICE) == (300, 0)


async def test_a_fine_draws_cash_to_its_floor_then_reaches_into_bank(sessions) -> None:
    """Cash floor is -1,000 and bank floor -10,000, so 5,000 is fully collectable.

    The bank floor extends how far a fine can reach — 1,000 from cash, then the
    remaining 4,000 from bank.
    """
    await _configure(sessions, chance="0", fine=(5_000, 5_000))
    result = await _try(sessions, chance_roll="0.5")
    assert (result.collected, result.uncollected) == (5_000, 0)
    assert await _balances(sessions, ALICE) == (-1_000, -4_000)


async def test_a_fine_beyond_both_floors_reports_the_shortfall(sessions) -> None:
    """Only 11,000 is reachable in total; the rest is uncollected, not forgiven silently."""
    await _configure(sessions, chance="0", fine=(20_000, 20_000))
    result = await _try(sessions, chance_roll="0.5")
    assert (result.collected, result.uncollected) == (11_000, 9_000)
    assert await _balances(sessions, ALICE) == (-1_000, -10_000)


async def test_steal_moves_cash_between_members(sessions) -> None:
    await _configure(sessions, STEAL, reward=(300, 300))
    await _grant(sessions, BOB, 1_000)
    result = await _try(sessions, STEAL, target=BOB)
    assert result.outcome.succeeded
    assert await _balances(sessions, ALICE) == (300, 0)
    assert await _balances(sessions, BOB) == (700, 0)


async def test_steal_is_capped_at_the_targets_cash(sessions) -> None:
    await _configure(sessions, STEAL, reward=(900, 900))
    await _grant(sessions, BOB, 200)
    result = await _try(sessions, STEAL, target=BOB)
    assert result.outcome.capped
    assert await _balances(sessions, BOB) == (0, 0)
    assert await _balances(sessions, ALICE) == (200, 0)


async def test_steal_conserves_currency(sessions) -> None:
    """Unlike work, stealing must not create money."""
    await _configure(sessions, STEAL, reward=(400, 400))
    await _grant(sessions, BOB, 1_000)
    await _try(sessions, STEAL, target=BOB)
    alice = await _balances(sessions, ALICE)
    bob = await _balances(sessions, BOB)
    assert sum(alice) + sum(bob) == 1_000


async def test_stealing_from_yourself_is_refused(sessions) -> None:
    await _configure(sessions, STEAL)
    with pytest.raises(ActivityError, match="from yourself"):
        await _try(sessions, STEAL, target=ALICE)


async def test_steal_writes_both_sides_to_the_ledger(sessions) -> None:
    await _configure(sessions, STEAL, reward=(250, 250))
    await _grant(sessions, BOB, 1_000)
    await _try(sessions, STEAL, target=BOB)

    async with sessions() as session:
        entries = (
            (await session.execute(select(LedgerEntry).where(LedgerEntry.category.like("steal%"))))
            .scalars()
            .all()
        )
    assert len(entries) == 2
    assert len({e.correlation_id for e in entries}) == 1
    assert sum(e.cash_delta for e in entries) == 0


async def test_concurrent_attempts_respect_the_cooldown(sessions) -> None:
    """Ten simultaneous /work must succeed once; the other nine hit the cooldown.

    Assert on the *count of successes*, not the final balance. Without the row
    lock all ten read "no cooldown", each computes 0 + 100, and PostgreSQL
    serialises the UPDATEs so the last write still lands on 100 — the balance is
    correct by accident while ten members were each told they earned a wage.
    Verified by removing `with_for_update()` and watching this fail at 10 != 1.
    """
    await _configure(sessions)
    await _grant(sessions, ALICE, 0)  # pre-create; otherwise the insert serialises

    async def go() -> bool:
        try:
            await _try(sessions)
            return True
        except activity_db.OnCooldown:
            return False

    results = await asyncio.gather(*(go() for _ in range(10)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"unexpected errors: {errors}"

    assert sum(1 for r in results if r is True) == 1, "more than one attempt got through"
    assert await _balances(sessions, ALICE) == (100, 0)


async def test_reconfiguring_does_not_duplicate_the_rule(sessions) -> None:
    await _configure(sessions, reward=(100, 100))
    await _configure(sessions, reward=(900, 900))
    async with sessions() as session:
        rules = await activity_db.list_rules(session, GUILD)
    assert len(rules) == 1
    assert rules[0].success_max == 900


async def test_enable_disable_round_trip(sessions) -> None:
    await _configure(sessions)
    async with sessions.begin() as session:
        assert await activity_db.set_enabled(session, GUILD, WORK, False) is True
    with pytest.raises(ActivityError):
        await _try(sessions)
    async with sessions.begin() as session:
        assert await activity_db.set_enabled(session, GUILD, WORK, True) is True
    assert (await _try(sessions)).outcome.succeeded


async def test_enabling_an_unconfigured_activity_reports_false(sessions) -> None:
    async with sessions.begin() as session:
        assert await activity_db.set_enabled(session, GUILD, Activity.CRIME, True) is False


async def test_invalid_configuration_is_rejected_before_writing(sessions) -> None:
    with pytest.raises(ActivityError, match="below the minimum"):
        await _configure(sessions, reward=(900, 100))
    async with sessions() as session:
        assert await activity_db.list_rules(session, GUILD) == []


# --- Hungry Games -------------------------------------------------------------


async def _game(sessions, *, fee=100, seed=0, min_players=3, signup=60, round_seconds=15):
    async with sessions.begin() as session:
        return await game_db.create_game(
            session,
            GUILD,
            channel_id=1,
            created_by=ADMIN,
            entry_fee=fee,
            seeded_pot=seed,
            min_players=min_players,
            signup_seconds=signup,
            round_seconds=round_seconds,
        )


async def _join(sessions, game_id, user_id, name=""):
    async with sessions.begin() as session:
        game = await session.get(Game, game_id)
        return await game_db.join_game(session, game, user_id, display_name=name)


async def _play_out(sessions, game_id, seed=5):
    """Run a game from signup to completion. Returns the final RoundOutcome."""
    generator = random.Random(seed)
    async with sessions.begin() as session:
        g = await session.get(Game, game_id)
        await game_db.close_signups(session, g)
        for _ in range(40):
            outcome = await game_db.run_round(session, g, rng=generator)
            if outcome.finished:
                return outcome
    raise AssertionError("game never finished")


async def test_creating_a_game_opens_signups(sessions) -> None:
    game = await _game(sessions)
    assert game.status == "signup"


async def test_only_one_active_game_per_guild(sessions) -> None:
    """Enforced by a partial unique index, not just by this check."""
    await _game(sessions)
    with pytest.raises(game_db.GameError, match="already open"):
        await _game(sessions)


async def test_a_seeded_pot_is_audited_as_currency_creation(sessions) -> None:
    """It comes from nowhere, so it must appear in the audit trail."""
    await _game(sessions, seed=5_000)
    async with sessions() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.event_type == "currency_created")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].details["amount"] == 5_000
    assert events[0].details["reason"] == "hungrygames_seed"


async def test_joining_takes_the_entry_fee(sessions) -> None:
    game = await _game(sessions, fee=250)
    await _grant(sessions, ALICE, 1_000)
    await _join(sessions, game.id, ALICE)
    assert await _balances(sessions, ALICE) == (750, 0)


async def test_cannot_join_without_the_fee(sessions) -> None:
    game = await _game(sessions, fee=250)
    await _grant(sessions, ALICE, 100)
    with pytest.raises(BankingError, match="entry fee"):
        await _join(sessions, game.id, ALICE)


async def test_cannot_join_twice(sessions) -> None:
    game = await _game(sessions, fee=0)
    await _join(sessions, game.id, ALICE)
    with pytest.raises(game_db.GameError, match="already entered"):
        await _join(sessions, game.id, ALICE)


async def test_pot_is_seed_plus_every_fee(sessions) -> None:
    game = await _game(sessions, fee=100, seed=500)
    for user in (ALICE, BOB):
        await _grant(sessions, user, 1_000)
        await _join(sessions, game.id, user)
    async with sessions() as session:
        g = await session.get(Game, game.id)
        assert await game_db.pot_for(session, g) == 700


async def test_too_few_tributes_cancels_and_refunds(sessions) -> None:
    game = await _game(sessions, fee=300, min_players=3)
    for user in (ALICE, BOB):
        await _grant(sessions, user, 1_000)
        await _join(sessions, game.id, user)

    async with sessions.begin() as session:
        g = await session.get(Game, game.id)
        started = await game_db.close_signups(session, g)
    assert started is False

    assert await _balances(sessions, ALICE) == (1_000, 0), "entry fee refunded"
    assert await _balances(sessions, BOB) == (1_000, 0)
    async with sessions() as session:
        assert (await session.get(Game, game.id)).status == "cancelled"


async def test_enough_tributes_starts_the_game(sessions) -> None:
    game = await _game(sessions, fee=0, min_players=3)
    for user in (ALICE, BOB, CAROL):
        await _join(sessions, game.id, user)
    async with sessions.begin() as session:
        g = await session.get(Game, game.id)
        assert await game_db.close_signups(session, g) is True
        assert g.status == "running"
        assert g.next_tick_at is not None


async def test_a_game_runs_to_a_single_winner_and_pays_the_pot(sessions) -> None:
    game = await _game(sessions, fee=100, seed=400, min_players=3)
    players = [ALICE, BOB, ADMIN]
    for user in players:
        await _grant(sessions, user, 1_000)
        await _join(sessions, game.id, user)

    async with sessions.begin() as session:
        g = await session.get(Game, game.id)
        await game_db.close_signups(session, g)

    generator = random.Random(7)
    outcome = None
    for _ in range(50):
        async with sessions.begin() as session:
            g = await session.get(Game, game.id)
            if g.status != "running":
                break
            outcome = await game_db.run_round(session, g, rng=generator)
            if outcome.finished:
                break
    assert outcome is not None and outcome.finished
    assert outcome.winner_user_id in players
    assert outcome.pot == 700  # 400 seed + 3 x 100

    total = 0
    for user in players:
        cash, _ = await _balances(sessions, user)
        total += cash
    # Each started with 1000, each paid 100, the 400 seed entered circulation.
    assert total == 3 * 1_000 - 3 * 100 + 700


async def test_entry_fees_are_redistributive_not_inflationary(sessions) -> None:
    """With no seed, a game must not change the total currency in circulation."""
    game = await _game(sessions, fee=250, seed=0, min_players=3)
    players = [ALICE, BOB, ADMIN]
    for user in players:
        await _grant(sessions, user, 1_000)
        await _join(sessions, game.id, user)

    async with sessions.begin() as session:
        g = await session.get(Game, game.id)
        await game_db.close_signups(session, g)

    generator = random.Random(3)
    for _ in range(50):
        async with sessions.begin() as session:
            g = await session.get(Game, game.id)
            if g.status != "running":
                break
            if (await game_db.run_round(session, g, rng=generator)).finished:
                break

    total = 0
    for user in players:
        cash, _ = await _balances(sessions, user)
        total += cash
    assert total == 3_000, "a fee-funded game created or destroyed currency"


async def test_placements_are_recorded_for_everyone(sessions) -> None:
    game = await _game(sessions, fee=0, min_players=3)
    players = [ALICE, BOB, ADMIN]
    for user in players:
        await _join(sessions, game.id, user)
    async with sessions.begin() as session:
        g = await session.get(Game, game.id)
        await game_db.close_signups(session, g)

    generator = random.Random(11)
    for _ in range(50):
        async with sessions.begin() as session:
            g = await session.get(Game, game.id)
            if g.status != "running":
                break
            if (await game_db.run_round(session, g, rng=generator)).finished:
                break

    async with sessions() as session:
        rows = await game_db.participants(session, game.id)
    places = sorted(p.placement for p in rows)
    assert places == [1, 2, 3], f"expected a full ranking, got {places}"


async def test_due_games_finds_work(sessions) -> None:
    game = await _game(sessions, fee=0, signup=0)
    async with sessions.begin() as session:
        due = await game_db.due_games(session)
    assert [g.id for g in due] == [game.id]


async def test_due_games_ignores_games_that_are_not_ready(sessions) -> None:
    await _game(sessions, fee=0, signup=3_600)
    async with sessions.begin() as session:
        assert await game_db.due_games(session) == []


async def test_a_completed_game_frees_the_guild_for_another(sessions) -> None:
    game = await _game(sessions, fee=0, min_players=3)
    for user in (ALICE, BOB):
        await _join(sessions, game.id, user)
    async with sessions.begin() as session:
        g = await session.get(Game, game.id)
        await game_db.close_signups(session, g)
    generator = random.Random(5)
    for _ in range(50):
        async with sessions.begin() as session:
            g = await session.get(Game, game.id)
            if g.status != "running":
                break
            if (await game_db.run_round(session, g, rng=generator)).finished:
                break
    # The partial unique index only covers signup/running, so this must succeed.
    second = await _game(sessions, fee=0)
    assert second.status == "signup"


# --- Game numbering, the event log, and departures -----------------------------
#
# Numbers exist to be a stable public record ("Game #12"), so the properties that
# matter are that they are contiguous and that they only ever describe a game
# that actually finished.


async def test_a_finished_game_gets_number_one(sessions) -> None:
    game = await _game(sessions, fee=0)
    for user in (ALICE, BOB, CAROL):
        await _join(sessions, game.id, user)
    outcome = await _play_out(sessions, game.id)
    assert outcome.game_number == 1
    async with sessions() as session:
        assert (await session.get(Game, game.id)).game_number == 1


async def test_numbers_are_contiguous_across_games(sessions) -> None:
    for expected in (1, 2, 3):
        game = await _game(sessions, fee=0)
        for user in (ALICE, BOB, CAROL):
            await _join(sessions, game.id, user)
        assert (await _play_out(sessions, game.id)).game_number == expected


async def test_a_cancelled_game_burns_no_number(sessions) -> None:
    """The whole reason numbers are assigned at completion rather than at start.

    A game that was called off did not happen as far as the record is concerned,
    so the next real game must still be #2 and not #3.
    """
    first = await _game(sessions, fee=0)
    for user in (ALICE, BOB, CAROL):
        await _join(sessions, first.id, user)
    assert (await _play_out(sessions, first.id)).game_number == 1

    abandoned = await _game(sessions, fee=0)
    await _join(sessions, abandoned.id, ALICE)
    async with sessions.begin() as session:
        g = await session.get(Game, abandoned.id)
        assert await game_db.close_signups(session, g) is False  # too few tributes

    async with sessions() as session:
        cancelled = await session.get(Game, abandoned.id)
        assert cancelled.status == "cancelled"
        assert cancelled.game_number is None

    third = await _game(sessions, fee=0)
    for user in (ALICE, BOB, CAROL):
        await _join(sessions, third.id, user)
    assert (await _play_out(sessions, third.id)).game_number == 2


async def test_a_game_below_the_floor_cannot_even_be_created(sessions) -> None:
    with pytest.raises(game_db.GameError, match="at least 3"):
        await _game(sessions, min_players=2)


async def test_every_tribute_gets_an_event_in_every_round(sessions) -> None:
    """Nobody is silently skipped, so the narration accounts for the full field."""
    game = await _game(sessions, fee=0)
    for user in (ALICE, BOB, CAROL):
        await _join(sessions, game.id, user)
    outcome = await _play_out(sessions, game.id)

    async with sessions() as session:
        rows = list(
            (await session.execute(select(GameEvent).where(GameEvent.game_id == game.id)))
            .scalars()
            .all()
        )
    for round_number in range(1, outcome.round_number + 1):
        in_round = [r for r in rows if r.round_number == round_number]
        subjects = {r.subject_participant_id for r in in_round}
        victims = {r.victim_participant_id for r in in_round if r.victim_participant_id}
        # A kill covers two people in one row, so subjects + victims is the count.
        assert len(subjects | victims) == len(subjects) + len(victims)


async def test_kills_carry_a_victim_and_deaths_do_not(sessions) -> None:
    """Enforced by a CHECK constraint too — this proves the writer honours it."""
    game = await _game(sessions, fee=0)
    for user in (ALICE, BOB, CAROL):
        await _join(sessions, game.id, user)
    await _play_out(sessions, game.id)

    async with sessions() as session:
        rows = list(
            (await session.execute(select(GameEvent).where(GameEvent.game_id == game.id)))
            .scalars()
            .all()
        )
    assert rows, "a completed game must have written events"
    for row in rows:
        if row.kind == "kill":
            assert row.victim_participant_id is not None
        else:
            assert row.victim_participant_id is None


async def test_a_departing_member_is_anonymised_not_erased(sessions) -> None:
    """The decision that makes game history durable.

    Deleting these rows would rewrite games that already finished. Keeping them
    with the identifiers stripped preserves the shape of the game while retaining
    nothing that points back at the person.
    """
    game = await _game(sessions, fee=0)
    for user, name in ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")):
        await _join(sessions, game.id, user, name=name)
    await _play_out(sessions, game.id)

    async with sessions() as session:
        before = len(
            list(
                (await session.execute(select(GameEvent).where(GameEvent.game_id == game.id)))
                .scalars()
                .all()
            )
        )

    async with sessions.begin() as session:
        assert await game_db.anonymise_participants(session, GUILD, ALICE) == 1

    async with sessions() as session:
        rows = list(
            (
                await session.execute(
                    select(GameParticipant).where(GameParticipant.game_id == game.id)
                )
            )
            .scalars()
            .all()
        )
        after = len(
            list(
                (await session.execute(select(GameEvent).where(GameEvent.game_id == game.id)))
                .scalars()
                .all()
            )
        )

    assert after == before, "history must survive the departure intact"
    assert len(rows) == 3, "the tribute is kept, not deleted"

    departed = [r for r in rows if r.display_name == game_db.TOMBSTONE]
    assert len(departed) == 1
    assert departed[0].user_id is None, "no identifier may survive"
    assert departed[0].account_id is None
    # The other two are untouched.
    assert {r.display_name for r in rows if r.user_id is not None} == {"Bob", "Carol"}


async def test_anonymising_leaves_other_guilds_alone(sessions) -> None:
    """Guild isolation is a core claim; a departure is a per-guild event."""
    game = await _game(sessions, fee=0)
    for user in (ALICE, BOB, CAROL):
        await _join(sessions, game.id, user)
    await _play_out(sessions, game.id)
    async with sessions.begin() as session:
        assert await game_db.anonymise_participants(session, GUILD + 1, ALICE) == 0


async def test_leaving_deletes_the_economy_but_keeps_the_game(sessions) -> None:
    """The full departure path, exercised end to end.

    The balance must be gone and the game must still read correctly. Ordering is
    the subtle part: anonymising has to happen before the account is deleted.
    """
    from shadow_bot.db import member_cleanup

    game = await _game(sessions, fee=0)
    for user, name in ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")):
        await _join(sessions, game.id, user, name=name)
    await _play_out(sessions, game.id)

    async with sessions.begin() as session:
        assert await member_cleanup.delete_member_economy(session, GUILD, ALICE) == 1

    async with sessions() as session:
        gone = (
            await session.execute(
                select(EconomyAccount).where(
                    EconomyAccount.guild_id == GUILD, EconomyAccount.user_id == ALICE
                )
            )
        ).scalar_one_or_none()
        rows = list(
            (
                await session.execute(
                    select(GameParticipant).where(GameParticipant.game_id == game.id)
                )
            )
            .scalars()
            .all()
        )

    assert gone is None, "the account itself must be deleted"
    assert len(rows) == 3, "the game must still have three tributes"
    assert sum(1 for r in rows if r.user_id is None) == 1
    assert all(r.user_id != ALICE for r in rows), "no identifier may survive"


# --- Leaderboards --------------------------------------------------------------


async def _finished_game(sessions, names):
    game = await _game(sessions, fee=0)
    for user, name in names:
        await _join(sessions, game.id, user, name=name)
    return await _play_out(sessions, game.id)


async def test_leaderboard_counts_only_completed_games(sessions) -> None:
    """A cancelled game must not appear in anybody's record."""
    from shadow_bot.db import game_stats

    roster = ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol"))
    await _finished_game(sessions, roster)

    abandoned = await _game(sessions, fee=0)
    await _join(sessions, abandoned.id, ALICE, name="Alice")
    async with sessions.begin() as session:
        await game_db.close_signups(session, await session.get(Game, abandoned.id))

    async with sessions() as session:
        played = await game_stats.leaderboard(session, GUILD, "games")
    assert {row.value for row in played} == {1.0}, "the cancelled game must not count"


async def test_leaderboard_reports_a_winner(sessions) -> None:
    from shadow_bot.db import game_stats

    outcome = await _finished_game(sessions, ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")))
    async with sessions() as session:
        wins = await game_stats.leaderboard(session, GUILD, "wins")
    assert len(wins) == 1
    assert wins[0].user_id == outcome.winner_user_id
    assert wins[0].value == 1.0


async def test_win_rate_ignores_anyone_below_the_floor(sessions) -> None:
    """One game and one win is not a 100% win rate worth publishing."""
    from shadow_bot.db import game_stats

    await _finished_game(sessions, ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")))
    async with sessions() as session:
        assert await game_stats.leaderboard(session, GUILD, "win_rate") == []


async def test_kills_and_deaths_reconcile(sessions) -> None:
    """Every kill has exactly one victim, so the two boards must total the same."""
    from shadow_bot.db import game_stats

    await _finished_game(sessions, ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")))
    async with sessions() as session:
        kills = await game_stats.leaderboard(session, GUILD, "kills")
        killed = await game_stats.leaderboard(session, GUILD, "deaths_by_others")
    assert sum(r.value for r in kills) == sum(r.value for r in killed)


async def test_member_stats_add_up(sessions) -> None:
    from shadow_bot.db import game_stats

    outcome = await _finished_game(sessions, ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")))
    async with sessions() as session:
        stats = await game_stats.member_stats(session, GUILD, outcome.winner_user_id)
    assert stats.games == 1
    assert stats.wins == 1
    assert stats.best_placement == 1
    assert stats.win_rate is None, "one game is below the reporting floor"


async def test_a_departed_member_leaves_the_leaderboards(sessions) -> None:
    """Anonymised rows have no user id, so they drop out without extra filtering."""
    from shadow_bot.db import game_stats

    await _finished_game(sessions, ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")))
    async with sessions() as session:
        before = await game_stats.leaderboard(session, GUILD, "games")
    assert len(before) == 3

    async with sessions.begin() as session:
        await game_db.anonymise_participants(session, GUILD, ALICE)

    async with sessions() as session:
        after = await game_stats.leaderboard(session, GUILD, "games")
    assert len(after) == 2
    assert all(row.user_id != ALICE for row in after)


async def test_every_declared_stat_actually_runs(sessions) -> None:
    """Cheap guard against a board that raises the first time someone opens it."""
    from shadow_bot.db import game_stats

    await _finished_game(sessions, ((ALICE, "Alice"), (BOB, "Bob"), (CAROL, "Carol")))
    async with sessions() as session:
        for key in game_stats.STAT_KEYS:
            await game_stats.leaderboard(session, GUILD, key)
