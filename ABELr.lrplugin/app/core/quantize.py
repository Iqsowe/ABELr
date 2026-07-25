"""Slider-grid quantization (PLAN.md step Q) — user-imposed personal workflow
constraint, confirmed 2026-07-25, **not a bug fix**: every HSL/Calibration/WB
Tint value this app computes must land on a 5-unit step, WB Temperature on a
250-unit step (Lr's own 2000-12000K Temperature range divides evenly by 250,
not by the initially-stated 150). `Exposure2012` is explicitly out of scope —
stays continuous.
"""

from __future__ import annotations


def snap(value: float, step: float) -> int:
    """Nearest multiple of `step`.

    Must be applied **after** clamping to the slider's bounds, never before —
    the bounds in play (-100/100, -150/150, 2000/12000) are themselves exact
    multiples of their respective steps (5, 5, 250), so snapping a
    post-clamp value can never drift back out of range.
    """
    return int(step * round(value / step))
