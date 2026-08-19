"""Balance arithmetic for money movement.

Pure functions, so the rules can be tested without a database.

The rule the member-facing half encodes: **members cannot voluntarily go
negative.** Balance floors exist so that fines can draw an account into debt
(see `domain.fines`); they are not a credit line a member may help themselves
to. `spendable` therefore treats a negative balance as nothing available.

Administrative operations at the bottom of this module are different: creating
and removing currency is how money enters and leaves circulation at all, so
removal is allowed to push a balance down to its configured floor.
"""

from __future__ import annotations

from shadow_bot.domain.amounts import MAX_AMOUNT


class BankingError(ValueError):
    """Raised when a balance change would break a rule. Safe to show a member."""


def spendable(balance: int) -> int:
    """How much of a balance a member may voluntarily move.

    A negative balance — someone who has been fined into debt — has nothing
    available rather than a negative amount available.
    """
    return max(0, balance)


def apply_deposit(*, cash: int, bank: int, amount: int) -> tuple[int, int]:
    """Move cash into the bank. Returns the new ``(cash, bank)``."""
    _require_positive(amount)
    if amount > spendable(cash):
        raise BankingError("You do not have that much cash to deposit.")
    return cash - amount, bank + amount


def apply_withdrawal(*, cash: int, bank: int, amount: int) -> tuple[int, int]:
    """Move banked funds into cash. Returns the new ``(cash, bank)``."""
    _require_positive(amount)
    if amount > spendable(bank):
        raise BankingError("You do not have that much banked to withdraw.")
    return cash + amount, bank - amount


def apply_payment(*, sender_cash: int, recipient_cash: int, amount: int) -> tuple[int, int]:
    """Move cash between two members. Returns the new ``(sender, recipient)`` cash.

    Payments use cash only and cannot overdraft the sender, per ECONOMY_SPEC.md.
    """
    _require_positive(amount)
    if amount > spendable(sender_cash):
        raise BankingError("You do not have that much cash to send.")
    return sender_cash - amount, recipient_cash + amount


def _require_positive(amount: int) -> None:
    if amount <= 0:
        raise BankingError("Amount must be greater than zero.")


# --- Administrative operations -------------------------------------------------
#
# Creating and removing currency is the only way money enters or leaves a guild's
# economy. These bypass the "no voluntary negative" rule because they are not
# voluntary — an owner is acting on the account, not the member.


def apply_grant(*, balance: int, amount: int) -> int:
    """Create currency into a balance. Returns the new balance.

    Guards the BIGINT ceiling. Balances are stored as PostgreSQL ``BIGINT``, so
    repeated large grants can overflow the column; without this check the
    failure would surface as a database error mid-transaction rather than as a
    message explaining what happened.
    """
    _require_positive(amount)
    if balance > MAX_AMOUNT - amount:
        raise BankingError("That grant would push the balance past the maximum this bot can store.")
    return balance + amount


def apply_removal(*, balance: int, amount: int, floor: int) -> tuple[int, int, int]:
    """Remove currency from a balance, stopping at its configured floor.

    Returns ``(new_balance, removed, uncollected)``. Mirrors the reporting shape
    of `domain.fines.apply_fine`: an owner should be told what actually came out
    and what could not, rather than the command silently doing less than asked.

    A balance already at or below its floor yields zero removed and the whole
    amount uncollected.
    """
    _require_positive(amount)
    if floor > 0:
        raise BankingError("A balance floor cannot be positive.")

    available = max(0, balance - floor)
    removed = min(amount, available)
    return balance - removed, removed, amount - removed
