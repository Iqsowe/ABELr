# Architecture — ABELr

How the system works. For the agent's **working rules** → [`../CLAUDE.md`](../CLAUDE.md).
For the **roadmap / status** → [`../PLAN.md`](../PLAN.md). For the **Lua SDK API** →
[`lr15_sdk_api_reference.md`](lr15_sdk_api_reference.md).

> State verified by code review (2026-07-25). The module "status" column reflects
> what the code actually does, not the intent.

---

## 1. Overview

Lightroom Classic plugin (Lua + Lr SDK) + external Python application. The plugin is a
**bridge** to Lightroom; the App does all the computation (RAW decoding, analysis, adjustment
planning) and drives the plugin via a job queue.

Core function: **white balance / exposure / HSL / camera calibration batch per photo**,
calibrated on **seeds** (reference photos manually retouched, explicitly marked) via
k-NN matching on the sharp-area RAW analysis. Calibration (ShadowTint, Hue/Saturation
R/G/B) has no measurable target from a render — it is **always transplanted** from the
nearest seeds (like Temperature/Tint), in both reference modes.

```
User ──GUI──▶ Python App (FastAPI server + GUI, same process)
                         │  creates a job in job_queue
                         ▼
                    [ job_queue ]  ◀── HTTP polling 300 ms ── Lua Plugin (client)
                         │                                         │ executes via Lr SDK
                         │  ◀────────── POST /jobs/{id}/result ─────┘
                         ▼
                    App decodes RAW (GPU), measures, plans ──▶ apply_adjustments job ──▶ plugin applies
```

---

## 2. Plugin ↔ App communication

**Invariant: the plugin is ALWAYS the HTTP client, the App ALWAYS the server** (`127.0.0.1:5000`).
The plugin cannot easily expose a server → it *polls*. The App never pushes: it
deposits a job, the plugin picks it up on the next poll.

GUI and FastAPI server live in the **same** Python process: server in a daemon thread,
GUI on the main Qt thread. They share the in-memory `job_queue` instance (`Lock` +
`threading.Event`), no HTTP between them.

### FastAPI endpoints (`app/server/api.py`)

| Endpoint | Method | Role |
|---|---|---|
| `/health` | GET | Healthcheck (plugin checks at startup) |
| `/status` | GET | App state (pending jobs, bridge connected) |
| `/bridge` | GET | Bridge heartbeat (seconds since last poll) |
| `/jobs/pending` | GET | Plugin retrieves the next job (204 if empty) — marks the heartbeat |
| `/jobs/{id}/result` | POST | Plugin submits the result |
| `/shutdown` | POST | Clean process shutdown (used by "Relaunch") |

### Job queue (`app/server/job_queue.py`)

Singleton `job_queue`, thread-safe FIFO. Cycle: `submit()` → `wait_result(timeout)` on the GUI
side; `GET /jobs/pending` then `POST result` on the plugin side → `done_event.set()` unblocks
the GUI. Orphan eviction (TTL), saturation guard (max pending), plugin heartbeat
(`mark_poll()` / `bridge_connected(threshold=5 s)`).

### Job types (App → plugin)

Source of truth: `JobType` enum in [`app/server/models.py`](../ABELr.lrplugin/app/server/models.py)
and `dispatch()` in [`PollingLoop.lua`](../ABELr.lrplugin/PollingLoop.lua) — one enum member ⇔
one `elseif` branch, kept in sync on any addition. Base jobs (`test`, `get_selected_photos`,
`get_catalog_photos`, `get_thumbnails`, `render_probe`, `apply_adjustments`) plus Phase 2 jobs
(metadata, collections, develop presets — see `models.py` for the full list).

### Plugin side (`ABELr.lrplugin/`)

