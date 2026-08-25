"""Database operations for the Hungry Games.

The game is paced over real time, so its state lives here rather than in memory:
a restart mid-game must resume rather than strand everyone's entry fees.

Money movement follows the same rules as everywhere else — entry fees come out
of cash and cannot overdraft, every movement writes a ledger entry, and an
admin-seeded pot is currency *creation* and is audited as such.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shadow_bot.db.economy import _audit, _ledger, find_account, get_or_create_account
from shadow_bot.db.models import EconomyAccount, Game, GameParticipant
from shadow_bot.domain.banking import BankingError, spendable
from shadow_bot.domain.games import Rng, RoundPlan, Tribute, placements, plan_round, split_pot


class GameError(RuntimeError):
    """Raised when a game action cannot proceed. Safe to show a member."""


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    """One tick's result, ready for the cog to narrate."""

    plan: RoundPlan
    round_number: int
    finished: bool = False
    winner_user_id: int | None = None
    pot: int = 0
    payouts: dict[int, int] = field(default_factory=dict)


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
                .order_by(GameParticipant.joined_at)
            )
        )
        .scalars()
        .all()
    )


async def pot_for(session: AsyncSession, game: Game) -> int:
    """Seed plus every entry fee actually paid."""
    joined = await participants(session, game.id)
    return game.seeded_pot + game.entry_fee * len(joined)


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
    now: datetime | None = None,
) -> Game:
    """Open signups.

    A seeded pot **creates currency** — there is no balance it comes out of — so
    it is written to the audit trail as `currency_created`, exactly like
    `/economy add`. Entry fees are redistributive and create nothing.

    The database enforces one active game per guild via a partial unique index,
    so two people running this at the same moment cannot both succeed.
    """
    now = now or datetime.now(UTC)
    if min_players < 2:
        raise GameError("A game needs at least two tributes.")
    if entry_fee < 0 or seeded_pot < 0:
        raise GameError("Entry fee and seed cannot be negative.")

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


async def join_game(session: AsyncSession, game: Game, user_id: int) -> GameParticipant:
    """Enter a tribute, taking the entry fee from their cash."""
    if game.status != "signup":
        raise GameError("Signups for this game have closed.")

    account = await get_or_create_account(session, game.guild_id, user_id, lock=True)

    existing = await session.get(GameParticipant, (game.id, account.id))
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
        game_id=game.id, account_id=account.id, user_id=user_id, alive=True
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
        account = await session.get(EconomyAccount, participant.account_id)
        if account is None:  # the member left; their data cascaded away
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
        refunded.append(participant.user_id)
    return refunded


async def cancel_game(session: AsyncSession, game: Game, reason: str) -> list[int]:
    """Cancel and refund. Returns the user IDs refunded."""
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


async def close_signups(
    session: AsyncSession, game: Game, *, round_seconds: int, now: datetime | None = None
) -> bool:
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
    game.next_tick_at = now + timedelta(seconds=round_seconds)
    return True


async def run_round(
    session: AsyncSession,
    game: Game,
    *,
    rng: Rng,
    round_seconds: int,
    now: datetime | None = None,
    winner_shares: tuple[int, ...] = (100,),
) -> RoundOutcome:
    """Advance one round, applying eliminations and paying out if it ends."""
    now = now or datetime.now(UTC)
    if game.status != "running":
        raise GameError("That game is not running.")

    everyone = await participants(session, game.id)
    by_user = {p.user_id: p for p in everyone}
    alive = [Tribute(user_id=p.user_id, name=str(p.user_id)) for p in everyone if p.alive]

    game.round_number += 1
    plan = plan_round(alive, rng=rng)

    for tribute in plan.eliminated:
        participant = by_user[tribute.user_id]
        participant.alive = False
        participant.eliminated_round = game.round_number

    if not plan.is_final:
        game.next_tick_at = now + timedelta(seconds=round_seconds)
        return RoundOutcome(plan=plan, round_number=game.round_number)

    # Finished. Work out standings from elimination order, then pay.
    eliminated_in_order = sorted(
        (p for p in everyone if not p.alive),
        key=lambda p: (p.eliminated_round or 0, p.joined_at),
    )
    winner = plan.winner
    standings = placements(
        [Tribute(user_id=p.user_id, name=str(p.user_id)) for p in eliminated_in_order],
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
