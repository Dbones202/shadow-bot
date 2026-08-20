from datetime import UTC, datetime

import pytest

from shadow_bot.domain.durations import (
    MAX_SECONDS,
    DurationError,
    format_duration,
    parse_duration,
    relative_timestamp,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30s", 30),
        ("90m", 5_400),
        ("12h", 43_200),
        ("2d", 172_800),
        ("1w", 604_800),
        ("1d12h", 129_600),
        ("1h 30m", 5_400),
        ("  2D  ", 172_800),
        ("1 day", 86_400),
        ("45 minutes", 2_700),
        ("2 hours 30 mins", 9_000),
    ],
)
def test_parses_durations(raw: str, expected: int) -> None:
    assert parse_duration(raw) == expected


def test_bare_number_is_refused_rather_than_guessed() -> None:
    """3600 could be seconds, minutes or hours — guessing wrong is invisible.

    A cooldown that is 60x too long looks like income that never arrives, and
    nobody thinks to check the unit.
    """
    with pytest.raises(DurationError, match="needs a unit"):
        parse_duration("3600")


def test_bare_number_error_suggests_the_likely_units() -> None:
    with pytest.raises(DurationError, match=r"`12m`, `12h`, or `12d`"):
        parse_duration("12")


@pytest.mark.parametrize("raw", ["", "   ", "soon", "12x", "h", "1h2x", "-5m", "1.5h"])
def test_rejects_unparseable(raw: str) -> None:
    with pytest.raises(DurationError):
        parse_duration(raw)


def test_rejects_a_repeated_unit() -> None:
    with pytest.raises(DurationError, match="repeats the same unit"):
        parse_duration("1h2h")


def test_rejects_zero() -> None:
    with pytest.raises(DurationError, match="longer than zero"):
        parse_duration("0m")


def test_rejects_beyond_a_year() -> None:
    with pytest.raises(DurationError, match="longer than a year"):
        parse_duration("400d")
    assert parse_duration("365d") == MAX_SECONDS


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (-5, "0s"),
        (30, "30s"),
        (60, "1m"),
        (5_400, "1h 30m"),
        (86_400, "1d"),
        (129_600, "1d 12h"),
        (604_800, "7d"),
    ],
)
def test_formats_durations(seconds: int, expected: str) -> None:
    assert format_duration(seconds) == expected


def test_formatting_shows_at_most_two_units() -> None:
    # 1d 1h 1m 1s — the seconds and minutes are noise at this scale.
    assert format_duration(86_400 + 3_600 + 60 + 1) == "1d 1h"


def test_parse_and_format_round_trip_for_clean_values() -> None:
    for text in ("30s", "15m", "12h", "2d", "1d 12h"):
        assert format_duration(parse_duration(text)) == text.replace("  ", " ")


def test_relative_timestamp_uses_discord_syntax() -> None:
    moment = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    assert relative_timestamp(moment) == f"<t:{int(moment.timestamp())}:R>"


def test_naive_datetimes_are_treated_as_utc() -> None:
    """Persistence is timezone-aware UTC, but a naive value must not shift."""
    aware = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    naive = datetime(2026, 8, 19, 12, 0)
    assert relative_timestamp(naive) == relative_timestamp(aware)
