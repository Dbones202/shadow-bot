"""Database operations for the Hungry Games.

The game is paced over real time, so its state lives here rather than in memory:
a restart mid-game must resume rather than strand everyone's entry fees.

Money movement follows the same rules as everywhere else — entry fees come out
of cash and cannot overdraft, every movement writes a ledger entry, and an
admin-seeded pot is currency *creation* and is audited as such.

Two rules about the record, both deliberate:

* **Only games that finish get a number.** A game that was cancelled, or never
  reached its minimum, did not happen as far as history is concerned. Numbers are
  therefore assigned at completion, which is also what keeps them contiguous.
* **Every event is stored.** Kills, arena deaths and survivals all become rows,
  so a new statistic later needs no migration and can be backfilled over games
  that already happened.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from shadow_bot.db.economy import _audit, _ledger, find_account, get_or_create_account
from shadow_bot.db.models import EconomyAccount, Game, GameEvent, GameParticipant, GuildSettings
from shadow_bot.domain.banking import BankingError, spendable
from shadow_bot.domain.games import Rng, RoundPlan, Tribute, placements, plan_round, split_pot


class GameError(RuntimeError):
    """Raised when a game action cannot proceed. Safe to show a member."""


#: A game needs enough people for the eliminations to mean anything. Two is a
#: coin flip with narration attached, not a game.
MIN_PLAYERS_FLOOR = 3

STYLES = ("standard", "random_tasks", "organizer_defined")

#: Bounds on the round interval. Below five seconds a busy channel starts
#: crowding Discord's rate limit, and once battle cards are rendered the upload
#: alone can outlast a very short tick.
MIN_ROUND_SECONDS = 5
MAX_ROUND_SECONDS = 300

#: What a departed member's history reads as. See `anonymise_participants`.
TOMBSTONE = "a departed tribute"


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    """One tick's result, ready for the cog to narrate."""

    plan: RoundPlan
    round_number: int
    finished: bool = False
    winner_user_id: int | None = None
    pot: int = 0
    payouts: dict[int, int] = field(default_factory=dict)
    game_number: int | None = None
    #: Final standings by user id, only populated when the game finishes.
    standings: dict[int, int] = field(default_factory=dict)


ACTIVE_STATUSES = ("signup", "running")


async def active_game(session: AsyncSession, guild_id: int) -> Game | None:
    return (
        await session.execute(
            select(Game).where(Game.guild_id == guild_id, Game.status.in_(ACTIVE_STATUSES))
        )
    ).scalar_one_or_none()


async def participants(session: AsyncSession, game_id: uuid.UUID) -> list[GameParticipant]:
    return list(
        (
            await session.execute(
                select(GameParticipant)
                .where(GameParticipant.game_id == game_id)
                .order_by(GameParticipant.joined_at, GameParticipant.id)
            )
        )
        .scalars()
        .all()
    )


async def pot_for(session: AsyncSession, game: Game) -> int:
    """Seed plus every entry fee actually paid."""
    joined = await participants(session, game.id)
    return game.seeded_pot + game.entry_fee * len(joined)


async def _next_game_number(session: AsyncSession, guild_id: int) -> int:
    """The next sequential number for this guild.

    Locks the guild settings row first. The partial unique index on active games
    stops two games *running* at once, but says nothing about two finishing at
    the same moment, which is exactly when two readers could compute the same
    next number.
    """
    await session.execute(
        select(GuildSettings.guild_id).where(GuildSettings.guild_id == guild_id).with_for_update()
    )
    highest = (
        await session.execute(select(func.max(Game.game_number)).where(Game.guild_id == guild_id))
    ).scalar_one()
    return (highest or 0) + 1


