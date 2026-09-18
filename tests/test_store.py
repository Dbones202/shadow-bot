import pytest

from shadow_bot.domain.store import (
    StoreError,
    apply_purchase,
    require_valid_price,
    require_valid_stock,
)


def test_apply_purchase_deducts_cash_and_stock() -> None:
    result = apply_purchase(cash=1_000, price=100, quantity=3, stock=10, owned=0)
    assert result.total_cost == 300
    assert result.remaining_cash == 700
    assert result.remaining_stock == 7


def test_apply_purchase_unlimited_stock_stays_none() -> None:
    result = apply_purchase(cash=1_000, price=100, quantity=3, stock=None, owned=0)
    assert result.remaining_stock is None


def test_apply_purchase_free_item_costs_nothing() -> None:
    result = apply_purchase(cash=0, price=0, quantity=5, stock=None, owned=0)
    assert result.total_cost == 0
    assert result.remaining_cash == 0


def test_apply_purchase_rejects_zero_or_negative_quantity() -> None:
    with pytest.raises(StoreError):
        apply_purchase(cash=1_000, price=10, quantity=0, stock=None, owned=0)
    with pytest.raises(StoreError):
        apply_purchase(cash=1_000, price=10, quantity=-1, stock=None, owned=0)


def test_apply_purchase_rejects_insufficient_cash() -> None:
    with pytest.raises(StoreError):
        apply_purchase(cash=50, price=100, quantity=1, stock=None, owned=0)


def test_apply_purchase_negative_cash_treated_as_nothing_available() -> None:
    # A member fined into debt has nothing to spend, same as domain.banking.spendable.
    with pytest.raises(StoreError):
        apply_purchase(cash=-500, price=1, quantity=1, stock=None, owned=0)


def test_apply_purchase_rejects_more_than_stock() -> None:
    with pytest.raises(StoreError):
        apply_purchase(cash=1_000, price=10, quantity=5, stock=3, owned=0)


def test_apply_purchase_out_of_stock_has_its_own_message() -> None:
    with pytest.raises(StoreError, match="out of stock"):
        apply_purchase(cash=1_000, price=10, quantity=1, stock=0, owned=0)


def test_apply_purchase_rejects_overflow_quantity() -> None:
    with pytest.raises(StoreError, match="overflow"):
        apply_purchase(cash=10**18, price=10**18, quantity=10**18, stock=None, owned=0)


def test_require_valid_price_rejects_negative() -> None:
    with pytest.raises(StoreError):
        require_valid_price(-1)
    require_valid_price(0)  # does not raise


def test_require_valid_stock_rejects_negative_but_allows_none() -> None:
    with pytest.raises(StoreError):
        require_valid_stock(-1)
    require_valid_stock(None)  # does not raise
    require_valid_stock(0)  # does not raise
