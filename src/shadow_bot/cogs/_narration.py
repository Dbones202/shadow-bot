"""Shared narration-library assembly for GamesCog and ActivitiesCog.

Leading underscore: not a cog, so `tests/test_extensions.py`'s registration
check — which walks every non-underscore module in `cogs/` and asserts it is
in `bot.EXTENSIONS` — skips this file. It has no `setup()` and is never passed
to `load_extension`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shadow_bot.db.narration import flavor_overrides
from shadow_bot.domain.narration import NarrationLibrary

if TYPE_CHECKING:
    from shadow_bot.bot import EconomyBot


async def guild_library(bot: EconomyBot, guild_id: int) -> NarrationLibrary:
    """This guild's narration: `bot.narration_defaults`, its own lines on top."""
    async with bot.database.sessions() as session:
        overrides = await flavor_overrides(session, guild_id)
    return NarrationLibrary(defaults=bot.narration_defaults, overrides=overrides)
