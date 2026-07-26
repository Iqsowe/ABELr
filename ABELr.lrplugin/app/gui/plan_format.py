"""Plan-line / summary formatting (PLAN.md U5) — pure, no Qt.

Extracted out of `MainWindow` so the string-building logic (the part with
actual branches to get wrong) is testable without instantiating a window.
"""

from __future__ import annotations


def format_adjustment(adj, current_develop: dict) -> str:
    """Preview line "key: current → target (delta)" — embedded values are
    absolute, so the gap vs. the current setting is the useful info."""
    parts = []
    for k, v in adj.develop.items():
        cur = current_develop.get(k)
        if isinstance(v, (int, float)) and isinstance(cur, (int, float)):
            parts.append(f"{k}: {cur:g} → {v:g} (Δ{v - cur:+g})")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: ? → {v:g}")
        else:
            parts.append(f"{k}: {cur} → {v}")
    return f"  {adj.photo_id[:8]} → " + ", ".join(parts)


def format_plan_summary(diag, n_measured: int, n_skipped: int) -> str:
    """"Mode X — N seed(s), M target(s), ..." plan-summary line."""
    mode_label = "embedded (neutral anchor)" if diag.mode == "embedded" else diag.mode
    summary = (
        f"Mode {mode_label} — {diag.n_seeds} seed(s), {diag.n_targets} target(s), "
        f"{n_measured} measured, {n_skipped} not measurable."
    )
    if diag.n_low_confidence:
        summary += f"  ⚠ {diag.n_low_confidence} low-confidence match(es)"
    return summary
