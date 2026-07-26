"""PLAN.md N1c — `ensure_neutral_previews` end-to-end via `FakePlugin`, no HTTP,
no Qt, no GPU/RAW (gpu_jpeg.decode_file / analyze_rendered_gpu_dual stubbed).

R3 (full re-measure) is Lr-required, not applicable here. R1's undersized-
render rejection case lives below (`test_undersized_render_rejected`), now
that `render_metrics_gpu.reject_if_undersized` exists.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import cache as cachemod
from app.core import render_metrics
from app.core.pipeline import RenderAnalysisDual
from app.gui import neutral_preview_worker as npw
from app.server.job_queue import JobQueue
from app.server.models import PhotoResult
from app.tools.mock_plugin import (
    FakePlugin,
    render_probe_all_timeout,
    render_probe_error_status,
    render_probe_no_response,
    render_probe_ok,
    render_probe_partial,
    render_probe_restore_failed,
)

from .conftest import make_analysis


@pytest.fixture
def conn(tmp_path):
    c = cachemod.open_cache(tmp_path / "Catalog.lrcat")
    yield c
    c.close()


@pytest.fixture
def queue(monkeypatch):
    q = JobQueue()
    monkeypatch.setattr(npw, "job_queue", q)
    return q


@pytest.fixture
def plugin(queue):
    return FakePlugin(queue=queue)


@pytest.fixture(autouse=True)
def _stub_gpu_and_fast_timeouts(monkeypatch):
    dual = RenderAnalysisDual(sharp=make_analysis(), glob=make_analysis(), mask_sharp_frac=0.5)
    # Shape-only stand-in (R1 needs .shape[-1]/.shape[-2] at grid size to pass
    # reject_if_undersized; content is never read, analyze_rendered_gpu_dual
    # below is stubbed to ignore it).
    fake_chw = SimpleNamespace(shape=(3, render_metrics.MEASURE_LONG_EDGE, render_metrics.MEASURE_LONG_EDGE))
    monkeypatch.setattr(npw.gpu_jpeg, "decode_file", lambda path: fake_chw)
    monkeypatch.setattr(npw.render_metrics_gpu, "analyze_rendered_gpu_dual", lambda chw: dual)
    # Real budget constants (30s floor, ~10.5s/photo at 2048x2048) would make a
    # no-response test take 30s+ for real — this is control-flow testing, not
    # timing testing (N3: timeouts now come from app.server.budget).
    monkeypatch.setattr(npw.budget, "MIN_TIMEOUT", 0.5)
    monkeypatch.setattr(npw.budget, "PROBE_S_PER_MPX", 0.001)
    monkeypatch.setattr(npw.budget, "PROBE_MIN", 0.05)
    monkeypatch.setattr(npw.budget, "JOB_OVERHEAD", 0.0)
    monkeypatch.setattr(npw, "DEFAULT_SETTLE", 0.0)
    monkeypatch.setattr(npw, "_RETRY_SETTLE", 0.0)


def _photos(n: int) -> list[PhotoResult]:
    return [
        PhotoResult(photo_id=f"uuid-{i:03d}", path=f"P{i}.ARW", current_develop={"Exposure2012": 0.0})
        for i in range(n)
    ]


def test_happy_path_all_probes_succeed(conn, plugin):
    plugin.render_probe_hook = render_probe_ok()
    photos = _photos(20)
    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(photos, conn, chunk_size=8)

    assert outcome.n_refreshed == 20
    assert outcome.n_requested == 20
    assert len(outcome.by_id) == 20
    assert outcome.failures == {}
    failed, msg = npw._summarize(outcome, len(photos))
    assert failed is False
    assert "20 recomputed" in msg


def test_cancel_stops_before_remaining_chunks_are_submitted(conn, plugin):
    """PLAN.md U3 — should_cancel is checked BETWEEN chunks (never mid-chunk):
    a chunk already submitted always finishes; a cancel requested before the
    2nd chunk means the 2nd chunk is never submitted at all."""
    plugin.render_probe_hook = render_probe_ok()
    photos = _photos(24)  # 3 chunks of 8

    calls = {"n": 0}

    def cancel_after_first_chunk():
        calls["n"] += 1
        return calls["n"] > 1  # False on the 1st check, True from then on

    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(
            photos, conn, chunk_size=8, should_cancel=cancel_after_first_chunk,
        )

    assert outcome.cancelled is True
    assert outcome.n_refreshed == 8  # only the 1st chunk ran
    assert len(outcome.by_id) == 8
    failed, msg = npw._summarize(outcome, len(photos))
    assert failed is False  # a user-requested stop is not a failure
    assert "cancelled" in msg.lower()
    assert "8" in msg


def test_undersized_render_rejected(conn, plugin, monkeypatch):
    """PLAN.md R1 — a render below MEASURE_LONG_EDGE is a failure, never a
    silently-accepted measurement: requestJpegThumbnail ignores the requested
    size and serves whichever pyramid tier Lr has cached."""
    undersized_chw = SimpleNamespace(shape=(3, 484, 484))
    monkeypatch.setattr(npw.gpu_jpeg, "decode_file", lambda path: undersized_chw)
    plugin.render_probe_hook = render_probe_ok()
    photos = _photos(3)
    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(photos, conn, chunk_size=8)

    assert outcome.n_refreshed == 0
    assert outcome.by_id == {}
    assert len(outcome.failures) == 3
    for reason in outcome.failures.values():
        assert "undersized render" in reason
        assert "484" in reason


def test_all_timeout_is_reported_as_failed_not_fake_success(conn, plugin):
    """Pins the N2c fix: this used to emit a "calibrated: 0 recomputed" SUCCESS
    message even though nothing whatsoever was refreshed."""
    plugin.render_probe_hook = render_probe_all_timeout()
    photos = _photos(5)
    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(photos, conn, chunk_size=8)

    assert outcome.n_refreshed == 0
    assert len(outcome.failures) == 5
    failed, msg = npw._summarize(outcome, len(photos))
    assert failed is True
    assert "failed" in msg.lower()


def test_partial_chunk_failure_still_caches_the_good_ones(conn, plugin):
    plugin.render_probe_hook = render_probe_partial(n_fail=3)
    photos = _photos(8)
    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(photos, conn, chunk_size=8)

    assert outcome.n_refreshed == 5
    assert len(outcome.by_id) == 5
    assert len(outcome.failures) == 3
    for pid in [p.photo_id for p in photos[:3]]:
        assert pid in outcome.failures
    failed, msg = npw._summarize(outcome, len(photos))
    assert failed is False
    assert "5 recomputed" in msg
    assert "failed" in msg  # names the cause, doesn't just say "without a thumbnail"


def test_status_error_with_thumbnails_present_is_all_failure(conn, plugin):
    """N2a/N2c: a job-level status='error' means the whole chunk failed, even
    though the plugin still attached thumbnail rows to the JobResult."""
    plugin.render_probe_hook = render_probe_error_status()
    photos = _photos(4)
    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(photos, conn, chunk_size=8)

    assert outcome.n_refreshed == 0
    assert len(outcome.failures) == 4
    failed, _msg = npw._summarize(outcome, len(photos))
    assert failed is True


def test_plugin_never_responds_raises_runtime_error(conn, plugin):
    plugin.render_probe_hook = render_probe_no_response()
    photos = _photos(3)
    with pytest.raises(RuntimeError, match="Timeout"):
        with plugin.run_in_thread():
            npw.ensure_neutral_previews(photos, conn, chunk_size=8)


def test_put_neutral_preview_failure_is_not_counted_as_refreshed(conn, plugin, monkeypatch):
    plugin.render_probe_hook = render_probe_ok()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated cache write failure")

    monkeypatch.setattr(npw.cachemod, "put_neutral_preview", _boom)

    photos = _photos(3)
    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(photos, conn, chunk_size=8)

    assert outcome.n_refreshed == 0
    assert len(outcome.by_id) == 0
    assert len(outcome.failures) == 3
    assert all(reason == "cache write failed" for reason in outcome.failures.values())


def test_restore_error_emits_warning_but_anchor_is_still_cached(conn, plugin):
    """Current behavior, asserted deliberately (PLAN.md N1c)."""
    plugin.render_probe_hook = render_probe_restore_failed(n_restore_fail=2)
    photos = _photos(4)
    warnings: list[str] = []
    with plugin.run_in_thread():
        outcome = npw.ensure_neutral_previews(
            photos, conn, chunk_size=8, progress=warnings.append,
        )

    assert any("restore failed" in w for w in warnings)
    assert outcome.n_refreshed == 4
    assert len(outcome.by_id) == 4
    assert outcome.failures == {}


def test_circuit_breaker_aborts_after_two_consecutive_failing_chunks(conn, plugin):
    plugin.render_probe_hook = render_probe_all_timeout()
    photos = _photos(24)  # 3 chunks of 8, all failing
    with pytest.raises(RuntimeError, match="Circuit breaker"):
        with plugin.run_in_thread():
            npw.ensure_neutral_previews(photos, conn, chunk_size=8)

    # Only the first 2 chunks (16 photos) were ever submitted as jobs — the
    # 3rd chunk's photos never got a render_probe job at all.
    assert len(plugin.queue._jobs) == 0  # no orphaned pending job from a 3rd submit
