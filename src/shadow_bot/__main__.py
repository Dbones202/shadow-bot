from __future__ import annotations

import asyncio

from shadow_bot.bot import run_bot


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
