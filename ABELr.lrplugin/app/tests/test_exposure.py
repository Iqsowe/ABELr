"""Exposure planning (`core.exposure`) — render-space L* -> Exposure2012,
including the headroom safeguard (`_headroom_factor`, `_MAX_STEP_EV`).
Origin: PLAN.md step W5.
"""

from __future__ import annotations

import pytest

from app.core import exposure as exp
from app.core.response import ExposureResponse


# --------------------------------------------------------------------------- #
# _headroom_factor
# --------------------------------------------------------------------------- #
def test_headroom_factor_full_below_limit():
    assert exp._headroom_factor(0.0, 0.02) == pytest.approx(1.0)
    assert exp._headroom_factor(0.02, 0.02) == pytest.approx(1.0)  # at the limit, still full


def test_headroom_factor_decays_linearly_to_zero_at_2x_limit():
    limit = 0.02
    assert exp._headroom_factor(1.5 * limit, limit) == pytest.approx(0.5)
    assert exp._headroom_factor(2.0 * limit, limit) == pytest.approx(0.0)


def test_headroom_factor_never_negative_beyond_2x_limit():
    assert exp._headroom_factor(10.0, 0.02) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# plan_from_render
# --------------------------------------------------------------------------- #
def _sample(pid="p1", current_l=50.0, current_exposure=0.0, desired_l=67.0, clipped_hi=0.0, clipped_lo=0.0):
    return exp.ExposureSample(pid, current_l, current_exposure, desired_l, clipped_hi, clipped_lo)


def test_plan_from_render_skips_samples_with_no_desired_l():
    samples = [_sample(desired_l=None)]
    out = exp.plan_from_render(samples)
    assert out == []


def test_plan_from_render_nominal_positive_delta():
    # +17 L* at the 17 L*/EV nominal prior -> +1 EV.
    out = exp.plan_from_render([_sample(current_l=50.0, desired_l=67.0)])
    assert len(out) == 1
    assert out[0].photo_id == "p1"
    assert out[0].develop["Exposure2012"] == pytest.approx(1.0, abs=1e-6)


def test_plan_from_render_nominal_negative_delta():
    out = exp.plan_from_render([_sample(current_l=50.0, desired_l=33.0)])
    assert out[0].develop["Exposure2012"] == pytest.approx(-1.0, abs=1e-2)


def test_plan_from_render_accumulates_on_current_exposure():
    # current_exposure is NOT 0 -> the new value must add the delta on top of it.
    out = exp.plan_from_render([_sample(current_l=50.0, current_exposure=0.5, desired_l=67.0)])
    assert out[0].develop["Exposure2012"] == pytest.approx(1.5, abs=1e-6)


def test_plan_from_render_clamps_to_max_step_ev():
    # A huge gap must not translate into an unbounded EV jump.
    out = exp.plan_from_render([_sample(current_l=5.0, desired_l=95.0)], max_step_ev=2.0)
    assert out[0].develop["Exposure2012"] == pytest.approx(2.0, abs=1e-6)


def test_plan_from_render_clamps_negative_to_minus_max_step_ev():
    out = exp.plan_from_render([_sample(current_l=95.0, desired_l=5.0)], max_step_ev=2.0)
    assert out[0].develop["Exposure2012"] == pytest.approx(-2.0, abs=1e-6)


def test_plan_from_render_headroom_attenuates_positive_dev_on_highlight_clipping():
    hi_limit = 0.02
    full = exp.plan_from_render(
        [_sample(current_l=50.0, desired_l=67.0, clipped_hi=0.0)], hi_limit=hi_limit
    )[0].develop["Exposure2012"]
    attenuated = exp.plan_from_render(
        [_sample(current_l=50.0, desired_l=67.0, clipped_hi=2 * hi_limit)], hi_limit=hi_limit
    )[0].develop["Exposure2012"]
    assert attenuated == pytest.approx(0.0, abs=1e-6)
    assert full > attenuated


def test_plan_from_render_headroom_attenuates_negative_dev_on_shadow_clipping():
    lo_limit = 0.02
    full = exp.plan_from_render(
        [_sample(current_l=50.0, desired_l=33.0, clipped_lo=0.0)], lo_limit=lo_limit
    )[0].develop["Exposure2012"]
    attenuated = exp.plan_from_render(
        [_sample(current_l=50.0, desired_l=33.0, clipped_lo=2 * lo_limit)], lo_limit=lo_limit
    )[0].develop["Exposure2012"]
    assert attenuated == pytest.approx(0.0, abs=1e-6)
    assert full < attenuated  # full is negative, attenuated is ~0 -> full < attenuated


def test_plan_from_render_headroom_does_not_attenuate_the_opposite_direction():
    # Highlight clipping must not throttle a NEGATIVE (darkening) delta.
    hi_limit = 0.02
    out = exp.plan_from_render(
        [_sample(current_l=50.0, desired_l=33.0, clipped_hi=10 * hi_limit)], hi_limit=hi_limit
    )
    assert out[0].develop["Exposure2012"] < 0.0
    assert out[0].develop["Exposure2012"] == pytest.approx(-1.0, abs=1e-2)


def test_plan_from_render_uses_calibrated_response_curve():
    resp = ExposureResponse(ev=[-1.0, 0.0, 1.0], lstar=[20.0, 50.0, 80.0])  # 30 L*/EV
    out = exp.plan_from_render([_sample(current_l=50.0, desired_l=80.0)], resp)
    assert out[0].develop["Exposure2012"] == pytest.approx(1.0, abs=1e-6)


def test_plan_from_render_multiple_samples_independent():
    out = exp.plan_from_render([
        _sample(pid="a", current_l=50.0, desired_l=67.0),
        _sample(pid="b", desired_l=None),
        _sample(pid="c", current_l=50.0, desired_l=33.0),
    ])
    ids = [a.photo_id for a in out]
    assert ids == ["a", "c"]  # "b" skipped (no desired_l)
