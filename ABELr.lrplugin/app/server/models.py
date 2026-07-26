"""Pydantic models — JSON contracts exchanged between the App and the Lr plugin.

JSON keys in snake_case. Lr SDK develop parameter names remain in PascalCase
(e.g. Exposure, Temperature) inside the `develop` dict.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class JobType(str, Enum):
    """Job types sent to the plugin via polling."""

    GET_SELECTED_PHOTOS = "get_selected_photos"
    GET_CATALOG_PHOTOS = "get_catalog_photos"  # all photos in the active catalog
    GET_THUMBNAILS = "get_thumbnails"           # JPEG thumbnails of the selection (→ preview analysis)
    RENDER_PROBE = "render_probe"               # applies temp settings → thumbnail → restores (response calibration)
    APPLY_ADJUSTMENTS = "apply_adjustments"
    TEST = "test"  # plugin ping: triggers a Hello World popup on the Lr side

    # --- Phase 2: parity with the third-party Lightroom MCP (API access) ---
    SET_RATING = "set_rating"                   # 0-5 rating on photos
    SET_FLAG_COLOR = "set_flag_color"           # pick/reject/none flag + color label
    SET_KEYWORDS = "set_keywords"               # adds/removes keywords
    LIST_COLLECTIONS = "list_collections"       # collection tree (→ data)
    CREATE_COLLECTION = "create_collection"     # creates a collection (→ data)
    ADD_TO_COLLECTION = "add_to_collection"     # adds photos to a collection
    LIST_DEVELOP_PRESETS = "list_develop_presets"   # available develop presets (→ data)
    APPLY_DEVELOP_PRESET = "apply_develop_preset"    # applies a develop preset


class JobStatus(str, Enum):
    """Lifecycle of a job on the App side."""

    PENDING = "pending"      # waiting for the plugin to pick it up
    IN_PROGRESS = "in_progress"  # picked up by the plugin, result expected
    DONE = "done"            # result received
    FAILED = "failed"        # the plugin reported an error


class Job(BaseModel):
    """Job pushed into the queue, retrieved by the plugin via GET /jobs/pending."""

    job_id: str
    type: JobType
    # Payload specific to the type (e.g. adjustments for apply_adjustments).
    payload: dict[str, Any] = Field(default_factory=dict)


class ExifData(BaseModel):
    iso: Optional[int] = None
    aperture: Optional[float] = None
    shutter_speed: Optional[str] = None
    focal_length: Optional[float] = None
    camera: Optional[str] = None


class PhotoResult(BaseModel):
    """Photo data returned by the plugin."""

    photo_id: str
    path: str
    # Path of the active .lrcat — lets the App locate the Previews.lrdata /
    # Smart Previews.lrdata bundles associated with this photo.
    catalog_path: Optional[str] = None
    exif: ExifData = Field(default_factory=ExifData)
    # Current develop settings — Lr SDK PascalCase keys.
    current_develop: dict[str, Any] = Field(default_factory=dict)


class ThumbnailResult(BaseModel):
    """JPEG thumbnail written by the plugin for a photo (get_thumbnails / render_probe jobs)."""

    photo_id: str
    thumbnail_path: Optional[str] = None  # local absolute path of the JPEG, or None on error
    error: Optional[str] = None
    # Actual pixel size of the JPEG, read from its SOF marker by the plugin.
    # requestJpegThumbnail ignores the requested size and serves whichever
    # preview tier Lr has cached, so "which size did we really get" is only
    # knowable per result (cache.py ANALYSIS_VERSION "v8-grid-enforced").
    width: Optional[int] = None
    height: Optional[int] = None
    # True if requestJpegThumbnail never delivered a full-size render and this
    # JPEG came from the LrExportSession fallback instead (Thumbnails.lua
    # exportViaSession). A one-shot render, not a cached tier some other chunk
    # might still want — callers delete it themselves right after decoding
    # (see gpu_schedule._delete_if_export / fresh_render_worker / mcp tools).
    is_export: bool = False
    # Filled by render_probe: numeric Temperature/Tint read back after the apply
    # (if the probe sets WhiteBalance='As Shot', this is the As Shot numeric value).
    asshot_temp: Optional[float] = None
    asshot_tint: Optional[float] = None
    # render_probe: restoring the original state failed — the photo was left
    # in a neutral state (Fable 5 review L-03). Strong signal: surface to the user.
    restore_error: Optional[str] = None


class JobResult(BaseModel):
    """Result submitted by the plugin via POST /jobs/{id}/result."""

    job_id: str
    status: str = "ok"  # "ok" | "error"
    error: Optional[str] = None
    photos: list[PhotoResult] = Field(default_factory=list)
    thumbnails: list[ThumbnailResult] = Field(default_factory=list)
    # Filled by the apply_adjustments job (apply diagnostics).
    applied: Optional[int] = None
    matched: Optional[int] = None
    total: Optional[int] = None
    # Error summary of a PARTIAL apply (status='ok' but some photos failed)
    # — Fable 5 review L-04: error texts are no longer lost.
    errors_summary: Optional[str] = None
    # Generic return payload for Phase 2 jobs whose shape doesn't fit
    # photos/thumbnails (e.g. list_collections → tree, list_develop_presets → list,
    # create_collection → created collection info). Avoids one field per capability.
    data: Optional[dict[str, Any]] = None


class PhotoAdjustment(BaseModel):
    """Adjustments to apply to a photo — develop keys in SDK PascalCase."""

    photo_id: str
    develop: dict[str, Any]
