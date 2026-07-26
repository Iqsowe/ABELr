"""PLAN.md U5 — plan_format.py, extracted out of MainWindow. No Qt."""

from __future__ import annotations

from types import SimpleNamespace

from app.gui.plan_format import format_adjustment, format_plan_summary


def test_format_adjustment_numeric_delta():
    adj = SimpleNamespace(photo_id="uuid-abcdefgh", develop={"Exposure2012": 0.5})
    line = format_adjustment(adj, {"Exposure2012": 0.2})
    assert "uuid-abc" in line
    assert "0.2 → 0.5" in line
    assert "Δ+0.3" in line


def test_format_adjustment_unknown_current_value():
    adj = SimpleNamespace(photo_id="uuid-x", develop={"Exposure2012": 0.5})
    line = format_adjustment(adj, {})
    assert "? → 0.5" in line


def test_format_adjustment_non_numeric_value():
    adj = SimpleNamespace(photo_id="uuid-x", develop={"WhiteBalance": "Custom"})
    line = format_adjustment(adj, {"WhiteBalance": "As Shot"})
    assert "As Shot → Custom" in line


def test_format_plan_summary_embedded_mode_label():
    diag = SimpleNamespace(mode="embedded", n_seeds=0, n_targets=5, n_low_confidence=0)
    summary = format_plan_summary(diag, n_measured=5, n_skipped=1)
    assert "embedded (neutral anchor)" in summary
    assert "5 target(s)" in summary
    assert "1 not measurable" in summary


def test_format_plan_summary_low_confidence_flagged():
    diag = SimpleNamespace(mode="seeds", n_seeds=3, n_targets=2, n_low_confidence=1)
    summary = format_plan_summary(diag, n_measured=2, n_skipped=0)
    assert "1 low-confidence match(es)" in summary
