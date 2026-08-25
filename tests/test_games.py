import random
from decimal import Decimal

import pytest

from shadow_bot.domain.games import (
    EventKind,
    Tribute,
    eliminations_for,
    placements,
    plan_round,
    split_pot,
)


def tributes(n: int) -> list[Tribute]:
    return [Tribute(user_id=i, name=f"T{i}") for i in range(1, n + 1)]


def rng(seed: int = 42) -> random.Random:
    return random.Random(seed)


# --- How many fall ------------------------------------------------------------


@pytest.mark.parametrize("alive", [0, 1])
def test_no_eliminations_when_the_game_is_already_over(alive: int) -> None:
    assert eliminations_for(alive) == 0


def test_two_tributes_leaves_exactly_one() -> None:
    assert eliminations_for(2) == 1


def test_always_eliminates_at_least_one_so_a_game_terminates() -> None:
    """Without this a run of small fields could loop forever."""
    for alive in range(2, 8):
        assert eliminations_for(alive, Decimal("0.001")) >= 1


def test_never_eliminates_everyone() -> None:
    """The arena must leave a winner, not be emptied."""
    for alive in range(2, 40):
        assert eliminations_for(alive, Decimal("1")) == alive - 1


def test_rate_scales_with_the_field() -> None:
    assert eliminations_for(10, Decimal("0.3")) == 3
    assert eliminations_for(20, Decimal("0.3")) == 6


def test_rounding_is_half_up() -> None:
    # 5 * 0.3 = 1.5 -> 2, where Python's round() would give 2 as well, but
    # 5 * 0.5 = 2.5 -> 3, where banker's rounding would give 2.
    assert eliminations_for(5, Decimal("0.5")) == 3


# --- Round planning -----------------------------------------------------------


def test_every_tribute_appears_in_exactly_one_event() -> None:
    """Nobody is silently skipped, so the narration covers the whole field."""
    field = tributes(12)
    plan = plan_round(field, rng=rng())
    appearing = [e.subject.user_id for e in plan.events]
    appearing += [e.victim.user_id for e in plan.events if e.victim]
    assert sorted(appearing) == sorted(t.user_id for t in field)


def test_eliminated_and_survivors_partition_the_field() -> None:
    field = tributes(9)
    plan = plan_round(field, rng=rng())
    assert len(plan.eliminated) + len(plan.survivors) == 9
    assert not ({t.user_id for t in plan.eliminated} & {t.user_id for t in plan.survivors})


def test_a_killer_is_always_a_survivor() -> None:
    """Crediting a kill to someone who just died would read as nonsense."""
    for seed in range(25):
        plan = plan_round(tributes(10), rng=rng(seed))
        survivor_ids = {t.user_id for t in plan.survivors}
        for event in plan.events:
            if event.kind is EventKind.KILL:
                assert event.subject.user_id in survivor_ids


def test_a_victim_is_always_eliminated() -> None:
    for seed in range(25):
        plan = plan_round(tributes(10), rng=rng(seed))
        doomed = {t.user_id for t in plan.eliminated}
        for event in plan.events:
            if event.victim is not None:
                assert event.victim.user_id in doomed


def test_nobody_kills_twice_in_one_round() -> None:
    """Otherwise one name dominates a round's narration."""
    for seed in range(25):
        plan = plan_round(tributes(12), rng=rng(seed), kill_share=1.0)
        killers = [e.subject.user_id for e in plan.events if e.kind is EventKind.KILL]
        assert len(killers) == len(set(killers))


def test_kill_share_of_zero_gives_only_deaths() -> None:
    plan = plan_round(tributes(10), rng=rng(), kill_share=0.0)
    assert all(e.kind is not EventKind.KILL for e in plan.events)


def test_two_tributes_produces_a_winner() -> None:
    plan = plan_round(tributes(2), rng=rng())
    assert plan.is_final
    assert plan.winner is not None
    assert len(plan.eliminated) == 1


def test_a_single_tribute_is_already_the_winner() -> None:
    plan = plan_round(tributes(1), rng=rng())
    assert plan.winner is not None
    assert plan.events == ()


def test_an_empty_field_is_handled() -> None:
    plan = plan_round([], rng=rng())
    assert plan.events == () and plan.winner is None


def test_a_game_always_terminates() -> None:
    """Run it to completion from many sizes and seeds; must always end with one."""
    for size in (2, 3, 5, 11, 24):
        for seed in range(10):
            alive = tributes(size)
            generator = rng(seed)
            rounds = 0
            while len(alive) > 1:
                plan = plan_round(alive, rng=generator)
                alive = list(plan.survivors)
                rounds += 1
                assert rounds < 200, "game failed to terminate"
            assert len(alive) == 1


# --- Standings and payouts ----------------------------------------------------


def test_placements_rank_the_winner_first_and_the_first_out_last() -> None:
    field = tributes(4)
    eliminated = [field[0], field[1], field[2]]  # in elimination order
    standings = placements(eliminated, winner=field[3])
    assert standings[field[3].user_id] == 1
    assert standings[field[2].user_id] == 2  # last eliminated
    assert standings[field[0].user_id] == 4  # first eliminated


def test_winner_takes_all_by_default() -> None:
    standings = {10: 1, 20: 2, 30: 3}
    assert split_pot(1_000, standings) == {10: 1_000}


def test_pot_can_be_split_across_places() -> None:
    standings = {10: 1, 20: 2, 30: 3}
    payouts = split_pot(1_000, standings, shares=(70, 20, 10))
    assert payouts == {10: 700, 20: 200, 30: 100}


def test_the_whole_pot_is_always_distributed() -> None:
    """Rounding must not quietly destroy currency."""
    for pot in (1, 7, 33, 101, 999, 1_000_001):
        payouts = split_pot(pot, {10: 1, 20: 2, 30: 3}, shares=(70, 20, 10))
        assert sum(payouts.values()) == pot, pot


def test_remainder_goes_to_first_place() -> None:
    payouts = split_pot(10, {10: 1, 20: 2, 30: 3}, shares=(33, 33, 33))
    assert sum(payouts.values()) == 10
    assert payouts[10] > payouts[20]


def test_shares_beyond_the_field_are_ignored() -> None:
    payouts = split_pot(100, {10: 1}, shares=(70, 20, 10))
    assert payouts == {10: 100}


def test_no_pot_pays_nothing() -> None:
    assert split_pot(0, {10: 1}) == {}
