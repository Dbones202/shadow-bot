"""Battle cards — the picture posted alongside a round's eliminations.

The whole background is drawn in code rather than composited onto a supplied
PNG. That is deliberate: servers can rename the game, and a name baked into an
image asset would have every server's card announcing the same thing regardless
of what they called theirs. Drawing it means the card says *their* name.

Everything renders into memory and is handed back as bytes. Nothing touches the
filesystem, which matters because the systemd unit runs `ProtectSystem=strict` —
a disk-writing design would have forced a hole in that hardening.

This module knows nothing about Discord. It takes avatar bytes and names, and
returns PNG bytes plus timings, so the expensive part can be measured and tested
without a gateway connection.
"""

from __future__ import annotations

import io
import math
import time
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from PIL import Image, ImageDraw, ImageFont

#: Card geometry. Sized so two faces sit comfortably side by side and the whole
#: thing still reads on a phone without tapping to expand.
CARD_WIDTH = 900
CARD_HEIGHT = 460
FACE = 260
MARGIN = 40
#: Vertical space reserved for the title. Generous because the sword tips reach
#: upward and would otherwise cut through the lettering.
TITLE_BAND = 104

#: Palette. Warm and high-contrast so it stands out in a channel, and legible
#: against both Discord themes.
BACKDROP = (196, 44, 84)
BACKDROP_DEEP = (122, 22, 58)
EMBER = (245, 158, 52)
CREAM = (255, 243, 224)
INK = (36, 12, 22)
STEEL = (214, 220, 228)
STEEL_DARK = (150, 158, 170)
HILT = (122, 78, 42)


@dataclass(frozen=True, slots=True)
class Fighter:
    """One face on the card."""

    name: str
    #: Raw avatar bytes. None renders a placeholder rather than failing — a
    #: missing avatar must never take a round down.
    avatar: bytes | None = None
    #: Eliminated tributes are greyscaled and dimmed so the outcome reads at a
    #: glance without covering the picture people came to look at.
    defeated: bool = False


@dataclass(frozen=True, slots=True)
class RenderResult:
    png: bytes
    #: Wall-clock milliseconds spent drawing, excluding avatar fetching.
    render_ms: float
    faces: int

    @property
    def bytes_written(self) -> int:
        return len(self.png)


