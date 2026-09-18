"""Database operations for the media allowlist and requests (M10).

The allowlist is deliberately tiny CRUD — it is a handful of rows gatekept by
one owner id. `create_request`/`due_requests`/`mark_*` exist so the daily
poller (cogs/media.py) never has to hold request state in memory between
restarts, matching the same "state lives in the database" reasoning used for
the Hungry Games clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.models import MediaAllowlistEntry, MediaIssueReport, MediaRequest


async def is_allowed(session: AsyncSession, user_id: int) -> bool:
    return (
        await session.execute(
            select(MediaAllowlistEntry.user_id).where(MediaAllowlistEntry.user_id == user_id)
        )
    ).scalar_one_or_none() is not None


async def allow_user(
    session: AsyncSession, *, user_id: int, username: str, added_by: int
) -> MediaAllowlistEntry:
    """Add or refresh an allowlist entry. Re-running `/media allow` on someone
    already listed just updates their cached username instead of failing."""
    existing = (
        await session.execute(
            select(MediaAllowlistEntry).where(MediaAllowlistEntry.user_id == user_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.username = username
        return existing
    entry = MediaAllowlistEntry(user_id=user_id, username=username, added_by=added_by)
    session.add(entry)
    return entry


async def revoke_user(session: AsyncSession, *, user_id: int) -> bool:
    entry = (
        await session.execute(
            select(MediaAllowlistEntry).where(MediaAllowlistEntry.user_id == user_id)
        )
    ).scalar_one_or_none()
    if entry is None:
        return False
    await session.delete(entry)
    return True


async def list_allowed(session: AsyncSession) -> list[MediaAllowlistEntry]:
    return list(
        (await session.execute(select(MediaAllowlistEntry).order_by(MediaAllowlistEntry.added_at)))
        .scalars()
        .all()
    )


async def create_request(
    session: AsyncSession,
    *,
    guild_id: int,
    channel_id: int,
    requested_by: int,
    media_type: str,
    external_id: int,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
    title: str,
    year: int | None,
) -> MediaRequest:
    request = MediaRequest(
        guild_id=guild_id,
        channel_id=channel_id,
        requested_by=requested_by,
        media_type=media_type,
        external_id=external_id,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        imdb_id=imdb_id,
        title=title,
        year=year,
    )
    session.add(request)
    return request


async def pending_requests(session: AsyncSession) -> Sequence[MediaRequest]:
    """Everything the daily poller still needs to check on."""
    return (
        (await session.execute(select(MediaRequest).where(MediaRequest.status == "pending")))
        .scalars()
        .all()
    )


async def mark_downloaded(session: AsyncSession, request: MediaRequest) -> None:
    request.status = "downloaded"
    request.notified_at = datetime.now(UTC)


async def find_request_by_title(session: AsyncSession, title: str) -> MediaRequest | None:
    """Best-effort link from an issue report back to the request that brought
    the title in, so the original requester can be credited/notified.

    Exact, case-insensitive match against the most recent matching request —
    good enough for a small personal library. A title requested more than
    once (a re-request after a bad rip) resolves to the newest one, which is
    also the one most likely to be what is currently in the library.
    """
    return (
        (
            await session.execute(
                select(MediaRequest)
                .where(MediaRequest.title.ilike(title))
                .order_by(MediaRequest.created_at.desc())
            )
        )
        .scalars()
        .first()
    )


async def create_issue_report(
    session: AsyncSession,
    *,
    guild_id: int,
    channel_id: int,
    reported_by: int,
    title: str,
    description: str,
    media_request_id: uuid.UUID | None,
) -> MediaIssueReport:
    report = MediaIssueReport(
        guild_id=guild_id,
        channel_id=channel_id,
        reported_by=reported_by,
        title=title,
        description=description,
        media_request_id=media_request_id,
    )
    session.add(report)
    return report
