"""Integration tests for the media allowlist and request CRUD against a real
PostgreSQL database. Skipped unless TEST_DATABASE_URL is set — see
test_economy_db.py's module docstring for how to run these locally.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shadow_bot.db import media as media_db
from shadow_bot.db.models import MediaRequest

TEST_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set"),
    pytest.mark.asyncio,
]

OWNER = 999_000_000_000_000_009
ALICE = 111_000_000_000_000_011
BOB = 222_000_000_000_000_022


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(TEST_URL, pool_size=10, max_overflow=10)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker.begin() as session:
        await session.execute(text("TRUNCATE media_requests, media_allowlist CASCADE"))

    yield maker
    await engine.dispose()


async def test_a_user_is_not_allowed_until_added(sessions) -> None:
    async with sessions() as session:
        assert await media_db.is_allowed(session, ALICE) is False


async def test_allow_user_grants_access(sessions) -> None:
    async with sessions.begin() as session:
        await media_db.allow_user(session, user_id=ALICE, username="Alice", added_by=OWNER)

    async with sessions() as session:
        assert await media_db.is_allowed(session, ALICE) is True


async def test_allow_user_twice_refreshes_the_cached_username_not_a_duplicate_row(sessions) -> None:
    async with sessions.begin() as session:
        await media_db.allow_user(session, user_id=ALICE, username="Alice", added_by=OWNER)
    async with sessions.begin() as session:
        await media_db.allow_user(session, user_id=ALICE, username="Alice#new", added_by=OWNER)

    async with sessions() as session:
        entries = await media_db.list_allowed(session)
    assert len(entries) == 1
    assert entries[0].username == "Alice#new"


async def test_revoke_user_removes_access(sessions) -> None:
    async with sessions.begin() as session:
        await media_db.allow_user(session, user_id=ALICE, username="Alice", added_by=OWNER)
    async with sessions.begin() as session:
        removed = await media_db.revoke_user(session, user_id=ALICE)
    assert removed is True

    async with sessions() as session:
        assert await media_db.is_allowed(session, ALICE) is False


async def test_revoke_user_not_on_the_list_reports_nothing_removed(sessions) -> None:
    async with sessions.begin() as session:
        removed = await media_db.revoke_user(session, user_id=ALICE)
    assert removed is False


async def test_list_allowed_returns_everyone_added(sessions) -> None:
    async with sessions.begin() as session:
        await media_db.allow_user(session, user_id=ALICE, username="Alice", added_by=OWNER)
        await media_db.allow_user(session, user_id=BOB, username="Bob", added_by=OWNER)

    async with sessions() as session:
        entries = await media_db.list_allowed(session)
    assert {e.user_id for e in entries} == {ALICE, BOB}


async def test_create_request_starts_pending(sessions) -> None:
    async with sessions.begin() as session:
        await media_db.create_request(
            session,
            guild_id=1,
            channel_id=2,
            requested_by=ALICE,
            media_type="movie",
            external_id=101,
            tmdb_id=329865,
            tvdb_id=None,
            imdb_id="tt2543164",
            title="Arrival",
            year=2016,
        )

    async with sessions() as session:
        pending = await media_db.pending_requests(session)
    assert len(pending) == 1
    assert pending[0].status == "pending"
    assert pending[0].title == "Arrival"


async def test_mark_downloaded_drops_it_out_of_pending(sessions) -> None:
    async with sessions.begin() as session:
        request = await media_db.create_request(
            session,
            guild_id=1,
            channel_id=2,
            requested_by=ALICE,
            media_type="tv",
            external_id=202,
            tmdb_id=None,
            tvdb_id=280619,
            imdb_id="tt3230854",
            title="The Expanse",
            year=2015,
        )
        await session.flush()
        request_id = request.id

    async with sessions.begin() as session:
        row = await session.get(MediaRequest, request_id)
        await media_db.mark_downloaded(session, row)

    async with sessions() as session:
        pending = await media_db.pending_requests(session)
    assert pending == []
