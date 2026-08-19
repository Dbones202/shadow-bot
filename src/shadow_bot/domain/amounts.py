"""Parsing and formatting of currency amounts.

Everything here is pure so it can be tested without a database or a Discord
connection. Money is always whole integers — see ECONOMY_SPEC.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: PostgreSQL BIGINT upper bound. Balances are stored as BigInteger, so any
#: single operation larger than this could not be persisted anyway. Rejecting
#: it here produces a clear message instead of a database error at commit time.
MAX_AMOUNT = 9_223_372_036_854_775_807

_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# Optional leading +, digits with , or _ separators, optional decimal part,
# optional k/m/b suffix. Anything else is rejected.
_NUMERIC = re.compile(r"^\+?(\d[\d,_]*)(\.\d+)?([kmb])?$")


class AmountError(ValueError):
    """Raised when user input cannot be read as a usable amount.

    The message is written to be shown directly to the member who typed it.
    """


@dataclass(frozen=True, slots=True)
class CurrencyStyle:
    """How one guild displays its currency.

    Kept separate from the ORM model so formatting can be tested directly and
    reused anywhere a full ``GuildSettings`` row is not on hand.
    """

    symbol: str
    singular: str
    plural: str

    @classmethod
    def from_settings(cls, settings: object) -> CurrencyStyle:
        return cls(
            symbol=getattr(settings, "currency_symbol", "🪙"),
            singular=getattr(settings, "currency_name", "coin"),
            plural=getattr(settings, "currency_name_plural", "coins"),
        )


def parse_amount(raw: str, *, available: int, what: str = "spend") -> int:
    """Read a member-supplied amount, bounded by what they actually have.

    Accepts plain integers, thousands separators (``1,000`` / ``1_000``),
    magnitude suffixes (``5k``, ``2.5m``), and the keywords ``all``/``max``
    and ``half``.

    ``available`` is the ceiling — the caller decides whether that means cash,
    bank, or something else. ``what`` appears in error messages, so pass a verb
    that reads naturally, e.g. ``"deposit"``.

    Raises:
        AmountError: with a message suitable for showing to the member.
    """
    text = raw.strip().lower().replace(" ", "")
    if not text:
        raise AmountError("Enter an amount, or `all`.")

    if available <= 0:
        raise AmountError(f"You have nothing to {what}.")

    if text in {"all", "max", "everything"}:
        return available

    if text in {"half", "1/2"}:
        half = available // 2
        if half <= 0:
            raise AmountError(f"Half of your balance rounds down to nothing to {what}.")
        return half

    match = _NUMERIC.match(text)
    if not match:
        raise AmountError(
            f"`{raw.strip()}` is not an amount I understand. "
            "Try a number like `250`, a shorthand like `2.5k`, or `all`."
        )

    whole, fraction, suffix = match.groups()
    try:
        value = Decimal(whole.replace(",", "").replace("_", "") + (fraction or ""))
    except InvalidOperation as exc:  # pragma: no cover - regex should prevent this
        raise AmountError(f"`{raw.strip()}` is not a valid number.") from exc

    if suffix:
        value *= _SUFFIXES[suffix]

    if value != value.to_integral_value():
        raise AmountError(f"Amounts must be whole numbers. `{raw.strip()}` works out to {value}.")

    amount = int(value)

    if amount <= 0:
        raise AmountError("Enter an amount greater than zero.")
    if amount > MAX_AMOUNT:
        raise AmountError("That amount is too large.")
    if amount > available:
        raise AmountError(
            f"You only have {available:,} available to {what} — you asked for {amount:,}."
        )

    return amount


def format_money(amount: int, style: CurrencyStyle) -> str:
    """Render an amount the way a guild has configured its currency.

    Pluralisation follows the magnitude, so -1 reads as "-1 coin" rather than
    "-1 coins".
    """
    noun = style.singular if abs(amount) == 1 else style.plural
    return f"{style.symbol} {amount:,} {noun}"
