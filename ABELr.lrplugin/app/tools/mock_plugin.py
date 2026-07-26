"""Mock of the Lr plugin — simulates polling to test the App without Lightroom.

Reproduces the Lua plugin's behavior: loops on GET /jobs/pending then
POST /jobs/{id}/result with fake data.

Usage (App already running on :5000):
    python -m app.tools.mock_plugin
"""

from __future__ import annotations

import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

import requests

from ..server.job_queue import JobQueue
from ..server.job_queue import job_queue as _default_job_queue
from ..server.models import JobResult, JobType

BASE = "http://127.0.0.1:5000"
POLL_INTERVAL = 0.3

# Fake thumbnails (gray JPEG) written here for get_thumbnails / render_probe.
THUMBS_DIR = Path(tempfile.gettempdir()) / "abelr_mock_thumbs"

FAKE_PHOTOS = [
    {
        "photo_id": "uuid-aaa",
        "path": "C:/temp/DSC00123.ARW",
        "exif": {
            "iso": 800,
            "aperture": 2.8,
            "shutter_speed": "1/200",
            "focal_length": 85,
            "camera": "ILCE-7M4",
        },
        "current_develop": {"Exposure2012": 0.0, "Temperature": 5500, "Tint": 0},
    },
    {
        "photo_id": "uuid-bbb",
        "path": "C:/temp/DSC00124.ARW",
        "exif": {
            "iso": 1600,
            "aperture": 4.0,
            "shutter_speed": "1/125",
            "focal_length": 85,
            "camera": "ILCE-7M4",
        },
        "current_develop": {"Exposure2012": -0.3, "Temperature": 5200, "Tint": 5},
    },
]

# Fake As Shot WB returned by render_probe (like the plugin after apply).
FAKE_ASSHOT = {"temp": 5300.0, "tint": 4.0}

# Fake collection tree (Phase 2 list_collections jobs).
FAKE_COLLECTIONS = {
    "collections": [
        {"name": "Best of 2025", "id": "col-1", "kind": "collection", "photo_count": 12,
         "children": []},
        {"name": "Voyages", "id": "set-1", "kind": "set", "children": [
            {"name": "Japon", "id": "col-2", "kind": "collection", "photo_count": 40,
             "children": []},
        ]},
    ]
}

# Fake develop presets (Phase 2 list_develop_presets jobs).
FAKE_PRESETS = {
    "presets": [
        {"name": "Sony Portrait", "uuid": "preset-aaa", "folder": "User Presets"},
        {"name": "B&W Contrast", "uuid": "preset-bbb", "folder": "User Presets"},
    ]
}


def _batch_ok(job_id: str, applied: int, total: int) -> dict:
    """Standard result for a Phase 2 batch job (set_rating/keywords/preset…)."""
    return {"job_id": job_id, "status": "ok", "photos": [],
            "applied": applied, "total": total}


def _write_gray_jpeg(photo_id: str, level: int, size: tuple[int, int] = (256, 384)) -> str:
    """Writes a solid gray JPEG (fake thumbnail) and returns its absolute path.

    `size` = (h, w) — defaults to the pre-existing fake-thumbnail size; a
    caller simulating an undersized render (below `MEASURE_LONG_EDGE`) passes
    a smaller size.
    """
    import cv2
    import numpy as np

    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    path = THUMBS_DIR / f"{photo_id}.jpg"
    img = np.full((size[0], size[1], 3), level, np.uint8)
    cv2.imwrite(str(path), img)
    return str(path)


def _thumbnails_for(photo_ids: list[str], level: int, asshot: bool = False) -> list[dict]:
    out = []
    for pid in photo_ids:
        entry: dict = {"photo_id": pid, "thumbnail_path": _write_gray_jpeg(pid, level)}
        if asshot:
            entry["asshot_temp"] = FAKE_ASSHOT["temp"]
            entry["asshot_tint"] = FAKE_ASSHOT["tint"]
        out.append(entry)
    return out


