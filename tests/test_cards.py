"""Battle card rendering.

The card is cosmetic, but it is drawn from member-controlled input — avatars and
display names — inside a background loop that is posting a live game. So the
property that actually matters is that it **never raises**: a strange profile
picture or a 200-character nickname must degrade, not take the round down.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from shadow_bot.domain.cards import CARD_WIDTH, Fighter, render_duel


def _avatar(color: tuple[int, int, int] = (70, 120, 180), size: int = 128) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


# --- It produces a real image -------------------------------------------------


def test_renders_a_valid_png() -> None:
    result = render_duel("Hungry Games", 1, [(Fighter("A", _avatar()), Fighter("B", _avatar()))])
    image = _open(result.png)
    assert image.format == "PNG"
    assert image.width == CARD_WIDTH
    assert result.bytes_written == len(result.png)


def test_reports_the_faces_it_drew() -> None:
    """The count feeds the cost metrics, so it has to match what was rendered."""
    pairs = [
        (Fighter("A", _avatar()), Fighter("B", _avatar())),
        (Fighter("C", _avatar()), None),
    ]
    assert render_duel("Hungry Games", 1, pairs).faces == 3


def test_render_time_is_measured() -> None:
    result = render_duel("Hungry Games", 1, [(Fighter("A", _avatar()), None)])
    assert result.render_ms > 0


# --- It degrades instead of failing -------------------------------------------


def test_a_missing_avatar_renders_a_placeholder() -> None:
    result = render_duel("Hungry Games", 1, [(Fighter("Nobody", None), None)])
    assert _open(result.png).format == "PNG"


def test_a_corrupt_avatar_does_not_raise() -> None:
    """Discord can hand back anything. A decode failure must not kill the round."""
    result = render_duel("Hungry Games", 1, [(Fighter("Broken", b"this is not an image"), None)])
    assert _open(result.png).format == "PNG"


def test_a_truncated_image_does_not_raise() -> None:
    good = _avatar()
    result = render_duel("Hungry Games", 1, [(Fighter("Half", good[: len(good) // 2]), None)])
    assert _open(result.png).format == "PNG"


def test_an_absurd_name_still_renders() -> None:
    """Names are shrunk to fit rather than overflowing the card."""
    result = render_duel("Hungry Games", 1, [(Fighter("Bartholomew " * 20, _avatar()), None)])
    assert _open(result.png).width == CARD_WIDTH


def test_an_absurd_game_name_still_renders() -> None:
    result = render_duel("Kaos Pit " * 30, 99, [(Fighter("A", _avatar()), None)])
    assert _open(result.png).width == CARD_WIDTH


def test_no_pairs_still_renders() -> None:
    """A round of pure survival has nothing to draw, and must not crash."""
    assert _open(render_duel("Hungry Games", 1, []).png).format == "PNG"


@pytest.mark.parametrize("mode", ["L", "RGBA", "P"])
def test_unusual_avatar_modes_are_handled(mode: str) -> None:
    """Greyscale, transparent and paletted avatars all exist in the wild."""
    buffer = io.BytesIO()
    Image.new(mode, (96, 96)).save(buffer, format="PNG")
    result = render_duel("Hungry Games", 1, [(Fighter("Odd", buffer.getvalue()), None)])
    assert _open(result.png).format == "PNG"


def test_a_non_square_avatar_is_not_distorted() -> None:
    """Cover-fit crops rather than squashing, so faces keep their proportions."""
    buffer = io.BytesIO()
    Image.new("RGB", (400, 100), (10, 200, 10)).save(buffer, format="PNG")
    result = render_duel("Hungry Games", 1, [(Fighter("Wide", buffer.getvalue()), None)])
    assert _open(result.png).format == "PNG"


# --- Bounded work -------------------------------------------------------------


def test_more_than_three_pairs_are_capped() -> None:
    """A twenty-player round would otherwise render faces too small to recognise,
    and spend real CPU doing it."""
    many = [(Fighter(f"K{i}", _avatar()), Fighter(f"V{i}", _avatar())) for i in range(8)]
    capped = render_duel("Hungry Games", 1, many)
    assert capped.faces == 6, "at most three pairs are drawn"
    three = [(Fighter(f"K{i}", _avatar()), Fighter(f"V{i}", _avatar())) for i in range(3)]
    assert _open(capped.png).height == _open(render_duel("Hungry Games", 1, three).png).height


# --- The outcome is visible ---------------------------------------------------


def test_a_defeated_face_is_drawn_differently() -> None:
    """The whole point of the flag. If these matched, the card would be lying."""
    avatar = _avatar((20, 90, 220))
    alive = render_duel("Hungry Games", 1, [(Fighter("A", avatar, defeated=False), None)])
    dead = render_duel("Hungry Games", 1, [(Fighter("A", avatar, defeated=True), None)])
    assert alive.png != dead.png


def test_a_defeated_face_loses_its_colour() -> None:
    """Greyscaled and dimmed, so it reads at a glance rather than by comparison."""
    avatar = _avatar((20, 90, 220))  # strongly blue
    dead = _open(render_duel("Hungry Games", 1, [(Fighter("A", avatar, defeated=True), None)]).png)
    centre = dead.convert("RGB").getpixel((CARD_WIDTH // 2, dead.height // 2))
    red, green, blue = centre
    assert abs(red - green) < 12 and abs(green - blue) < 12, f"still coloured: {centre}"
    assert max(centre) < 170, f"not dimmed: {centre}"
