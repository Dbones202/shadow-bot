"""Game numbering, styles, event log, and per-guild game settings.

Hand-written, like 0002. 0003 repaired the doubled CHECK constraint names that
made autogenerate unusable, so names in this file are the plain ones the models
declare and Alembic's naming convention supplies the `ck_<table>_` prefix.

Three changes here are worth reading before running this:

1. **`game_participants` is re-keyed.** It used a composite primary key of
   `(game_id, account_id)` with a cascade from `economy_accounts`, so a member
   leaving the server erased them from every finished game. Game history is now
   kept and anonymised instead, which means the row has to survive its account
   being deleted. It gets a surrogate primary key, a nullable account and user
   id, and a stored display name.

2. **Game numbers are assigned at completion**, not at creation, so cancelled
   games leave no gaps. Existing rows are backfilled in creation order, and only
   completed games get a number.

3. **`min_players` rises from 2 to 3.** Any existing row below the new floor is
   raised first, or the CHECK constraint would refuse to apply.

Revision ID: 0004_games_expansion
Revises: 0003_fix_check_constraint_names
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_games_expansion"
down_revision = "0003_fix_check_constraint_names"
branch_labels = None
depends_on = None

#: Only CHECK constraints were affected by the naming bug 0003 repaired; foreign
#: and primary keys were named correctly from the start.
ACCOUNT_FK = "fk_game_participants_account_id_economy_accounts"


def upgrade() -> None:
    # --- Per-guild game settings ------------------------------------------
    # server_default is required on a NOT NULL column added to a table that
    # already has rows; the ORM default only applies to new inserts.
    op.add_column(
        "guild_settings",
        sa.Column("game_name", sa.String(length=64), nullable=False, server_default="Hungry Games"),
    )
    op.add_column(
        "guild_settings",
        sa.Column("game_round_seconds", sa.Integer(), nullable=False, server_default="15"),
    )
    op.add_column(
        "guild_settings",
        sa.Column("game_signup_seconds", sa.Integer(), nullable=False, server_default="300"),
    )
    op.add_column(
        "guild_settings",
        sa.Column(
            "game_default_style", sa.String(length=24), nullable=False, server_default="standard"
        ),
    )
    op.add_column(
        "guild_settings",
        sa.Column("game_spoiler_percent", sa.Integer(), nullable=False, server_default="10"),
    )

    # --- games -------------------------------------------------------------
    op.add_column(
        "games", sa.Column("round_seconds", sa.Integer(), nullable=False, server_default="15")
    )
    op.add_column("games", sa.Column("game_number", sa.Integer(), nullable=True))
    op.add_column(
        "games",
        sa.Column("style", sa.String(length=24), nullable=False, server_default="standard"),
    )
    op.add_column("games", sa.Column("outcome_winner", sa.Text(), nullable=True))
    op.add_column("games", sa.Column("outcome_killed_by", sa.Text(), nullable=True))
    op.add_column("games", sa.Column("outcome_killed_self", sa.Text(), nullable=True))

    # Number the games that already finished, oldest first, per guild.
    # Cancelled and in-flight games stay null on purpose.
    op.execute(
        """
        UPDATE games AS g
        SET game_number = numbered.seq
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY guild_id ORDER BY created_at, id) AS seq
            FROM games
            WHERE status = 'complete'
        ) AS numbered
        WHERE g.id = numbered.id
        """
    )

    op.create_index(
        "uq_games_number_per_guild",
        "games",
        ["guild_id", "game_number"],
        unique=True,
        postgresql_where=sa.text("game_number IS NOT NULL"),
    )
    op.create_check_constraint(
        "style_valid", "games", "style IN ('standard', 'random_tasks', 'organizer_defined')"
    )

    # Raise anything below the new floor before tightening the constraint,
    # otherwise the ALTER fails on existing data.
    op.execute("UPDATE games SET min_players = 3 WHERE min_players < 3")
    op.drop_constraint("min_players_sane", "games", type_="check")
    op.create_check_constraint("min_players_sane", "games", "min_players >= 3")
    op.alter_column("games", "min_players", server_default="3")

    # --- game_participants: re-key so history survives a departure ---------
    op.add_column(
        "game_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute("UPDATE game_participants SET id = gen_random_uuid() WHERE id IS NULL")
    op.alter_column("game_participants", "id", nullable=False)

    op.add_column(
        "game_participants",
        sa.Column("display_name", sa.String(length=64), nullable=False, server_default=""),
    )

    op.drop_constraint("pk_game_participants", "game_participants", type_="primary")
    op.create_primary_key("pk_game_participants", "game_participants", ["id"])

    # The old cascade is exactly the behaviour being replaced.
    op.drop_constraint(ACCOUNT_FK, "game_participants", type_="foreignkey")
    op.alter_column("game_participants", "account_id", nullable=True)
    op.create_foreign_key(
        ACCOUNT_FK,
        "game_participants",
        "economy_accounts",
        ["account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("game_participants", "user_id", nullable=True)

    op.create_index("ix_game_participants_game_id", "game_participants", ["game_id"])
    op.create_index("ix_game_participants_user_id", "game_participants", ["user_id"])
    op.create_index(
        "uq_game_participant_user",
        "game_participants",
        ["game_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    # --- game_events -------------------------------------------------------
    op.create_table(
        "game_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject_participant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("victim_participant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('death', 'kill', 'survive')", name="event_kind_valid"),
        sa.CheckConstraint(
            "(kind = 'kill') = (victim_participant_id IS NOT NULL)",
            name="event_victim_consistent",
        ),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["subject_participant_id"], ["game_participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["victim_participant_id"], ["game_participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_game_events_game_id", "game_events", ["game_id"])
    op.create_index("ix_game_events_kind", "game_events", ["kind"])
    op.create_index(
        "ix_game_events_subject_participant_id", "game_events", ["subject_participant_id"]
    )
    op.create_index(
        "ix_game_events_victim_participant_id", "game_events", ["victim_participant_id"]
    )


def downgrade() -> None:
    op.drop_table("game_events")

    op.drop_index("uq_game_participant_user", table_name="game_participants")
    op.drop_index("ix_game_participants_user_id", table_name="game_participants")
    op.drop_index("ix_game_participants_game_id", table_name="game_participants")

    # Going back to the composite key means rows that were anonymised, or whose
    # account was deleted, can no longer be represented. Drop them rather than
    # fail the migration — they are unaddressable under the old schema.
    op.execute("DELETE FROM game_participants WHERE account_id IS NULL OR user_id IS NULL")

    op.drop_constraint(ACCOUNT_FK, "game_participants", type_="foreignkey")
    op.alter_column("game_participants", "account_id", nullable=False)
    op.create_foreign_key(
        ACCOUNT_FK,
        "game_participants",
        "economy_accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("game_participants", "user_id", nullable=False)

    op.drop_constraint("pk_game_participants", "game_participants", type_="primary")
    op.create_primary_key("pk_game_participants", "game_participants", ["game_id", "account_id"])
    op.drop_column("game_participants", "display_name")
    op.drop_column("game_participants", "id")

    op.drop_constraint("min_players_sane", "games", type_="check")
    op.create_check_constraint("min_players_sane", "games", "min_players >= 2")
    op.alter_column("games", "min_players", server_default="2")
    op.drop_constraint("style_valid", "games", type_="check")
    op.drop_index("uq_games_number_per_guild", table_name="games")
    op.drop_column("games", "outcome_killed_self")
    op.drop_column("games", "outcome_killed_by")
    op.drop_column("games", "outcome_winner")
    op.drop_column("games", "style")
    op.drop_column("games", "game_number")
    op.drop_column("games", "round_seconds")

    op.drop_column("guild_settings", "game_spoiler_percent")
    op.drop_column("guild_settings", "game_default_style")
    op.drop_column("guild_settings", "game_signup_seconds")
    op.drop_column("guild_settings", "game_round_seconds")
    op.drop_column("guild_settings", "game_name")
