from decimal import Decimal

import pytest

from shadow_bot.domain.activities import (
    Activity,
    ActivityError,
    ActivitySettings,
    describe_chance,
    resolve_activity,
    validate_settings,
)


def settings(
    key: Activity = Activity.WORK,
    *,
    enabled: bool = True,
    chance: str = "0.5",
    success: tuple[int, int] = (100, 200),
    fine: tuple[int, int] = (10, 50),
) -> ActivitySettings:
    return ActivitySettings(
        key=key,
        enabled=enabled,
        cooldown_seconds=3_600,
        success_chance=Decimal(chance),
        success_min=success[0],
        success_max=success[1],
        fine_min=fine[0],
        fine_max=fine[1],
    )


# --- Success and failure ------------------------------------------------------


def test_roll_below_chance_succeeds() -> None:
    outcome = resolve_activity(settings(), chance_roll=Decimal("0.49"), amount_roll=0.0)
    assert outcome.succeeded


def test_roll_at_the_chance_fails() -> None:
    """Strictly-below, so the boundary is not silently generous."""
    outcome = resolve_activity(settings(), chance_roll=Decimal("0.5"), amount_roll=0.0)
    assert not outcome.succeeded


def test_zero_chance_never_succeeds() -> None:
    outcome = resolve_activity(settings(chance="0"), chance_roll=Decimal("0"), amount_roll=0.0)
    assert not outcome.succeeded


def test_certain_chance_always_succeeds() -> None:
    for roll in ("0", "0.5", "0.999999"):
        outcome = resolve_activity(settings(chance="1"), chance_roll=Decimal(roll), amount_roll=0.0)
        assert outcome.succeeded, roll


def test_disabled_activity_is_refused() -> None:
    with pytest.raises(ActivityError, match="not enabled"):
        resolve_activity(settings(enabled=False), chance_roll=Decimal("0"), amount_roll=0.0)


@pytest.mark.parametrize("roll", ["-0.1", "1.5"])
def test_impossible_roll_is_rejected(roll: str) -> None:
    with pytest.raises(ActivityError, match="between 0 and 1"):
        resolve_activity(settings(), chance_roll=Decimal(roll), amount_roll=0.0)


# --- Amounts ------------------------------------------------------------------


@pytest.mark.parametrize(("roll", "expected"), [(0.0, 100), (0.5, 150), (1.0, 200), (0.25, 125)])
def test_reward_scales_across_the_configured_range(roll: float, expected: int) -> None:
    outcome = resolve_activity(settings(), chance_roll=Decimal("0"), amount_roll=roll)
    assert outcome.amount == expected


def test_failure_uses_the_fine_range_not_the_reward_range() -> None:
    outcome = resolve_activity(settings(), chance_roll=Decimal("0.9"), amount_roll=1.0)
    assert not outcome.succeeded
    assert outcome.amount == 50


def test_a_fixed_range_always_pays_the_same() -> None:
    fixed = settings(success=(500, 500))
    for roll in (0.0, 0.37, 1.0):
        assert resolve_activity(fixed, chance_roll=Decimal("0"), amount_roll=roll).amount == 500


def test_rounding_is_half_up_not_bankers() -> None:
    """Python's round() is banker's rounding: round(0.5) == 0, not 1.

    ECONOMY_SPEC.md requires half-up so payouts match what an administrator
    would work out by hand.
    """
    outcome = resolve_activity(settings(success=(0, 1)), chance_roll=Decimal("0"), amount_roll=0.5)
    assert outcome.amount == 1


def test_inverted_range_is_rejected() -> None:
    broken = settings(success=(200, 100))
    with pytest.raises(ActivityError, match="below the minimum"):
        resolve_activity(broken, chance_roll=Decimal("0"), amount_roll=0.5)


# --- Steal --------------------------------------------------------------------


def test_steal_takes_the_rolled_amount_when_the_target_can_afford_it() -> None:
    outcome = resolve_activity(
        settings(Activity.STEAL), chance_roll=Decimal("0"), amount_roll=0.0, target_cash=5_000
    )
    assert outcome.amount == 100
    assert not outcome.capped


def test_steal_is_capped_at_what_the_target_actually_has() -> None:
    outcome = resolve_activity(
        settings(Activity.STEAL), chance_roll=Decimal("0"), amount_roll=1.0, target_cash=120
    )
    assert outcome.amount == 120
    assert outcome.capped


def test_stealing_from_someone_with_nothing_takes_nothing() -> None:
    outcome = resolve_activity(
        settings(Activity.STEAL), chance_roll=Decimal("0"), amount_roll=1.0, target_cash=0
    )
    assert outcome.succeeded
    assert outcome.amount == 0
    assert outcome.target_was_empty


def test_a_target_in_debt_cannot_be_robbed_further() -> None:
    """Debt is not takeable — it would push someone down a floor they did not choose."""
    outcome = resolve_activity(
        settings(Activity.STEAL), chance_roll=Decimal("0"), amount_roll=1.0, target_cash=-800
    )
    assert outcome.amount == 0
    assert outcome.target_was_empty


def test_steal_without_a_target_is_a_programming_error() -> None:
    with pytest.raises(ActivityError, match="needs a target"):
        resolve_activity(settings(Activity.STEAL), chance_roll=Decimal("0"), amount_roll=0.0)


def test_failed_steal_does_not_consult_the_target() -> None:
    """A failed attempt fines the thief; the target's balance is irrelevant."""
    outcome = resolve_activity(
        settings(Activity.STEAL), chance_roll=Decimal("0.99"), amount_roll=0.0, target_cash=0
    )
    assert not outcome.succeeded
    assert outcome.amount == 10


def test_only_steal_targets_a_member() -> None:
    assert Activity.STEAL.targets_a_member
    for other in (Activity.WORK, Activity.CRIME, Activity.SLUT):
        assert not other.targets_a_member


# --- Configuration validation -------------------------------------------------


def test_valid_settings_pass() -> None:
    validate_settings(
        success_chance=Decimal("0.65"),
        success_min=100,
        success_max=500,
        fine_min=50,
        fine_max=200,
    )


@pytest.mark.parametrize("chance", ["-0.1", "1.01"])
def test_chance_outside_zero_to_one_is_rejected(chance: str) -> None:
    with pytest.raises(ActivityError, match="between 0% and 100%"):
        validate_settings(
            success_chance=Decimal(chance),
            success_min=1,
            success_max=2,
            fine_min=1,
            fine_max=2,
        )


def test_negative_minimums_are_rejected() -> None:
    with pytest.raises(ActivityError, match="cannot be negative"):
        validate_settings(
            success_chance=Decimal("0.5"),
            success_min=-1,
            success_max=10,
            fine_min=0,
            fine_max=1,
        )


def test_inverted_ranges_are_rejected_with_the_numbers_shown() -> None:
    with pytest.raises(ActivityError, match="500.*below the minimum.*1,000"):
        validate_settings(
            success_chance=Decimal("0.5"),
            success_min=1_000,
            success_max=500,
            fine_min=0,
            fine_max=1,
        )
    with pytest.raises(ActivityError, match="maximum fine"):
        validate_settings(
            success_chance=Decimal("0.5"),
            success_min=0,
            success_max=1,
            fine_min=100,
            fine_max=10,
        )


@pytest.mark.parametrize(
    ("chance", "expected"), [("0.5", "50%"), ("0.655", "65.5%"), ("1", "100%"), ("0", "0%")]
)
def test_chance_is_displayed_as_a_percentage(chance: str, expected: str) -> None:
    assert describe_chance(Decimal(chance)) == expected