async def create_game(
    session: AsyncSession,
    guild_id: int,
    *,
    channel_id: int,
    created_by: int,
    entry_fee: int,
    seeded_pot: int,
    min_players: int,
    signup_seconds: int,
    round_seconds: int,
    style: str = "standard",
    outcome_winner: str | None = None,
    outcome_killed_by: str | None = None,
    outcome_killed_self: str | None = None,
    now: datetime | None = None,
) -> Game:
    """Open signups.

    A seeded pot **creates currency** — there is no balance it comes out of — so
    it is written to the audit trail as `currency_created`, exactly like
    `/economy add`. Entry fees are redistributive and create nothing.

    The database enforces one active game per guild via a partial unique index,
    so two people running this at the same moment cannot both succeed.

    `round_seconds` is copied onto the game rather than read from guild settings
    at each tick, so changing the guild default cannot re-pace a game already in
    flight.
    """
    now = now or datetime.now(UTC)
    if min_players < MIN_PLAYERS_FLOOR:
        raise GameError(f"A game needs at least {MIN_PLAYERS_FLOOR} tributes.")
    if entry_fee < 0 or seeded_pot < 0:
        raise GameError("Entry fee and seed cannot be negative.")
    if style not in STYLES:
        raise GameError(f"Unknown game style: {style}.")
    if not MIN_ROUND_SECONDS <= round_seconds <= MAX_ROUND_SECONDS:
        raise GameError(
            f"Round time must be between {MIN_ROUND_SECONDS} and {MAX_ROUND_SECONDS} seconds."
        )
    if style == "organizer_defined" and not all(
        (outcome_winner, outcome_killed_by, outcome_killed_self)
    ):
        raise GameError("Organizer-defined games need all three outcomes filled in.")

    if await active_game(session, guild_id) is not None:
        raise GameError("A game is already open or in progress on this server.")

    game = Game(
        id=uuid.uuid4(),
        guild_id=guild_id,
        channel_id=channel_id,
        status="signup",
        entry_fee=entry_fee,
        seeded_pot=seeded_pot,
        min_players=min_players,
        signup_closes_at=now + timedelta(seconds=signup_seconds),
        next_tick_at=None,
        round_number=0,
        created_by=created_by,
        round_seconds=round_seconds,
        style=style,
        outcome_winner=outcome_winner,
        outcome_killed_by=outcome_killed_by,
        outcome_killed_self=outcome_killed_self,
    )
    session.add(game)
    await session.flush()

    if seeded_pot:
        actor = await find_account(session, guild_id, created_by)
        session.add(
            _audit(
                guild_id=guild_id,
                event_type="currency_created",
                succeeded=True,
                subject=None,
                actor=actor,
                details={
                    "amount": seeded_pot,
                    "reason": "hungrygames_seed",
                    "game_id": str(game.id),
                    "actor_user_id": str(created_by),
                },
            )
        )
    return game


