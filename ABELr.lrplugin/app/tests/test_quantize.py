"""PLAN step Q6 — `core.quantize.snap`: nearest-multiple rounding, half-step
tie-breaking, boundary interaction with slider clamping.
"""

from __future__ import annotations

import pytest

from app.core import quantize


@pytest.mark.parametrize(
    "value,step,expected",
    [
        (0.0, 5, 0),
        (3.0, 5, 5),      # closer to 5 than to 0
        (-3.0, 5, -5),    # closer to -5 than to 0
        (12.0, 5, 10),    # closer to 10 than to 15
        (13.0, 5, 15),    # closer to 15 than to 10
        (100.0, 5, 100),  # exact multiple stays put
        (4000.0, 250, 4000),
        (4100.0, 250, 4000),  # closer to 4000 than 4250
        (4200.0, 250, 4250),  # closer to 4250 than 4000
    ],
)
def test_snap_nearest_multiple(value, step, expected):
    assert quantize.snap(value, step) == expected


def test_snap_returns_int():
    assert isinstance(quantize.snap(3.0, 5), int)


@pytest.mark.parametrize(
    "value,step,expected",
    [
        # `round()` (banker's rounding, per the module's literal formula) ties
        # to the nearest EVEN multiple index at an exact half-step.
        (2.5, 5, 0),     # 2.5/5=0.5 -> round to 0 (even) -> 0
        (7.5, 5, 10),    # 7.5/5=1.5 -> round to 2 (even) -> 10
        (12.5, 5, 10),   # 12.5/5=2.5 -> round to 2 (even) -> 10
        (17.5, 5, 20),   # 17.5/5=3.5 -> round to 4 (even) -> 20
        (4125.0, 250, 4000),  # 4125/250=16.5 -> round to 16 (even) -> 4000
        (4375.0, 250, 4500),  # 4375/250=17.5 -> round to 18 (even) -> 4500
    ],
)
def test_snap_half_step_ties_to_even(value, step, expected):
    assert quantize.snap(value, step) == expected


@pytest.mark.parametrize(
    "bound,step",
    [
        (-100.0, 5), (100.0, 5),      # HSL / Calibration
        (-150.0, 5), (150.0, 5),      # WB Tint
        (2000.0, 250), (12000.0, 250),  # WB Temperature
    ],
)
def test_snap_at_exact_slider_bound_is_a_no_op(bound, step):
    # A value already clamped to a slider bound must snap to itself, never
    # drift outside the bound (the bounds are themselves exact multiples of
    # their step — cf. module docstring).
    assert quantize.snap(bound, step) == pytest.approx(bound)
