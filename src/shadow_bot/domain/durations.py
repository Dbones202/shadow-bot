"""Parsing and formatting of time spans.

Cooldowns are stored as whole seconds (`cooldown_seconds`), but nobody wants to
type `86400`. These functions translate between the two, and are pure so the
awkward cases — compound spans, rounding, plural units — can be tested directly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

#: `cooldown_seconds` is a PostgreSQL ``INTEGER``, so the hard ceiling is about
#: 68 years. A far lower bound is more useful: a cooldown over a year is
#: essentially always a typo (someone meaning `7d` and typing `7y`), and
#: rejecting it costs nothing while catching a mistake that would otherwise look
#: like income silently never becoming available.
MAX_SECONDS = 365 * 24 * 3_600

_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3_600,
    "hr": 3_600,
    "hrs": 3_600,
    "hour": 3_600,
    "hours": 3_600,
    "d": 86_400,
    "day": 86_400,
    "days": 86_400,
    "w": 604_800,
    "week": 604_800,
    "weeks": 604_800,
}

# One number followed by one unit. A whole input is a run of these, so `1d12h`
# and `1d 12h` both work.
_TERM = re.compile(r"(\d+)\s*([a-z]+)")


class DurationError(ValueError):
    """Raised when a time span cannot be read. Message is safe to show a user."""


def parse_duration(raw: str) -> int:
    """Read a human time span into whole seconds.

    Accepts single terms (``30s``, ``90m``, ``12h``, ``2d``, ``1w``) and
    compounds (``1d12h``, ``1h 30m``). A bare number is rejected rather than
    guessed at — ``3600`` could plausibly mean seconds, minutes, or hours, and
    silently choosing wrong produces a cooldown nobody notices is incorrect
    until members complain.
    """
    text = raw.strip().lower()
    if not text:
        raise DurationError("Enter a duration, such as `12h` or `1d`.")

    if text.isdigit():
        raise DurationError(
            f"`{raw.strip()}` needs a unit — did you mean `{text}m`, `{text}h`, or `{text}d`?"
        )

    terms = _TERM.findall(text)
    if not terms or _TERM.sub("", text).strip():
        raise DurationError(
            f"`{raw.strip()}` is not a duration I understand. Try `30m`, `12h`, `2d`, or `1d12h`."
        )

    total = 0
    seen: set[str] = set()
    for value, unit in terms:
        if unit not in _UNITS:
            raise DurationError(f"`{unit}` is not a unit I know. Use s, m, h, d, or w.")
        canonical = str(_UNITS[unit])
        if canonical in seen:
            raise DurationError(f"`{raw.strip()}` repeats the same unit twice.")
        seen.add(canonical)
        total += int(value) * _UNITS[unit]

    if total <= 0:
        raise DurationError("A cooldown must be longer than zero.")
    if total > MAX_SECONDS:
        raise DurationError(
            f"`{raw.strip()}` is longer than a year. If that is really what you want, "
            "the maximum is `365d`."
        )
    return total


def format_duration(seconds: int) -> str:
    """Render whole seconds as a short human span, e.g. ``1d 12h``.

    Shows at most the two largest non-zero units: "1d 12h" is more useful at a
    glance than "1d 12h 0m 0s".
    """
    if seconds <= 0:
        return "0s"

    parts: list[str] = []
    for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts[:2])


def relative_timestamp(moment: datetime) -> str:
    """Format an instant as a Discord relative timestamp, e.g. ``<t:1699999999:R>``.

    Discord renders these in each viewer's own timezone and language, which is
    why ECONOMY_SPEC.md prefers them over formatting a date server-side — a
    guild's members are rarely all in the guild's configured timezone.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"<t:{int(moment.timestamp())}:R>"
