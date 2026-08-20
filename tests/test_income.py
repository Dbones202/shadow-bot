from datetime import UTC, datetime, timedelta

from shadow_bot.domain.income import IncomeOpportunity, plan_collection

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def opportunity(
    role_id: int, payout: int = 100, cooldown: int = 3_600, available_at=None
) -> IncomeOpportunity:
    return IncomeOpportunity(
        role_id=role_id,
        payout=payout,
        cooldown_seconds=cooldown,
        available_at=available_at,
    )


def test_nothing_held_collects_nothing() -> None:
    plan = plan_collection([], now=NOW)
    assert plan.collected == () and plan.waiting == ()
    assert plan.total == 0
    assert not plan.collected_anything
    assert plan.next_available_at is None


def test_never_collected_role_is_immediately_eligible() -> None:
    plan = plan_collection([opportunity(1, payout=250)], now=NOW)
    assert plan.total == 250
    assert plan.collected[0].role_id == 1


def test_expired_cooldown_is_eligible() -> None:
    plan = plan_collection([opportunity(1, available_at=NOW - timedelta(seconds=1))], now=NOW)
    assert plan.collected_anything


def test_cooldown_expiring_exactly_now_is_eligible() -> None:
    """Boundary: `available_at == now` must pay, not make them wait another cycle."""
    plan = plan_collection([opportunity(1, available_at=NOW)], now=NOW)
    assert plan.collected_anything


def test_future_cooldown_waits() -> None:
    later = NOW + timedelta(hours=3)
    plan = plan_collection([opportunity(1, available_at=later)], now=NOW)
    assert not plan.collected_anything
    assert plan.waiting[0].available_at == later
    assert plan.next_available_at == later


def test_collects_every_eligible_role_at_once() -> None:
    """ECONOMY_SPEC.md: /collect takes everything currently eligible."""
    plan = plan_collection(
        [opportunity(1, payout=100), opportunity(2, payout=250), opportunity(3, payout=50)],
        now=NOW,
    )
    assert plan.total == 400
    assert len(plan.collected) == 3


def test_mixed_eligibility_splits_correctly() -> None:
    plan = plan_collection(
        [
            opportunity(1, payout=100),
            opportunity(2, payout=250, available_at=NOW + timedelta(hours=5)),
            opportunity(3, payout=75, available_at=NOW - timedelta(minutes=1)),
        ],
        now=NOW,
    )
    assert plan.total == 175
    assert {c.role_id for c in plan.collected} == {1, 3}
    assert [w.role_id for w in plan.waiting] == [2]


def test_missed_windows_do_not_accumulate() -> None:
    """A member three days late on a 12h income collects once, not six times.

    The next window is measured from the moment of collection, so ignoring an
    income does not bank up payouts.
    """
    long_overdue = NOW - timedelta(days=3)
    plan = plan_collection(
        [opportunity(1, payout=100, cooldown=43_200, available_at=long_overdue)], now=NOW
    )
    assert plan.total == 100
    assert plan.collected[0].next_available_at == NOW + timedelta(seconds=43_200)


def test_each_role_keeps_its_own_cooldown() -> None:
    plan = plan_collection(
        [opportunity(1, cooldown=3_600), opportunity(2, cooldown=86_400)], now=NOW
    )
    by_role = {c.role_id: c.next_available_at for c in plan.collected}
    assert by_role[1] == NOW + timedelta(hours=1)
    assert by_role[2] == NOW + timedelta(days=1)


def test_collected_are_ordered_by_payout_descending() -> None:
    plan = plan_collection(
        [opportunity(1, payout=10), opportunity(2, payout=900), opportunity(3, payout=100)],
        now=NOW,
    )
    assert [c.payout for c in plan.collected] == [900, 100, 10]


def test_waiting_are_ordered_soonest_first() -> None:
    plan = plan_collection(
        [
            opportunity(1, available_at=NOW + timedelta(hours=9)),
            opportunity(2, available_at=NOW + timedelta(hours=1)),
            opportunity(3, available_at=NOW + timedelta(hours=4)),
        ],
        now=NOW,
    )
    assert [w.role_id for w in plan.waiting] == [2, 3, 1]
    assert plan.next_available_at == NOW + timedelta(hours=1)


def test_zero_payout_role_is_still_collected_and_starts_its_cooldown() -> None:
    """A payout of 0 is legal (the schema allows it) and must not be skipped.

    Skipping it would leave the cooldown unset, so the role would look
    perpetually ready.
    """
    plan = plan_collection([opportunity(1, payout=0)], now=NOW)
    assert plan.collected_anything
    assert plan.total == 0
    assert plan.collected[0].next_available_at == NOW + timedelta(hours=1)


def test_total_is_the_sum_of_collected_only() -> None:
    plan = plan_collection(
        [
            opportunity(1, payout=500),
            opportunity(2, payout=9_999, available_at=NOW + timedelta(days=1)),
        ],
        now=NOW,
    )
    assert plan.total == 500