@lru_cache(maxsize=8)
def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Load a bundled face. Cached — reparsing a 400 KB TTF per card is waste."""
    name = "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
    with (files("shadow_bot") / "data" / "fonts" / name).open("rb") as handle:
        return ImageFont.truetype(handle, size)


def _backdrop(width: int, height: int) -> Image.Image:
    """A warm vertical wash with an ember border.

    Built with a tiny gradient stretched to size rather than a per-pixel loop:
    the same look for a fraction of the work.
    """
    small = Image.new("RGB", (1, 64))
    for y in range(64):
        blend = y / 63
        small.putpixel(
            (0, y),
            tuple(int(BACKDROP[i] + (BACKDROP_DEEP[i] - BACKDROP[i]) * blend) for i in range(3)),
        )
    card = small.resize((width, height), Image.BILINEAR)

    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([(8, 8), (width - 9, height - 9)], radius=28, outline=EMBER, width=6)
    return card


def _circle_face(source: bytes | None, size: int, *, defeated: bool) -> Image.Image:
    """One avatar, cropped to a circle, with a ring.

    A defeated face is desaturated and darkened. Greyscale alone is too subtle
    against a warm background, and an overlay X would cover the face.
    """
    try:
        avatar = Image.open(io.BytesIO(source)).convert("RGB") if source else None
    except Exception:  # noqa: BLE001 - any decode failure degrades to a placeholder
        avatar = None

    if avatar is None:
        avatar = Image.new("RGB", (size, size), STEEL_DARK)
    else:
        # Cover-fit: fill the square without distorting the picture.
        scale = max(size / avatar.width, size / avatar.height)
        resized = avatar.resize(
            (max(1, int(avatar.width * scale)), max(1, int(avatar.height * scale))),
            Image.LANCZOS,
        )
        left = (resized.width - size) // 2
        top = (resized.height - size) // 2
        avatar = resized.crop((left, top, left + size, top + size))

    if defeated:
        avatar = avatar.convert("L").convert("RGB")
        avatar = Image.blend(avatar, Image.new("RGB", avatar.size, INK), 0.45)

    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse([(0, 0), (size * 4 - 1, size * 4 - 1)], fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)  # cheap antialiasing

    framed = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    framed.paste(avatar, (0, 0), mask)

    ring = STEEL_DARK if defeated else CREAM
    ImageDraw.Draw(framed).ellipse([(1, 1), (size - 2, size - 2)], outline=ring, width=6)
    return framed


def _sword(draw: ImageDraw.ImageDraw, cx: int, cy: int, length: int, angle: int) -> None:
    """One sword crossing the centre point, rotated by ±angle degrees.

    Drawn after the faces so the blades lie over them, which is what makes the
    pair read as a duel rather than two portraits with decoration between.
    """
    rad = math.radians(angle)
    dx, dy = math.sin(rad), -math.cos(rad)

    def along(distance: float) -> tuple[float, float]:
        return (cx + dx * distance, cy + dy * distance)

    half = length / 2
    tip = along(half)
    butt = along(-half)
    guard = along(-half * 0.42)

    # Perpendicular, for the crossguard and the tapered point.
    px, py = -dy, dx

    # Blade: a dark edge under a bright core reads as steel at small sizes.
    draw.line([guard, along(half * 0.86)], fill=STEEL_DARK, width=22)
    draw.line([guard, along(half * 0.86)], fill=STEEL, width=16)
    draw.line([guard, along(half * 0.86)], fill=CREAM, width=5)
    # Point.
    shoulder = along(half * 0.86)
    draw.polygon(
        [
            (shoulder[0] - px * 11, shoulder[1] - py * 11),
            (shoulder[0] + px * 11, shoulder[1] + py * 11),
            tip,
        ],
        fill=STEEL,
    )

    # Crossguard and grip.
    draw.line(
        [(guard[0] - px * 46, guard[1] - py * 46), (guard[0] + px * 46, guard[1] + py * 46)],
        fill=EMBER,
        width=16,
    )
    draw.line([butt, guard], fill=HILT, width=20)
    draw.ellipse([butt[0] - 13, butt[1] - 13, butt[0] + 13, butt[1] + 13], fill=EMBER)


def _fit(text: str, font_size: int, max_width: int, *, bold: bool = True):
    """Shrink text until it fits, so a long display name cannot overflow."""
    size = font_size
    while size > 10:
        font = _font(size, bold)
        if font.getlength(text) <= max_width:
            return font
        size -= 2
    return _font(10, bold)


def _label(draw: ImageDraw.ImageDraw, text: str, cx: int, top: int, max_width: int) -> None:
    font = _fit(text, 34, max_width)
    width = font.getlength(text)
    x = cx - width / 2
    # Drawn twice: a dark pass behind gives contrast over any avatar colour.
    draw.text((x + 2, top + 2), text, font=font, fill=INK)
    draw.text((x, top), text, font=font, fill=CREAM)


def render_duel(
    game_name: str,
    round_number: int,
    pairs: list[tuple[Fighter, Fighter | None]],
) -> RenderResult:
    """Draw one card for a round's eliminations.

    Each entry is either a killer and their victim (drawn as a duel, swords
    between them) or a lone tribute the arena took (drawn by itself). Survivors
    are not shown — the card is about what happened, and a full roster would
    make it unreadable.

    Rendering never raises for content reasons: an unreadable avatar becomes a
    placeholder and an over-long name is shrunk to fit. A round must not fail to
    post because someone set a strange profile picture.
    """
    started = time.perf_counter()
    pairs = pairs[:3]  # beyond three the faces are too small to recognise
    faces = sum(2 if b is not None else 1 for a, b in pairs)

    rows = max(1, len(pairs))
    row_height = FACE + 74
    height = MARGIN * 2 + TITLE_BAND + rows * row_height
    card = _backdrop(CARD_WIDTH, height)
    draw = ImageDraw.Draw(card)

    title = f"{game_name} — Round {round_number}"
    title_font = _fit(title, 44, CARD_WIDTH - MARGIN * 4)
    tw = title_font.getlength(title)
    draw.text(((CARD_WIDTH - tw) / 2 + 2, MARGIN + 2), title, font=title_font, fill=INK)
    draw.text(((CARD_WIDTH - tw) / 2, MARGIN), title, font=title_font, fill=CREAM)

    top = MARGIN + TITLE_BAND
    for left_fighter, right_fighter in pairs:
        centre_y = top + FACE // 2
        if right_fighter is None:
            face = _circle_face(left_fighter.avatar, FACE, defeated=left_fighter.defeated)
            x = (CARD_WIDTH - FACE) // 2
            card.paste(face, (x, top), face)
            _label(draw, left_fighter.name, CARD_WIDTH // 2, top + FACE + 12, CARD_WIDTH - 80)
        else:
            gap = 140
            left_x = (CARD_WIDTH - FACE * 2 - gap) // 2
            right_x = left_x + FACE + gap
            for fighter, x in ((left_fighter, left_x), (right_fighter, right_x)):
                face = _circle_face(fighter.avatar, FACE, defeated=fighter.defeated)
                card.paste(face, (x, top), face)
                _label(draw, fighter.name, x + FACE // 2, top + FACE + 12, FACE + gap - 20)
            blade = int(FACE * 1.45)
            _sword(draw, CARD_WIDTH // 2, centre_y, blade, 42)
            _sword(draw, CARD_WIDTH // 2, centre_y, blade, -42)
        top += row_height

    buffer = io.BytesIO()
    # optimize=True costs a few milliseconds and saves meaningfully more upload
    # time than it spends, which is the slower half of posting a round.
    card.save(buffer, format="PNG", optimize=True)

    return RenderResult(
        png=buffer.getvalue(),
        render_ms=(time.perf_counter() - started) * 1000,
        faces=faces,
    )
