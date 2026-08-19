"""Database operations for member balances.

Every function that changes money takes an ``AsyncSession`` already inside a
transaction (use ``async with database.sessions.begin()``) and locks the rows it
touches with ``SELECT ... FOR UPDATE``. Without that lock two commands running
concurrently can both read the same balance and the second write silently
discards the first — the classic way an economy bot leaks currency.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.models import AuditEvent, EconomyAccount, GuildSettings, LedgerEntry
from shadow_bot.domain.banking import (
    apply_deposit,
    apply_grant,
    apply_payment,
    apply_removal,
    apply_withdrawal,
)


class EconomyNotConfigured(RuntimeError):
    """Raised when a guild has no settings row yet.

    Accounts carry a foreign key to ``guild_settings``, so nothing can be
    created until an owner has run ``/setup``.
    """


async def get_settings(session: AsyncSession, guild_id: int) -> GuildSettings | None:
    return await session.get(GuildSettings, guild_id)


async def require_settings(session: AsyncSession, guild_id: int) -> GuildSettings:
    settings = await get_settings(session, guild_id)
    if settings is None:
        raise EconomyNotConfigured(guild_id)
    return settings


async def get_or_create_account(
    session: AsyncSession, guild_id: int, user_id: int, *, lock: bool = False
) -> EconomyAccount:
    """Fetch a member's account, creating a zero-balance one if needed.

    The insert uses ``ON CONFLICT DO NOTHING`` rather than "check then insert"
    because two commands from the same member can arrive at once; the unique
    constraint on ``(guild_id, user_id)`` decides the winner and both callers
    then read the same row.

    Set ``lock=True`` when the caller intends to modify the balance.
    """
    await session.execute(
        pg_insert(EconomyAccount)
        .values(id=uuid.uuid4(), guild_id=guild_id, user_id=user_id, cash=0, bank=0)
        .on_conflict_do_nothing(index_elements=["guild_id", "user_id"])
    )

    stmt = select(EconomyAccount).where(
        EconomyAccount.guild_id == guild_id,
        EconomyAccount.user_id == user_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    account = (await session.execute(stmt)).scalar_one()
    return account


async def _lock_accounts(
    session: AsyncSession, guild_id: int, user_ids: Sequence[int]
) -> dict[int, EconomyAccount]:
    """Lock several accounts at once, in a consistent order.

    Ordering by ``user_id`` matters: if one transfer locked A then B while
    another locked B then A, the two would deadlock. A single ordered statement
    means every caller acquires locks in the same sequence.
    """
    for user_id in sorted(set(user_ids)):
        await get_or_create_account(session, guild_id, user_id)

    rows = (
        await session.execute(
            select(EconomyAccount)
            .where(
                EconomyAccount.guild_id == guild_id,
                EconomyAccount.user_id.in_(list(user_ids)),
            )
            .order_by(EconomyAccount.user_id)
            .with_for_update()
        )
    ).scalars()
    return {account.user_id: account for account in rows}


def _ledger(
    *,
    guild_id: int,
    correlation_id: uuid.UUID,
    subject: EconomyAccount,
    category: str,
    cash_delta: int = 0,
    bank_delta: int = 0,
    actor: EconomyAccount | None = None,
    counterparty: EconomyAccount | None = None,
    amount: int | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        correlation_id=correlation_id,
        guild_id=guild_id,
        subject_account_id=subject.id,
        actor_account_id=(actor or subject).id,
        counterparty_account_id=counterparty.id if counterparty else None,
        category=category,
        cash_delta=cash_delta,
        bank_delta=bank_delta,
        attempted_amount=amount,
        applied_amount=amount,
        details={},
    )


async def deposit(
    session: AsyncSession, guild_id: int, user_id: int, amount: int
) -> EconomyAccount:
    """Move cash into the bank, recording a ledger entry."""
    account = await get_or_create_account(session, guild_id, user_id, lock=True)
    account.cash, account.bank = apply_deposit(cash=account.cash, bank=account.bank, amount=amount)
    session.add(
        _ledger(
            guild_id=guild_id,
            correlation_id=uuid.uuid4(),
            subject=account,
            category="deposit",
            cash_delta=-amount,
            bank_delta=amount,
            amount=amount,
        )
    )
    return account


async def withdraw(
    session: AsyncSession, guild_id: int, user_id: int, amount: int
) -> EconomyAccount:
    """Move banked funds into cash, recording a ledger entry."""
    account = await get_or_create_account(session, guild_id, user_id, lock=True)
    account.cash, account.bank = apply_withdrawal(
        cash=account.cash, bank=account.bank, amount=amount
    )
    session.add(
        _ledger(
            guild_id=guild_id,
            correlation_id=uuid.uuid4(),
            subject=account,
            category="withdraw",
            cash_delta=amount,
            bank_delta=-amount,
            amount=amount,
        )
    )
    return account


async def pay(
    session: AsyncSession, guild_id: int, sender_id: int, recipient_id: int, amount: int
) -> tuple[EconomyAccount, EconomyAccount]:
    """Transfer cash between two members.

    Writes two ledger entries sharing one ``correlation_id`` — one from each
    member's point of view — so either side of the transfer can be found by
    querying that member's own entries, while the pair remains linkable.
    """
    if sender_id == recipient_id:
        raise ValueError("Cannot pay yourself")

    accounts = await _lock_accounts(session, guild_id, [sender_id, recipient_id])
    sender, recipient = accounts[sender_id], accounts[recipient_id]

    sender.cash, recipient.cash = apply_payment(
        sender_cash=sender.cash, recipient_cash=recipient.cash, amount=amount
    )

    correlation_id = uuid.uuid4()
    session.add_all(
        [
            _ledger(
                guild_id=guild_id,
                correlation_id=correlation_id,
                subject=sender,
                category="pay_sent",
                cash_delta=-amount,
                counterparty=recipient,
                amount=amount,
            ),
            _ledger(
                guild_id=guild_id,
                correlation_id=correlation_id,
                subject=recipient,
                category="pay_received",
                cash_delta=amount,
                actor=sender,
                counterparty=sender,
                amount=amount,
            ),
        ]
    )
    return sender, recipient


# --- Administrative currency operations ----------------------------------------


async def find_account(session: AsyncSession, guild_id: int, user_id: int) -> EconomyAccount | None:
    """Look up an account without creating one.

    Used for the *actor* on audit records: an application owner acting in a
    support capacity may not be a member of the guild at all, and inventing an
    account for them would put a phantom member in the economy.
    """
    return (
        await session.execute(
            select(EconomyAccount).where(
                EconomyAccount.guild_id == guild_id,
                EconomyAccount.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


def _audit(
    *,
    guild_id: int,
    event_type: str,
    succeeded: bool,
    subject: EconomyAccount | None,
    actor: EconomyAccount | None,
    details: dict[str, object],
) -> AuditEvent:
    return AuditEvent(
        guild_id=guild_id,
        actor_account_id=actor.id if actor else None,
        subject_account_id=subject.id if subject else None,
        event_type=event_type,
        succeeded=succeeded,
        details=details,
    )


async def grant_currency(
    session: AsyncSession,
    guild_id: int,
    *,
    actor_id: int,
    target_id: int,
    amount: int,
    destination: str = "cash",
    authority: str = "guild_owner",
) -> EconomyAccount:
    """Create currency into a member's cash or bank.

    This is one of only two ways money enters a guild's economy, so it writes
    both a ledger entry (the movement) and an audit event (who authorised it).
    """
    if destination not in {"cash", "bank"}:
        raise ValueError(f"destination must be cash or bank, not {destination!r}")

    account = await get_or_create_account(session, guild_id, target_id, lock=True)
    actor = await find_account(session, guild_id, actor_id)

    setattr(account, destination, apply_grant(balance=getattr(account, destination), amount=amount))

    session.add(
        _ledger(
            guild_id=guild_id,
            correlation_id=uuid.uuid4(),
            subject=account,
            category="admin_grant",
            cash_delta=amount if destination == "cash" else 0,
            bank_delta=amount if destination == "bank" else 0,
            actor=actor or account,
            amount=amount,
        )
    )
    session.add(
        _audit(
            guild_id=guild_id,
            event_type="currency_created",
            succeeded=True,
            subject=account,
            actor=actor,
            details={
                "amount": amount,
                "destination": destination,
                "actor_user_id": str(actor_id),
                "authority": authority,
            },
        )
    )
    return account


async def remove_currency(
    session: AsyncSession,
    guild_id: int,
    *,
    actor_id: int,
    target_id: int,
    amount: int,
    source: str = "cash",
    floor: int,
    authority: str = "guild_owner",
) -> tuple[EconomyAccount, int, int]:
    """Remove currency from a member, stopping at the configured floor.

    Returns ``(account, removed, uncollected)``. A partial removal is still a
    success — the audit record carries both figures so the shortfall is visible
    rather than inferred from a balance that moved less than expected.
    """
    if source not in {"cash", "bank"}:
        raise ValueError(f"source must be cash or bank, not {source!r}")

    account = await get_or_create_account(session, guild_id, target_id, lock=True)
    actor = await find_account(session, guild_id, actor_id)

    new_balance, removed, uncollected = apply_removal(
        balance=getattr(account, source), amount=amount, floor=floor
    )
    setattr(account, source, new_balance)

    if removed:
        session.add(
            _ledger(
                guild_id=guild_id,
                correlation_id=uuid.uuid4(),
                subject=account,
                category="admin_remove",
                cash_delta=-removed if source == "cash" else 0,
                bank_delta=-removed if source == "bank" else 0,
                actor=actor or account,
                amount=removed,
            )
        )
    session.add(
        _audit(
            guild_id=guild_id,
            event_type="currency_removed",
            succeeded=True,
            subject=account,
            actor=actor,
            details={
                "attempted": amount,
                "removed": removed,
                "uncollected": uncollected,
                "source": source,
                "floor": floor,
                "actor_user_id": str(actor_id),
                "authority": authority,
            },
        )
    )
    return account, removed, uncollected
