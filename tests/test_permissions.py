from discord_economy_bot.domain.permissions import CapabilityGrant, combine_grants


def test_no_roles_means_not_allowed() -> None:
    assert not combine_grants([]).allowed


def test_most_generous_limits_win() -> None:
    result = combine_grants(
        [
            CapabilityGrant(per_action_limit=500, daily_limit=2_000),
            CapabilityGrant(per_action_limit=1_000, daily_limit=1_500),
        ]
    )
    assert result.allowed
    assert result.per_action_limit == 1_000
    assert result.daily_limit == 2_000


def test_unlimited_grant_wins() -> None:
    result = combine_grants(
        [
            CapabilityGrant(per_action_limit=500, daily_limit=2_000),
            CapabilityGrant(per_action_limit=None, daily_limit=None),
        ]
    )
    assert result.per_action_limit is None
    assert result.daily_limit is None
