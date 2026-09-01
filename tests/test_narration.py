import random
import re

import pytest

from shadow_bot.domain.narration import (
    EVENT_CATEGORY_FILES,
    NarrationError,
    NarrationLibrary,
    NarrationSession,
    load_bundle,
    load_event_library,
    parse,
    placeholders_in,
    render,
    unknown_placeholders,
)

SAMPLE = """
# A comment, ignored.

[work.success]
{user} pulled a double and made {amount}.
{user} sold portraits and earned {amount}.

[work.failure]
{user} slept in. Docked {amount}.

[steal.empty]
{user} found nothing in {target}'s pockets.
"""


# --- Parsing ------------------------------------------------------------------


def test_parses_sections_and_lines() -> None:
    parsed = parse(SAMPLE)
    assert len(parsed[("work", "success")]) == 2
    assert len(parsed[("work", "failure")]) == 1
    assert parsed[("steal", "empty")] == ["{user} found nothing in {target}'s pockets."]


def test_comments_and_blank_lines_are_ignored() -> None:
    parsed = parse("[a.b]\n\n# note\n\nline one\n")
    assert parsed[("a", "b")] == ["line one"]


def test_apostrophes_and_quotes_need_no_escaping() -> None:
    """The whole reason for this format rather than TOML or JSON."""
    text = '[work.success]\n{user} didn\'t quit, and the boss said "fine", paying {amount}.\n'
    assert parse(text)[("work", "success")][0].count("'") == 1


def test_headers_are_case_insensitive_but_normalise_to_lower() -> None:
    assert ("work", "success") in parse("[Work.Success]\nline\n")


def test_text_before_any_header_is_an_error() -> None:
    """Silently dropping it would lose lines someone wrote."""
    with pytest.raises(NarrationError, match="before any"):
        parse("a stray line\n[work.success]\nreal line\n")


def test_malformed_header_is_an_error() -> None:
    with pytest.raises(NarrationError, match="not a valid section header"):
        parse("[work success]\nline\n")


def test_error_reports_the_line_number() -> None:
    with pytest.raises(NarrationError, match="Line 3"):
        parse("[a.b]\nfine\n[broken\n")


def test_an_empty_section_is_kept_but_empty() -> None:
    parsed = parse("[work.success]\n[work.failure]\nonly this\n")
    assert parsed[("work", "success")] == []


# --- Rendering ----------------------------------------------------------------


def test_renders_known_placeholders() -> None:
    assert render("{user} earned {amount}.", {"user": "Ada", "amount": "50 coins"}) == (
        "Ada earned 50 coins."
    )


def test_unknown_placeholder_is_left_visible() -> None:
    """A typo should be obvious to whoever wrote the line, not silently blank."""
    assert render("{user} earned {amout}.", {"user": "Ada"}) == "Ada earned {amout}."


def test_repeated_placeholders_all_substitute() -> None:
    assert render("{user}, {user}, {user}", {"user": "Ada"}) == "Ada, Ada, Ada"


def test_attribute_access_is_not_possible() -> None:
    """str.format would evaluate this and leak internals. Substitution must not.

    This is the reason the module does not use str.format at all: narration text
    is editable from Discord, so it must never be able to reach into objects.
    """
    hostile = "{user.__class__.__mro__}"
    assert render(hostile, {"user": "Ada"}) == hostile


def test_braces_that_are_not_placeholders_survive() -> None:
    assert render("100% {of} it", {}) == "100% {of} it"


def test_placeholders_in_reports_names() -> None:
    assert placeholders_in("{user} paid {target} {amount}") == {"user", "target", "amount"}


def test_unknown_placeholders_helps_validate_new_lines() -> None:
    known = {"user", "amount"}
    assert unknown_placeholders("{user} got {amount} from {mystery}", known) == {"mystery"}
    assert unknown_placeholders("{user} got {amount}", known) == set()


# --- Library ------------------------------------------------------------------


