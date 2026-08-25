import pytest

from shadow_bot.domain.narration import (
    NarrationError,
    NarrationLibrary,
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
