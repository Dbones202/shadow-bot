"""Leaderboards and per-member game statistics.

Every figure here is derived from `game_events` and `game_participants` rather
than kept in a running tally. That costs a query instead of a column, and buys
two things worth more: a new statistic can be added later without a migration,
and it is computed over games that already happened rather than starting from
zero on the day it ships.

Three rules apply to every query in this module:

* **Only completed games count.** A cancelled game did not happen.
* **Anonymised tributes are excluded**, because their `user_id` is null. A
  member who left the server drops off the boards without their absence
  rewriting anyone else's history.
* **Everything is scoped to one guild.** Isolation is a spec claim, not an
  implementation detail.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Numeric, Select, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.models import Game, GameEvent, GameParticipant

#: A win rate needs a floor or somebody who played once and won sits at 100%
#: forever, above people with a genuine record.
MIN_GAMES_FOR_RATE = 3


@dataclass(frozen=True, slots=True)
class StatDefinition:
    key: str
    label: str
    #: How the number reads in a message, e.g. "4 wins".
    unit: str
    #: True when a *lower* number is better (average placement).
    ascending: bool = False


STATS: tuple[StatDefinition, ...] = (
    StatDefinition("wins", "Most wins", "wins"),
    StatDefinition("kills", "Most kills", "kills"),
    StatDefinition("deaths_by_others", "Most often killed", "times"),
    StatDefinition("self_deaths", "Most deaths by arena", "times"),
    StatDefinition("games", "Most games played", "games"),
    StatDefinition("win_rate", "Best win rate", "%"),
    StatDefinition("winnings", "Most won", ""),
    StatDefinition("avg_placement", "Best average placement", "avg place", ascending=True),
    StatDefinition("best_streak", "Longest kill streak in one game", "kills"),
)

STAT_KEYS = tuple(s.key for s in STATS)
BY_KEY = {s.key: s for s in STATS}


@dataclass(frozen=True, slots=True)
class StatRow:
    user_id: int
    display_name: str
    value: float


def _completed(guild_id: int) -> Select:
    """Participants in finished games in this guild, still identifiable."""
    return (
        select(GameParticipant)
        .join(Game, Game.id == GameParticipant.game_id)
        .where(
            Game.guild_id == guild_id,
            Game.status == "complete",
            GameParticipant.user_id.is_not(None),
        )
    )


def _latest_names(guild_id: int):
    """Each member's most recent display name, for labelling rows.

    Names are stored per game, so someone who renamed appears under several.
    The newest entry wins rather than an arbitrary one.
    """
    ranked = (
        select(
            GameParticipant.user_id.label("user_id"),
            GameParticipant.display_name.label("display_name"),
            func.row_number()
            .over(
                partition_by=GameParticipant.user_id,
                order_by=GameParticipant.joined_at.desc(),
            )
            .label("rank"),
        )
        .join(Game, Game.id == GameParticipant.game_id)
        .where(Game.guild_id == guild_id, GameParticipant.user_id.is_not(None))
        .subquery()
    )
    return select(ranked.c.user_id, ranked.c.display_name).where(ranked.c.rank == 1).subquery()


def _event_counts(guild_id: int, kind: str, *, by_victim: bool = False):
    """Count events of one kind per member.

    Joins through the participant row rather than storing user ids on events,
    which is what lets one update anonymise a departed member's whole history.
    """
    column = GameEvent.victim_participant_id if by_victim else GameEvent.subject_participant_id
    return (
        select(GameParticipant.user_id, func.count().label("value"))
        .select_from(GameEvent)
        .join(GameParticipant, GameParticipant.id == column)
        .join(Game, Game.id == GameEvent.game_id)
        .where(
            Game.guild_id == guild_id,
            Game.status == "complete",
            GameEvent.kind == kind,
            GameParticipant.user_id.is_not(None),
        )
        .group_by(GameParticipant.user_id)
    )


def _base_query(guild_id: int, key: str):
    """The (user_id, value) query for one statistic."""
    if key == "kills":
        return _event_counts(guild_id, "kill")
    if key == "deaths_by_others":
        return _event_counts(guild_id, "kill", by_victim=True)
    if key == "self_deaths":
        return _event_counts(guild_id, "death")

    participants = _completed(guild_id).subquery()
    wins = func.count(case((participants.c.placement == 1, 1)))
    played = func.count(participants.c.id)

    if key == "wins":
        value = wins
    elif key == "games":
        value = played
    elif key == "win_rate":
        # PostgreSQL only defines round(x, n) for numeric, not double
        # precision, so the division has to be cast before rounding.
        value = func.round(cast(100.0 * wins / func.nullif(played, 0), Numeric), 1)
    elif key == "avg_placement":
        value = func.round(cast(func.avg(participants.c.placement), Numeric), 2)
    else:
        raise ValueError(f"Unknown statistic: {key}")

    query = (
        select(participants.c.user_id, value.label("value"))
        .select_from(participants)
        .group_by(participants.c.user_id)
    )
    if key == "win_rate":
        # Applied as HAVING so the floor filters groups, not rows.
        query = query.having(played >= MIN_GAMES_FOR_RATE)
    return query


async def leaderboard(
    session: AsyncSession, guild_id: int, key: str, *, limit: int = 10
) -> list[StatRow]:
    """Top members for one statistic, best first."""
    if key not in BY_KEY:
        raise ValueError(f"Unknown statistic: {key}")
    if key in {"winnings", "best_streak"}:
        return await _special(session, guild_id, key, limit=limit)

    values = _base_query(guild_id, key).subquery()
    names = _latest_names(guild_id)
    ordering = values.c.value.asc() if BY_KEY[key].ascending else values.c.value.desc()

    rows = (
        await session.execute(
            select(values.c.user_id, names.c.display_name, values.c.value)
            .join(names, names.c.user_id == values.c.user_id)
            .where(values.c.value.is_not(None), values.c.value > 0)
            .order_by(ordering, names.c.display_name)
            .limit(limit)
        )
    ).all()
    return [StatRow(user_id=r[0], display_name=r[1] or str(r[0]), value=float(r[2])) for r in rows]


async def _special(session: AsyncSession, guild_id: int, key: str, *, limit: int) -> list[StatRow]:
    names = _latest_names(guild_id)

    if key == "best_streak":
        # Most kills by one member in a single game.
        per_game = (
            select(
                GameParticipant.user_id.label("user_id"),
                func.count().label("value"),
            )
            .select_from(GameEvent)
            .join(
                GameParticipant,
                GameParticipant.id == GameEvent.subject_participant_id,
            )
            .join(Game, Game.id == GameEvent.game_id)
            .where(
                Game.guild_id == guild_id,
                Game.status == "complete",
                GameEvent.kind == "kill",
                GameParticipant.user_id.is_not(None),
            )
            .group_by(GameParticipant.user_id, GameEvent.game_id)
            .subquery()
        )
        values = (
            select(per_game.c.user_id, func.max(per_game.c.value).label("value"))
            .group_by(per_game.c.user_id)
            .subquery()
        )
    else:  # winnings — read from the ledger, which is the authority on money
        from shadow_bot.db.models import EconomyAccount, LedgerEntry

        values = (
            select(
                EconomyAccount.user_id.label("user_id"),
                func.sum(LedgerEntry.cash_delta).label("value"),
            )
            .join(EconomyAccount, EconomyAccount.id == LedgerEntry.subject_account_id)
            .where(
                LedgerEntry.guild_id == guild_id,
                LedgerEntry.category == "game_prize",
            )
            .group_by(EconomyAccount.user_id)
            .subquery()
        )

    rows = (
        await session.execute(
            select(values.c.user_id, names.c.display_name, values.c.value)
            .join(names, names.c.user_id == values.c.user_id)
            .where(values.c.value > 0)
            .order_by(values.c.value.desc(), names.c.display_name)
            .limit(limit)
        )
    ).all()
    return [StatRow(user_id=r[0], display_name=r[1] or str(r[0]), value=float(r[2])) for r in rows]


@dataclass(frozen=True, slots=True)
class MemberStats:
    games: int
    wins: int
    kills: int
    times_killed: int
    self_deaths: int
    best_placement: int | None
    average_placement: float | None

    @property
    def win_rate(self) -> float | None:
        """None below the floor rather than a flattering number off one game."""
        if self.games < MIN_GAMES_FOR_RATE:
            return None
        return round(100.0 * self.wins / self.games, 1)


async def member_stats(session: AsyncSession, guild_id: int, user_id: int) -> MemberStats:
    """Everything one member's record says, in a single round trip per shape."""
    summary = (
        await session.execute(
            select(
                func.count(GameParticipant.id),
                func.count(case((GameParticipant.placement == 1, 1))),
                func.min(GameParticipant.placement),
                func.avg(GameParticipant.placement),
            )
            .select_from(GameParticipant)
            .join(Game, Game.id == GameParticipant.game_id)
            .where(
                Game.guild_id == guild_id,
                Game.status == "complete",
                GameParticipant.user_id == user_id,
            )
        )
    ).one()

    counts = dict.fromkeys(("kill", "death"), 0)
    rows = (
        await session.execute(
            select(GameEvent.kind, func.count())
            .select_from(GameEvent)
            .join(GameParticipant, GameParticipant.id == GameEvent.subject_participant_id)
            .join(Game, Game.id == GameEvent.game_id)
            .where(
                Game.guild_id == guild_id,
                Game.status == "complete",
                GameParticipant.user_id == user_id,
            )
            .group_by(GameEvent.kind)
        )
    ).all()
    for kind, count in rows:
        counts[kind] = count

    times_killed = (
        await session.execute(
            select(func.count())
            .select_from(GameEvent)
            .join(GameParticipant, GameParticipant.id == GameEvent.victim_participant_id)
            .join(Game, Game.id == GameEvent.game_id)
            .where(
                and_(
                    Game.guild_id == guild_id,
                    Game.status == "complete",
                    GameParticipant.user_id == user_id,
                )
            )
        )
    ).scalar_one()

    return MemberStats(
        games=summary[0] or 0,
        wins=summary[1] or 0,
        kills=counts.get("kill", 0),
        times_killed=times_killed or 0,
        self_deaths=counts.get("death", 0),
        best_placement=summary[2],
        average_placement=round(float(summary[3]), 2) if summary[3] is not None else None,
    )