async def join_game(
    session: AsyncSession, game: Game, user_id: int, *, display_name: str = ""
) -> GameParticipant:
    """Enter a tribute, taking the entry fee from their cash.

    The display name is captured now rather than resolved later, so a finished
    game reads back correctly even after the member has left or renamed.
    """
    if game.status != "signup":
        raise GameError("Signups for this game have closed.")

    account = await get_or_create_account(session, game.guild_id, user_id, lock=True)

    existing = (
        await session.execute(
            select(GameParticipant).where(
                GameParticipant.game_id == game.id, GameParticipant.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise GameError("You have already entered this game.")

    if game.entry_fee:
        if game.entry_fee > spendable(account.cash):
            raise BankingError("You do not have enough cash for the entry fee.")
        account.cash -= game.entry_fee
        session.add(
            _ledger(
                guild_id=game.guild_id,
                correlation_id=game.id,
                subject=account,
                category="game_entry",
                cash_delta=-game.entry_fee,
                amount=game.entry_fee,
                details={"game_id": str(game.id)},
            )
        )

    participant = GameParticipant(
        id=uuid.uuid4(),
        game_id=game.id,
        account_id=account.id,
        user_id=user_id,
        display_name=(display_name or str(user_id))[:64],
        alive=True,
    )
    session.add(participant)
    await session.flush()
    return participant


async def _refund(session: AsyncSession, game: Game) -> list[int]:
    """Return every entry fee. Used when a game is cancelled."""
    refunded: list[int] = []
    if not game.entry_fee:
        return refunded

    for participant in await participants(session, game.id):
        if participant.account_id is None:  # the member left; nothing to refund into
            continue
        account = await session.get(EconomyAccount, participant.account_id)
        if account is None:
            continue
        account.cash += game.entry_fee
        session.add(
            _ledger(
                guild_id=game.guild_id,
                correlation_id=game.id,
                subject=account,
                category="game_refund",
                cash_delta=game.entry_fee,
                amount=game.entry_fee,
                details={"game_id": str(game.id)},
            )
        )
        if participant.user_id is not None:
            refunded.append(participant.user_id)
    return refunded


async def cancel_game(session: AsyncSession, game: Game, reason: str) -> list[int]:
    """Cancel and refund. Returns the user IDs refunded.

    A cancelled game keeps its row and its ledger trail — that money really did
    move — but never receives a number and contributes nothing to any statistic.
    """
    refunded = await _refund(session, game)
    game.status = "cancelled"
    game.next_tick_at = None
    session.add(
        _audit(
            guild_id=game.guild_id,
            event_type="game_cancelled",
            succeeded=True,
            subject=None,
            actor=None,
            details={"game_id": str(game.id), "reason": reason, "refunded": len(refunded)},
        )
    )
    return refunded


async def close_signups(session: AsyncSession, game: Game, *, now: datetime | None = None) -> bool:
    """Move a game from signup to running, or cancel it for too few tributes.

    Returns whether the game started.
    """
    now = now or datetime.now(UTC)
    if game.status != "signup":
        raise GameError("That game is not in signup.")

    joined = await participants(session, game.id)
    if len(joined) < game.min_players:
        await cancel_game(session, game, reason="not_enough_players")
        return False

    game.status = "running"
    game.next_tick_at = now + timedelta(seconds=game.round_seconds)
    return True


async def run_round(
    session: AsyncSession,
    game: Game,
    *,
    rng: Rng,
    now: datetime | None = None,
    winner_shares: tuple[int, ...] = (100,),
) -> RoundOutcome:
    """Advance one round, applying eliminations and paying out if it ends."""
    now = now or datetime.now(UTC)
    if game.status != "running":
        raise GameError("That game is not running.")

    everyone = await participants(session, game.id)
    by_user = {p.user_id: p for p in everyone if p.user_id is not None}
    participant_ids = {p.user_id: p.id for p in everyone if p.user_id is not None}
    alive = [
        Tribute(user_id=p.user_id, name=p.display_name or str(p.user_id))
        for p in everyone
        if p.alive and p.user_id is not None
    ]

    game.round_number += 1
    plan = plan_round(alive, rng=rng)

    for tribute in plan.eliminated:
        participant = by_user[tribute.user_id]
        participant.alive = False
        participant.eliminated_round = game.round_number

    # Store what happened before deciding whether the game is over, so the final
    # round is recorded exactly like any other.
    for event in plan.events:
        session.add(
            GameEvent(
                id=uuid.uuid4(),
                game_id=game.id,
                round_number=game.round_number,
                kind=str(event.kind),
                subject_participant_id=participant_ids[event.subject.user_id],
                victim_participant_id=(
                    participant_ids[event.victim.user_id] if event.victim is not None else None
                ),
            )
        )

    if not plan.is_final:
        game.next_tick_at = now + timedelta(seconds=game.round_seconds)
        return RoundOutcome(plan=plan, round_number=game.round_number)

    # Finished. Work out standings from elimination order, then pay.
    eliminated_in_order = sorted(
        (p for p in everyone if not p.alive and p.user_id is not None),
        key=lambda p: (p.eliminated_round or 0, p.joined_at),
    )
    winner = plan.winner
    standings = placements(
        [Tribute(user_id=p.user_id, name=p.display_name) for p in eliminated_in_order],
        winner=winner,
    )
    for user_id, place in standings.items():
        by_user[user_id].placement = place

    pot = await pot_for(session, game)
    payouts = split_pot(pot, standings, shares=winner_shares)

    for user_id, amount in payouts.items():
        if not amount:
            continue
        account = await get_or_create_account(session, game.guild_id, user_id, lock=True)
        account.cash += amount
        session.add(
            _ledger(
                guild_id=game.guild_id,
                correlation_id=game.id,
                subject=account,
                category="game_prize",
                cash_delta=amount,
                amount=amount,
                details={"game_id": str(game.id), "place": str(standings[user_id])},
            )
        )

    game.game_number = await _next_game_number(session, game.guild_id)
    game.status = "complete"
    game.next_tick_at = None
    session.add(
        _audit(
            guild_id=game.guild_id,
            event_type="game_complete",
            succeeded=True,
            subject=None,
            actor=None,
            details={
                "game_id": str(game.id),
                "game_number": game.game_number,
                "pot": pot,
                "rounds": game.round_number,
                "winner_user_id": str(winner.user_id) if winner else None,
                "tributes": len(everyone),
            },
        )
    )

    return RoundOutcome(
        plan=plan,
        round_number=game.round_number,
        finished=True,
        winner_user_id=winner.user_id if winner else None,
        pot=pot,
        payouts=payouts,
        game_number=game.game_number,
        standings=standings,
    )


async def due_games(session: AsyncSession, *, now: datetime | None = None) -> list[Game]:
    """Games needing attention — signups to close, or a round to run.

    Ordered so the longest-waiting is handled first if several come due at once.
    """
    now = now or datetime.now(UTC)
    return list(
        (
            await session.execute(
                select(Game)
                .where(
                    ((Game.status == "signup") & (Game.signup_closes_at <= now))
                    | ((Game.status == "running") & (Game.next_tick_at <= now))
                )
                .order_by(Game.updated_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )


async def elimination_causes(
    session: AsyncSession, game_id: uuid.UUID
) -> dict[int, tuple[str, int | None]]:
    """How each eliminated tribute went out, by user id.

    Maps to `("kill", killer_user_id)` or `("death", None)`. Drives the final
    recap, where the consequence shown for each participant depends on whether
    another tribute killed them (and who) or the arena did. Built from
    `game_events` rather than carried on `GameParticipant`, since the event log
    is already the source of truth and this needs no new column.

    `subject`/`victim` are two aliases of the same table — `GameEvent` records
    a KILL as (subject=killer, victim=killed) and a DEATH as (subject=the one
    who died, victim=None), so resolving both sides in one query needs a
    self-join. A participant who has since left keeps their `user_id` cleared
    by `anonymise_participants`, so those rows are skipped here too — there is
    no user id left to key a recap line on.
    """
    subject = aliased(GameParticipant)
    victim = aliased(GameParticipant)
    rows = (
        await session.execute(
            select(GameEvent.kind, subject.user_id, victim.user_id)
            .select_from(GameEvent)
            .join(subject, GameEvent.subject_participant_id == subject.id)
            .outerjoin(victim, GameEvent.victim_participant_id == victim.id)
            .where(GameEvent.game_id == game_id, GameEvent.kind.in_(("kill", "death")))
        )
    ).all()
    causes: dict[int, tuple[str, int | None]] = {}
    for kind, subject_user_id, victim_user_id in rows:
        if kind == "kill" and victim_user_id is not None:
            causes[victim_user_id] = ("kill", subject_user_id)
        elif kind == "death" and subject_user_id is not None:
            causes[subject_user_id] = ("death", None)
    return causes


async def anonymise_participants(session: AsyncSession, guild_id: int, user_id: int) -> int:
    """Strip a departing member's identity out of their game history.

    The rest of their economy data is deleted outright. Deleting these rows too
    would silently rewrite games that already finished — their kills would vanish
    and their victims would be left without a killer — so the row is kept and the
    identifiers are removed instead. Nothing that could identify them survives:
    the account link, the user id and the stored name all go.

    Returns how many rows were anonymised.
    """
    rows = (
        (
            await session.execute(
                select(GameParticipant)
                .join(Game, Game.id == GameParticipant.game_id)
                .where(Game.guild_id == guild_id, GameParticipant.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.account_id = None
        row.user_id = None
        row.display_name = TOMBSTONE
    return len(rows)
