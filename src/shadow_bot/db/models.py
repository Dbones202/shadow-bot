from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shadow_bot.db.base import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class GuildSettings(TimestampMixin, Base):
    __tablename__ = "guild_settings"
    __table_args__ = (
        CheckConstraint("cash_floor <= 0", name="cash_floor_nonpositive"),
        CheckConstraint("bank_floor <= 0", name="bank_floor_nonpositive"),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    currency_name: Mapped[str] = mapped_column(String(50), default="coin", nullable=False)
    currency_name_plural: Mapped[str] = mapped_column(String(50), default="coins", nullable=False)
    currency_symbol: Mapped[str] = mapped_column(String(100), default="🪙", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    cash_floor: Mapped[int] = mapped_column(BigInteger, default=-1_000, nullable=False)
    bank_floor: Mapped[int] = mapped_column(BigInteger, default=-10_000, nullable=False)
    audit_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    economy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class EconomyAccount(TimestampMixin, Base):
    __tablename__ = "economy_accounts"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", name="uq_account_guild_user"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    cash: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bank: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, nullable=False, index=True
    )
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subject_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("economy_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("economy_accounts.id", ondelete="SET NULL")
    )
    counterparty_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("economy_accounts.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    cash_delta: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bank_delta: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    attempted_amount: Mapped[int | None] = mapped_column(BigInteger)
    applied_amount: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class ActivityCooldown(Base):
    __tablename__ = "activity_cooldowns"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("economy_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    activity_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RoleIncomeRule(TimestampMixin, Base):
    __tablename__ = "role_income_rules"
    __table_args__ = (
        UniqueConstraint("guild_id", "role_id", name="uq_role_income_guild_role"),
        CheckConstraint("payout >= 0", name="payout_nonnegative"),
        CheckConstraint("cooldown_seconds > 0", name="cooldown_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RoleCollectionCooldown(Base):
    __tablename__ = "role_collection_cooldowns"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("economy_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("role_income_rules.id", ondelete="CASCADE"),
        primary_key=True,
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActivityRule(TimestampMixin, Base):
    __tablename__ = "activity_rules"
    __table_args__ = (
        CheckConstraint("cooldown_seconds > 0", name="activity_cooldown_positive"),
        CheckConstraint("success_chance >= 0 AND success_chance <= 1", name="success_chance_range"),
        CheckConstraint("success_min >= 0", name="success_min_nonnegative"),
        CheckConstraint("success_max >= success_min", name="success_range_valid"),
        CheckConstraint("fine_min >= 0", name="fine_min_nonnegative"),
        CheckConstraint("fine_max >= fine_min", name="fine_range_valid"),
    )

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        primary_key=True,
    )
    activity_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    success_chance: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    success_min: Mapped[int] = mapped_column(BigInteger, nullable=False)
    success_max: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fine_min: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fine_max: Mapped[int] = mapped_column(BigInteger, nullable=False)


class FlavorText(Base):
    __tablename__ = "flavor_texts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    activity_key: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class InterestPolicy(TimestampMixin, Base):
    __tablename__ = "interest_policies"
    __table_args__ = (
        CheckConstraint("rate >= 0", name="interest_rate_nonnegative"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="interest_weekday_range"),
    )

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 10), default=0, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    local_time: Mapped[time] = mapped_column(
        Time(timezone=False), default=time(0, 0), nullable=False
    )


class InterestRun(Base):
    __tablename__ = "interest_runs"
    __table_args__ = (
        UniqueConstraint("guild_id", "period_start_utc", name="uq_interest_run_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    accounts_paid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_paid: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class EconomyRolePermission(TimestampMixin, Base):
    __tablename__ = "economy_role_permissions"
    __table_args__ = (
        CheckConstraint(
            "per_action_limit IS NULL OR per_action_limit >= 0", name="per_action_limit_nonnegative"
        ),
        CheckConstraint("daily_limit IS NULL OR daily_limit >= 0", name="daily_limit_nonnegative"),
    )

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    capability: Mapped[str] = mapped_column(String(40), primary_key=True)
    per_action_limit: Mapped[int | None] = mapped_column(BigInteger)
    daily_limit: Mapped[int | None] = mapped_column(BigInteger)


class CapabilityUsage(Base):
    __tablename__ = "capability_usage"
    __table_args__ = (CheckConstraint("amount_used >= 0", name="amount_used_nonnegative"),)

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("economy_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    capability: Mapped[str] = mapped_column(String(40), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    amount_used: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("economy_accounts.id", ondelete="SET NULL")
    )
    subject_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("economy_accounts.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