| File | Role |
|---|---|
| `Info.lua` | Manifest (`LrToolkitIdentifier = com.abelr.plugin`, SDK 12, menus) |
| `MenuConnect` / `MenuRelaunch` / `ShowMessage` | Menu entries |
| `PluginInfoProvider.lua` | Plug-in Manager section (connect/relaunch/status/test buttons) |
| `Actions.lua` | connect / relaunch / checkStatus |
| `AppLauncher.lua` | Start/stop/relaunch of the Python process via `launch.ps1` |
| `PollingLoop.lua` | 300 ms loop, job dispatch, heartbeat `_G.ABELR_BRIDGE_HEARTBEAT` (5 s timeout) |
| `HttpClient.lua` | GET/POST JSON wrappers (LrHttp) |
| `PhotoData.lua` | Extracts path/EXIF/develop settings/catalog_path (`DEVELOP_KEYS` list) |
| `Adjustments.lua` | `applyDevelopSettings` batch inside `withWriteAccessDo` |
| `Thumbnails.lua` | `requestJpegThumbnail` (`fetch`) + apply/render/restore cycle (`fetchProbe` → `render_probe`) |
| `Json.lua` | Embedded JSON encoder/decoder |
| `Utils.lua` | Logging, project paths |
| `Collections.lua` / `Metadata.lua` / `Presets.lua` | Phase 2: collections, ratings/flags/keywords, develop presets — required by `PollingLoop.lua` |
| `PhotoLookup.lua` | Photo resolution helper, required transitively by the three modules above |

---

## 3. Python module map — actual status

Root `app/`: `main.py` (FastAPI daemon thread + Qt GUI), `requirements.txt`, `README.md`.
Venv expected at `app/.venv` (cf. `launch.ps1` / `bootstrap.ps1`).

### `core/` — image & compute pipeline

**Live (actual execution path):**

| Module | Role | Main entry points |
|---|---|---|
| `gpu.py` | Device selection (cuda-or-cpu, never raises), VRAM budget, stream pool | `device()`, `require_cuda()` (opt-in, CUDA-only call sites) |
| `gpu_raw.py` | Bayer → demosaic + WB + matrix → ProPhoto + stats (GPU) | `analyze_raw_gpu()` |
| `gpu_jpeg.py` | GPU JPEG decoding (nvJPEG) + stream extraction | `decode_blobs()`, `extract_jpeg_stream()` |
| `gpu_schedule.py` | VRAM-aware scheduler: unified unpack (1 rawpy open), CPU/GPU double-buffer, per-pipeline waves (Fable 5 review G7) | `process_combined_batch()` (+ wrappers `process_raw_batch()`/`process_embedded_batch()`), `analyze_render_blobs()` |
| `analysis.py` | Exposure metrics (Y) + gray-world in linear space | `ExposureStats`, `ev100()` |
| `render_metrics.py` | Tone L* / neutral a*b* / HSL bands in CIELAB (numpy, source of truth) | `tone_stats()`, `neutral_stats()`, `band_stats()` |
| `render_metrics_gpu.py` | torch CUDA port of `render_metrics` (constants imported from the numpy version) | `analyze_rendered_gpu_dual()` |
| `sharpness.py` | "Sharp area" mask (Laplacian, top 25%) — restricts histograms to the subject | CPU+GPU |
| `seed_match.py` | k-NN on seeds → Temp/Tint/tone/bands/calibration target (1/distance weighting) | `build_seed_pool()`, `target_from_seeds()` |
| `autocorrect.py` | Orchestrates exposure+WB+HSL+calibration per photo (seeds/embedded modes) | `plan()` → `PhotoAdjustment[]` |
| `exposure.py` | ΔEV from current L* → target L* | (via `autocorrect`) |
| `hsl.py` | Per-band HSL deltas vs target (saturation = reduction only) | (via `autocorrect`) |
| `response.py` | Calibrated ∂render/∂slider model (disk cache) | `load()` |
| `wb_model.py` | Post-k-NN Temp/Tint refinement (**live**) | `refine_temp_tint()` (called from `autocorrect.py:554`) |
| `cache.py` | SQLite cache (5 tables) | see §5 |
| `quantize.py` | Downsampling to the common measurement grid (`render_metrics.MEASURE_LONG_EDGE`) | used by `sharp_mask`/`tone_stats`/`band_stats` paths |
| `embedded_jpeg.py` | In-camera JPEG (embedded target) + as-shot WB; imports `raw` | `RawReference` |
| `raw.py` | ARW decoding via rawpy (**live** via `embedded_jpeg`, + tools) | `load_linear()`, `load_rgb()` |
| `color.py` | Color spaces: linear ProPhoto, Y, → sRGB | conversion constants |
| `catalog.py` | Locates `.lrcat` + `.lrdata` bundles, read-only SQLite | catalog resolution |
| `previews.py` | Resolves `id_global` → preview files | `PreviewIndex` |
| `measure.py` | Render channel selection (fresh thumbnail / passive preview) | render path resolution |
| `pipeline.py` | `RenderAnalysis` / `RenderAnalysisDual` dataclasses, helpers (band_map) | — |
| `exif_profile.py` | Sony creative-style profile via external `exiftool` binary (absence → None, non-blocking) | `read_capture_profiles()` |

