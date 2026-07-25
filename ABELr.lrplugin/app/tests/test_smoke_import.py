"""Anti-crash guard: every `app/core/*` and `app/gui/*` module present on disk
must import cleanly (Qt modules import fine without a display — only
instantiation requires a screen). Catches syntax errors and broken imports
before they reach a real Lr session.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]


def _modules(subpkg: str) -> list[str]:
    return sorted(
        f"app.{subpkg}.{p.stem}"
        for p in (APP_DIR / subpkg).glob("*.py")
        if p.stem != "__init__"
    )


@pytest.mark.parametrize("module", _modules("core") + _modules("gui"))
def test_smoke_import(module):
    importlib.import_module(module)
