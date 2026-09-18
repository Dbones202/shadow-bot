"""Pure store/inventory arithmetic — price and stock checks, no database.

Mirrors domain.banking's split: the rules that decide whether a purchase is
allowed live here and are unit tested directly; db/store.py does the actual
row locking and writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from shadow_bot.domain.amounts import MAX_AMOUNT


class StoreError(ValueError):
    """Raised when a store operation would break a rule. Safe to show a member."""


@dataclass(frozen=True, slots=True)
class Purchase:
    total_cost: int
    remaining_cash: int
    remaining_stock: int | None


def require_valid_price(price: int) -> None:
    if price < 0:
        raise StoreError("Price cannot be negative.")


def require_valid_stock(stock: int | None) -> None:
    if stock is not None and stock < 0:
        raise StoreError("Stock cannot be negative.")


def apply_purchase(
    *, cash: int, price: int, quantity: int, stock: int | None, owned: int
) -> Purchase:
    """Check and price one purchase.

    Returns the new cash/stock the caller should write, or raises
    ``StoreError`` with a message safe to show the buyer directly.

    ``owned`` is how many of this item the member already has. Unused for now
    — v1 items have no per-member cap — but threaded through so a future
    "limit N per member" rule has somewhere to read from without changing
    every caller's signature.
    """
    if quantity <= 0:
        raise StoreError("Enter a quantity greater than zero.")
    if price > 0 and quantity > MAX_AMOUNT // price:
        raise StoreError("That quantity would overflow — try a smaller amount.")

    total_cost = price * quantity
    available = max(0, cash)
    if total_cost > available:
        raise StoreError(f"That costs {total_cost:,} but you only have {available:,}.")

    if stock is not None:
        if stock <= 0:
            raise StoreError("That item is out of stock.")
        if quantity > stock:
            raise StoreError(f"Only {stock:,} left in stock — you asked for {quantity:,}.")

    remaining_stock = None if stock is None else stock - quantity
    return Purchase(
        total_cost=total_cost, remaining_cash=cash - total_cost, remaining_stock=remaining_stock
    )