**Tool-only (dead on the app side, kept for `tools/`):**
- `image_source.py` (`LoadedImage`) — imported only by `tools/{sharp_raw_predict,series_audit,analyze_ground_truth}.py`.
- `regime.py` — physical/artistic regime detection; imported by `tools/validate_wb_seeds.py`. The
  live k-NN path doesn't need it. (`regime.py` imports `wb_model`.)

**Removed / never existed:** `core/seeds.py`, `core/adjustments.py` (removed — `is_seed` now
lives in the DB via `cache`, matching via `seed_match`). `core/prediction.py` never existed.

### `gui/`

| Module | Status | Role |
|---|---|---|
| `main_window.py` | live | Seeds / analyze / apply-per-axis / calibrate-neutral buttons, bridge indicator; delegates layout to `main_window_layout.py` |
| `main_window_layout.py` | live | `MainWindowLayoutMixin._build_ui` — widget construction + layout, split out of `main_window.py` (PLAN.md U5) |
| `plan_format.py` | live | Pure plan-line/summary string formatting, extracted out of `MainWindow` (PLAN.md U5) |
| `op.py` | live | `Op` enum for `MainWindow`'s current-operation state (PLAN.md U5) |
| `status.py` | live | `build_status`/`format_status_lines` — persistent status panel, pure (PLAN.md U2) |
| `log_bridge.py` | live | `QtLogHandler` — bridges the `abelr` logger to the GUI's Log tab (PLAN.md L2/U2) |
| `job_worker.py` | live | Generic QThread: submits a job, waits for the plugin result |
| `autocorrect_worker.py` | live | QThread: RAW+in-camera JPEG+preview (sharp area) → cache → `autocorrect.plan`; `analyze_only` mode |
| `neutral_preview_worker.py` | live | QThread: neutral anchors (`render_probe`) → `NeutralPreviewJPEG` cache |
| `fresh_render_worker.py` | live | Forces the plugin to render a fresh thumbnail before a measurement (bypasses stale-preview risk) |

> `analysis_worker.py` removed (PLAN step 1, Fable 5 review) — no non-reappearance guard test
> currently exists for it; `app/tests/test_smoke_import.py` catches broken imports in any
> `core/`/`gui/` module that does exist.

### `server/`
`api.py` (routes), `job_queue.py` (queue + heartbeat), `models.py` (Pydantic: `Job`, `JobResult`,
`PhotoResult`, `ThumbnailResult`, `ExifData`, `PhotoAdjustment`, enums `JobType` / `JobStatus`).

### `mcp/`
`server.py` (FastMCP instance + tool definitions, mounted on `/mcp` in `server/api.py`),
`tools.py` (`require_bridge()`, `run_job()` helpers shared by the tools). Re-exposes the job
types above as MCP tools for Claude Code — see root `CLAUDE.md` § Communication.

### `data/`
Static/reference data shipped with the app (see folder for current contents).

---

## 4. Image pipeline — source & analysis space

**Source = original RAW via rawpy** (decision validated by real calibration, cf.
`tools/calibrate_sp_vs_raw.py`). rawpy performs a complete, consistent development (WB, matrix,
demosaic) — the only source giving metrics comparable across photos.