def handle(job: dict) -> dict:
    job_id = job["job_id"]
    job_type = job["type"]
    payload = job.get("payload") or {}
    if job_type == "test":
        print("  [mock] test: Hello World popup (simulated)")
        return {"job_id": job_id, "status": "ok", "photos": []}
    if job_type == "get_selected_photos":
        return {"job_id": job_id, "status": "ok", "photos": FAKE_PHOTOS}
    if job_type == "get_thumbnails":
        ids = payload.get("photo_ids") or [p["photo_id"] for p in FAKE_PHOTOS]
        print(f"  [mock] get_thumbnails: {len(ids)} gray thumbnail(s)")
        return {
            "job_id": job_id, "status": "ok", "photos": [],
            "thumbnails": _thumbnails_for(ids, level=120),
        }
    if job_type == "render_probe":
        adjustments = payload.get("adjustments") or []
        ids = [a["photo_id"] for a in adjustments]
        print(
            f"  [mock] render_probe: {len(ids)} simulated neutral render(s) "
            f"(settle={payload.get('settle')})"
        )
        # Gray level slightly different from get_thumbnails: anchor != current render
        # (otherwise the anti-stale-probe guard would rightly trigger).
        return {
            "job_id": job_id, "status": "ok", "photos": [],
            "thumbnails": _thumbnails_for(ids, level=140, asshot=True),
        }
    if job_type == "apply_adjustments":
        print(f"  [mock] apply_adjustments: {payload}")
        n = len(payload.get("adjustments") or [])
        return {
            "job_id": job_id, "status": "ok", "photos": [],
            "applied": n, "matched": n, "total": n,
        }
    # --- Phase 2 ---
    if job_type in ("set_rating", "set_flag_color", "set_keywords",
                    "add_to_collection", "apply_develop_preset"):
        ids = payload.get("photo_ids") or []
        print(f"  [mock] {job_type}: {len(ids)} photo(s) | {payload}")
        return _batch_ok(job_id, applied=len(ids), total=len(ids))
    if job_type == "list_collections":
        print("  [mock] list_collections")
        return {"job_id": job_id, "status": "ok", "photos": [], "data": FAKE_COLLECTIONS}
    if job_type == "create_collection":
        name = payload.get("name")
        print(f"  [mock] create_collection: {name} (parent={payload.get('parent')})")
        return {"job_id": job_id, "status": "ok", "photos": [],
                "data": {"name": name, "id": "col-new", "created": True}}
    if job_type == "list_develop_presets":
        print("  [mock] list_develop_presets")
        return {"job_id": job_id, "status": "ok", "photos": [], "data": FAKE_PRESETS}
    return {"job_id": job_id, "status": "error", "error": f"unknown type: {job_type}"}


# ---------------------------------------------------------------------- #
# Injectable render_probe behaviors (PLAN.md N1a) — each factory returns a
# hook `(job: dict) -> dict | None` called instead of `handle`'s default
# render_probe branch. Returning None simulates a plugin that picked up the
# job but never posts a result (job_queue.wait_result then times out).
# ---------------------------------------------------------------------- #
RenderProbeHook = Callable[[dict], Optional[dict]]


def _probe_ids(job: dict) -> list[str]:
    return [a["photo_id"] for a in (job.get("payload") or {}).get("adjustments") or []]


def render_probe_ok(level: int = 140) -> RenderProbeHook:
    """Every photo gets a usable thumbnail — same shape as `handle`'s default."""
    def _hook(job: dict) -> dict:
        ids = _probe_ids(job)
        return {
            "job_id": job["job_id"], "status": "ok", "photos": [],
            "thumbnails": _thumbnails_for(ids, level=level, asshot=True),
        }
    return _hook


def render_probe_partial(n_fail: int, level: int = 140) -> RenderProbeHook:
    """The first `n_fail` photos in the chunk fail (no thumbnail_path), the rest succeed."""
    def _hook(job: dict) -> dict:
        ids = _probe_ids(job)
        thumbs = []
        for i, pid in enumerate(ids):
            if i < n_fail:
                thumbs.append({"photo_id": pid, "thumbnail_path": None, "error": "timeout"})
            else:
                thumbs.append({
                    "photo_id": pid, "thumbnail_path": _write_gray_jpeg(pid, level),
                    "asshot_temp": FAKE_ASSHOT["temp"], "asshot_tint": FAKE_ASSHOT["tint"],
                })
        return {"job_id": job["job_id"], "status": "ok", "photos": [], "thumbnails": thumbs}
    return _hook


def render_probe_all_timeout() -> RenderProbeHook:
    """Every photo in the chunk fails (status stays 'ok' — a per-item timeout,
    not a job-level failure — mirrors Thumbnails.lua marking pending entries
    as `error='timeout'` while still returning normally)."""
    def _hook(job: dict) -> dict:
        ids = _probe_ids(job)
        thumbs = [{"photo_id": pid, "thumbnail_path": None, "error": "timeout"} for pid in ids]
        return {"job_id": job["job_id"], "status": "ok", "photos": [], "thumbnails": thumbs}
    return _hook


