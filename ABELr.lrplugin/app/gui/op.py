"""The current MainWindow operation (PLAN.md U5 — replaces bare `_op` strings)."""

from __future__ import annotations

from enum import Enum


class Op(Enum):
    REF = "ref"
    SEED_REMOVE = "seed_remove"
    NEUTRAL = "neutral"
    PREVIEW = "preview"
    APPLY = "apply"
