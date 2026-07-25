"""PLAN.md COV1 — `app/server/job_queue.py` direct unit tests.

Previously only exercised indirectly (mocked out) via `test_mcp_tools.py`.
Covers: submit/wait_result round-trip, saturation guard, orphan pruning,
bridge heartbeat timing, next_pending/submit_result lifecycle.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.server.job_queue import JobQueue
from app.server.models import JobResult, JobStatus, JobType


def _queue() -> JobQueue:
    return JobQueue()


def test_submit_creates_pending_job():
    q = _queue()
    job_id = q.submit(JobType.TEST, {"a": 1})
    assert q.pending_count() == 1
    assert q.status_of(job_id) == JobStatus.PENDING


def test_next_pending_moves_to_in_progress_and_empties_queue():
    q = _queue()
    job_id = q.submit(JobType.TEST)
    job = q.next_pending()
    assert job is not None
    assert job.job_id == job_id
    assert q.status_of(job_id) == JobStatus.IN_PROGRESS
    assert q.pending_count() == 0
    assert q.next_pending() is None


def test_submit_result_unblocks_wait_result():
    q = _queue()
    job_id = q.submit(JobType.TEST)
    q.next_pending()
    result = JobResult(job_id=job_id, status="ok")
    assert q.submit_result(result) is True
    got = q.wait_result(job_id, timeout=1.0)
    assert got is not None
    assert got.job_id == job_id
    # consumed: a second wait sees nothing (entry popped on first success)
    assert q.wait_result(job_id, timeout=0.1) is None


def test_submit_result_failed_status():
    q = _queue()
    job_id = q.submit(JobType.TEST)
    q.submit_result(JobResult(job_id=job_id, status="error", error="boom"))
    assert q.status_of(job_id) == JobStatus.FAILED


def test_submit_result_unknown_job_returns_false():
    q = _queue()
    assert q.submit_result(JobResult(job_id="does-not-exist", status="ok")) is False


def test_wait_result_unknown_job_returns_none_immediately():
    q = _queue()
    start = time.monotonic()
    assert q.wait_result("does-not-exist", timeout=5.0) is None
    assert time.monotonic() - start < 1.0  # must not block the full timeout


def test_wait_result_times_out_when_never_answered():
    q = _queue()
    job_id = q.submit(JobType.TEST)
    assert q.wait_result(job_id, timeout=0.05) is None


def test_submit_saturation_guard():
    q = _queue()
    q._MAX_PENDING = 3  # narrow the cap so the test doesn't submit 100 jobs
    for _ in range(3):
        q.submit(JobType.TEST)
    with pytest.raises(RuntimeError, match="saturated"):
        q.submit(JobType.TEST)


def test_prune_evicts_orphaned_entries_past_ttl():
    q = _queue()
    q._ENTRY_TTL = 0.05
    job_id = q.submit(JobType.TEST)
    assert q.status_of(job_id) == JobStatus.PENDING
    time.sleep(0.1)
    # any lock-guarded op runs _prune_locked internally
    q.submit(JobType.TEST)
    assert q.status_of(job_id) is None
    assert job_id not in list(q._pending)


def test_prune_does_not_evict_fresh_entries():
    q = _queue()
    job_id = q.submit(JobType.TEST)
    q.submit(JobType.TEST)
    assert q.status_of(job_id) == JobStatus.PENDING


def test_bridge_connected_false_before_any_poll():
    q = _queue()
    assert q.seconds_since_poll() is None
    assert q.bridge_connected() is False


def test_bridge_connected_true_right_after_poll():
    q = _queue()
    q.mark_poll()
    assert q.bridge_connected(threshold=5.0) is True
    assert q.seconds_since_poll() < 1.0


def test_bridge_connected_false_after_threshold():
    q = _queue()
    q.mark_poll()
    assert q.bridge_connected(threshold=0.0) is False


def test_concurrent_submit_and_wait_across_threads():
    """Smoke-check the Lock/Event actually synchronize across real threads
    (the queue's whole reason to exist — GUI thread vs FastAPI thread)."""
    q = _queue()
    job_id = q.submit(JobType.TEST)

    def _plugin_side():
        job = q.next_pending()
        assert job is not None
        time.sleep(0.05)
        q.submit_result(JobResult(job_id=job.job_id, status="ok"))

    t = threading.Thread(target=_plugin_side)
    t.start()
    result = q.wait_result(job_id, timeout=2.0)
    t.join()
    assert result is not None
    assert result.status == "ok"