def _library() -> NarrationLibrary:
    return NarrationLibrary(
        defaults={("work", "success"): ["default line"], ("work", "failure"): ["default fail"]},
        overrides={("work", "success"): ["custom line"]},
    )


def test_guild_overrides_replace_defaults_for_that_section_only() -> None:
    library = _library()
    assert library.lines_for("work", "success") == ["custom line"]
    assert library.lines_for("work", "failure") == ["default fail"]


def test_missing_section_returns_nothing() -> None:
    assert _library().lines_for("crime", "success") == []


def test_pick_renders_a_configured_line() -> None:
    library = NarrationLibrary(defaults={("work", "success"): ["{user} won {amount}"]})
    assert library.pick("work", "success", {"user": "Ada", "amount": "5"}) == "Ada won 5"


def test_pick_falls_back_when_nothing_is_configured() -> None:
    """A missing section must degrade to a plain sentence, not an empty embed."""
    library = NarrationLibrary()
    assert (
        library.pick("work", "success", {"amount": "5 coins"}, fallback="You earned {amount}.")
        == "You earned 5 coins."
    )


def test_pick_returns_empty_when_there_is_no_fallback_either() -> None:
    assert NarrationLibrary().pick("work", "success", {}) == ""


def test_pick_only_ever_returns_a_configured_line() -> None:
    library = NarrationLibrary(defaults={("a", "b"): ["one", "two", "three"]})
    seen = {library.pick("a", "b", {}) for _ in range(50)}
    assert seen <= {"one", "two", "three"}


# --- Session (no-repeat) ------------------------------------------------------
#
# The bug these cover: independent random draws repeat far more than people
# expect. A real five-player game produced two identical kill lines in adjacent
# events, which reads as broken even though each draw was fair.


def _session(lines: dict[tuple[str, str], list[str]], seed: int = 7) -> NarrationSession:
    return NarrationSession(NarrationLibrary(defaults=lines), rng=random.Random(seed))


def test_a_full_pass_uses_every_line_exactly_once() -> None:
    """The hard guarantee: N picks from N lines is a permutation, not a sample.

    This is what plain random.choice cannot promise, and asserting the
    permutation rather than 'usually distinct' keeps the test deterministic.
    """
    lines = [f"line {i}" for i in range(10)]
    session = _session({("hungrygames", "kill"): lines})
    picks = [session.pick("hungrygames", "kill", {}) for _ in range(10)]
    assert sorted(picks) == sorted(lines)


def test_the_pool_resets_once_exhausted() -> None:
    """A long game must not run out of things to say."""
    session = _session({("a", "b"): ["one", "two"]})
    picks = [session.pick("a", "b", {}) for _ in range(6)]
    assert len(picks) == 6
    assert set(picks) == {"one", "two"}
    # Three complete passes, so each line appears exactly three times.
    assert picks.count("one") == 3
    assert picks.count("two") == 3


def test_exhausting_one_section_does_not_reset_another() -> None:
    """Otherwise a short section keeps wiping the memory of a long one."""
    session = _session({("a", "b"): ["only"], ("c", "d"): ["x", "y", "z"]})
    first = session.pick("c", "d", {})
    for _ in range(5):
        session.pick("a", "b", {})  # exhausts and resets (a, b) repeatedly
    following = [session.pick("c", "d", {}) for _ in range(2)]
    assert first not in following


def test_session_renders_placeholders() -> None:
    session = _session({("hungrygames", "kill"): ["{killer} takes out {victim}."]})
    assert session.pick("hungrygames", "kill", {"killer": "Bree", "victim": "Odile"}) == (
        "Bree takes out Odile."
    )


def test_session_falls_back_when_nothing_is_configured() -> None:
    session = _session({})
    assert session.pick("a", "b", {"n": "3"}, fallback="{n} fell.") == "3 fell."


def test_session_returns_empty_with_no_fallback() -> None:
    assert _session({}).pick("a", "b", {}) == ""


