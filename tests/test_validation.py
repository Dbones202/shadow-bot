import pytest

from shadow_bot.domain.validation import (
    MAX_NAME_LENGTH,
    MAX_SYMBOL_LENGTH,
    SettingError,
    validate_currency_name,
    validate_currency_symbol,
    validate_floor,
    validate_timezone,
)


def test_currency_name_is_whitespace_normalised() -> None:
    assert validate_currency_name("  gold   coin  ") == "gold coin"


def test_currency_name_rejects_empty() -> None:
    with pytest.raises(SettingError, match="cannot be empty"):
        validate_currency_name("   ")


def test_currency_name_length_is_bounded_by_the_column() -> None:
    assert validate_currency_name("a" * MAX_NAME_LENGTH)
    with pytest.raises(SettingError, match="or fewer"):
        validate_currency_name("a" * (MAX_NAME_LENGTH + 1))


def test_currency_name_error_names_the_field() -> None:
    with pytest.raises(SettingError, match="Plural name"):
        validate_currency_name("", field="Plural name")


@pytest.mark.parametrize(
    "symbol", ["🪙", "$", "§", "<:gold:123456789012345678>", "<a:spin:123456789012345678>"]
)
def test_accepts_reasonable_symbols(symbol: str) -> None:
    assert validate_currency_symbol(symbol) == symbol


def test_rejects_malformed_custom_emoji() -> None:
    with pytest.raises(SettingError, match="custom emoji"):
        validate_currency_symbol("<:gold:>")


def test_symbol_length_is_bounded() -> None:
    with pytest.raises(SettingError, match="or fewer"):
        validate_currency_symbol("x" * (MAX_SYMBOL_LENGTH + 1))


def test_accepts_iana_timezone() -> None:
    assert validate_timezone("America/Denver") == "America/Denver"


def test_timezone_matching_is_case_insensitive_but_returns_canonical() -> None:
    assert validate_timezone("america/denver") == "America/Denver"


def test_rejects_unknown_timezone() -> None:
    with pytest.raises(SettingError, match="not a recognised timezone"):
        validate_timezone("Middle/Earth")


def test_unknown_bare_abbreviation_gets_a_hint() -> None:
    with pytest.raises(SettingError, match="include the region"):
        validate_timezone("PDQ")


@pytest.mark.parametrize("zone", ["MST", "EST", "HST"])
def test_rejects_fixed_offset_abbreviations_despite_being_valid_iana(zone: str) -> None:
    """These are real tz database entries but never observe daylight saving.

    Accepting them would let an owner in Denver pick `MST` and have interest run
    an hour early for two thirds of the year — a bug that shows up as mistimed
    payouts rather than as a bad setting.
    """
    with pytest.raises(SettingError, match="daylight saving"):
        validate_timezone(zone)


def test_utc_remains_acceptable() -> None:
    assert validate_timezone("UTC") == "UTC"
    assert validate_timezone("utc") == "UTC"


@pytest.mark.parametrize("value", [0, -1_000, -10_000])
def test_floors_may_be_zero_or_negative(value: int) -> None:
    assert validate_floor(value, field="Cash floor") == value


def test_positive_floor_is_rejected() -> None:
    with pytest.raises(SettingError, match="zero or negative"):
        validate_floor(500, field="Cash floor")
