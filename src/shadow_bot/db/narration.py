"""Per-guild narration overrides — the ``flavor_texts`` table.

A guild's own lines, once ``/flavor add`` exists to write them. Reading is
already wired end to end; only the write commands are unbuilt.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.models import FlavorText


async def flavor_overrides(
    session: AsyncSession, guild_id: int
) -> dict[tuple[str, str], list[str]]:
    """This guild's narration lines, grouped by ``(category, outcome)``.

    Layered over the bot's narration defaults by `NarrationLibrary` — a guild
    that has written nothing here gets the shipped or `EVENTS_DIR` defaults.
    """
    rows = (
        (await session.execute(select(FlavorText).where(FlavorText.guild_id == guild_id)))
        .scalars()
        .all()
    )
    overrides: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        overrides.setdefault((row.activity_key.lower(), row.outcome.lower()), []).append(row.text)
    return overrides
