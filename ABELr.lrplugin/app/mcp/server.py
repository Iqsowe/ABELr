"""FastMCP instance + Lightroom MCP tools (Phase 1: the 6 existing jobs).

Each tool = `require_bridge()` (preflight) then `run_job(...)` (submit +
offloaded wait_result). The tools' docstrings are what Claude sees: they
describe usage and conventions (PascalCase develop keys, etc.).

Mounted on FastAPI in `app/server/api.py`:
    http_app = mcp.streamable_http_app()   # creates the session manager (lazy)
    app.mount("/mcp", http_app)            # -> http://127.0.0.1:5000/mcp
with the lifespan forwarded (`async with mcp.session_manager.run()`).
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..core.render_metrics import MEASURE_LONG_EDGE
from ..server import budget as budgetmod
from ..server.job_queue import job_queue
from ..server.models import JobType, PhotoResult, ThumbnailResult
from .tools import require_bridge, run_job

# stateless_http: no session store to leak/crash (a single local client).
# json_response: POST returns application/json (not an SSE stream) -> simple transport.
# streamable_http_path='/': mounted at '/mcp', the effective URL is exactly /mcp
# (otherwise /mcp/mcp + 307 redirects).
mcp = FastMCP(
    "Lightroom Classic (ABELr)",
    instructions=(
        "Drives Adobe Lightroom Classic via the ABELr plugin. Prerequisites: "
        "Lightroom open + plugin connected + App running. First fetch the "
        "photo_id via get_selected_photos, then act on them (apply_adjustments...). "
        "Develop setting keys are PascalCase Lr SDK names."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


# --------------------------------------------------------------------------- #
# Serialization helpers (JobResult -> compact dict for MCP output)
# --------------------------------------------------------------------------- #
def _photo_to_dict(p: PhotoResult, include_develop: bool) -> dict[str, Any]:
    d: dict[str, Any] = {
        "photo_id": p.photo_id,
        "path": p.path,
        "exif": p.exif.model_dump(exclude_none=True) if p.exif else {},
    }
    if include_develop:
        d["current_develop"] = p.current_develop
    return d


def _thumb_to_dict(t: ThumbnailResult) -> dict[str, Any]:
    d: dict[str, Any] = {"photo_id": t.photo_id, "thumbnail_path": t.thumbnail_path}
    if t.error:
        d["error"] = t.error
    if t.width is not None:
        d["width"] = t.width
    if t.height is not None:
        d["height"] = t.height
    if t.is_export:
        d["is_export"] = t.is_export
    if t.asshot_temp is not None:
        d["asshot_temp"] = t.asshot_temp
    if t.asshot_tint is not None:
        d["asshot_tint"] = t.asshot_tint
    if t.restore_error:
        d["restore_error"] = t.restore_error
    return d


# --------------------------------------------------------------------------- #
# Tools — introspection
# --------------------------------------------------------------------------- #
@mcp.tool()
async def bridge_status() -> dict:
    """State of the Lightroom plugin bridge (without triggering a job).

    Returns {connected, last_poll_s_ago, pending_jobs}. `connected=false` =>
    Lightroom/plugin closed or App not running: the other tools will fail.
    """
    return {
        "connected": job_queue.bridge_connected(),
        "last_poll_s_ago": job_queue.seconds_since_poll(),
        "pending_jobs": job_queue.pending_count(),
    }


@mcp.tool()
async def ping() -> dict:
    """Ping the plugin (shows a "Hello World" popup in Lightroom). Verifies the round-trip."""
    require_bridge()
    await run_job(JobType.TEST, None, timeout=15.0)
    return {"ok": True, "message": "The Lightroom plugin responded."}


# --------------------------------------------------------------------------- #
# Tools — reading
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_selected_photos(include_develop: bool = False, timeout: float = 30.0) -> dict:
    """Photos currently selected in Lightroom (id, path, EXIF).

    include_develop=True adds the ~90 current develop settings per photo
    (PascalCase SDK) — large payload: leave it False for big selections
    (MCP output limit). The photo_id values feed the other tools (apply_adjustments...).
    """
    require_bridge()
    result = await run_job(JobType.GET_SELECTED_PHOTOS, None, timeout)
    photos = [_photo_to_dict(p, include_develop) for p in result.photos]
    return {"count": len(photos), "photos": photos}


@mcp.tool()
async def get_catalog_photos(include_develop: bool = False, timeout: float = 120.0) -> dict:
    """ALL photos in the active catalog (id, path, EXIF).

    Potentially huge: avoid include_develop=True here. Prefer
    get_selected_photos when you only want part of the catalog.
    """
    require_bridge()
    result = await run_job(JobType.GET_CATALOG_PHOTOS, None, timeout)
    photos = [_photo_to_dict(p, include_develop) for p in result.photos]
    return {"count": len(photos), "photos": photos}


@mcp.tool()
async def get_thumbnails(
    photo_ids: Optional[list[str]] = None,
    width: int = 2048,
    height: int = 2048,
    timeout: float = 120.0,
) -> dict:
    """Renders JPEG thumbnails of the selection (or of specific `photo_ids`).

    Returns local **file paths** (never base64 data). Without
    photo_ids, renders Lightroom's current selection.
    """
    require_bridge()
    payload: dict[str, Any] = {"width": width, "height": height}
    if photo_ids:
        payload["photo_ids"] = photo_ids
    result = await run_job(JobType.GET_THUMBNAILS, payload, timeout)
    thumbs = [_thumb_to_dict(t) for t in result.thumbnails]
    return {"count": len(thumbs), "thumbnails": thumbs}


@mcp.tool()
async def render_probe(
    adjustments: list[dict[str, Any]],
    settle: float = 0.6,
    width: int = MEASURE_LONG_EDGE,
    height: int = MEASURE_LONG_EDGE,
    timeout: Optional[float] = None,
) -> dict:
    """Trial preview: applies TEMPORARY settings, renders a thumbnail, RESTORES.

    Does not durably modify the photos (restores the original state). `adjustments`
    = [{"photo_id": "...", "develop": {<PascalCase key>: value, ...}}, ...].
    Useful for previewing a setting before apply_adjustments. Also re-reads
    Temperature/Tint after the apply (the As Shot numeric value if
    WhiteBalance='As Shot'). Non-empty `restore_error` = the photo stayed in a
    modified state (worth flagging).

    `width`/`height` default to the App's own measurement grid (PLAN.md R1) —
    a probe rendered smaller than that is rejected as an undersized
    measurement (`render_metrics_gpu.reject_if_undersized`) wherever it feeds
    a calibration; Lua's 2048 default (`PollingLoop.lua`) is a legacy fallback
    only, reached if this tool is called without the App's own defaults.
    """
    require_bridge()
    if not adjustments:
        raise ToolError("No adjustment provided.")
    if timeout is None:
        # PLAN.md X3: the old max(30, 5*n) was under-dimensioned at the
        # measurement grid (tuned for the 2048 default) — route through the
        # same budget the plugin's own workers use (PLAN.md N3).
        timeout = budgetmod.job_timeout(len(adjustments), width, height, settle, "probe")
    payload = {
        "adjustments": adjustments, "settle": settle,
        "width": width, "height": height, "timeout_s": timeout,
    }
    result = await run_job(JobType.RENDER_PROBE, payload, timeout)
    thumbs = [_thumb_to_dict(t) for t in result.thumbnails]
    return {"count": len(thumbs), "thumbnails": thumbs}


# --------------------------------------------------------------------------- #
# Tools — writing
# --------------------------------------------------------------------------- #
@mcp.tool()
async def apply_adjustments(
    adjustments: list[dict[str, Any]],
    timeout: Optional[float] = None,
) -> dict:
    """DURABLY applies develop settings to specific photos.

    `adjustments` = [{"photo_id": "<id>", "develop": {<key>: value, ...}}, ...].
    Fetch the photo_id values via get_selected_photos.

    Develop key conventions (Lr SDK, PhotoProcess PV2012):
    - PascalCase names with the 2012 suffix for tonal: Exposure2012,
      Contrast2012, Highlights2012, Shadows2012, Whites2012, Blacks2012.
    - White balance: set WhiteBalance='Custom' for Temperature and Tint to
      take effect (e.g. {"WhiteBalance": "Custom", "Temperature": 5650, "Tint": -5}).
    - HSL/color/curves: exact SDK names (e.g. SaturationAdjustmentRed,
      LuminanceAdjustmentBlue...).

    Returns {applied, matched, total}; `warnings` if a partial apply failed
    on some photos.
    """
    require_bridge()
    if not adjustments:
        raise ToolError("No adjustment provided.")
    if timeout is None:
        timeout = max(60.0, 2.0 * len(adjustments))
    payload = {"adjustments": adjustments}
    result = await run_job(JobType.APPLY_ADJUSTMENTS, payload, timeout)
    out: dict[str, Any] = {
        "applied": result.applied,
        "matched": result.matched,
        "total": result.total,
    }
    if result.errors_summary:  # partial apply — don't swallow it (Fable 5 review L-04)
        out["warnings"] = result.errors_summary
    return out


# --------------------------------------------------------------------------- #
# Phase 2 helpers
# --------------------------------------------------------------------------- #
def _applied_out(result: Any) -> dict[str, Any]:
    """Standard output for Phase 2 batch jobs: {applied, total, [warnings]}."""
    out: dict[str, Any] = {"applied": result.applied, "total": result.total}
    if result.errors_summary:
        out["warnings"] = result.errors_summary
    return out


# --------------------------------------------------------------------------- #
# Phase 2 — rating / flags / labels
# --------------------------------------------------------------------------- #
@mcp.tool()
async def set_rating(photo_ids: list[str], rating: int, timeout: float = 60.0) -> dict:
    """Star rating 0 to 5 for the given photos (same rating for all).

    rating=0 clears the stars. Fetch the photo_id values via get_selected_photos.
    """
    require_bridge()
    if not photo_ids:
        raise ToolError("No photo_id provided.")
    if not (0 <= rating <= 5):
        raise ToolError("rating must be between 0 and 5.")
    result = await run_job(
        JobType.SET_RATING, {"photo_ids": photo_ids, "rating": rating}, timeout
    )
    return _applied_out(result)


@mcp.tool()
async def set_flag_color(
    photo_ids: list[str],
    flag: Optional[str] = None,
    color: Optional[str] = None,
    timeout: float = 60.0,
) -> dict:
    """Flag (pick/reject) and/or color label for the given photos.

    flag: "pick" | "reject" | "none" (or None to leave untouched).
    color: "red" | "yellow" | "green" | "blue" | "purple" | "none" (or None).
    At least one of the two must be provided.
    """
    require_bridge()
    if not photo_ids:
        raise ToolError("No photo_id provided.")
    if flag is None and color is None:
        raise ToolError("Provide at least flag or color.")
    flags = {"pick", "reject", "none"}
    colors = {"red", "yellow", "green", "blue", "purple", "none"}
    if flag is not None and flag not in flags:
        raise ToolError(f"Invalid flag: {flag}. Expected: {sorted(flags)}.")
    if color is not None and color not in colors:
        raise ToolError(f"Invalid color: {color}. Expected: {sorted(colors)}.")
    payload: dict[str, Any] = {"photo_ids": photo_ids}
    if flag is not None:
        payload["flag"] = flag
    if color is not None:
        payload["color"] = color
    result = await run_job(JobType.SET_FLAG_COLOR, payload, timeout)
    return _applied_out(result)


@mcp.tool()
async def set_keywords(
    photo_ids: list[str],
    add: Optional[list[str]] = None,
    remove: Optional[list[str]] = None,
    timeout: float = 60.0,
) -> dict:
    """Adds and/or removes keywords (by name) on the given photos.

    `add`: keyword names to add (created if they don't exist, at the
    root level). `remove`: names to remove. At least one of the two non-empty.
    """
    require_bridge()
    if not photo_ids:
        raise ToolError("No photo_id provided.")
    if not add and not remove:
        raise ToolError("Provide at least one keyword in add or remove.")
    payload = {"photo_ids": photo_ids, "add": add or [], "remove": remove or []}
    result = await run_job(JobType.SET_KEYWORDS, payload, timeout)
    return _applied_out(result)


# --------------------------------------------------------------------------- #
# Phase 2 — collections
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_collections(timeout: float = 30.0) -> dict:
    """Tree of collections and collection sets in the catalog.

    Returns {collections: [ {name, id, kind: "collection"|"set", photo_count?,
    children: [...] } ]}. Use the id (or names) with add_to_collection.
    """
    require_bridge()
    result = await run_job(JobType.LIST_COLLECTIONS, None, timeout)
    return result.data or {"collections": []}


@mcp.tool()
async def create_collection(
    name: str, parent: Optional[str] = None, timeout: float = 30.0
) -> dict:
    """Creates a collection (returns the existing one if already present).

    `parent`: name or id of a parent collection set (None = root).
    Returns {name, id, created}.
    """
    require_bridge()
    if not name:
        raise ToolError("Empty collection name.")
    payload: dict[str, Any] = {"name": name}
    if parent is not None:
        payload["parent"] = parent
    result = await run_job(JobType.CREATE_COLLECTION, payload, timeout)
    return result.data or {}


@mcp.tool()
async def add_to_collection(
    collection: str, photo_ids: list[str], timeout: float = 60.0
) -> dict:
    """Adds photos to a collection (identified by id or name).

    Fetch the id/name via list_collections, the photo_id values via get_selected_photos.
    Returns {applied, total, [warnings]}.
    """
    require_bridge()
    if not collection:
        raise ToolError("Collection not specified.")
    if not photo_ids:
        raise ToolError("No photo_id provided.")
    result = await run_job(
        JobType.ADD_TO_COLLECTION,
        {"collection": collection, "photo_ids": photo_ids},
        timeout,
    )
    return _applied_out(result)


# --------------------------------------------------------------------------- #
# Phase 2 — develop presets
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_develop_presets(timeout: float = 30.0) -> dict:
    """Develop presets available in Lightroom.

    Returns {presets: [ {name, uuid, folder} ]}. Apply with apply_develop_preset.
    """
    require_bridge()
    result = await run_job(JobType.LIST_DEVELOP_PRESETS, None, timeout)
    return result.data or {"presets": []}


@mcp.tool()
async def apply_develop_preset(
    photo_ids: list[str], preset: str, timeout: Optional[float] = None
) -> dict:
    """Applies a develop preset (by uuid or name) to the given photos.

    Fetch the uuid/name via list_develop_presets. Prefer the uuid (names may
    not be unique). Returns {applied, total, [warnings]}.
    """
    require_bridge()
    if not photo_ids:
        raise ToolError("No photo_id provided.")
    if not preset:
        raise ToolError("Preset not specified.")
    if timeout is None:
        timeout = max(60.0, 2.0 * len(photo_ids))
    result = await run_job(
        JobType.APPLY_DEVELOP_PRESET,
        {"photo_ids": photo_ids, "preset": preset},
        timeout,
    )
    return _applied_out(result)
