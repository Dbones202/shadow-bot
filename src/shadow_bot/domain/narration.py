"""Narration text — the lines shown when something happens in the economy.

Two jobs:

* **Parsing** a plain-text file of lines grouped by ``[category.outcome]``.
* **Rendering** one of those lines with placeholders filled in.

The file format is deliberately not JSON, TOML or YAML. These files are written
by hand, in bulk, by people writing jokes — and every one of those formats needs
escaping for the apostrophes and quotes that natural writing is full of
(``don't``, ``the boss's car``). One entry per line with no quoting at all is
the format that makes the actual job easy::

    # comments and blank lines are ignored

    [work.success]
    {user} pulled a double at the diner and made {amount}.
    {user} sold portraits outside the station and earned {amount}.

    [work.failure]
    {user} slept through the alarm. Docked {amount}.

Rendering deliberately avoids ``str.format``. Format strings permit attribute
access, so a line containing ``{user.__class__.__mro__}`` would leak internals —
a needless hazard when the text can be edited from Discord. Substitution here is
a whitelist: a known placeholder is replaced, and an unknown one is left exactly
as written so a typo like ``{amout}`` is visible in the output instead of
crashing or silently vanishing.
"""

from __future__ import annotations

import logging
import random
import re
from collections import defaultdict
from collections.abc import Iterable
from importlib.resources import as_file, files
from pathlib import Path

#: ``{name}`` where name is letters, digits and underscores.
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SECTION = re.compile(r"^\[([a-z0-9_]+)\.([a-z0-9_]+)\]$", re.IGNORECASE)

_RNG = random.SystemRandom()

LOGGER = logging.getLogger(__name__)

#: The event-file library (M9): one file per category, holding every
#: [outcome] section for that category. Order does not matter for loading —
#: it only matters here in that it is the authoritative list of what ships.
EVENT_CATEGORY_FILES: tuple[str, ...] = (
    "hungrygames.md",
    "work.md",
    "crime.md",
    "steal.md",
    "slut.md",
)


class NarrationError(ValueError):
    """Raised when a narration file cannot be read."""


