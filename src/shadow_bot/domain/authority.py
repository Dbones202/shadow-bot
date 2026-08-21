"""Who is allowed to administer a guild's economy.

Deliberately pure and dependency-free so the rule can be tested directly and
stated in one place rather than re-derived in every cog.

ECONOMY_SPEC.md establishes that the guild owner is the root economy
administrator and cannot be locked out, and that the application owner has
emergency cross-guild access for support.

**Interim deviation from the spec (2026-08-19):** the spec says Discord's
Administrator permission grants no economy access on its own, with delegation
happening through capability grants. Those grants are not built yet, which would
leave a server owner as the only person able to run anything. Administrator is
therefore accepted for now. When `domain.permissions` is wired up, revisit this —
`Authority.GUILD_ADMIN` is the seam to remove.

The distinction is preserved in the returned value rather than collapsed to a
boolean, so audit records show *why* someone was allowed to act. "An
administrator did this" and "the owner did this" are different facts.
"""

from __future__ import annotations

from enum import StrEnum


class Authority(StrEnum):
    """Why someone was allowed to act. Recorded on audit events."""

    GUILD_OWNER = "guild_owner"
    APP_OWNER = "app_owner"
    #: Holds Discord's Administrator permission. Interim — see the module docstring.
    GUILD_ADMIN = "guild_admin"
    NONE = "none"


def authority_of(
    user_id: int,
    *,
    guild_owner_id: int | None,
    app_owner_ids: frozenset[int],
    has_administrator: bool = False,
) -> Authority:
    """Classify a user's standing in one guild.

    Checked most-specific first, so someone who is several of these at once is
    recorded under the least surprising explanation: an application owner who
    also owns the server is acting as its owner, not as support.
    """
    if guild_owner_id is not None and user_id == guild_owner_id:
        return Authority.GUILD_OWNER
    if user_id in app_owner_ids:
        return Authority.APP_OWNER
    if has_administrator:
        return Authority.GUILD_ADMIN
    return Authority.NONE


def is_economy_admin(
    user_id: int,
    *,
    guild_owner_id: int | None,
    app_owner_ids: frozenset[int],
    has_administrator: bool = False,
) -> bool:
    return (
        authority_of(
            user_id,
            guild_owner_id=guild_owner_id,
            app_owner_ids=app_owner_ids,
            has_administrator=has_administrator,
        )
        is not Authority.NONE
    )


def has_admin_permission(user: object) -> bool:
    """Read Discord's Administrator permission off a member, safely.

    Returns False for a `User` rather than a `Member` — outside a guild there
    are no guild permissions to read, and defaulting to False means an
    unexpected type can never accidentally grant authority.
    """
    permissions = getattr(user, "guild_permissions", None)
    return bool(getattr(permissions, "administrator", False))
