import pytest

from shadow_bot.domain.amounts import MAX_AMOUNT
from shadow_bot.domain.banking import (
    BankingError,
    apply_deposit,
    apply_grant,
    apply_payment,
    apply_removal,
    apply_withdrawal,
    spendable,
)


def test_spendable_ignores_debt() -> None:
    assert spendable(500) == 500
    assert spendable(0) == 0
    assert spendable(-1_000) == 0


def test_deposit_moves_cash_to_bank() -> None:
    assert apply_deposit(cash=500, bank=100, amount=300) == (200, 400)


def test_withdrawal_moves_bank_to_cash() -> None:
    assert apply_withdrawal(cash=500, bank=100, amount=100) == (600, 0)


def test_payment_moves_cash_between_members() -> None:
    assert apply_payment(sender_cash=500, recipient_cash=10, amount=250) == (250, 260)


def test_cannot_deposit_more_cash_than_held() -> None:
    with pytest.raises(BankingError, match="that much cash"):
        apply_deposit(cash=100, bank=0, amount=101)


def test_cannot_withdraw_more_than_banked() -> None:
    with pytest.raises(BankingError, match="that much banked"):
        apply_withdrawal(cash=0, bank=100, amount=101)


def test_cannot_pay_more_cash_than_held() -> None:
    with pytest.raises(BankingError, match="that much cash"):
        apply_payment(sender_cash=100, recipient_cash=0, amount=101)


def test_debt_cannot_be_used_as_a_credit_line() -> None:
    """A fined member has nothing to move, even though a floor allows further debt.

    Floors exist so fines can collect; they are not an overdraft members may
    draw on themselves.
    """
    with pytest.raises(BankingError):
        apply_deposit(cash=-500, bank=0, amount=1)
    with pytest.raises(BankingError):
        apply_withdrawal(cash=0, bank=-500, amount=1)
    with pytest.raises(BankingError):
        apply_payment(sender_cash=-500, recipient_cash=0, amount=1)


def test_a_member_in_debt_can_still_receive() -> None:
    assert apply_payment(sender_cash=100, recipient_cash=-900, amount=100) == (0, -800)


@pytest.mark.parametrize("amount", [0, -1])
def test_rejects_non_positive_amounts(amount: int) -> None:
    with pytest.raises(BankingError, match="greater than zero"):
        apply_deposit(cash=1_000, bank=0, amount=amount)


def test_total_is_conserved_by_a_payment() -> None:
    sender, recipient = apply_payment(sender_cash=750, recipient_cash=250, amount=400)
    assert sender + recipient == 1_000


def test_total_is_conserved_by_banking() -> None:
    cash, bank = apply_deposit(cash=900, bank=100, amount=650)
    assert cash + bank == 1_000


# --- Administrative operations ------------------------------------------------


def test_grant_adds_to_a_balance() -> None:
    assert apply_grant(balance=0, amount=500) == 500
    assert apply_grant(balance=250, amount=250) == 500


def test_grant_can_lift_an_account_out_of_debt() -> None:
    assert apply_grant(balance=-800, amount=1_000) == 200


def test_grant_refuses_to_overflow_the_column() -> None:
    """Balances are BIGINT; without this the failure would be a database error."""
    with pytest.raises(BankingError, match="maximum this bot can store"):
        apply_grant(balance=MAX_AMOUNT, amount=1)
    assert apply_grant(balance=MAX_AMOUNT - 1, amount=1) == MAX_AMOUNT


@pytest.mark.parametrize("amount", [0, -1])
def test_grant_rejects_non_positive(amount: int) -> None:
    with pytest.raises(BankingError, match="greater than zero"):
        apply_grant(balance=0, amount=amount)


def test_removal_takes_what_is_there() -> None:
    assert apply_removal(balance=500, amount=200, floor=-1_000) == (300, 200, 0)


def test_removal_may_push_a_balance_negative_to_the_floor() -> None:
    """Unlike member actions, an admin removal is allowed to create debt."""
    assert apply_removal(balance=100, amount=500, floor=-1_000) == (-400, 500, 0)


def test_removal_stops_at_the_floor_and_reports_the_shortfall() -> None:
    assert apply_removal(balance=-900, amount=500, floor=-1_000) == (-1_000, 100, 400)


def test_removal_from_an_account_already_at_the_floor_takes_nothing() -> None:
    assert apply_removal(balance=-1_000, amount=500, floor=-1_000) == (-1_000, 0, 500)


def test_removal_with_a_zero_floor_cannot_create_debt() -> None:
    assert apply_removal(balance=100, amount=500, floor=0) == (0, 100, 400)


def test_removal_rejects_a_positive_floor() -> None:
    with pytest.raises(BankingError, match="cannot be positive"):
        apply_removal(balance=100, amount=10, floor=50)


def test_removal_conserves_the_reported_amounts() -> None:
    """removed + uncollected must always equal what was attempted."""
    for balance in (-1_000, -500, 0, 250, 5_000):
        _, removed, uncollected = apply_removal(balance=balance, amount=750, floor=-1_000)
        assert removed + uncollected == 750
