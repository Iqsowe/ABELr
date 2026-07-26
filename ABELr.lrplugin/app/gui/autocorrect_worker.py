"""Qt worker — analysis (RAW + in-camera JPEG + preview/neutral anchor) and planning.

**GPU + cache** pipeline (off the GUI thread):
1+2. **Source RAW + in-camera JPEG** in one merged pass (Fable 5 review G7):
   `SourceRAW`/`InCameraJPEG` caches (same key = RAW signature); misses →
   `gpu_schedule.process_combined_batch` (one rawpy open per photo, GPU demosaic
   + nvJPEG, CPU/GPU double buffer).
3. Reference-state measurement, **depending on mode**:
   - **seeds**: current rendered preview (`PreviewJPEG` cache; with
     `force_fresh_preview=True` the cache is never read, only written).
   - **embedded**: **neutral anchor** (`ensure_neutral_previews` — `NeutralPreviewJPEG`
     cache, plugin `render_probe` jobs for misses). No measurement of the
     current render: the planned values are absolute (idempotent) and
     insensitive to Lr preview freshness.
4. `analyze_only=True` mode ("Mark + analyze"): stops after populating the
   cache (RAW + in-camera JPEG + preview), does not call `autocorrect.plan`.
5. Otherwise: seed pool → `autocorrect.plan(...)` (the embedded profile bias
   was removed — "bias ignored" decision, Fable 5 review DB-06).

**GPU-first, CPU-fallback** policy: if no CUDA is usable, computation continues
on CPU (slower) — a warning is emitted via `progress` rather than failing
(see `core/gpu.py`).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Signal

from ..core import analysis as analysismod
from ..core import autocorrect, cache as cachemod, exif_profile, gpu, gpu_jpeg, gpu_schedule, measure
from ..core import response, seed_match
from ..core.autocorrect import PhotoMeasure, PlanDiagnostics
from ..core.previews import PreviewIndex
from ..server.models import PhotoAdjustment, PhotoResult
from .neutral_preview_worker import ensure_neutral_previews


@dataclass
class AutoCorrectResult:
    adjustments: list[PhotoAdjustment]
    diagnostics: PlanDiagnostics | None
    n_measured: int
    n_skipped: int
    notes: list[str] = field(default_factory=list)
    # Seeds marked in DB vs actually usable (RAW analysis present).
    # marked > usable ⇒ silent embedded fallback to flag on the GUI side.
    seeds_marked: int = 0
    seeds_usable: int = 0
    # Detected (camera, in-camera profile) used to load the response model —
    # None in analyze_only mode (no plan() call, cf. PLAN.md U2 status panel).
    camera: str | None = None
    profile: str | None = None


def _safe(fn) -> None:
    """Executes a cache write while ignoring errors (the cache must never
    make the analysis fail)."""
    try:
        fn()
    except Exception:
        pass


class AutoCorrectWorker(QThread):
    """Measures the selection (GPU + cache) and plans/applies the correction."""

    finished_result = Signal(object)   # AutoCorrectResult
    progress = Signal(str)             # step message
    progress_count = Signal(int, int)  # (done, total) for a step → determinate progress bar
    failed = Signal(str)

    def __init__(
        self,
        photos: list[PhotoResult],
        axes: frozenset[str] = autocorrect.DEFAULT_AXES,
        forced_embedded: bool = False,
        thumbnail_paths: dict[str, str] | None = None,
        analyze_only: bool = False,
        force_fresh_preview: bool = False,
    ) -> None:
        super().__init__()
        self._photos = photos
        self._axes = axes
        self._forced_embedded = forced_embedded
        self._thumbs = thumbnail_paths or {}
        self._analyze_only = analyze_only
        self._force_fresh_preview = force_fresh_preview
        self._profile_cache: dict[str, str | None] = {}  # path → in-camera creative profile

    def _batch_progress(self, label: str):
        """Callback `(done, total)` for a GPU step → emits both the text AND the
        counters (the latter drive the determinate loading bar on the GUI side)."""
        def cb(done: int, total: int) -> None:
            self.progress.emit(f"{label} {done}/{total} (GPU)…")
            self.progress_count.emit(done, total)
        return cb

    def _profiles(self, paths: list[str]) -> dict[str, str | None]:
        """In-camera creative profile for a batch (exiftool, batched, memoized on the worker).

        A single exiftool invocation per batch of not-yet-read paths; robust to
        exiftool being absent (None values)."""
        todo = [p for p in paths if p not in self._profile_cache]
        if todo:
            got = exif_profile.read_capture_profiles(todo)
            for p in todo:
                self._profile_cache[p] = got.get(p)
        return {p: self._profile_cache.get(p) for p in paths}

    def run(self) -> None:
        conn = None  # closed in the finally block — even on exception (Fable 5 review B-04)
        try:
            photos = self._photos
            if not photos:
                self.failed.emit("No photo selected.")
                return

            # GPU-first, CPU-fallback (no blocking failure — see core/gpu.py).
            if not gpu.is_available():
                self.progress.emit(
                    f"No GPU — analyzing on {gpu.device_name()} (slower)."
                )

            catalog_path = next((p.catalog_path for p in photos if p.catalog_path), None)
            if catalog_path:
                try:
                    conn = cachemod.open_cache(catalog_path)
                except Exception:
                    conn = None
            idx = PreviewIndex(catalog_path) if catalog_path else None

            try:
                raw_by_id, embedded_by_id = self._collect_raw_and_embedded(photos, conn)

                if self._analyze_only:
                    # Also populates the preview cache (useful for future seeds).
                    self._collect_renders(photos, conn, idx)
                    n = len(raw_by_id)
                    marked = len(cachemod.list_seed_uuids(conn)) if conn else 0
                    usable = len(seed_match.build_seed_pool(conn)) if conn else 0
                    self.finished_result.emit(
                        AutoCorrectResult(
                            adjustments=[], diagnostics=None,
                            n_measured=n, n_skipped=len(photos) - n,
                            notes=[f"Analysis: {n}/{len(photos)} photo(s) (RAW+in-camera JPEG+preview)."],
                            seeds_marked=marked, seeds_usable=usable,
                        )
                    )
                    return

                seeds_marked = len(cachemod.list_seed_uuids(conn)) if conn else 0
                seed_pool = seed_match.build_seed_pool(conn) if conn else []
                mode_embedded = self._forced_embedded or not seed_pool

                notes: list[str] = []
                channels: Counter[str] = Counter()
                measures: list[PhotoMeasure] = []

                if mode_embedded:
                    # Neutral anchor (hash_style cache, render_probe jobs for
                    # misses) — NO measurement of the current render.
                    neutral_outcome = ensure_neutral_previews(
                        photos, conn, progress=self.progress.emit,
                        progress_count=self.progress_count.emit,
                    )
                    neutral_by_id = neutral_outcome.by_id
                    n_refreshed = neutral_outcome.n_refreshed
                    if n_refreshed:
                        notes.append(f"{n_refreshed} neutral anchor(s) recalibrated via Lightroom.")
                    for p in photos:
                        nd = neutral_by_id.get(p.photo_id)
                        emb = embedded_by_id.get(p.photo_id) or (None, None)
                        if nd is None or (emb[0] is None and emb[1] is None):
                            continue
                        raw_d = raw_by_id.get(p.photo_id) or {}
                        measures.append(
                            PhotoMeasure(
                                photo_id=p.photo_id,
                                path=p.path,
                                current_develop=p.current_develop or {},
                                exif_camera=p.exif.camera if p.exif else None,
                                analysis=None,
                                is_seed=cachemod.is_seed(conn, p.photo_id) if conn else False,
                                raw_tone=raw_d.get("tone"),
                                raw_bands=raw_d.get("bands"),
                                embedded_sharp=emb[0],
                                embedded_global=emb[1],
                                neutral_sharp=nd.get("sharp"),
                                neutral_global=nd.get("glob"),
                                neutral_asshot_temp=nd.get("asshot_temp"),
                                neutral_asshot_tint=nd.get("asshot_tint"),
                                hash_style=cachemod.style_hash(p.current_develop or {}),
                                asshot_rg=raw_d.get("asshot_rg"),
                                asshot_bg=raw_d.get("asshot_bg"),
                                profile_capture=raw_d.get("profile_capture"),
                                ev100=raw_d.get("ev100"),
                            )
                        )
                else:
                    render_by_id, channels, _skipped_render = self._collect_renders(
                        photos, conn, idx
                    )
                    for p in photos:
                        ra = render_by_id.get(p.photo_id)
                        if ra is None:
                            continue
                        raw_d = raw_by_id.get(p.photo_id) or {}
                        measures.append(
                            PhotoMeasure(
                                photo_id=p.photo_id,
                                path=p.path,
                                current_develop=p.current_develop or {},
                                exif_camera=p.exif.camera if p.exif else None,
                                analysis=ra,
                                is_seed=cachemod.is_seed(conn, p.photo_id) if conn else False,
                                raw_tone=raw_d.get("tone"),
                                raw_bands=raw_d.get("bands"),
                                asshot_rg=raw_d.get("asshot_rg"),
                                asshot_bg=raw_d.get("asshot_bg"),
                                profile_capture=raw_d.get("profile_capture"),
                                ev100=raw_d.get("ev100"),
                            )
                        )
            finally:
                if idx is not None:
                    idx.close()

            skipped = len(photos) - len(measures)
            if not measures:
                if mode_embedded:
                    self.failed.emit(
                        f"No measurable photo out of {len(photos)}: missing neutral "
                        f"anchor or in-camera JPEG. Check the plugin bridge (render_probe "
                        f"jobs) and that the RAW files contain an embedded JPEG."
                    )
                else:
                    self.failed.emit(self._no_render_message(len(photos), channels, idx))
                return

            camera = next((m.exif_camera for m in measures if m.exif_camera), None)
            # Keyed by the in-camera creative profile (IN/SH/Neutral/…), matching
            # how calibrate_hsl_response.py/calibrate_wb_response.py save their
            # response (`exif_profile.read_capture_profiles`) — NOT `current_develop
            # ["CameraProfile"]` (the Lr DCP color profile, a different axis
            # entirely: e.g. "Camera FL" for every photo in a catalog regardless of
            # in-camera style). Using the DCP key here silently loaded no
            # calibration ever, on any catalog where the DCP profile is constant.
            profiles = Counter(m.profile_capture for m in measures if m.profile_capture)
            profile = profiles.most_common(1)[0][0] if profiles else None
            model = response.load(camera, profile)

            self.progress.emit("Planning corrections…")
            adjustments, diag = autocorrect.plan(
                measures,
                axes=self._axes,
                forced_embedded=self._forced_embedded,
                model=model,
                seed_pool=seed_pool,
            )
            self.finished_result.emit(
                AutoCorrectResult(
                    adjustments=adjustments,
                    diagnostics=diag,
                    n_measured=len(measures),
                    n_skipped=skipped,
                    notes=notes,
                    seeds_marked=seeds_marked,
                    seeds_usable=len(seed_pool),
                    camera=camera,
                    profile=profile,
                )
            )
        except Exception as exc:  # safety net
            self.failed.emit(str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Steps 1+2 merged: source RAW + in-camera JPEG (Fable 5 review G7/P-02)
    # ------------------------------------------------------------------ #
    def _collect_raw_and_embedded(self, photos, conn) -> tuple[dict[str, dict], dict[str, tuple]]:
        """RAW (tone+sharp-zone bands, as-shot, exposure, gray-world) + in-camera
        JPEG targets {uuid: (sharp, glob)} in ONE GPU pass.

        Both caches share the same freshness key (`raw_signature`): a photo
        missing from both sides only opens the ARW container once
        (`gpu_schedule.process_combined_batch`, unified unpack + double buffer).
        Writes `SourceRAW`/`LightroomPicture`/`InCameraJPEG` with the **precomputed
        deltas** vs RAW, in ONE transaction (P-07)."""
        raw_out: dict[str, dict] = {}
        emb_out: dict[str, tuple] = {}
        raw_misses: list[PhotoResult] = []
        emb_misses: list[PhotoResult] = []
        sig: dict[str, str] = {}
        for p in photos:
            s = cachemod.raw_signature(p.path)
            sig[p.photo_id] = s
            cached = cachemod.get_source_raw(conn, p.photo_id, s) if conn else None
            if cached is not None and cached["tone"] is not None:
                raw_out[p.photo_id] = cached
            else:
                raw_misses.append(p)
            cached_j = cachemod.get_in_camera_jpeg(conn, p.photo_id, s) if conn else None
            if cached_j is not None:
                emb_out[p.photo_id] = (cached_j["sharp"], cached_j["global"])
            else:
                emb_misses.append(p)

        if not raw_misses and not emb_misses:
            return raw_out, emb_out

        self.progress.emit(
            f"Reading RAW + in-camera JPEG (GPU) — {len(raw_misses)} RAW / "
            f"{len(emb_misses)} missing JPEG(s)…"
        )
        got_raw, got_emb = gpu_schedule.process_combined_batch(
            [p.path for p in raw_misses],
            [p.path for p in emb_misses],
            progress=self._batch_progress("RAW + in-camera JPEG"),
        )
        miss_paths = list(dict.fromkeys(p.path for p in raw_misses + emb_misses))
        profiles = self._profiles(miss_paths)

        for p in raw_misses:
            r = got_raw.get(p.path)
            if r is None:
                continue
            prof = profiles.get(p.path)
            ev = analysismod.ev100(
                p.exif.iso if p.exif else None,
                p.exif.aperture if p.exif else None,
                p.exif.shutter_speed if p.exif else None,
            )
            raw_out[p.photo_id] = {
                "asshot_rg": r.asshot_rg, "asshot_bg": r.asshot_bg,
                "tone": r.tone, "bands": r.bands,
                "ev100": ev, "profile_capture": prof,
            }
            if conn is not None:
                s = sig[p.photo_id]
                _safe(lambda r=r, s=s, p=p, ev=ev, prof=prof: cachemod.put_source_raw(
                    conn, p.photo_id, s,
                    asshot_rg=r.asshot_rg, asshot_bg=r.asshot_bg,
                    exposure_global=r.exposure, exposure_sharp=r.exposure_sharp,
                    grayworld_global=(r.grayworld_rg, r.grayworld_bg),
                    grayworld_sharp=(r.grayworld_rg_sharp, r.grayworld_bg_sharp),
                    mask_sharp_frac=r.mask_sharp_frac, ev100=ev, profile_capture=prof,
                    tone=r.tone, bands=r.bands, commit=False,
                ))
                _safe(lambda p=p, prof=prof: cachemod.put_picture(
                    conn, p.photo_id, path=p.path, catalog_path=p.catalog_path,
                    exif=(p.exif.model_dump() if p.exif else None),
                    current_develop=p.current_develop or {}, profile_capture=prof,
                    commit=False))

        for p in emb_misses:
            r = got_emb.get(p.path)
            if r is None or r.sharp is None:
                emb_out[p.photo_id] = (None, None)
                continue
            emb_out[p.photo_id] = (r.sharp, r.glob)
            if conn is not None:
                s = sig[p.photo_id]
                prof = profiles.get(p.path)
                _safe(lambda r=r, s=s, p=p, prof=prof:
                      cachemod.put_in_camera_jpeg(
                          conn, p.photo_id, s,
                          sharp=r.sharp, glob=r.glob,
                          mask_sharp_frac=r.mask_sharp_frac, profile_capture=prof,
                          commit=False))

        # A single commit for the whole pass (Fable 5 review P-07): avoids
        # ~2-3 commits/photo (WAL churn) on large batches.
        if conn is not None:
            _safe(conn.commit)
        return raw_out, emb_out

    # ------------------------------------------------------------------ #
    # Step 3: current rendered preview (tone/neutral/sharp-zone bands) — cache + GPU
    # ------------------------------------------------------------------ #
    def _collect_renders(self, photos, conn, idx):
        analysis_by_id: dict[str, object] = {}
        channels: Counter[str] = Counter()
        misses: list[tuple[str, bytes]] = []
        miss_sig: dict[str, str] = {}
        skipped = 0
        for p in photos:
            path, ch = measure.resolve_render_path(
                thumbnail_path=self._thumbs.get(p.photo_id),
                preview_index=idx,
                id_global=p.photo_id,
            )
            channels[ch.value] += 1
            if path is None:
                skipped += 1
                continue
            psig = cachemod.raw_signature(path)
            cached = (
                None if self._force_fresh_preview
                else (cachemod.get_preview_jpeg(conn, p.photo_id, psig) if conn else None)
            )
            if cached is not None:
                analysis_by_id[p.photo_id] = cached
                continue
            try:
                stream = gpu_jpeg.extract_jpeg_stream(path.read_bytes())
            except OSError:
                stream = None
            if stream is None:
                skipped += 1
                continue
            misses.append((p.photo_id, stream))
            miss_sig[p.photo_id] = psig

        if misses:
            self.progress.emit(f"Rendered previews (GPU) — {len(misses)} photo(s)…")
            decoded = gpu_schedule.analyze_render_blobs(
                misses,
                progress=self._batch_progress("Preview"),
            )
            for pid, dual in decoded.items():
                if dual is None:
                    continue
                analysis_by_id[pid] = dual.sharp  # current state = sharp zone
                if conn is not None:
                    _safe(lambda pid=pid, dual=dual: cachemod.put_preview_jpeg(
                        conn, pid, miss_sig[pid],
                        sharp=dual.sharp, glob=dual.glob,
                        mask_sharp_frac=dual.mask_sharp_frac, commit=False))
            if conn is not None:
                _safe(conn.commit)  # P-07: one commit per step
        return analysis_by_id, channels, skipped

    # ------------------------------------------------------------------ #
    # "No render" failure message — precise diagnostic
    # ------------------------------------------------------------------ #
    def _no_render_message(self, n_photos: int, channels: Counter, idx) -> str:
        cause = (
            "no catalog_path received from the plugin → cannot locate Previews.lrdata"
            if idx is None
            else "no preview found in Previews.lrdata for the selection"
        )
        return (
            f"No measurable render out of {n_photos} photo(s) (channels: {dict(channels)}). "
            f"Likely cause: {cause}. Thumbnails provided by the plugin: "
            f"{len(self._thumbs)} (channel not wired on the GUI side). Fix: generate "
            f"standard/1:1 previews in Lightroom (Library > Previews > Build "
            f"Previews), then retry."
        )
