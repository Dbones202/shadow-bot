from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_PERMISSIONS = "manage_permissions"
    MANAGE_ROLE_INCOME = "manage_role_income"
    MANAGE_ACTIVITIES = "manage_activities"
    CREATE_CURRENCY = "create_currency"
    REMOVE_CURRENCY = "remove_currency"
    RESET_ACCOUNTS = "reset_accounts"
    VIEW_AUDIT = "view_audit"


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    per_action_limit: int | None
    daily_limit: int | None


@dataclass(frozen=True, slots=True)
class EffectiveGrant:
    allowed: bool
    per_action_limit: int | None = 0
    daily_limit: int | None = 0


def combine_grants(grants: list[CapabilityGrant]) -> EffectiveGrant:
    """Combine role grants, choosing the most permissive limit in each category."""
    if not grants:
        return EffectiveGrant(allowed=False)

    per_action = (
        None
        if any(g.per_action_limit is None for g in grants)
        else max(g.per_action_limit for g in grants if g.per_action_limit is not None)
    )
    daily = (
        None
        if any(g.daily_limit is None for g in grants)
        else max(g.daily_limit for g in grants if g.daily_limit is not None)
    )
    return EffectiveGrant(allowed=True, per_action_limit=per_action, daily_limit=daily)