| Decision | Value | Reason |
|---|---|---|
| Format | float32 scene-linear 0-1 | No gamma (WB/clipping stay accurate), no 8-bit clipping |
| Primaries | ProPhoto (wide gamut) | sRGB clips saturated colors → bias up to ×2 on gray-world ratios |
| Luminance | Y from XYZ (ProPhoto→XYZ row) | Exact, gamut-independent |
| WB | `use_camera_wb=True` | Without it the ratios measure the sensor, not the scene |
| GUI display | uint8 sRGB on demand | Never for analysis |

**Why not the Smart Preview**: its DNG is in `PhotometricInterpretation = 34892` (LinearRaw,
before WB and matrix); LibRaw/rawpy doesn't decode its JPEG XL tiles; calibration gives an
inconsistent Δexposure (σ ≈ 0.7-1.3 stop). SPs are also often missing. `previews.py` /
`catalog.py` remain useful for locating bundles and decoding the **rendered preview** (checking a
result), not for measuring.

### GPU-first, CPU fallback

Pixel decoding prefers **GPU** (torch CUDA + nvJPEG) over LibRaw `postprocess`, but the plugin
must also run on machines without an NVIDIA card.

| Step | Where |
|---|---|
| ARW decompression/unpack → 16-bit bayer plane | **Irreducibly CPU** (pool bounded to physical cores) — no GPU codec exists for Sony ARW |
| Black-level, CFA WB, demosaic, matrix → ProPhoto, stats | GPU if available, else CPU (`gpu_raw`) |
| JPEG decoding (rendered preview + in-camera JPEG) | GPU nvJPEG if available (`gpu_jpeg`) |
| tone / neutral / bands (CIELAB) | GPU if available (`render_metrics_gpu`, validated exact vs numpy ≤ 8 M px; beyond that, subsampled quantiles — negligible bias, cf. REVIEW_FABLE5 C-04) |

`gpu.device()` returns `cuda` if usable, otherwise `cpu` — **never raises**; every step above
routes through it, so the whole pipeline switches automatically. `require_cuda()` /
`GpuUnavailable` stay available for call sites that explicitly want to require CUDA (grep
`gpu.py` for current callers) — not the default path. VRAM managed by `gpu_schedule` (waves
sized to the budget; falls back to a conservative fixed RAM cap on CPU). Parity verified by
`tools/validate_gpu_vs_libraw` (exposure Y corr 1.000; gray-world corr 0.97-0.9995, small
constant bias absorbed by seed calibration).

---

## 5. SQLite cache (`core/cache.py`)

`ABELr_cache.db` in the active catalog's folder. Two independent version constants in
`cache.py` (values not repeated here, they move on every bump — read the file):
`SCHEMA_VERSION` (table structure — bump = DROP+recreate, no migration) and
`ANALYSIS_VERSION` (salted into the freshness hashes — bump = full re-measure whenever the
measurement algorithm changes, no row-by-row migration). Workers consult the cache first →
2nd pass = zero decoding. That's the real gain on 500-1000 series, on top of the GPU.

| Table | Hash key | Content |
|---|---|---|
| `LightroomPicture` | `hash_develop` | path, EXIF, `current_develop`, `is_seed` flag, DCP/capture profiles |
| `SourceRAW` | `hash_raw` (size:mtime:ANALYSIS_VERSION) | as-shot WB, global+sharp exposure, global+sharp gray-world, sharp-area tone/hsl, ev100 |
| `InCameraJPEG` | `hash_jpeg` (= RAW signature size:mtime+version — the JPEG lives inside the .ARW) | global+sharp tone/neutral/hsl, precomputed RAW↔JPEG deltas |
| `PreviewJPEG` | `hash_preview` (= source file signature + version) | measurements of the current render (state-dependent) |
| `NeutralPreviewJPEG` | `hash_style` | neutral anchor + `wb_asshot_temp/tint` |

`is_seed`: marked/unmarked in the DB (no pixel decode). k-NN `seed_match` reads the seed pool.

