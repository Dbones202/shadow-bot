import pytest

from shadow_bot.domain.amounts import (
    MAX_AMOUNT,
    AmountError,
    CurrencyStyle,
    format_money,
    parse_amount,
)

STYLE = CurrencyStyle(symbol="🪙", singular="coin", plural="coins")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100", 100),
        ("1,000", 1_000),
        ("1_000", 1_000),
        ("  250  ", 250),
        ("+75", 75),
        ("5k", 5_000),
        ("2.5m", 2_500_000),
        ("1.234k", 1_234),
        ("3B", 3_000_000_000),
        ("1 000", 1_000),
    ],
)
def test_parses_valid_amounts(raw: str, expected: int) -> None:
    assert parse_amount(raw, available=MAX_AMOUNT) == expected


@pytest.mark.parametrize("keyword", ["all", "ALL", "max", "everything"])
def test_all_keywords_take_the_full_balance(keyword: str) -> None:
    assert parse_amount(keyword, available=4_321) == 4_321


def test_half_rounds_down() -> None:
    assert parse_amount("half", available=99) == 49


def test_half_of_one_is_rejected_rather_than_zero() -> None:
    with pytest.raises(AmountError, match="rounds down to nothing"):
        parse_amount("half", available=1)


@pytest.mark.parametrize("raw", ["abc", "", "   ", "12abc", "1..5", "-50", "1.5.2", "$100", "1e5"])
def test_rejects_unparseable_input(raw: str) -> None:
    with pytest.raises(AmountError):
        parse_amount(raw, available=10_000)


def test_rejects_fractional_currency() -> None:
    with pytest.raises(AmountError, match="whole numbers"):
        parse_amount("1.5", available=10_000)


def test_rejects_fraction_that_survives_a_suffix() -> None:
    # 1.2345k is 1234.5 — a suffix does not make a fractional result acceptable.
    with pytest.raises(AmountError, match="whole numbers"):
        parse_amount("1.2345k", available=10_000)


def test_rejects_zero() -> None:
    with pytest.raises(AmountError, match="greater than zero"):
        parse_amount("0", available=10_000)


def test_rejects_more_than_available() -> None:
    with pytest.raises(AmountError, match="only have 500"):
        parse_amount("501", available=500)


def test_rejects_amount_beyond_bigint() -> None:
    with pytest.raises(AmountError, match="too large"):
        parse_amount(str(MAX_AMOUNT + 1), available=MAX_AMOUNT)


def test_nothing_available_reports_the_action() -> None:
    with pytest.raises(AmountError, match="nothing to deposit"):
        parse_amount("all", available=0, what="deposit")


def test_negative_balance_counts_as_nothing_available() -> None:
    with pytest.raises(AmountError, match="nothing to send"):
        parse_amount("10", available=-500, what="send")


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "🪙 0 coins"),
        (1, "🪙 1 coin"),
        (2, "🪙 2 coins"),
        (-1, "🪙 -1 coin"),
        (-1_000, "🪙 -1,000 coins"),
        (1_234_567, "🪙 1,234,567 coins"),
    ],
)
def test_formats_with_guild_currency(amount: int, expected: str) -> None:
    assert format_money(amount, STYLE) == expected


def test_formatting_uses_the_guilds_own_words() -> None:
    style = CurrencyStyle(symbol="§", singular="credit", plural="credits")
    assert format_money(1, style) == "§ 1 credit"
    assert format_money(5, style) == "§ 5 credits"
