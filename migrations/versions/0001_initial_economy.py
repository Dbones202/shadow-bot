"""Create the initial guild-scoped economy schema.

Revision ID: 0001_initial_economy
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_economy"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
UTC_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "guild_settings",
        sa.Column("guild_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("currency_name", sa.String(50), nullable=False, server_default="coin"),
        sa.Column("currency_name_plural", sa.String(50), nullable=False, server_default="coins"),
        sa.Column("currency_symbol", sa.String(100), nullable=False, server_default="🪙"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("cash_floor", sa.BigInteger(), nullable=False, server_default="-1000"),
        sa.Column("bank_floor", sa.BigInteger(), nullable=False, server_default="-10000"),
        sa.Column("audit_channel_id", sa.BigInteger()),
        sa.Column("economy_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.CheckConstraint("cash_floor <= 0", name="ck_guild_settings_cash_floor_nonpositive"),
        sa.CheckConstraint("bank_floor <= 0", name="ck_guild_settings_bank_floor_nonpositive"),
    )

    op.create_table(
        "economy_accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("cash", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bank", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.UniqueConstraint("guild_id", "user_id", name="uq_account_guild_user"),
    )
    op.create_index("ix_economy_accounts_guild_id", "economy_accounts", ["guild_id"])
    op.create_index("ix_economy_accounts_user_id", "economy_accounts", ["user_id"])

    op.create_table(
        "role_income_rules",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("payout", sa.BigInteger(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.UniqueConstraint("guild_id", "role_id", name="uq_role_income_guild_role"),
        sa.CheckConstraint("payout >= 0", name="ck_role_income_rules_payout_nonnegative"),
        sa.CheckConstraint("cooldown_seconds > 0", name="ck_role_income_rules_cooldown_positive"),
    )
    op.create_index("ix_role_income_rules_guild_id", "role_income_rules", ["guild_id"])

    op.create_table(
        "activity_rules",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("activity_key", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("success_chance", sa.Numeric(6, 5), nullable=False),
        sa.Column("success_min", sa.BigInteger(), nullable=False),
        sa.Column("success_max", sa.BigInteger(), nullable=False),
        sa.Column("fine_min", sa.BigInteger(), nullable=False),
        sa.Column("fine_max", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.CheckConstraint(
            "cooldown_seconds > 0", name="ck_activity_rules_activity_cooldown_positive"
        ),
        sa.CheckConstraint(
            "success_chance >= 0 AND success_chance <= 1",
            name="ck_activity_rules_success_chance_range",
        ),
        sa.CheckConstraint("success_min >= 0", name="ck_activity_rules_success_min_nonnegative"),
        sa.CheckConstraint(
            "success_max >= success_min", name="ck_activity_rules_success_range_valid"
        ),
        sa.CheckConstraint("fine_min >= 0", name="ck_activity_rules_fine_min_nonnegative"),
        sa.CheckConstraint("fine_max >= fine_min", name="ck_activity_rules_fine_range_valid"),
    )

    op.create_table(
        "flavor_texts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("activity_key", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index("ix_flavor_texts_guild_id", "flavor_texts", ["guild_id"])

    op.create_table(
        "interest_policies",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rate", sa.Numeric(12, 10), nullable=False, server_default="0"),
        sa.Column("weekday", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("local_time", sa.Time(timezone=False), nullable=False, server_default="00:00:00"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.CheckConstraint("rate >= 0", name="ck_interest_policies_interest_rate_nonnegative"),
        sa.CheckConstraint(
            "weekday >= 0 AND weekday <= 6", name="ck_interest_policies_interest_weekday_range"
        ),
    )

    op.create_table(
        "economy_role_permissions",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role_id", sa.BigInteger(), primary_key=True),
        sa.Column("capability", sa.String(40), primary_key=True),
        sa.Column("per_action_limit", sa.BigInteger()),
        sa.Column("daily_limit", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
        sa.CheckConstraint(
            "per_action_limit IS NULL OR per_action_limit >= 0",
            name="ck_economy_role_permissions_per_action_limit_nonnegative",
        ),
        sa.CheckConstraint(
            "daily_limit IS NULL OR daily_limit >= 0",
            name="ck_economy_role_permissions_daily_limit_nonnegative",
        ),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_account_id",
            UUID,
            sa.ForeignKey("economy_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_account_id", UUID, sa.ForeignKey("economy_accounts.id", ondelete="SET NULL")
        ),
        sa.Column(
            "counterparty_account_id",
            UUID,
            sa.ForeignKey("economy_accounts.id", ondelete="SET NULL"),
        ),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("cash_delta", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bank_delta", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempted_amount", sa.BigInteger()),
        sa.Column("applied_amount", sa.BigInteger()),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
    )
    op.create_index("ix_ledger_entries_correlation_id", "ledger_entries", ["correlation_id"])
    op.create_index("ix_ledger_entries_guild_id", "ledger_entries", ["guild_id"])
    op.create_index(
        "ix_ledger_entries_subject_account_id", "ledger_entries", ["subject_account_id"]
    )
    op.create_index("ix_ledger_entries_category", "ledger_entries", ["category"])
    op.create_index("ix_ledger_entries_created_at", "ledger_entries", ["created_at"])

    op.create_table(
        "activity_cooldowns",
        sa.Column(
            "account_id",
            UUID,
            sa.ForeignKey("economy_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("activity_key", sa.String(32), primary_key=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "role_collection_cooldowns",
        sa.Column(
            "account_id",
            UUID,
            sa.ForeignKey("economy_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_rule_id",
            UUID,
            sa.ForeignKey("role_income_rules.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "interest_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW
        ),
        sa.Column("accounts_paid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_paid", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("guild_id", "period_start_utc", name="uq_interest_run_period"),
    )
    op.create_index("ix_interest_runs_guild_id", "interest_runs", ["guild_id"])

    op.create_table(
        "capability_usage",
        sa.Column(
            "account_id",
            UUID,
            sa.ForeignKey("economy_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("capability", sa.String(40), primary_key=True),
        sa.Column("usage_date", sa.Date(), primary_key=True),
        sa.Column("amount_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("amount_used >= 0", name="ck_capability_usage_amount_used_nonnegative"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_account_id", UUID, sa.ForeignKey("economy_accounts.id", ondelete="SET NULL")
        ),
        sa.Column(
            "subject_account_id", UUID, sa.ForeignKey("economy_accounts.id", ondelete="SET NULL")
        ),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=UTC_NOW),
    )
    op.create_index("ix_audit_events_guild_id", "audit_events", ["guild_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("capability_usage")
    op.drop_table("interest_runs")
    op.drop_table("role_collection_cooldowns")
    op.drop_table("activity_cooldowns")
    op.drop_table("ledger_entries")
    op.drop_table("economy_role_permissions")
    op.drop_table("interest_policies")
    op.drop_table("flavor_texts")
    op.drop_table("activity_rules")
    op.drop_table("role_income_rules")
    op.drop_table("economy_accounts")
    op.drop_table("guild_settings")