`hash_style` is keyed on `cache.py`'s `_STYLE_KEYS` — the develop settings that affect the
neutral render (tone, crop, calibration, color grading) minus the ones the probe already
neutralizes (Temp/Tint/Exposure/HSL). Applying an HSL correction must not invalidate the
anchor, applying a calibration correction must.

### Neutral-anchored embedded mode

`neutral_preview_worker` has the plugin render each photo (job `render_probe`: WB As Shot +
`Exposure2012=0` + 24 HSL sliders at 0, DCP/tone/crop style intact), measures this neutral render
(GPU), and caches it under `hash_style` (stable if Temp/Tint/HSL move, changes if
tone/clarity/calibration change — the probe doesn't neutralize calibration). The
delta `in-camera JPEG − neutral render` gives **absolute** settings (idempotent), independent of
the current render. Stale-probe guard: `neutral_preview_worker._anchor_suspect` refuses to cache
a suspect anchor.

---

## 6. uuid → preview resolution (verification / inspection)

The `uuid` that names the preview files is **not** the one sent by the plugin
(`getRawMetadata('uuid')` = `id_global`). Two SQLite hops (`previews.PreviewIndex`):

```
id_global  ──(.lrcat: Adobe_images)──▶  id_local
id_local   ──(previews.db: ImageCacheEntry)──▶  uuid de cache + digest
```

The cache `uuid` names the files (`{uuid}-{digest}_{size}`) and the Smart Preview DNG
(`{uuid}.dng`), under `{uuid[0]}/{uuid[:4]}`. `.lrcat` and `previews.db` opened read-only
immutable (`mode=ro&immutable=1`): coexists with Lightroom open.

---

## 7. Seeds → correction model (Apply flow)

1. **Mark + analyze references**: `AutoCorrectWorker(analyze_only=True)` — RAW (GPU demosaic,
   sharp area) + in-camera JPEG + rendered preview → populates the cache, marks `is_seed`, applies
   nothing.
2. **Apply `<axis>`**: `AutoCorrectWorker(axes={axis}, force_fresh_preview=True)` — measures
   (preview always re-decoded, never served from cache during an Apply) → target = in-camera JPEG
   (embedded mode) or k-NN on the nearest seeds (`seed_match`, seeds mode) → `autocorrect.plan` →
   `apply_adjustments` job. The plugin applies it via `photo:applyDevelopSettings()`.
3. `wb_model.refine_temp_tint` refines Temp/Tint after the k-NN (seeds mode).

Targets are **absolute** (L*/Temperature aimed for, not an added delta) → convergence: a 2nd
Apply on an up-to-date preview gives a delta ≈ 0.

---

## 8. Known limitations

- **`requestJpegThumbnail` lock**: `Thumbnails.fetch` assumes the thumbnail reflects the current
  develop state, not a stale cache. `force_fresh_preview=True` bypasses the App's SQLite cache,
  but if **Lightroom** hasn't regenerated `Previews.lrdata` between two clicks, the measurement
  stays stale. Planned but unwired fallback: `RenderChannel.EXPORT` channel
  (`Thumbnails.fetchProbeExport` + `render_probe_export` job). To validate under real conditions
  (2nd-Preview convergence ≈ 0). Cf. PLAN.md step 8.

---

## Stack

| Layer | Technology |
|---|---|
| Lr plugin | Lua 5.1 + Adobe Lr Classic SDK 12+ |
| Server | Python 3.11+ + FastAPI + uvicorn |
| GUI | PySide6 (Qt6) |
| Image / RAW | rawpy + numpy + opencv |
| GPU | torch 2.6.0 + torchvision 0.21.0 (cu124) — nvJPEG |
| Previews | tifffile + imagecodecs (libjxl) + SQLite |
| Analysis | scipy + scikit-learn |
| Camera profile | external `exiftool` binary (outside pip) |

> RAW decoding (LibRaw/rawpy), JPEG XL decompression, and OpenCV are already C/C++: no
> Rust gain on these parts. Profile (`py-spy`, `cProfile`) before considering PyO3.
