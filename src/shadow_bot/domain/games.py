"""Round planning for the Hungry Games.

Pure structure only: this module decides *who* is eliminated and *how*, never
what the message says. Narration is applied by the caller from
`domain.narration`, which keeps the rules testable without any text and lets a
guild rewrite every line without touching the logic.

Randomness is injected as an object with `shuffle` and `choice` — tests pass a
seeded `random.Random`, production passes `random.SystemRandom`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Protocol


class Rng(Protocol):
    def shuffle(self, x: list) -> None: ...
    def choice(self, seq): ...
    def random(self) -> float: ...


class EventKind(StrEnum):
    #: Eliminated with no one responsible — the arena, hunger, bad berries.
    DEATH = "death"
    #: Eliminated by another tribute, who survives the round.
    KILL = "kill"
    #: Survived the round. Flavour only; changes nothing.
    SURVIVE = "survive"


@dataclass(frozen=True, slots=True)
class Tribute:
    user_id: int
    name: str


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    subject: Tribute
    #: Present only for KILL — the tribute eliminated by `subject`.
    victim: Tribute | None = None


@dataclass(frozen=True, slots=True)
class RoundPlan:
    events: tuple[Event, ...]
    eliminated: tuple[Tribute, ...]
    survivors: tuple[Tribute, ...]

    @property
    def is_final(self) -> bool:
        return len(self.survivors) <= 1

    @property
    def winner(self) -> Tribute | None:
        return self.survivors[0] if len(self.survivors) == 1 else None


DEFAULT_ELIMINATION_RATE = Decimal("0.30")
#: Chance an elimination is attributed to another tribute rather than the arena.
DEFAULT_KILL_SHARE = 0.5


def eliminations_for(alive: int, rate: Decimal = DEFAULT_ELIMINATION_RATE) -> int:
    """How many tributes fall this round.

    Two rules keep a game finite and non-degenerate:

    * **At least one** elimination whenever more than one tribute remains —
      otherwise a run of unlucky rounds could continue forever.
    * **Never everyone** — the round must leave a survivor, so the game ends
      with a winner rather than an empty arena.
    """
    if alive <= 1:
        return 0
    scaled = (Decimal(alive) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(1, min(int(scaled), alive - 1))


def plan_round(
    alive: list[Tribute],
    *,
    rng: Rng,
    rate: Decimal = DEFAULT_ELIMINATION_RATE,
    kill_share: float = DEFAULT_KILL_SHARE,
) -> RoundPlan:
    """Decide what happens to every living tribute this round.

    Every tribute appears in exactly one event, so nobody is silently skipped
    and the narration always accounts for the full field.
    """
    if not alive:
        return RoundPlan(events=(), eliminated=(), survivors=())
    if len(alive) == 1:
        return RoundPlan(events=(), eliminated=(), survivors=(alive[0],))

    order = list(alive)
    rng.shuffle(order)

    count = eliminations_for(len(order), rate)
    doomed = order[:count]
    survivors = order[count:]

    events: list[Event] = []
    killers_used: set[int] = set()

    for victim in doomed:
        # A kill needs a survivor to credit. Attribute at most one kill per
        # tribute per round so a single member does not appear three times.
        candidates = [t for t in survivors if t.user_id not in killers_used]
        if candidates and rng.random() < kill_share:
            killer = rng.choice(candidates)
            killers_used.add(killer.user_id)
            events.append(Event(kind=EventKind.KILL, subject=killer, victim=victim))
        else:
            events.append(Event(kind=EventKind.DEATH, subject=victim))

    for survivor in survivors:
        if survivor.user_id not in killers_used:
            events.append(Event(kind=EventKind.SURVIVE, subject=survivor))

    return RoundPlan(
        events=tuple(events),
        eliminated=tuple(doomed),
        survivors=tuple(survivors),
    )


def placements(eliminated_in_order: list[Tribute], winner: Tribute | None) -> dict[int, int]:
    """Final standings: 1st for the winner, then reverse elimination order.

    The last tribute eliminated placed second, the first eliminated placed last.
    """
    standings: dict[int, int] = {}
    if winner is not None:
        standings[winner.user_id] = 1
    place = len(eliminated_in_order) + (1 if winner else 0)
    for tribute in eliminated_in_order:
        standings[tribute.user_id] = place
        place -= 1
    return standings


def split_pot(
    pot: int, standings: dict[int, int], *, shares: tuple[int, ...] = (100,)
) -> dict[int, int]:
    """Divide the pot by finishing position.

    ``shares`` are percentages by place — ``(100,)`` is winner-take-all,
    ``(70, 20, 10)`` pays the top three. Any remainder from rounding goes to
    first place, so the full pot is always distributed and the numbers add up.
    """
    if pot <= 0 or not standings:
        return {}

    by_place = {place: user_id for user_id, place in standings.items()}
    payouts: dict[int, int] = {}
    distributed = 0

    for index, share in enumerate(shares, start=1):
        user_id = by_place.get(index)
        if user_id is None:
            continue
        amount = int(
            (Decimal(pot) * Decimal(share) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        payouts[user_id] = amount
        distributed += amount

    first = by_place.get(1)
    if first is not None and distributed != pot:
        payouts[first] = payouts.get(first, 0) + (pot - distributed)
    return payouts
