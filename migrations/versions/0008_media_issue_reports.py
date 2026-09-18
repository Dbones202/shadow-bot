"""Plex media issue reports (M10 follow-up).

Lets anyone on the media allowlist flag a problem with something already in
the library — wrong audio language, a bad rip, missing subtitles — separate
from requesting something new. `media_request_id` links back to the original
request when a title match is found; NULL is normal for anything that
predates the bot.

Revision ID: 0008_media_issue_reports
Revises: 0007_store
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_media_issue_reports"
down_revision = "0007_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_issue_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("reported_by", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "media_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_media_issue_reports_guild_id", "media_issue_reports", ["guild_id"])
    op.create_index("ix_media_issue_reports_reported_by", "media_issue_reports", ["reported_by"])
    op.create_index("ix_media_issue_reports_created_at", "media_issue_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_media_issue_reports_created_at", table_name="media_issue_reports")
    op.drop_index("ix_media_issue_reports_reported_by", table_name="media_issue_reports")
    op.drop_index("ix_media_issue_reports_guild_id", table_name="media_issue_reports")
    op.drop_table("media_issue_reports")