def render_probe_error_status(level: int = 140) -> RenderProbeHook:
    """Job-level status='error' even though thumbnails are present."""
    def _hook(job: dict) -> dict:
        ids = _probe_ids(job)
        return {
            "job_id": job["job_id"], "status": "error", "error": "simulated plugin error",
            "photos": [], "thumbnails": _thumbnails_for(ids, level=level, asshot=True),
        }
    return _hook


def render_probe_no_response() -> RenderProbeHook:
    """The job is picked up (next_pending) but never gets a submitted result."""
    def _hook(job: dict) -> None:
        return None
    return _hook


def render_probe_restore_failed(n_restore_fail: int, level: int = 140) -> RenderProbeHook:
    """The first `n_restore_fail` photos render fine but carry `restore_error`."""
    def _hook(job: dict) -> dict:
        ids = _probe_ids(job)
        thumbs = []
        for i, pid in enumerate(ids):
            entry = {
                "photo_id": pid, "thumbnail_path": _write_gray_jpeg(pid, level),
                "asshot_temp": FAKE_ASSHOT["temp"], "asshot_tint": FAKE_ASSHOT["tint"],
            }
            if i < n_restore_fail:
                entry["restore_error"] = "applyDevelopSettings restore failed"
            thumbs.append(entry)
        return {"job_id": job["job_id"], "status": "ok", "photos": [], "thumbnails": thumbs}
    return _hook


def render_probe_undersized(level: int = 140, size: tuple[int, int] = (256, 256)) -> RenderProbeHook:
    """Every thumbnail renders below the measurement grid (R1's rejection target)."""
    def _hook(job: dict) -> dict:
        ids = _probe_ids(job)
        thumbs = [
            {
                "photo_id": pid, "thumbnail_path": _write_gray_jpeg(pid, level, size=size),
                "asshot_temp": FAKE_ASSHOT["temp"], "asshot_tint": FAKE_ASSHOT["tint"],
            }
            for pid in ids
        ]
        return {"job_id": job["job_id"], "status": "ok", "photos": [], "thumbnails": thumbs}
    return _hook


class FakePlugin:
    """In-process fake of the Lr plugin — drives `job_queue` directly, no HTTP.

    `handle()` (above) is already transport-free; this wraps it so tests can
    exercise the full `ensure_neutral_previews` flow against a real
    `JobQueue` without a running FastAPI server or Lightroom. Set
    `render_probe_hook` to override the default render_probe behavior with
    one of the `render_probe_*` factories above.
    """

    def __init__(self, queue: Optional[JobQueue] = None) -> None:
        self.queue = queue if queue is not None else _default_job_queue
        self.render_probe_hook: Optional[RenderProbeHook] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def pump(self) -> bool:
        """One poll cycle: next_pending -> handle (or hook) -> submit_result.

        Returns True if a job was picked up (whether or not a result was
        submitted — `render_probe_no_response` deliberately submits nothing).
        """
        job = self.queue.next_pending()
        if job is None:
            return False
        job_dict = {"job_id": job.job_id, "type": job.type, "payload": job.payload}
        if job.type == JobType.RENDER_PROBE and self.render_probe_hook is not None:
            result_dict = self.render_probe_hook(job_dict)
        else:
            result_dict = handle(job_dict)
        if result_dict is not None:
            self.queue.submit_result(JobResult(**result_dict))
        return True

    @contextmanager
    def run_in_thread(self, poll_interval: float = 0.01):
        """Runs `pump()` in a loop on a background thread for the block's duration."""
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                if not self.pump():
                    time.sleep(poll_interval)

        self._thread = threading.Thread(target=_loop, daemon=True, name="fake-plugin")
        self._thread.start()
        try:
            yield self
        finally:
            self._stop.set()
            self._thread.join(timeout=5)


def main() -> None:
    print(f"Mock plugin -> {BASE} (Ctrl+C to stop)")
    while True:
        try:
            resp = requests.get(f"{BASE}/jobs/pending", timeout=5)
        except requests.RequestException:
            time.sleep(1.0)
            continue
        if resp.status_code == 204 or not resp.content:
            time.sleep(POLL_INTERVAL)
            continue
        job = resp.json()
        print(f"Job received: {job['type']} ({job['job_id']})")
        result = handle(job)
        requests.post(f"{BASE}/jobs/{job['job_id']}/result", json=result, timeout=5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopping mock plugin.")
