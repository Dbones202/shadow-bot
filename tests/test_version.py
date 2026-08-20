"""The version must be declared once.

It was previously hardcoded in three places — `VERSION`, `pyproject.toml`, and
`__init__.py`. They agreed on the day they were written and would have drifted
at the first release bump, with nothing to catch it. `VERSION` is now the single
source: pyproject reads the file, and `__init__` reports whatever was actually
installed.
"""

from __future__ import annotations

import pathlib
import re

import shadow_bot

VERSION_FILE = pathlib.Path(__file__).resolve().parents[1] / "VERSION"


def test_version_file_exists_and_is_a_release_number() -> None:
    assert VERSION_FILE.is_file(), "VERSION is the single source of truth"
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION_FILE.read_text().strip())


def test_installed_version_matches_the_version_file() -> None:
    """Catches an install that has gone stale against the source tree."""
    assert shadow_bot.__version__ == VERSION_FILE.read_text().strip()


def test_pyproject_does_not_hardcode_a_second_copy() -> None:
    pyproject = (VERSION_FILE.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert re.search(r'^version = "', pyproject, re.MULTILINE) is None
