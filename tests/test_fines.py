import pytest

from shadow_bot.domain.fines import apply_fine


def test_fine_uses_cash_before_bank() -> None:
    result = apply_fine(cash=50, bank=500, amount=1_200, cash_floor=-1_000, bank_floor=-10_000)
    assert result.cash == -1_000
    assert result.bank == 350
    assert result.cash_taken == 1_050
    assert result.bank_taken == 150
    assert result.uncollected == 0


def test_fine_stops_at_both_floors() -> None:
    result = apply_fine(cash=-900, bank=-9_900, amount=500, cash_floor=-1_000, bank_floor=-10_000)
    assert result.cash == -1_000
    assert result.bank == -10_000
    assert result.collected == 200
    assert result.uncollected == 300


def test_negative_fine_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        apply_fine(cash=0, bank=0, amount=-1, cash_floor=-1_000, bank_floor=-10_000)
