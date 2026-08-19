import pytest

from shadow_bot.domain.authority import Authority, authority_of, is_economy_admin

OWNER = 100
APP_OWNER = 200
STRANGER = 300
OWNERS = frozenset({APP_OWNER})


def test_guild_owner_has_authority() -> None:
    assert authority_of(OWNER, guild_owner_id=OWNER, app_owner_ids=OWNERS) is Authority.GUILD_OWNER


def test_app_owner_has_support_authority() -> None:
    assert (
        authority_of(APP_OWNER, guild_owner_id=OWNER, app_owner_ids=OWNERS) is Authority.APP_OWNER
    )


def test_everyone_else_has_none() -> None:
    assert authority_of(STRANGER, guild_owner_id=OWNER, app_owner_ids=OWNERS) is Authority.NONE
    assert not is_economy_admin(STRANGER, guild_owner_id=OWNER, app_owner_ids=OWNERS)


def test_guild_ownership_is_reported_ahead_of_app_ownership() -> None:
    """When one person is both, the audit trail should show the ordinary reason."""
    assert (
        authority_of(APP_OWNER, guild_owner_id=APP_OWNER, app_owner_ids=OWNERS)
        is Authority.GUILD_OWNER
    )


def test_guild_owner_cannot_be_locked_out_by_empty_app_owners() -> None:
    """ECONOMY_SPEC.md: the guild owner is root and cannot be locked out."""
    assert is_economy_admin(OWNER, guild_owner_id=OWNER, app_owner_ids=frozenset())


def test_missing_guild_owner_does_not_grant_authority() -> None:
    """Outside a guild there is no owner to match, so only app owners qualify."""
    assert not is_economy_admin(OWNER, guild_owner_id=None, app_owner_ids=OWNERS)
    assert is_economy_admin(APP_OWNER, guild_owner_id=None, app_owner_ids=OWNERS)


@pytest.mark.parametrize("user_id", [OWNER, APP_OWNER])
def test_is_economy_admin_agrees_with_authority_of(user_id: int) -> None:
    standing = authority_of(user_id, guild_owner_id=OWNER, app_owner_ids=OWNERS)
    assert is_economy_admin(user_id, guild_owner_id=OWNER, app_owner_ids=OWNERS) is (
        standing is not Authority.NONE
    )
