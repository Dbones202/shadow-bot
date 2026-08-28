"""Media allowlist and requests (M10: Radarr/Sonarr integration).

Two tables. `media_allowlist` is global — no guild_id — because Donovan wants
one list that governs /request_movie and /request_tv the same way in every
server the bot is in. `media_requests` tracks each request through to the
daily poller finding it complete, so a restart never loses track of what is
still pending.

Revision ID: 0006_media_requests
Revises: 0005_battle_cards
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_media_requests"
down_revision = "0005_battle_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_allowlist",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "media_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=8), nullable=False),
        sa.Column("external_id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("tvdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_media_requests_guild_id", "media_requests", ["guild_id"])
    op.create_index("ix_media_requests_requested_by", "media_requests", ["requested_by"])
    op.create_check_constraint(
        "media_request_type_valid", "media_requests", "media_type IN ('movie', 'tv')"
    )
    op.create_check_constraint(
        "media_request_status_valid",
        "media_requests",
        "status IN ('pending', 'downloaded', 'failed')",
    )
    op.create_index(
        "ix_media_requests_pending",
        "media_requests",
        ["status"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_media_requests_pending", table_name="media_requests")
    op.drop_constraint("media_request_status_valid", "media_requests", type_="check")
    op.drop_constraint("media_request_type_valid", "media_requests", type_="check")
    op.drop_index("ix_media_requests_requested_by", table_name="media_requests")
    op.drop_index("ix_media_requests_guild_id", table_name="media_requests")
    op.drop_table("media_requests")
    op.drop_table("media_allowlist")
