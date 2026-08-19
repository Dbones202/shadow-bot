"""Validation for guild-configurable settings.

Pure functions so `/setup` stays a thin shell around testable rules. Each
raises `SettingError` with a message written for the guild owner who typed it.
"""

from __future__ import annotations

import re
from zoneinfo import available_timezones

#: Column widths in db.models.GuildSettings. Validating against them here means
#: a too-long value is rejected with an explanation rather than raising
#: DataError at flush time.
MAX_NAME_LENGTH = 50
MAX_SYMBOL_LENGTH = 100

#: Discord custom emoji, e.g. <:gold:12345> or animated <a:spin:12345>.
_CUSTOM_EMOJI = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")


class SettingError(ValueError):
    """Raised when a submitted setting cannot be accepted."""


def _single_line(value: str) -> str:
    return " ".join(value.split())


def validate_currency_name(value: str, *, field: str = "Currency name") -> str:
    """Check a currency noun, returning it whitespace-normalised."""
    name = _single_line(value)
    if not name:
        raise SettingError(f"{field} cannot be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise SettingError(
            f"{field} must be {MAX_NAME_LENGTH} characters or fewer (yours is {len(name)})."
        )
    return name


def validate_currency_symbol(value: str) -> str:
    """Check a currency symbol.

    Accepts anything short — a plain character, a unicode emoji, or a Discord
    custom emoji. Custom emoji only render if the bot shares a server with the
    emoji, but that is a display concern rather than a reason to reject input.
    """
    symbol = _single_line(value)
    if not symbol:
        raise SettingError("Currency symbol cannot be empty.")
    if len(symbol) > MAX_SYMBOL_LENGTH:
        raise SettingError(
            f"Currency symbol must be {MAX_SYMBOL_LENGTH} characters or fewer "
            f"(yours is {len(symbol)})."
        )
    if symbol.startswith("<") and not _CUSTOM_EMOJI.match(symbol):
        raise SettingError(
            "That looks like a custom emoji but is not formatted correctly. "
            "Paste it exactly as it appears when you type a backslash before it, "
            "e.g. `<:gold:123456789012345678>`."
        )
    return symbol


def validate_timezone(value: str) -> str:
    """Check an IANA timezone name, e.g. ``America/Denver``.

    Comparison is case-insensitive because owners type ``america/denver``, but
    the canonical spelling is returned since ZoneInfo is case-sensitive on load.

    Region/City form is *required* (with ``UTC`` as the one exception). Bare
    abbreviations like ``MST`` and ``EST`` are real entries in the tz database,
    but they are fixed offsets that never observe daylight saving — an owner in
    Denver choosing ``MST`` would be an hour out for two thirds of the year.
    Since this timezone drives interest scheduling and per-local-day limits,
    that error would surface as money arriving at the wrong time rather than as
    anything obviously wrong with the setting.
    """
    name = value.strip()
    if not name:
        raise SettingError("Timezone cannot be empty.")

    zones = available_timezones()
    canonical: str | None = None
    if name in zones:
        canonical = name
    else:
        folded = {zone.lower(): zone for zone in zones}
        canonical = folded.get(name.lower())

    if canonical is None:
        hint = ""
        if "/" not in name:
            hint = " Timezones look like `America/Denver` or `Europe/London` — include the region."
        raise SettingError(f"`{name}` is not a recognised timezone.{hint}")

    if canonical != "UTC" and "/" not in canonical:
        raise SettingError(
            f"`{canonical}` is a fixed offset that never adjusts for daylight saving, "
            "so scheduled payouts would drift by an hour for part of the year. "
            "Use the Region/City form instead, such as `America/Denver`."
        )

    return canonical


def validate_floor(value: int, *, field: str) -> int:
    """Check a balance floor.

    Floors are ceilings on debt, so they must be zero or negative — the database
    enforces this too (`cash_floor <= 0`), but catching it here explains why.
    """
    if value > 0:
        raise SettingError(
            f"{field} must be zero or negative. It is the furthest a balance may fall, "
            f"so -1000 allows a debt of 1000 and 0 allows none."
        )
    return value
