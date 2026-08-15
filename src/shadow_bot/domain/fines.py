from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FineResult:
    cash: int
    bank: int
    cash_taken: int
    bank_taken: int
    uncollected: int

    @property
    def collected(self) -> int:
        return self.cash_taken + self.bank_taken


def apply_fine(
    *, cash: int, bank: int, amount: int, cash_floor: int, bank_floor: int
) -> FineResult:
    """Apply a fine to cash first, then bank, without crossing either floor."""
    if amount < 0:
        raise ValueError("Fine amount cannot be negative")
    if cash < cash_floor or bank < bank_floor:
        raise ValueError("Starting balances cannot already be below their configured floors")

    cash_taken = min(amount, cash - cash_floor)
    remaining = amount - cash_taken
    bank_taken = min(remaining, bank - bank_floor)
    uncollected = remaining - bank_taken

    return FineResult(
        cash=cash - cash_taken,
        bank=bank - bank_taken,
        cash_taken=cash_taken,
        bank_taken=bank_taken,
        uncollected=uncollected,
    )
