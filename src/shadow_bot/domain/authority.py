"""Who is allowed to administer a guild's economy.

Deliberately pure and dependency-free so the rule can be tested directly and
stated in one place rather than re-derived in every cog.

ECONOMY_SPEC.md is specific about this: the guild owner is the root economy
administrator and cannot be locked out, and the application owner has emergency
cross-guild access for support. Discord's own Administrator permission is
**not** on the list — a server admin has no economy authority unless the owner
delegates it through a capability grant.
"""

from __future__ import annotations

from enum import StrEnum


class Authority(StrEnum):
    """Why someone was allowed to act. Recorded on audit events."""

    GUILD_OWNER = "guild_owner"
    APP_OWNER = "app_owner"
    NONE = "none"


def authority_of(
    user_id: int, *, guild_owner_id: int | None, app_owner_ids: frozenset[int]
) -> Authority:
    """Classify a user's standing in one guild.

    Guild ownership is checked first so that an application owner who also owns
    the server is recorded as acting in the ordinary capacity rather than as
    support — the audit trail should show the least surprising explanation.
    """
    if guild_owner_id is not None and user_id == guild_owner_id:
        return Authority.GUILD_OWNER
    if user_id in app_owner_ids:
        return Authority.APP_OWNER
    return Authority.NONE


def is_economy_admin(
    user_id: int, *, guild_owner_id: int | None, app_owner_ids: frozenset[int]
) -> bool:
    return (
        authority_of(user_id, guild_owner_id=guild_owner_id, app_owner_ids=app_owner_ids)
        is not Authority.NONE
    )
