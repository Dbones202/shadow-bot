"""Guards against the class of bug that silently broke startup once already.

A package rename left `cogs/member_lifecycle.py` importing the old module path.
Nothing caught it because the domain tests never touch the cogs, so the bot only
failed at runtime inside `setup_hook`. These tests import every extension the bot
loads, so the same mistake now fails CI instead of the deployment.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import shadow_bot.cogs
from shadow_bot.bot import EXTENSIONS


def _discovered_cog_modules() -> list[str]:
    return [
        f"{shadow_bot.cogs.__name__}.{module.name}"
        for module in pkgutil.iter_modules(shadow_bot.cogs.__path__)
        if not module.name.startswith("_")
    ]


@pytest.mark.parametrize("extension", EXTENSIONS)
def test_extension_imports_cleanly(extension: str) -> None:
    importlib.import_module(extension)


@pytest.mark.parametrize("extension", EXTENSIONS)
def test_extension_exposes_async_setup(extension: str) -> None:
    module = importlib.import_module(extension)
    setup = getattr(module, "setup", None)
    assert setup is not None, f"{extension} has no setup() — load_extension will fail"
    assert inspect.iscoroutinefunction(setup), f"{extension}.setup must be async"


def test_every_cog_module_is_registered() -> None:
    """A cog file that nobody loads is dead code; catch it at review time."""
    assert sorted(_discovered_cog_modules()) == sorted(EXTENSIONS)
