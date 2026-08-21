"""Resolving an activity attempt — work, crime, steal, slut.

Pure logic. **Randomness is injected, never generated here**: the caller rolls
the dice and passes the results in. That is what makes a gambling mechanic
testable — every branch, boundary and rounding rule can be exercised
deterministically, and "it paid the wrong amount" becomes a failing test rather
than an argument about luck.

Every number an activity uses is configured per guild in `activity_rules`.
Nothing is hardcoded: an administrator sets the cooldown, the success chance,
the reward range and the fine range, and may change any of them later.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum


class Activity(StrEnum):
    """The configurable activities. Values are the `activity_key` column."""

    WORK = "work"
    CRIME = "crime"
    STEAL = "steal"
    SLUT = "slut"

    @property
    def targets_a_member(self) -> bool:
        """Whether this activity takes from another member rather than minting.

        Steal moves existing currency between accounts; the others create it.
        """
        return self is Activity.STEAL


class ActivityError(ValueError):
    """Raised when an activity cannot run. Message is safe to show a member."""


@dataclass(frozen=True, slots=True)
class ActivitySettings:
    """One guild's configuration for one activity.

    Mirrors `db.models.ActivityRule` without the ORM, so resolution can be
    tested without a database.
    """

    key: Activity
    enabled: bool
    cooldown_seconds: int
    success_chance: Decimal
    success_min: int
    success_max: int
    fine_min: int
    fine_max: int


@dataclass(frozen=True, slots=True)
class ActivityOutcome:
    succeeded: bool
    #: Gained on success, or the fine *attempted* on failure. What a fine
    #: actually collected depends on the member's balance and floors — see
    #: `domain.fines.apply_fine`.
    amount: int
    #: True when a steal was reduced because the target did not have enough.
    capped: bool = False
    #: True when a steal found the target with nothing worth taking.
    target_was_empty: bool = False


def _scale(low: int, high: int, roll: float) -> int:
    """Pick a value in [low, high] from a roll in [0, 1], half-up.

    ECONOMY_SPEC.md requires half-up rounding, and Python's built-in `round`
    is banker's rounding — round(0.5) is 0, not 1. Using Decimal here keeps
    payouts matching what an administrator would compute by hand.
    """
    if high < low:
        raise ActivityError("The configured maximum is below the minimum.")
    span = Decimal(high - low)
    offset = (span * Decimal(str(roll))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return low + int(offset)


def resolve_activity(
    settings: ActivitySettings,
    *,
    chance_roll: Decimal,
    amount_roll: float,
    target_cash: int | None = None,
) -> ActivityOutcome:
    """Decide what one attempt produced.

    ``chance_roll`` and ``amount_roll`` are both in [0, 1) and supplied by the
    caller. A ``chance_roll`` strictly below the configured success chance
    succeeds, so a chance of 0 never succeeds and a chance of 1 always does.

    ``target_cash`` is required for activities that take from another member and
    caps the amount — you cannot steal more than someone has.
    """
    if not settings.enabled:
        raise ActivityError("That activity is not enabled on this server.")
    if not 0 <= chance_roll <= 1:
        raise ActivityError("Success chance roll must be between 0 and 1.")

    succeeded = chance_roll < settings.success_chance

    if not succeeded:
        return ActivityOutcome(
            succeeded=False, amount=_scale(settings.fine_min, settings.fine_max, amount_roll)
        )

    amount = _scale(settings.success_min, settings.success_max, amount_roll)

    if settings.key.targets_a_member:
        if target_cash is None:
            raise ActivityError(f"{settings.key} needs a target.")
        # Only positive cash is takeable. A member already in debt has nothing
        # to lose, and taking from them would push them further down a floor
        # they did not choose.
        available = max(0, target_cash)
        if available == 0:
            return ActivityOutcome(succeeded=True, amount=0, capped=True, target_was_empty=True)
        if amount > available:
            return ActivityOutcome(succeeded=True, amount=available, capped=True)

    return ActivityOutcome(succeeded=True, amount=amount)


def describe_chance(chance: Decimal) -> str:
    """Render a success chance as a percentage for display, e.g. ``65%``."""
    percent = (chance * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{percent.normalize():f}%".replace(".0%", "%")


def validate_settings(
    *,
    success_chance: Decimal,
    success_min: int,
    success_max: int,
    fine_min: int,
    fine_max: int,
) -> None:
    """Check a proposed configuration, matching the database CHECK constraints.

    Validating here means an administrator gets an explanation rather than an
    IntegrityError surfacing as "that command could not be completed".
    """
    if not 0 <= success_chance <= 1:
        raise ActivityError("Success chance must be between 0% and 100%.")
    if success_min < 0 or fine_min < 0:
        raise ActivityError("Minimum amounts cannot be negative.")
    if success_max < success_min:
        raise ActivityError(
            f"The maximum reward ({success_max:,}) is below the minimum ({success_min:,})."
        )
    if fine_max < fine_min:
        raise ActivityError(f"The maximum fine ({fine_max:,}) is below the minimum ({fine_min:,}).")
