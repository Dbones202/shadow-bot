"""Battle card kill switch and cost instrumentation.

Two things, both about being able to back out of the feature.

The **kill switch** is a per-guild setting rather than a config file value, so
turning images off is a slash command rather than a deploy. If rendering turns
out to cost more than it is worth on the container, that decision should take
seconds.

The **counters** roll up per game: how many cards were drawn, how long they took
and how many bytes were uploaded. Logs answer "was that round slow"; these
answer "what has this feature cost me over a month", which is the question that
actually decides whether it stays.

Revision ID: 0005_battle_cards
Revises: 0004_games_expansion
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_battle_cards"
down_revision = "0004_games_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guild_settings",
        sa.Column("battle_cards", sa.String(length=8), nullable=False, server_default="round"),
    )
    op.create_check_constraint(
        "battle_cards_valid", "guild_settings", "battle_cards IN ('off', 'round', 'kill')"
    )

    # The signup message, so the join button can be disabled and the final
    # roster written back once signups close. Without it the button would stay
    # live-looking on an old message and only fail when pressed.
    op.add_column("games", sa.Column("signup_message_id", sa.BigInteger(), nullable=True))

    op.add_column(
        "games", sa.Column("cards_rendered", sa.Integer(), nullable=False, server_default="0")
    )
    # Milliseconds, accumulated. Integer is plenty: a game would need to spend
    # nearly a month rendering to overflow it.
    op.add_column(
        "games", sa.Column("card_render_ms", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "games", sa.Column("card_bytes", sa.BigInteger(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("games", "card_bytes")
    op.drop_column("games", "card_render_ms")
    op.drop_column("games", "cards_rendered")
    op.drop_column("games", "signup_message_id")
    op.drop_constraint("battle_cards_valid", "guild_settings", type_="check")
    op.drop_column("guild_settings", "battle_cards")
