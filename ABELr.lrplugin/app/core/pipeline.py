"""Render-space analysis dataclasses — shared by the CPU (`render_metrics`) and
GPU (`render_metrics_gpu`) measurement paths.

The composition functions that used to live here (`analyze_rendered`,
`analyze_rendered_dual`, CPU-only, one shared CIELAB pass) are dead — the live
path is GPU (`render_metrics_gpu.analyze_rendered_gpu`/`analyze_rendered_gpu_dual`,
PLAN.md D1) — removed rather than kept unused. These dataclasses stay: both
paths construct them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .render_metrics import BandStats, NeutralStats, ToneStats


@dataclass
class RenderAnalysis:
    """Complete measurements of a render (exposure + WB + HSL), one CIELAB pass."""

    tone: ToneStats              # L* lightness → exposure
    neutral: NeutralStats        # a*/b* cast on neutrals → WB refinement
    bands: list[BandStats]       # per-HSL-band stats → HSL calibration


@dataclass
class RenderAnalysisDual:
    """Pair of measurements of a render: **global** (full frame) + **sharp** (sharp zone).

    The global↔sharp delta reveals backlighting (dark sharp subject / bright background)
    and background≠subject cast; `mask_sharp_frac` diagnoses the mask's reliability
    (≈1 = image blurry everywhere → sharp ≈ global, no usable sharp zone).
    """

    sharp: RenderAnalysis
    glob: RenderAnalysis
    mask_sharp_frac: float
