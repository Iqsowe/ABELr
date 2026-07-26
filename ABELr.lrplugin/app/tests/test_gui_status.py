"""PLAN.md U2 — `build_status`/`format_status_lines`, no Qt.

Exercises the pure-function side of the status panel: DB reads (catalog
count, references marked/usable, neutral-anchor readiness) and response-model
inspection (per-axis calibration state).
"""

from __future__ import annotations

import pytest

from app.core import cache as cachemod
from app.core.response import BandResponse, ExposureResponse, ResponseModel, WBResponse
from app.gui import status as statusmod
from app.server.models import PhotoResult

from .conftest import make_tone


@pytest.fixture
def conn(tmp_path):
    c = cachemod.open_cache(tmp_path / "Catalog.lrcat")
    yield c
    c.close()


def _photo(pid: str, develop: dict | None = None) -> PhotoResult:
    return PhotoResult(photo_id=pid, path=f"{pid}.ARW", current_develop=develop or {})


def test_build_status_empty_cache_and_no_model():
    snap = statusmod.build_status(None, [], None, bridge_connected=False)
    assert snap.catalog_photo_count == 0
    assert snap.references_marked == 0
    assert snap.references_usable == 0
    assert snap.neutral_anchors_ready == 0
    assert snap.neutral_anchors_total == 0
    assert snap.camera == "unknown" and snap.profile == "unknown"
    assert snap.expo_calibrated is False
    assert snap.wb_calibrated is False
    assert snap.hsl_calibrated_bands == 0
    assert snap.bridge_connected is False


def test_build_status_catalog_and_references_marked_vs_usable(conn):
    for pid in ("p1", "p2", "p3"):
        cachemod.put_picture(
            conn, pid, path=f"{pid}.ARW", catalog_path=None, exif=None,
            current_develop={}, commit=False,
        )
    cachemod.set_seed(conn, "p1", True, commit=False)
    cachemod.set_seed(conn, "p2", True, commit=False)
    # Only p1 gets a RAW analysis — build_seed_pool.build_seed_vector requires
    # asshot_rg to consider a seed "usable" (cf. seed_match.build_seed_vector).
    cachemod.put_source_raw(
        conn, "p1", "hash-p1", asshot_rg=1.8, asshot_bg=1.3, tone=make_tone(),
    )
    conn.commit()

    snap = statusmod.build_status(conn, [], None, bridge_connected=True)
    assert snap.catalog_photo_count == 3
    assert snap.references_marked == 2  # p1 + p2 flagged is_seed
    assert snap.references_usable == 1  # only p1 has a RAW analysis
    assert snap.bridge_connected is True


def _analysis():
    from app.core.pipeline import RenderAnalysis

    return RenderAnalysis(tone=make_tone(), neutral=None, bands=[])


def test_build_status_neutral_anchors_scoped_to_selection(conn):
    hs = cachemod.style_hash({})
    cachemod.put_neutral_preview(conn, "p1", hs, sharp=_analysis())
    photos = [_photo("p1"), _photo("p2")]  # p2 has no neutral anchor cached
    snap = statusmod.build_status(conn, photos, None, bridge_connected=True)
    assert snap.neutral_anchors_ready == 1
    assert snap.neutral_anchors_total == 2


def test_build_status_calibration_state_from_model():
    model = ResponseModel(
        camera="ILCE-7M4", profile="Neutral",
        exposure=ExposureResponse(ev=[-1.0, 0.0, 1.0], lstar=[30.0, 50.0, 70.0]),
        wb=WBResponse(da_dtemp=0.01, db_dtemp=0.0, da_dtint=0.0, db_dtint=0.02),
        bands={
            "Blue": BandResponse(dl_dlum=0.5),
            "Red": BandResponse(),  # all-zero -> not calibrated
        },
    )
    snap = statusmod.build_status(None, [], model, bridge_connected=False)
    assert snap.camera == "ILCE-7M4"
    assert snap.profile == "Neutral"
    assert snap.expo_calibrated is True
    assert snap.wb_calibrated is True
    assert snap.hsl_calibrated_bands == 1
    assert snap.hsl_total_bands == 2


def test_build_status_exposure_not_calibrated_when_empty():
    model = ResponseModel(camera="c", profile="p")
    snap = statusmod.build_status(None, [], model, bridge_connected=False)
    assert snap.expo_calibrated is False
    assert snap.wb_calibrated is False


def test_format_status_lines_no_selection_says_so():
    snap = statusmod.build_status(None, [], None, bridge_connected=False)
    lines = statusmod.format_status_lines(snap)
    assert any("no photo selected" in line for line in lines)
    assert any(line.startswith("Catalog:") for line in lines)
    assert any(line.startswith("Bridge:") for line in lines)


def test_format_status_lines_with_selection_shows_ready_count():
    snap = statusmod.StatusSnapshot(
        catalog_photo_count=10, references_marked=3, references_usable=2,
        neutral_anchors_ready=4, neutral_anchors_total=5,
        camera="ILCE-7M4", profile="Neutral",
        expo_calibrated=True, wb_calibrated=False,
        hsl_calibrated_bands=2, hsl_total_bands=8,
        bridge_connected=True,
    )
    lines = statusmod.format_status_lines(snap)
    assert any("4/5 ready" in line for line in lines)
    assert any("3 marked / 2 usable" in line for line in lines)
