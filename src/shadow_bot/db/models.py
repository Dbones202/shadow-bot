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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
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
        CheckConstraint("battle_cards IN ('off', 'round', 'kill')", name="battle_cards_valid"),
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

    # --- Games -------------------------------------------------------------
    #: What this guild calls the game. Cosmetic only: Discord command names are
    #: global, so `/hungrygames` is the invocation everywhere no matter what a
    #: server renames it to. This name is what appears in embeds and recaps.
    game_name: Mapped[str] = mapped_column(String(64), default="Hungry Games", nullable=False)
    #: Seconds between rounds. Per-guild default; the organizer may override it
    #: for a single game.
    game_round_seconds: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    #: How long signups stay open, in seconds. Same override rule.
    game_signup_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    #: Style used when the organizer does not pick one.
    game_default_style: Mapped[str] = mapped_column(String(24), default="standard", nullable=False)
    #: Chance per round that the round's names and card post behind a spoiler,
    #: as a percentage. Deliberately rare — always-on spoilers become a chore,
    #: an occasional one stays a surprise.
    game_spoiler_percent: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    #: Battle card policy: 'off', 'round' (one card per round) or 'kill' (one
    #: per duel). A setting rather than a config value so switching images off
    #: is a slash command, not a deploy.
    battle_cards: Mapped[str] = mapped_column(String(8), default="round", nullable=False)


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


