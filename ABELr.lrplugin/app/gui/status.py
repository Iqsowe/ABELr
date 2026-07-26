"""Persistent status panel — pure computation (PLAN.md U2).

`build_status` carries the risk (DB reads, response-model inspection); the
Qt side (`MainWindow`) only renders `format_status_lines`'s output. Testable
without Qt, in the same spirit as `neutral_preview_worker`'s module-level
functions (COV5: GUI *workers* stay manual-only, the logic around them
doesn't have to).

Surfaces state that already exists in the DB/response cache but was never
shown anywhere: catalog photo count, references marked vs. actually usable
(the delta silently produces an empty plan today — cf. PLAN.md U4a), fresh
neutral anchors available for the current selection, and per-axis
calibration state (is `ExposureResponse` populated? `WBResponse`
calibrated? how many HSL bands?).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core import cache as cachemod
from ..core import seed_match
from ..core.response import ResponseModel


@dataclass
class StatusSnapshot:
    catalog_photo_count: int
    references_marked: int
    references_usable: int
    neutral_anchors_ready: int
    neutral_anchors_total: int
    camera: str
    profile: str
    expo_calibrated: bool
    wb_calibrated: bool
    hsl_calibrated_bands: int
    hsl_total_bands: int
    bridge_connected: bool


def build_status(
    conn, photos: list, model: Optional[ResponseModel], *, bridge_connected: bool
) -> StatusSnapshot:
    """`conn`: open cache connection or None (no catalog analyzed yet).
    `photos`: the current Lr selection (PhotoResult list) — neutral-anchor
    readiness is scoped to it, not the whole catalog (mirrors
    `ensure_neutral_previews`'s own per-selection cache lookup).
    `model`: the loaded `ResponseModel` for the detected (camera, profile),
    or None before any camera/profile is known.
    """
    if conn is not None:
        catalog_photo_count = cachemod.count_pictures(conn)
        references_marked = len(cachemod.list_seed_uuids(conn))
        references_usable = len(seed_match.build_seed_pool(conn))
        neutral_anchors_ready = sum(
            1 for p in photos
            if cachemod.get_neutral_preview(
                conn, p.photo_id, cachemod.style_hash(p.current_develop or {})
            ) is not None
        )
    else:
        catalog_photo_count = 0
        references_marked = 0
        references_usable = 0
        neutral_anchors_ready = 0

    if model is not None:
        camera, profile = model.camera, model.profile
        expo_calibrated = bool(model.exposure.ev)
        wb_calibrated = model.wb.is_calibrated()
        hsl_calibrated_bands = sum(1 for b in model.bands.values() if b.is_calibrated())
        hsl_total_bands = len(model.bands)
    else:
        camera, profile = "unknown", "unknown"
        expo_calibrated = wb_calibrated = False
        hsl_calibrated_bands = hsl_total_bands = 0

    return StatusSnapshot(
        catalog_photo_count=catalog_photo_count,
        references_marked=references_marked,
        references_usable=references_usable,
        neutral_anchors_ready=neutral_anchors_ready,
        neutral_anchors_total=len(photos),
        camera=camera,
        profile=profile,
        expo_calibrated=expo_calibrated,
        wb_calibrated=wb_calibrated,
        hsl_calibrated_bands=hsl_calibrated_bands,
        hsl_total_bands=hsl_total_bands,
        bridge_connected=bridge_connected,
    )


def _yn(b: bool) -> str:
    return "yes" if b else "no"


def format_status_lines(snap: StatusSnapshot) -> list[str]:
    """Read-only grid content, one line per row — the Qt side just displays
    these (no formatting logic in `MainWindow`)."""
    lines = [
        f"Catalog: {snap.catalog_photo_count} photo(s)",
        f"References: {snap.references_marked} marked / {snap.references_usable} usable",
    ]
    if snap.neutral_anchors_total:
        lines.append(
            f"Neutral anchors (selection): {snap.neutral_anchors_ready}/"
            f"{snap.neutral_anchors_total} ready"
        )
    else:
        lines.append("Neutral anchors (selection): no photo selected")
    lines.append(f"Profile: {snap.camera} / {snap.profile}")
    lines.append(
        f"Calibration — Exposure: {_yn(snap.expo_calibrated)} · "
        f"WB: {_yn(snap.wb_calibrated)} · "
        f"HSL: {snap.hsl_calibrated_bands}/{snap.hsl_total_bands} band(s)"
    )
    lines.append(f"Bridge: {'connected' if snap.bridge_connected else 'disconnected'}")
    return lines