def test_overrides_are_respected_by_the_session() -> None:
    library = NarrationLibrary(
        defaults={("a", "b"): ["default"]}, overrides={("a", "b"): ["custom"]}
    )
    session = NarrationSession(library, rng=random.Random(1))
    assert session.pick("a", "b", {}) == "custom"


def test_a_pool_reset_never_repeats_the_line_it_just_used() -> None:
    """Three lines and four uses wraps the pool. The wrap must not land on the
    line that just fired — that is the exact back-to-back repeat being avoided.

    Checked over many seeds because the failure is a specific unlucky draw, not
    a certainty: with three options a naive reset repeats roughly a third of the
    time.
    """
    for seed in range(60):
        session = _session({("a", "b"): ["one", "two", "three"]}, seed=seed)
        picks = [session.pick("a", "b", {}) for _ in range(9)]
        for earlier, later in zip(picks, picks[1:], strict=False):
            assert earlier != later, f"seed {seed} repeated {earlier!r} back to back"


def test_a_single_line_section_still_works() -> None:
    """The guard must not empty the pool when there is nothing else to pick."""
    session = _session({("a", "b"): ["only line"]})
    assert [session.pick("a", "b", {}) for _ in range(3)] == ["only line"] * 3


# --- Event file library (M9) ---------------------------------------------------
#
# EVENTS_DIR lives outside the installed package so Donovan's edits survive a
# `pip install --upgrade`. These cover the fallback ladder: a good directory is
# used as-is, a missing/empty one falls back to the packaged copy, and a broken
# file in the directory falls back too rather than crashing the bot at startup.


def test_load_bundle_merges_multiple_files(tmp_path) -> None:
    (tmp_path / "work.md").write_text("[work.success]\nline one\n", encoding="utf-8")
    (tmp_path / "crime.md").write_text("[crime.failure]\nline two\n", encoding="utf-8")
    merged = load_bundle([tmp_path / "work.md", tmp_path / "crime.md"])
    assert merged[("work", "success")] == ["line one"]
    assert merged[("crime", "failure")] == ["line two"]


def test_load_bundle_raises_with_the_offending_path(tmp_path) -> None:
    bad = tmp_path / "work.md"
    bad.write_text("not a header\n", encoding="utf-8")
    with pytest.raises(NarrationError, match=re.escape(str(bad))):
        load_bundle([bad])


def test_load_event_library_prefers_events_dir_when_present(tmp_path) -> None:
    (tmp_path / "work.md").write_text("[work.success]\ncustom line\n", encoding="utf-8")
    library = load_event_library(tmp_path)
    assert library[("work", "success")] == ["custom line"]
    # Only the file that exists in events_dir is read — nothing packaged leaks in.
    assert ("crime", "success") not in library


def test_load_event_library_falls_back_when_events_dir_is_none() -> None:
    library = load_event_library(None)
    assert ("work", "success") in library
    assert ("hungrygames", "winner") in library


def test_load_event_library_falls_back_when_events_dir_is_missing(tmp_path) -> None:
    library = load_event_library(tmp_path / "does-not-exist")
    assert ("work", "success") in library


def test_load_event_library_falls_back_when_events_dir_is_empty(tmp_path) -> None:
    library = load_event_library(tmp_path)
    assert ("work", "success") in library


def test_load_event_library_falls_back_when_a_file_is_broken(tmp_path) -> None:
    """A bad edit must not leave the bot mute or take down startup."""
    (tmp_path / "work.md").write_text("not a header\n", encoding="utf-8")
    library = load_event_library(tmp_path)
    assert ("work", "success") in library


def test_event_category_files_cover_every_activity_and_hungrygames() -> None:
    assert set(EVENT_CATEGORY_FILES) == {
        "hungrygames.md",
        "work.md",
        "crime.md",
        "steal.md",
        "slut.md",
    }