class Game(TimestampMixin, Base):
    """One Hungry Games event.

    State lives in the database rather than in memory because the game is paced
    over real time — a bot restart mid-game must be able to pick it back up
    rather than stranding everyone's entry fees.
    """

    __tablename__ = "games"
    __table_args__ = (
        CheckConstraint(
            "status IN ('signup', 'running', 'complete', 'cancelled')", name="status_valid"
        ),
        CheckConstraint("entry_fee >= 0", name="entry_fee_nonnegative"),
        CheckConstraint("seeded_pot >= 0", name="seeded_pot_nonnegative"),
        CheckConstraint("min_players >= 3", name="min_players_sane"),
        CheckConstraint(
            "style IN ('standard', 'random_tasks', 'organizer_defined')", name="style_valid"
        ),
        # At most one game per guild may be open or in progress. A partial
        # unique index enforces this in the database, so two people running
        # /hungrygames start at the same moment cannot both succeed.
        Index(
            "uq_games_one_active_per_guild",
            "guild_id",
            unique=True,
            postgresql_where=text("status IN ('signup', 'running')"),
        ),
        # Numbers are assigned at completion, so cancelled games leave no gaps.
        # Partial, because in-flight and cancelled games have no number at all.
        Index(
            "uq_games_number_per_guild",
            "guild_id",
            "game_number",
            unique=True,
            postgresql_where=text("game_number IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="signup", nullable=False, index=True)
    entry_fee: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    seeded_pot: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    min_players: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    signup_closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When the next round should run. Null while signups are open.
    next_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    round_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Seconds between rounds for this game. Copied from the guild default at
    #: creation so that changing the guild setting mid-game does not re-pace a
    #: game already in flight.
    round_seconds: Mapped[int] = mapped_column(Integer, default=15, nullable=False)

    #: Sequential per guild, counting only games that ran to completion.
    #: Null while in flight and forever for cancelled games — a game that never
    #: finished did not happen as far as the record is concerned.
    game_number: Mapped[int | None] = mapped_column(Integer)
    style: Mapped[str] = mapped_column(String(24), default="standard", nullable=False)

    #: Only used by the `organizer_defined` style. Filled in at start and shown
    #: in the signup embed, so nobody joins without knowing what they are in for.
    outcome_winner: Mapped[str | None] = mapped_column(Text)
    outcome_killed_by: Mapped[str | None] = mapped_column(Text)
    outcome_killed_self: Mapped[str | None] = mapped_column(Text)

    #: The signup message, so the join button can be disabled and the roster
    #: written back when signups close.
    signup_message_id: Mapped[int | None] = mapped_column(BigInteger)

    # Card cost, accumulated per game. Logs say whether one round was slow;
    # these say what the feature costs over a month, which is the question that
    # decides whether it stays.
    cards_rendered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    card_render_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    card_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class GameParticipant(Base):
    """One tribute in one game.

    Deliberately **not** keyed on the account. A member who leaves the server has
    their economy data deleted, but erasing them here would silently rewrite
    finished games — their kills would vanish and their victims would lose a
    killer. Instead the account and user ids are nulled and the stored name is
    replaced with a tombstone, so no identifier is retained and the game still
    reads back correctly. See `db.member_cleanup`.
    """

    __tablename__ = "game_participants"
    __table_args__ = (
        # One entry per member per game, but only while they are identifiable —
        # several anonymised rows in the same game must be able to coexist.
        Index(
            "uq_game_participant_user",
            "game_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null once the member has left the server.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("economy_accounts.id", ondelete="SET NULL")
    )
    #: Null once the member has left the server, which is also what drops them
    #: off every leaderboard.
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    #: Captured at join time so a finished game can be read back years later
    #: without asking Discord who a departed ID used to be.
    display_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    alive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    eliminated_round: Mapped[int | None] = mapped_column(Integer)
    placement: Mapped[int | None] = mapped_column(Integer)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GameEvent(Base):
    """One thing that happened to one tribute in one round.

    Every stat Donovan asked for is a query over this table, and keeping the raw
    events means a new stat idea later needs no migration and can be backfilled
    over games that already happened.

    Subject and victim point at participants rather than user ids, so
    anonymising a departed member anonymises their whole history in one update
    instead of leaving orphaned identifiers scattered through the log.
    """

    __tablename__ = "game_events"
    __table_args__ = (
        CheckConstraint("kind IN ('death', 'kill', 'survive')", name="event_kind_valid"),
        # A kill needs a victim; the other two must not have one. Cheap to
        # enforce here and it makes every downstream count trustworthy.
        CheckConstraint(
            "(kind = 'kill') = (victim_participant_id IS NOT NULL)", name="event_victim_consistent"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    game_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 'death' means nobody was responsible — the arena, or their own bad idea.
    #: That is the same bucket as "killed self"; there is no third category.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    subject_participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("game_participants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    victim_participant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("game_participants.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MediaAllowlistEntry(Base):
    """A Discord user allowed to run /request_movie and /request_tv.

    Global rather than per-guild — Donovan asked for one list that works the
    same in every server the bot is in, since it maps to one Radarr/Sonarr
    pair he runs, not to any one guild's economy. `added_by` is who ran
    `/media allow`, which today can only be MEDIA_OWNER_ID, but the column
    stays even though it is a single value in practice — an audit record
    should say who granted access without a reader having to already know
    that rule.
    """

    __tablename__ = "media_allowlist"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: Cached at grant time so `/media list` reads as names, not bare ids, even
    #: for someone who has since left every mutual guild with the bot.
    username: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MediaRequest(Base):
    """One /request_movie or /request_tv request, tracked through to completion.

    Guild/channel are captured so the completion poller knows where to reply —
    Donovan chose a channel reply over a DM, so there is no other way back to
    the right place once the interaction that created this row is long gone.
    `external_id` is the Radarr/Sonarr internal id for the added movie/series,
    used to poll that app's queue and history for the request's status.
    """

    __tablename__ = "media_requests"
    __table_args__ = (
        CheckConstraint("media_type IN ('movie', 'tv')", name="media_request_type_valid"),
        CheckConstraint(
            "status IN ('pending', 'downloaded', 'failed')", name="media_request_status_valid"
        ),
        Index("ix_media_requests_pending", "status", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_by: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    media_type: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Radarr's movie id or Sonarr's series id — what the poller asks about.
    external_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    tvdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