def render(template: str, values: dict[str, str]) -> str:
    """Fill ``{placeholders}`` from ``values``, leaving unknown ones untouched."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def placeholders_in(template: str) -> set[str]:
    """Every placeholder name a line references. Useful for validating new lines."""
    return set(_PLACEHOLDER.findall(template))


def unknown_placeholders(template: str, known: set[str]) -> set[str]:
    """Placeholders a line uses that nothing will fill in.

    Reported to whoever is adding the line rather than discovered later by a
    member seeing `{amout}` in the middle of a sentence.
    """
    return placeholders_in(template) - known


def parse(text: str) -> dict[tuple[str, str], list[str]]:
    """Read narration text into ``{(category, outcome): [lines]}``.

    Raises:
        NarrationError: if a line appears before any section header, or a
            header is malformed. Both are silent data loss otherwise.
    """
    lines: dict[tuple[str, str], list[str]] = defaultdict(list)
    current: tuple[str, str] | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("["):
            match = _SECTION.match(line)
            if match is None:
                raise NarrationError(
                    f"Line {number}: `{line}` is not a valid section header. "
                    "Headers look like `[work.success]`."
                )
            current = (match.group(1).lower(), match.group(2).lower())
            lines.setdefault(current, [])
            continue

        if current is None:
            raise NarrationError(
                f"Line {number}: text appears before any `[category.outcome]` header."
            )
        lines[current].append(line)

    return dict(lines)


def load(path: Path) -> dict[tuple[str, str], list[str]]:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise NarrationError(f"Could not read {path}: {exc}") from exc


def load_bundle(paths: Iterable[Path]) -> dict[tuple[str, str], list[str]]:
    """Load and merge several narration files.

    Sections are additive across files — two files defining the same
    ``[category.outcome]`` header both contribute lines rather than one
    replacing the other, though in practice each category owns one file.
    A parse error anywhere aborts the whole bundle: ``load_event_library``
    treats that as "this source is unusable" and falls back, rather than
    silently shipping a half-loaded library.
    """
    merged: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path in paths:
        try:
            for key, lines in load(path).items():
                merged[key].extend(lines)
        except NarrationError as exc:
            raise NarrationError(f"{path}: {exc}") from exc
    return dict(merged)


def load_event_library(events_dir: Path | None) -> dict[tuple[str, str], list[str]]:
    """Load the narration defaults every guild falls back to.

    Donovan is the sole editor of these. ``events_dir`` — conventionally
    ``/etc/shadow-bot/events/`` — lives outside the installed package so a
    ``pip install --upgrade`` cannot clobber his edits; the packaged copy
    under ``data/narration/`` ships as a working default so a fresh install
    is never mute.

    Reloading is not a live operation — there is no running command that
    swaps this out from under the bot. It is read once, at startup. To pick
    up an edit, restart the bot; that is a deliberate simplification, not an
    oversight, because only Donovan has access to the box.

    Falls back to the packaged copy whenever ``events_dir`` is unset, does
    not exist, contains none of the expected category files, or fails to
    parse — a bad edit should not leave the bot without narration, or worse,
    fail to start.
    """
    if events_dir is not None:
        try:
            candidates = [
                events_dir / name
                for name in EVENT_CATEGORY_FILES
                if (events_dir / name).is_file()
            ]
        except OSError:
            candidates = []

        if candidates:
            try:
                return load_bundle(candidates)
            except NarrationError:
                LOGGER.exception(
                    "EVENTS_DIR %s has an invalid narration file; "
                    "falling back to the packaged defaults",
                    events_dir,
                )
        else:
            LOGGER.info(
                "EVENTS_DIR %s has none of the expected narration files "
                "(%s); using the packaged defaults",
                events_dir,
                ", ".join(EVENT_CATEGORY_FILES),
            )

    packaged = files("shadow_bot") / "data" / "narration"
    with as_file(packaged) as packaged_dir:
        try:
            return load_bundle(
                packaged_dir / name
                for name in EVENT_CATEGORY_FILES
                if (packaged_dir / name).is_file()
            )
        except NarrationError:
            LOGGER.exception(
                "Packaged narration is invalid; the bot is starting with no narration at all"
            )
            return {}


class NarrationLibrary:
    """Lines for one guild: its own entries first, bundled defaults as fallback.

    A guild that has written its own lines for `work.success` uses only those.
    One that has not gets the defaults, so a new server reads well immediately
    without anyone having to write anything.
    """

    def __init__(
        self,
        defaults: dict[tuple[str, str], list[str]] | None = None,
        overrides: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        self.defaults = defaults or {}
        self.overrides = overrides or {}

    def lines_for(self, category: str, outcome: str) -> list[str]:
        key = (category.lower(), outcome.lower())
        custom = self.overrides.get(key)
        if custom:
            return custom
        return self.defaults.get(key, [])

    def pick(
        self, category: str, outcome: str, values: dict[str, str], *, fallback: str = ""
    ) -> str:
        """Choose a line at random and render it.

        Returns ``fallback`` when nothing is configured, so a missing section
        degrades to a plain message rather than an empty embed.

        Every call is independent. For anything with a run of related messages —
        a whole game — use `NarrationSession`, which avoids repeats.
        """
        options = self.lines_for(category, outcome)
        if not options:
            return render(fallback, values) if fallback else ""
        return render(_RNG.choice(options), values)


class NarrationSession:
    """A library plus the memory of what it has already said.

    Independent random choices repeat far more than people expect. A five-player
    game runs three rounds off five kill lines, and picking each one freshly
    produced this in testing::

        Bree eliminates Odile and does not seem sorry about it.
        Cassius eliminates Petra and does not seem sorry about it.

    Two identical lines side by side read as a bug even though each draw was
    perfectly fair. Drawing without replacement fixes it: a line is not offered
    again until its section is exhausted, at which point the pool resets and the
    section starts over.

    Scope one of these to a single game (or a single command response) — the
    memory is meant to be short-lived. `NarrationLibrary` stays stateless so it
    can be shared across guilds and cached.
    """

    def __init__(self, library: NarrationLibrary, *, rng: random.Random | None = None) -> None:
        self.library = library
        self._rng = rng or _RNG
        #: (category, outcome, line) triples already used in this session.
        self._used: set[tuple[str, str, str]] = set()
        #: The most recent line per section, so a pool reset cannot immediately
        #: repeat the line it just finished on.
        self._last: dict[tuple[str, str], str] = {}

    def pick(
        self, category: str, outcome: str, values: dict[str, str], *, fallback: str = ""
    ) -> str:
        category, outcome = category.lower(), outcome.lower()
        options = self.library.lines_for(category, outcome)
        if not options:
            return render(fallback, values) if fallback else ""

        fresh = [line for line in options if (category, outcome, line) not in self._used]
        if not fresh:
            # Exhausted: forget this section only and start it over. Other
            # sections keep their memory, so a long game does not reset
            # everything the moment one short section runs dry.
            self._used -= {(category, outcome, line) for line in options}
            fresh = list(options)
            # A section with three lines and four uses wraps around, and without
            # this the wrap can land on the line that just fired — the exact
            # back-to-back repeat the whole class exists to prevent.
            previous = self._last.get((category, outcome))
            if previous is not None and len(fresh) > 1:
                fresh = [line for line in fresh if line != previous]

        chosen = self._rng.choice(fresh)
        self._used.add((category, outcome, chosen))
        self._last[(category, outcome)] = chosen
        return render(chosen, values)
