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

import random
import re
from collections import defaultdict
from pathlib import Path

#: ``{name}`` where name is letters, digits and underscores.
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SECTION = re.compile(r"^\[([a-z0-9_]+)\.([a-z0-9_]+)\]$", re.IGNORECASE)

_RNG = random.SystemRandom()


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
        """
        options = self.lines_for(category, outcome)
        if not options:
            return render(fallback, values) if fallback else ""
        return render(_RNG.choice(options), values)
