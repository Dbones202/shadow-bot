"""Store and inventory (M11).

Two tables. `store_items` is per-guild — economy admins define what members
can buy with their currency. `inventory_items` is one row per (account,
item), holding a running quantity rather than one row per purchase; the
ledger already records each individual `store_purchase` event via its
existing `category` column, so no ledger schema change is needed.

`store_items.effect_type`/`effect_data` are added now but unused by any code
path in this revision — always "none"/`{}` — so a future "items with real
effects" milestone needs a dispatcher and a taxonomy, not another migration.

Revision ID: 0007_store
Revises: 0006_media_requests
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_store"
down_revision = "0006_media_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "store_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("guild_settings.guild_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        # Reserved for a future "items can grant a role / unlock an activity /
        # affect a game" milestone. Nothing reads or writes anything but the
        # "none"/{} defaults yet — see the StoreItem model docstring.
        sa.Column("effect_type", sa.String(length=32), nullable=False, server_default="none"),
        sa.Column(
            "effect_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_store_items_guild_id", "store_items", ["guild_id"])
    op.create_unique_constraint("uq_store_item_guild_name", "store_items", ["guild_id", "name"])
    op.create_check_constraint("store_item_price_nonnegative", "store_items", "price >= 0")
    op.create_check_constraint(
        "store_item_stock_nonnegative", "store_items", "stock IS NULL OR stock >= 0"
    )

    op.create_table(
        "inventory_items",
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("economy_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("store_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_check_constraint(
        "inventory_item_quantity_positive", "inventory_items", "quantity > 0"
    )


def downgrade() -> None:
    op.drop_table("inventory_items")
    op.drop_constraint("store_item_stock_nonnegative", "store_items", type_="check")
    op.drop_constraint("store_item_price_nonnegative", "store_items", type_="check")
    op.drop_constraint("uq_store_item_guild_name", "store_items", type_="unique")
    op.drop_index("ix_store_items_guild_id", table_name="store_items")
    op.drop_table("store_items")
