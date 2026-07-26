"""PLAN.md N3d — anti-drift tests for app.server.budget.

Guards the invariants the N3 fix depends on: a monotonic, floored budget that
stays inside JobQueue._ENTRY_TTL, and a Lua wait fraction strictly below 1
(so Lua always returns before Python gives up).
"""

from __future__ import annotations

import pytest

from app.server import budget
from app.server.job_queue import JobQueue


@pytest.mark.parametrize("kind", ["probe", "fetch"])
def test_seconds_per_photo_monotonic_in_resolution(kind):
    per_photo = budget.probe_seconds_per_photo if kind == "probe" else budget.fetch_seconds_per_photo
    small = per_photo(512, 512)
    large = per_photo(2048, 2048)
    assert small <= large


def test_probe_seconds_per_photo_floored():
    assert budget.probe_seconds_per_photo(1, 1) == budget.PROBE_MIN


def test_fetch_seconds_per_photo_resolution_independent():
    assert budget.fetch_seconds_per_photo(512, 512) == budget.fetch_seconds_per_photo(4096, 4096)


@pytest.mark.parametrize("kind", ["probe", "fetch"])
def test_job_timeout_monotonic_in_n(kind):
    small = budget.job_timeout(1, 2048, 2048, 0.6, kind)
    large = budget.job_timeout(20, 2048, 2048, 0.6, kind)
    assert small <= large


def test_job_timeout_floor_respected():
    assert budget.job_timeout(0, 8, 8, 0.0, "probe") == budget.MIN_TIMEOUT


@pytest.mark.parametrize("kind", ["probe", "fetch"])
def test_chunk_size_respects_target_job_seconds(kind):
    for width, height in [(512, 512), (2048, 2048)]:
        cap = 40
        n = budget.chunk_size(width, height, kind, cap)
        per_photo = budget._per_photo(kind, width, height)
        assert n * per_photo <= budget.TARGET_JOB_SECONDS + 1e-9
        assert 1 <= n <= cap


def test_chunk_size_clamped_to_cap():
    # At a tiny per-photo cost, the budget alone would allow a huge chunk —
    # the caller's cap must still win.
    assert budget.chunk_size(1, 1, "fetch", cap=5) == 5


def test_job_timeout_stays_under_queue_ttl_for_largest_legal_chunk():
    cap = 16  # neutral_preview_worker's own cap
    n = budget.chunk_size(2048, 2048, "probe", cap)
    timeout = budget.job_timeout(n, 2048, 2048, 2.0, "probe")
    assert timeout < JobQueue._ENTRY_TTL


def test_lua_budget_fraction_below_one():
    assert 0 < budget.LUA_BUDGET_FRACTION < 1


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        budget.job_timeout(1, 512, 512, 0.0, "bogus")
