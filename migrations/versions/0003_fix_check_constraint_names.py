"""Repair doubled CHECK constraint names.

`Base.metadata` carries a naming convention of `ck_%(table_name)s_%(constraint_name)s`.
0001 and 0002 then passed names that *already* had that prefix baked in, so the
convention prefixed them a second time and every CHECK constraint in the database
ended up named `ck_<table>_ck_<table>_<rule>`.

This is the mismatch that made `alembic revision --autogenerate` propose a wall of
constraint renames on every run, which is why 0002 and everything after it had to
be hand-written. It is also actively dangerous: Alembic applies the naming
convention to `op.drop_constraint` as well, so a later migration that names a
constraint correctly cannot find it, and one that names it as it appears in the
database gets a *third* prefix. Both failure modes were hit while writing 0004.

Renaming a CHECK constraint is a catalog-only operation — no table rewrite, no
validation pass, no lock beyond a brief ACCESS EXCLUSIVE. Safe on a live database.

Names are written out literally rather than discovered at runtime so this file is
auditable and so a partially-migrated database cannot be silently mangled. Two of
them were truncated to 63 characters by PostgreSQL and were resolved by matching
`pg_get_constraintdef` output rather than guessed.

Revision ID: 0003_fix_check_constraint_names
Revises: 0002_hungry_games
"""

from __future__ import annotations

from alembic import op

revision = "0003_fix_check_constraint_names"
down_revision = "0002_hungry_games"
branch_labels = None
depends_on = None

#: (table, name in the database, name the models expect)
RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "activity_rules",
        "ck_activity_rules_ck_activity_rules_activity_cooldown_positive",
        "ck_activity_rules_activity_cooldown_positive",
    ),
    (
        "activity_rules",
        "ck_activity_rules_ck_activity_rules_fine_min_nonnegative",
        "ck_activity_rules_fine_min_nonnegative",
    ),
    (
        "activity_rules",
        "ck_activity_rules_ck_activity_rules_fine_range_valid",
        "ck_activity_rules_fine_range_valid",
    ),
    (
        "activity_rules",
        "ck_activity_rules_ck_activity_rules_success_chance_range",
        "ck_activity_rules_success_chance_range",
    ),
    (
        "activity_rules",
        "ck_activity_rules_ck_activity_rules_success_min_nonnegative",
        "ck_activity_rules_success_min_nonnegative",
    ),
    (
        "activity_rules",
        "ck_activity_rules_ck_activity_rules_success_range_valid",
        "ck_activity_rules_success_range_valid",
    ),
    (
        "capability_usage",
        "ck_capability_usage_ck_capability_usage_amount_used_nonnegative",
        "ck_capability_usage_amount_used_nonnegative",
    ),
    # Truncated by PostgreSQL at 63 characters. Resolved by constraint definition:
    # ..._bf9b guards per_action_limit, ..._d3af guards daily_limit.
    (
        "economy_role_permissions",
        "ck_economy_role_permissions_ck_economy_role_permissions_bf9b",
        "ck_economy_role_permissions_per_action_limit_nonnegative",
    ),
    (
        "economy_role_permissions",
        "ck_economy_role_permissions_ck_economy_role_permissions_d3af",
        "ck_economy_role_permissions_daily_limit_nonnegative",
    ),
    (
        "games",
        "ck_games_ck_games_entry_fee_nonnegative",
        "ck_games_entry_fee_nonnegative",
    ),
    (
        "games",
        "ck_games_ck_games_min_players_sane",
        "ck_games_min_players_sane",
    ),
    (
        "games",
        "ck_games_ck_games_seeded_pot_nonnegative",
        "ck_games_seeded_pot_nonnegative",
    ),
    (
        "games",
        "ck_games_ck_games_status_valid",
        "ck_games_status_valid",
    ),
    (
        "guild_settings",
        "ck_guild_settings_ck_guild_settings_bank_floor_nonpositive",
        "ck_guild_settings_bank_floor_nonpositive",
    ),
    (
        "guild_settings",
        "ck_guild_settings_ck_guild_settings_cash_floor_nonpositive",
        "ck_guild_settings_cash_floor_nonpositive",
    ),
    (
        "interest_policies",
        "ck_interest_policies_ck_interest_policies_interest_rate_6537",
        "ck_interest_policies_interest_rate_nonnegative",
    ),
    (
        "interest_policies",
        "ck_interest_policies_ck_interest_policies_interest_week_4597",
        "ck_interest_policies_interest_weekday_range",
    ),
    (
        "role_income_rules",
        "ck_role_income_rules_ck_role_income_rules_cooldown_positive",
        "ck_role_income_rules_cooldown_positive",
    ),
    (
        "role_income_rules",
        "ck_role_income_rules_ck_role_income_rules_payout_nonnegative",
        "ck_role_income_rules_payout_nonnegative",
    ),
)


def _rename(table: str, old: str, new: str) -> None:
    # Raw SQL on purpose: op.drop_constraint / op.create_constraint both run the
    # name through the metadata naming convention, which is the very thing being
    # repaired here. ALTER ... RENAME CONSTRAINT takes the literal name.
    # IF EXISTS is not available for RENAME CONSTRAINT, so guard in a DO block —
    # this keeps the migration idempotent on a database that was partly repaired
    # by hand.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = '{table}'::regclass AND conname = '{old}'
            ) THEN
                ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}";
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    for table, old, new in RENAMES:
        _rename(table, old, new)


def downgrade() -> None:
    for table, old, new in RENAMES:
        _rename(table, new, old)
