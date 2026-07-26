"""Tests for the MCP helper `run_job` / `require_bridge` (no server, no GPU).

Monkeypatches the `job_queue` imported in `app.mcp.tools` to simulate the
four outcomes: success, timeout, plugin error, saturated queue.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.core.render_metrics import MEASURE_LONG_EDGE
from app.mcp import server as mcp_server
from app.mcp import tools as mcp_tools
from app.server import budget as budgetmod
from app.server.models import JobResult, JobType


class _FakeQueue:
    def __init__(self, *, wait_result=None, submit_raises=False, connected=True):
        self._wait = wait_result
        self._submit_raises = submit_raises
        self._connected = connected
        self.submitted = []

    def submit(self, job_type, payload=None):
        if self._submit_raises:
            raise RuntimeError("Job queue saturated (100 pending) — bridge inactive?")
        self.submitted.append((job_type, payload))
        return "job-123"

    def wait_result(self, job_id, timeout):
        return self._wait

    def bridge_connected(self, threshold: float = 5.0):
        return self._connected


def _patch(monkeypatch, queue):
    monkeypatch.setattr(mcp_tools, "job_queue", queue)


def test_run_job_success(monkeypatch):
    res = JobResult(job_id="job-123", status="ok", applied=3, matched=3, total=3)
    _patch(monkeypatch, _FakeQueue(wait_result=res))
    out = asyncio.run(mcp_tools.run_job(JobType.APPLY_ADJUSTMENTS, {"adjustments": []}))
    assert out is res
    assert out.applied == 3


def test_run_job_timeout(monkeypatch):
    _patch(monkeypatch, _FakeQueue(wait_result=None))
    with pytest.raises(ToolError, match="Timeout"):
        asyncio.run(mcp_tools.run_job(JobType.GET_SELECTED_PHOTOS, None, timeout=0.1))


def test_run_job_plugin_error(monkeypatch):
    res = JobResult(job_id="job-123", status="error", error="boom on the Lr side")
    _patch(monkeypatch, _FakeQueue(wait_result=res))
    with pytest.raises(ToolError, match="boom"):
        asyncio.run(mcp_tools.run_job(JobType.TEST, None))


def test_run_job_queue_saturated(monkeypatch):
    _patch(monkeypatch, _FakeQueue(submit_raises=True))
    with pytest.raises(ToolError, match="satur"):
        asyncio.run(mcp_tools.run_job(JobType.TEST, None))


def test_require_bridge_disconnected(monkeypatch):
    _patch(monkeypatch, _FakeQueue(connected=False))
    with pytest.raises(ToolError, match="Lightroom bridge not connect"):
        mcp_tools.require_bridge()


def test_require_bridge_connected(monkeypatch):
    _patch(monkeypatch, _FakeQueue(connected=True))
    mcp_tools.require_bridge()  # does not raise


def test_render_probe_defaults_to_measure_grid_and_ships_a_budget(monkeypatch):
    """PLAN.md X3 — the MCP render_probe tool used to send no width/height at
    all (Lua then defaulted to 512), and its own timeout (max(30, 5*n)) was
    tuned for that 512 default, not the measurement grid."""
    res = JobResult(job_id="job-123", status="ok", thumbnails=[])
    queue = _FakeQueue(wait_result=res)
    _patch(monkeypatch, queue)

    n = 5
    asyncio.run(mcp_server.render_probe(
        adjustments=[{"photo_id": f"u{i}", "develop": {}} for i in range(n)], settle=0.6,
    ))

    assert len(queue.submitted) == 1
    job_type, payload = queue.submitted[0]
    assert job_type == JobType.RENDER_PROBE
    assert payload["width"] == MEASURE_LONG_EDGE
    assert payload["height"] == MEASURE_LONG_EDGE
    assert payload["timeout_s"] == pytest.approx(
        budgetmod.job_timeout(n, MEASURE_LONG_EDGE, MEASURE_LONG_EDGE, 0.6, "probe")
    )
    # Under-dimensioned legacy formula (max(30, 5*n)) must be gone, not just
    # coincidentally close to the new value.
    assert payload["timeout_s"] != max(30.0, 5.0 * n)
