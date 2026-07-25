# Complete Review — Fable 5

> Frozen audit artifact, 2026-07-18. Asserts the retired GPU-strict (no-CPU-fallback) contract
> as current policy — it has since been lifted (GPU-first, CPU fallback; see `CLAUDE.md`). Read
> as history, not current policy.

> **Goal**: 4-axis audit — bugs · architecture/dead code · perf · doc/roadmap.
> **Model**: `claude-fable-5` · **Method**: 4 sequential passes (see Journal).
> **Golden rule**: no finding without `file:line`. Develop parameter names verified
> against [`lr15_sdk_api_reference.md`](lr15_sdk_api_reference.md). SDK ⚠️ methods = PLAUSIBLE at best.

## Legend

**Severity**: 🔴 BLOCKING (breakage / corruption / wrong result) · 🟠 MAJOR (real bug, non-nominal path) · 🟡 MINOR (robustness, edge case) · ⚪ NIT (style, readability)
**Status**: `CONFIRMED` (reproduced / proven by the code read) · `PLAUSIBLE` (solid reasoning, unproven)
**Fix effort**: S (< 30 min) · M (< 1/2 day) · L (≥ 1/2 day)

---

## Pass 0 — Ground truth (architecture / doc)

> Confront the actual code against ARCHITECTURE.md §3 (module map) + PLAN.md. Establishes the
> live/dead scope BEFORE any bug hunting. A dead module isn't fixed, it's documented.

### 0.1 Module map corrections (§3 ARCHITECTURE.md)

**Verdict: ZERO status corrections.** The §3 map is accurate for the 25 `core/` modules
(23 live + 2 tool-only), the 7 `gui/`, the 3 `server/`, and the 14 Lua files. Full inventory
below (exhaustive audit, 2026-07-17). Chain nuances at the end of the section.

**`core/` — direct live** (imported by `gui/`/`server/`/`main.py`):

| Module | Doc status | Actual status | Evidence (inbound reference) |
|---|---|---|---|
| `analysis.py` | live | live | `gui/autocorrect_worker.py:31` |
| `autocorrect.py` | live | live | `gui/autocorrect_worker.py:32,34` |
| `cache.py` | live | live | `gui/main_window.py:44`, `gui/autocorrect_worker.py:32`, `gui/neutral_preview_worker.py:37` |
| `exif_profile.py` | live | live | import `gui/autocorrect_worker.py:32`, call `:143` |
| `gpu.py` | live | live | `gui/autocorrect_worker.py:32`, `gui/neutral_preview_worker.py:37` |
| `gpu_jpeg.py` | live | live | `gui/autocorrect_worker.py:32`, `gui/neutral_preview_worker.py:37` |
| `gpu_schedule.py` | live | live | `gui/autocorrect_worker.py:32` |
| `measure.py` | live | live | `gui/autocorrect_worker.py:32` |
| `previews.py` | live | live | `gui/autocorrect_worker.py:35` |
| `render_metrics.py` | live | live | `gui/neutral_preview_worker.py:37` |
| `render_metrics_gpu.py` | live | live | `gui/neutral_preview_worker.py:37` |
| `response.py` | live | live | import `gui/autocorrect_worker.py:33`, call `load()` `:290` |
| `seed_match.py` | live | live | `gui/autocorrect_worker.py:33` |

**`core/` — live via chain** (direct importer = another live core module):

| Module | Doc status | Actual status | Evidence (inbound chain) |
|---|---|---|---|
| `catalog.py` | live | live (chain) | `core/previews.py:35` ← previews live; + 8 tools |
| `color.py` | live | live (chain) | `core/gpu_raw.py:27`, `core/raw.py:23`, `core/analysis.py:23` |
| `embedded_jpeg.py` | live | live (chain) | `core/gpu_schedule.py:25-26` |
| `exposure.py` | live | live (chain) | `core/autocorrect.py:37` |
| `gpu_raw.py` | live | live (chain) | `core/gpu_schedule.py:25,27`, calls `:73,:75` |
| `hsl.py` | live | live (chain) | `core/autocorrect.py:36` |
| `pipeline.py` | live | live (chain) | `core/cache.py:44`, `core/autocorrect.py:41`, `core/embedded_jpeg.py:21`, `core/gpu_jpeg.py:22`, `core/gpu_schedule.py:28`, `core/render_metrics_gpu.py:19` |
| `raw.py` | live | live (chain) | `core/embedded_jpeg.py:20`; + tools (matches doc "live via `embedded_jpeg`, + tools") |
| `sharpness.py` | live | live (chain) | `core/gpu_raw.py:27`, `core/pipeline.py:20`, `core/render_metrics_gpu.py:212,230` |
| `wb_model.py` | live | live (chain) | `core/autocorrect.py:39`, call `refine_temp_tint` `core/autocorrect.py:554` (= line stated by the doc) |

**`core/` — tool-only** (no importer outside `app/tools/`):

| Module | Doc status | Actual status | Evidence |
|---|---|---|---|
| `image_source.py` | tool-only | tool-only | `tools/analyze_ground_truth.py:43`, `tools/series_audit.py:37`, `tools/sharp_raw_predict.py:33` — exactly the 3 tools cited by the doc |
| `regime.py` | tool-only | tool-only | `tools/validate_wb_seeds.py:21` only importer; does import `wb_model` (`core/regime.py:24`) as stated |

**`gui/`:**

| Module | Doc status | Actual status | Evidence |
|---|---|---|---|
| `main_window.py` | live | live | imported `app/main.py:62`, instantiated `:65` |
| `job_worker.py` | live | live | instantiated `gui/main_window.py:242,268,320,379,585` |
| `autocorrect_worker.py` | live | live | instantiated `gui/main_window.py:411,426` |
| `neutral_preview_worker.py` | live | live | instantiated `gui/main_window.py:345` |
| `analysis_worker.py` | DEAD | DEAD confirmed | class defined `gui/analysis_worker.py:39`, zero instantiation/import anywhere in `app/` (grep `AnalysisWorker`) |
| `photo_panel.py` | STUB | STUB confirmed | 13 lines, class `:11`, empty `__init__`, never imported |
| `analysis_panel.py` | STUB | STUB confirmed | 13 lines, class `:11`, empty `__init__`, never imported |

**`server/`:**

| Module | Doc status | Actual status | Evidence |
|---|---|---|---|
| `api.py` | live | live | `app/main.py:35` (uvicorn daemon thread `:56-57`) |
| `job_queue.py` | live | live | singleton `job_queue.py:168` ← `api.py:14`, `main_window.py:45`, `job_worker.py:13`, `neutral_preview_worker.py:38` |
| `models.py` | live | live | `api.py:15`, `job_queue.py:17`, `main_window.py:46`, `job_worker.py:14`, `neutral_preview_worker.py:39`, `autocorrect_worker.py:36` |

**Lua (14 files) — all alive**, no orphans:

| File | Inbound evidence |
|---|---|
| `Info.lua` | manifest, loaded by Lr (root of the graph) |
| `MenuConnect.lua` / `MenuRelaunch.lua` / `ShowMessage.lua` | `Info.lua:22,26,30` (+ Export menus `:38-46`, Help `:54-62`) |
| `PluginInfoProvider.lua` | `Info.lua:16` |
| `Actions.lua` | `MenuConnect.lua:19`, `MenuRelaunch.lua:14`, `PluginInfoProvider.lua:13` |
| `AppLauncher.lua` | `Actions.lua:11` |
| `PollingLoop.lua` | `Actions.lua:12` |
| `HttpClient.lua` | `Actions.lua:13`, `AppLauncher.lua:14`, `PollingLoop.lua:19` |
| `PhotoData.lua` | `PollingLoop.lua:20` |
| `Adjustments.lua` | `PollingLoop.lua:21` |
| `Thumbnails.lua` | `PollingLoop.lua:22` |
| `Json.lua` | `HttpClient.lua:9`, `PhotoData.lua:9`, `PollingLoop.lua:23` |
| `Utils.lua` | 7 inbound (`Adjustments.lua:14`, `AppLauncher.lua:15`, `HttpClient.lua:10`, `PluginInfoProvider.lua:14`, `PollingLoop.lua:24`, `ShowMessage.lua:6`, `Thumbnails.lua:15`) |

**Nuance to keep for PLAN.md step 1**: the only *direct* GUI importer of `gpu_raw`
and `raw` is the dead module `analysis_worker.py:19` — their live status holds only
via the `gpu_schedule`/`embedded_jpeg` chain. Removing `analysis_worker` (step 1) therefore
kills no core module, but will make `gpu_schedule` the sole importer of `gpu_raw`.

### 0.2 Doc ↔ code divergences (outside the module map)

| ID | Doc (file:section) | Claims | Actual code (file:line) | Gap |
|---|---|---|---|---|
| D-01 | ARCHITECTURE.md:80 (§2, plugin table) | `PhotoData.lua`: "**42 `DEVELOP_KEYS`**" | `PhotoData.lua:21`: table of **44** entries | Wrong count (44, not 42) |
| D-02 | ARCHITECTURE.md:143-144 (§3 `server/`) | `models.py` = "`Job`, `JobResult`, `PhotoResult`, `ExifData`, `PhotoAdjustment`, enum `JobType`" | also add `JobStatus` (`models.py:26`, used `job_queue.py:17`) and `ThumbnailResult` (`models.py:65`, used `neutral_preview_worker.py:39`) | Incomplete list (2 used types omitted) |
| D-03 | ARCHITECTURE.md:66-67 (§2 jobs) | `PollingLoop.lua` dispatch "≈ lines 43-149" | actual dispatch lines 47-121 (`PollingLoop.lua:47,55,61,67,96,121`) | Stale range (minor, "≈" acknowledged) |
| D-04 | docstring `core/analysis.py:12` | refers to "`core.wb_model` / `core.seeds`" | `core/seeds.py` removed (confirmed ARCHITECTURE.md:128-129; file absent from disk) | Doc embedded in the code points to a removed module |
| D-05 | docstring `core/seed_match.py:1` | "replaces `wb_model.py`/`regime.py` on the live app side" | `wb_model` is still live: `core/autocorrect.py:39`, call `refine_temp_tint` `:554` | Half-true: `regime` yes (tool-only), `wb_model` no (still on the live path) |

**Verified claims — CONFORMING** (no gap found):
- FastAPI endpoints: the 6 stated (ARCHITECTURE.md:47-54, CLAUDE.md) = exactly match the code — `api.py:22` `/health`, `:28` `/status`, `:39` `/bridge`, `:52` `/jobs/pending`, `:63` `/shutdown`, `:74` `/jobs/{job_id}/result`. Neither extra nor missing.
- Job types: 6/6 identical across doc↔`models.py:18-23`↔dispatch `PollingLoop.lua:47-121`.
- Cache: 5 tables (`cache.py:132,150,175,193,206` = `LightroomPicture`, `SourceRAW`, `InCameraJPEG`, `PreviewJPEG`, `NeutralPreviewJPEG`), `SCHEMA_VERSION=4` (`cache.py:51`), `ANALYSIS_VERSION="v4-neutral-anchor"` (`cache.py:56`), file `ABELr_cache.db` (`cache.py:47`).
- Queue: `submit` `job_queue.py:72`, `wait_result` `:92`, `mark_poll` `:112`, `bridge_connected(threshold=5.0)` `:124`, orphan TTL 900 s `:38`, saturation guard 100 `:41` — matches ARCHITECTURE.md:56-61 and §2 (5 s threshold).
- GPU-strict: `GpuUnavailable` `gpu.py:25`, `require_cuda()` `gpu.py:50`; torch 2.6.0 / torchvision 0.21.0 pinned `requirements.txt:21-22`.
- Poll 300 ms (`PollingLoop.lua:28` `POLL_INTERVAL = 0.3`) + heartbeat `_G.ABELR_BRIDGE_HEARTBEAT` (`PollingLoop.lua:38-39`) — matches.
- `Thumbnails.lua`: `fetch` `:46`, `fetchProbe` `:127`; `fetchProbeExport` **does not exist** — matches ARCHITECTURE.md §8 / PLAN.md step 8, which describe it as "planned but unwired".
- HTTP paths on the plugin side: `/health` (`HttpClient.lua:53`, `AppLauncher.lua:63`), `/shutdown` (`AppLauncher.lua:65`), `/jobs/pending` (`PollingLoop.lua:152`), `/jobs/{id}/result` (`PollingLoop.lua:188`). `/status` and `/bridge` are not called by the plugin (App-side inspection endpoints) — consistent with their documented role.
- `main.py`: FastAPI in a daemon thread (`main.py:56-57`, uvicorn `127.0.0.1:5000` `:39`), GUI on the main Qt thread (`:64-67`) — matches §2.
- PLAN.md step 1 ("`AnalysisWorker` never instantiated or imported — verified"): re-confirmed.
- "Removed / non-existent" modules (ARCHITECTURE.md:128-129): `core/seeds.py`, `core/adjustments.py`, `core/prediction.py` absent from disk — confirmed (glob `app/core/*.py`).

---

## Pass 1 — Bugs by subsystem

### (a) Lua plugin — `ABELr.lrplugin/` (14 files)

Verified compliance (no gap): develop writes all inside `withWriteAccessDo`
(`Adjustments.lua:54`, `Thumbnails.lua:154,178`); `LrHttp.post` only under
`postAsyncTaskWithContext` (`PollingLoop.lua:210`, `Actions.lua:18`); paths via
`LrPathUtils` everywhere; correct `import`/`require`; `Json.array` applied to all
outgoing tables; PV2012 respected (`Exposure2012`…, `WhiteBalance='Custom'` written with
each Temperature/Tint on the App side — `autocorrect.py:453,556`).

| ID | file:line | Sev | Status | Problem | Fix | Effort |
|---|---|---|---|---|---|---|
| L-01 | `Thumbnails.lua:63` | 🟠 | PLAUSIBLE | Return value of `requestJpegThumbnail` discarded. Known SDK gotcha (not covered by the local reference): the request object being garbage-collected can make the callback **never** fire → intermittent "no JPEG returned" timeouts | Keep the returns in a local table alive until the end of the wait loop | S |
| L-02 | `Thumbnails.lua:59,85-97` | 🟠 | PLAUSIBLE | Fixed output file `{photo_id}.jpg` + late callbacks: after a timeout, job N's callback can still write and **overwrite** job N+1's fresh file (or mutate `results` after return) → the App measures stale pixels (probe ≠ measured state) | Unique filename per call (counter/nonce) + generation token checked in the callback | S |
| L-03 | `Thumbnails.lua:178-185` | 🟠 | CONFIRMED | Probe restore: the result of `LrTasks.pcall` is ignored. If the restore fails, the photo stays in neutral state (WB As Shot / Exp 0 / HSL 0) **with no signal at all** in the job result | Collect restore errors, surface them in the result (`error` per photo_id), log | S |
| L-04 | `PollingLoop.lua:125-131` | 🟡 | CONFIRMED | Partial apply (applied>0 with errors) → `status='ok'`, error text lost (only applied/matched/total pass through); the GUI shows "Applied: n/m" with no cause for the failures | Attach a `report.errors` summary to the result even when status='ok' | S |
| L-05 | `PollingLoop.lua:219-225` | 🟡 | CONFIRMED | Heartbeat written once per loop, **before** dispatch: a long job (thumbnails/probe/apply, 1-3 min) makes `bridgeAlive()` false and `/bridge` disconnected during the work → GUI "Bridge inactive" + `_require_bridge` blocks, even though the bridge is working | Refresh `_G.ABELR_BRIDGE_HEARTBEAT` inside `Thumbnails.fetch`'s wait loop and the apply loop | S |
| L-06 | `HttpClient.lua:30` + `PollingLoop.lua:156-158` | 🟡 | CONFIRMED | Non-JSON 200 body → `Json.decode` nil → `pollOnce` treats it as "no job": the job stays IN_PROGRESS on the App side until the 900 s TTL, **no log** | Log rawBody when status=200 and decode is nil; distinguish 204 from "decode failed" | S |
| L-07 | `PollingLoop.lua:188-189` | 🟡 | CONFIRMED | Result POST not retried (status nil = network loss): the job **was executed** (apply included) but the App worker times out — invisible on the Lr side | 1-2 retries with backoff on `postJsonRaw`, log on final failure | S |
| L-08 | `Json.lua:142-159` | ⚪ | CONFIRMED | `\u` decoding without surrogate pairs (astral → 2×3 wrong bytes). Starlette sends raw UTF-8 (ensure_ascii=False) → path almost never taken | Handle D800-DBFF (pair → code point → 4-byte UTF-8) | S |
| L-09 | `Adjustments.lua:30-34` | ⚪ | CONFIRMED | Apply matches only the current selection (documented v1) whereas `Thumbnails.fetchProbe:143-146` has a `findPhotoByUuid` fallback: if the selection changes during measurement, photos are silently skipped | Same `catalog:findPhotoByUuid` fallback as fetchProbe | S |

### (b) HTTP bridge + server — `app/server/` + `HttpClient.lua` / `PollingLoop.lua`

Verified compliance: polling lifecycle via **generation** (`PollingLoop.lua:206-238`,
no shared boolean flag — matches the memory principle); job_id/type/payload
contract aligned on both sides (`models.py:35-41` ↔ `PollingLoop.lua:44-45`);
`submit_result` publishes state+event **under the lock** (`job_queue.py:143-152`);
`next_pending` cleanly skips evicted entries (`job_queue.py:129-139`).

| ID | file:line | Sev | Status | Problem | Fix | Effort |
|---|---|---|---|---|---|---|
| B-01 | `gui/main_window.py:565-570` | 🟠 | CONFIRMED | `_pending_ids` (populated `:546`) is **never compared**: "Apply" replays the Preview plan even if the Lr selection changed meanwhile → partial/inconsistent apply (the plugin applies only the intersection, without warning about the gap) | Re-fetch the selection and compare to `_pending_ids` before `_submit_apply`; otherwise re-plan | S |
| B-02 | `server/api.py:56-60` | 🟡 | CONFIRMED | `next_pending()` pops the job (IN_PROGRESS) **before** `model_dump(mode="json")`: a non-serializable payload value → 500 AND job never delivered (lost until the TTL). Current payloads are sound (pure floats, verified `exposure.py:80-86`, `autocorrect.py:453-476`), but the pattern is fragile | Serialize inside a try/except; on failure, mark FAILED + release the event instead of losing the job | S |
| B-03 | `gui/neutral_preview_worker.py:104-118` | 🟡 | CONFIRMED | `_anchor_suspect` returns False on **any** exception (including the `:112-114` cache read) → a genuinely suspect anchor then gets cached, which contradicts "a suspect anchor is NEVER cached" (the anchor poisons embedded mode until the style changes) | Don't swallow the exception: log and treat as suspect (or propagate) | S |
| B-04 | `gui/autocorrect_worker.py:166-314` | 🟡 | CONFIRMED | `conn` SQLite closed only on success paths (`:182`, `:270`, `:282`); any exception (including `ensure_neutral_previews` RuntimeError) exits via `except :313` **without closing** → handle leak on failure | Wrapping `try/finally` for `conn` (like `neutral_preview_worker.py:262-267`) | S |
| B-05 | `gui/main_window.py:585` + `Adjustments.lua:54-76` | 🟡 | PLAUSIBLE | Apply = ONE `withWriteAccessDo` transaction for the whole selection, 180 s GUI timeout: at 500+ photos exceeding it is plausible → GUI "Timeout" while the plugin is still applying (re-click = double apply) | Chunk `apply_adjustments` (same batches as render_probe) or timeout ∝ n | M |
| B-06 | `server/api.py:58-59` | ⚪ | PLAUSIBLE | 204 returned with body `{}` — RFC: 204 with no body; works with uvicorn[standard]/httptools (proven in prod), would break on h11 fallback | `return Response(status_code=204)` | S |

### (c) Core image / GPU — `raw`, `gpu*`, `pipeline`, `image_source`, `color`, `render_metrics*`

`image_source.py` excluded (tool-only, Pass 0). Verified compliance: `gpu.require_cuda()`
present at every entry point (`gpu_schedule.py:64,90,135`, `gpu_jpeg.py:52`,
workers `:157/:237`) — GPU-strict respected, no CPU fallback; CPU↔GPU color
matrices shared (`render_metrics_gpu.py:31-43` imports them from `render_metrics`,
`gpu_raw.py:39-42` from `color`); luminance = identical ProPhoto→XYZ(D50) Y row
(`color.py:34` ↔ `gpu_raw.py:182`); Lab/HSV/sharp-mask formula parity OK line by line;
GPU memory freed per wave (`gpu_schedule.py:79,121,148` `empty_cache`); Windows RAW
paths passed to rawpy as str with no manual concatenation.

| ID | file:line | Sev | Status | Problem | Fix | Effort |
|---|---|---|---|---|---|---|
| C-01 | `core/gpu_raw.py:159-165` | 🟠 | PLAUSIBLE | Per-CFA-site WB with no `wb[3]==0` guard: dcraw/LibRaw treat `cam_mul[G2]=0` as `=G1`; here the G2 sites would be multiplied by 0 → skewed green channel (demosaic averages zeros in). Sony ARW returns G2=G1 (hence the validated parity), but any body with cam_mul[3]=0 breaks | `if wb_arr[3]==0: wb_arr[3]=wb_arr[1]` — **would touch measurements ⇒ bump `ANALYSIS_VERSION`** (no-op on Sony, safe) | S |
| C-02 | `core/gpu.py:29-42` | 🟡 | CONFIRMED | `_diagnose` memoized with `lru_cache`: a **transient** CUDA init failure (OOM at launch, driver busy) gets memoized → `require_cuda` fails until the process restarts even after the GPU recovers | Memoize only success (or invalidate on failure) | S |
| C-03 | `core/raw.py:96` | 🟡 | CONFIRMED | `cv2.imdecode(...)[:, :, ::-1]` with no None test: corrupted embedded JPEG → TypeError. The live chain is protected by `embedded_jpeg.load_embedded_rgb:31-34`'s try, but any direct call (tools) crashes | Test for None before slicing | S |
| C-04 | `core/render_metrics_gpu.py:66-72` + `core/sharpness.py:63-67` | ⚪ | CONFIRMED | GPU quantiles subsampled beyond 8M px (`torch.quantile` limit): "exact vs numpy" parity not guaranteed on full-resolution 24MP RAW — negligible bias, documented in a comment, but ARCHITECTURE §4's claim is slightly too strong | Nothing (or refine the doc) | – |
| C-05 | `core/gpu_jpeg.py:34-37` | ⚪ | CONFIRMED | `extract_jpeg_stream` takes the **first** SOI in the buffer: on a multi-stream container this would give the small level. No effect in practice: `previews.find_rendered_preview:60-70` already picks the max-level file, the `.lrfprev` (small level) is only a documented fallback | Nothing (comment) | – |

### (d) SQLite cache — `cache.py` + hashing in `measure.py` / `exif_profile.py`

> Prefix `DB-` (the `D-` prefix is already taken by the doc divergences from Pass 0).

Verified compliance: 5 tables present and aligned read/write
(`cache.py:132-219`, get/put keys consistent per table); `ANALYSIS_VERSION` salted into
`raw_signature:240`, `blob_hash:247`, `style_hash:265`; `_ensure_schema` DROP+recreate
on `user_version` ≠ 4 (`cache.py:118-126`); `put_picture` UPSERT preserves `is_seed`
(`cache.py:331-340`); commit after every write; one connection per worker
(WAL, `check_same_thread=False`).

| ID | file:line | Sev | Status | Problem | Fix | Effort |
|---|---|---|---|---|---|---|
| DB-01 | `core/cache.py:68-78` ↔ `PhotoData.lua:21-40` | 🟠 | CONFIRMED | `_STYLE_KEYS` includes 14 `ColorGrade*` keys that `DEVELOP_KEYS` (Lua) **never extracts** → never in `current_develop` → `hash_style` is blind to Color Grading. Same for `Texture`, ToneCurve, Parametric* absent on BOTH sides. Consequence: changing Color Grading / curve / Texture does NOT recompute the neutral anchor → a stale anchor is served from cache → wrong embedded corrections, silently (contradicts ARCHITECTURE §5 "changes if tone/clarity change") | Add these keys to `DEVELOP_KEYS` (Lua) and complete `_STYLE_KEYS`; **bump `ANALYSIS_VERSION`** (otherwise stale anchors stay valid, since the keys are absent from old snapshots) | M |
| DB-02 | `gui/autocorrect_worker.py:384-386` ↔ `cache.py:14-15,245-247` | 🟡 | CONFIRMED | `hash_jpeg` (InCameraJPEG) and `hash_preview` (PreviewJPEG) = `raw_signature` (size:mtime), **not** "sha1 of the bytes" as claimed by cache.py's header and ARCHITECTURE §5. Consistent get/put so no freshness bug, but `blob_hash` is dead code and the doc describes a mechanism that doesn't exist | Fix the doc (or actually switch to `blob_hash`); remove/wire up `blob_hash` | S |
| DB-03 | `gui/main_window.py:445-459` | 🟡 | CONFIRMED | `_apply_seed_flag` runs on the **main Qt thread**: `put_picture`+`set_seed` per photo = 2 synchronous commits × n (300 photos ≈ 600 commits) → GUI freeze of several seconds (violates the "wait/IO off the Qt thread" rule applied everywhere else) | A single transaction (executemany + final commit) or move it into a worker | S |
| DB-04 | `core/cache.py:239-242` | ⚪ | CONFIRMED | `"0:0"` fallback (missing file) not salted with `ANALYSIS_VERSION` — never written to the DB (decoding fails before any put) so only a theoretical collision | Salt the fallback too | S |
| DB-05 | `core/cache.py:395-397,404-406` | ⚪ | CONFIRMED | `ORDER BY cached_at DESC LIMIT 1` on tables with `uuid` PRIMARY KEY (max 1 row per uuid) — misleading dead code (suggests a history that doesn't exist) | Simplify to a plain SELECT | S |
| DB-06 | `core/cache.py:687-727` | ⚪ | CONFIRMED | `get_bias_pool` has **no caller** (the worker always passes `bias_pools=None`, `autocorrect._build_bias_by_group:272` is likewise never called) — consistent with the "bias ignored" decision in `_plan_embedded:353`, but it's live-but-dead code | Address in an architecture pass (dead or wire it up) | – |

### (e) Analysis / seed-match — `analysis`, `measure`, `seed_match`, `wb_model`, `exposure`, `hsl`, `autocorrect`

`regime.py` excluded (tool-only, Pass 0). Verified compliance: HSL saturation =
**reduction only** (`hsl.py:102-121`, delta clamped ≤ 0); divisions protected
(`hsl.py:118,127,136` gains ~1e-9, `seed_match.py:158-161` weights, `wb_model` bounds,
`response.py:74-83` slope ≥ 1); exposure correctly in render-space L* with an absolute
target and Exposure2012=0 anchor in embedded mode (`autocorrect.py:392-419`); k-NN: target
excluded, per-feature z-score, 1/distance weighting (`seed_match.py:98-144`);
`refine_temp_tint` bounded and never a global gray-world (`wb_model.py:119-149`).

| ID | file:line | Sev | Status | Problem | Fix | Effort |
|---|---|---|---|---|---|---|
| A-01 | `core/exif_profile.py:83-88` | 🟠 | CONFIRMED | Batch passed **as argv**: at 500-1000 paths (~80 chars each) the Windows CreateProcess limit (32,767 chars) is exceeded from ~300 photos on → OSError → misleading "exiftool not found" warning and **the whole batch left without a profile** (degraded k-NN matching and bias groups). The docstring promises `-@ argfile`, never implemented | Write a temp argfile and call `exiftool -@ file` (+ `-charset filename=UTF8`) | S |
| A-02 | `core/exif_profile.py:83-86` | 🟡 | PLAUSIBLE | `text=True` with no `encoding`: exiftool stdout (UTF-8) decoded as cp1252 → accented paths (FR context) mojibake → `_match_path` fails → silent None profiles | `encoding="utf-8"` + exiftool charset options | S |
| A-03 | `core/analysis.py:57-76` | 🟡 | PLAUSIBLE | `parse_shutter_seconds`: FR Lr formats slow shutter speeds with a **comma** ("0,4 s") → `float()` ValueError → `ev100=None` for those photos (lost scene context, a diagnostic field) | Normalize `,` → `.` before parsing | S |
| A-04 | `core/autocorrect.py:554` | 🟡 | PLAUSIBLE | `refine_temp_tint(…, m.analysis.neutral, …)` with no guard: a cached `RenderAnalysis` can have `neutral=None` (`cache._analysis_from_row:295-302` allows it) → AttributeError → **the whole run** fails via the worker safety net | Guard `m.analysis.neutral is not None` (otherwise skip the refinement) | S |
| A-05 | `core/response.py:173-185` | 🟡 | CONFIRMED | `load()`: corrupted/truncated disk-cache JSON → unhandled exception → the entire analysis fails because of a cache file (data that's disposable by nature) | try/except → return the empty model (priors) + log | S |
| A-06 | `core/autocorrect.py:451` + `core/wb_model.py:144-147` | ⚪ | CONFIRMED | `Tint` never clamped to Lr bounds (±150) whereas Temperature is (2000-12000) — extreme deviations unlikely but unbounded | Clamp ±150 in both places | S |
| A-07 | `core/seed_match.py:135-144` | ⚪ | CONFIRMED | `k = min(3, max(1, pool//2))`: at 3 seeds k=1, at 4-5 k=2 — the advertised "k-NN up to 3" is only reached at ≥ 6 seeds; debatable but not wrong behavior | Comment the intent or `max(1, min(3, pool))` | S |

---

## Pass 2 — Performance (hot spots only)

> Targets: image/GPU pipeline, SQLite cache, 300 ms bridge polling. No micro-optim elsewhere.
> Each hotspot must state whether it touches the measurements → if so, note the `ANALYSIS_VERSION` impact.
> **No profiling was run** (no GPU available for the review): costs = reasoned
> estimates from the code, to confirm with `py-spy`/`torch.profiler` before any large-scale work.

Verified compliance (no hotspot found): cache lookups all on PRIMARY KEY `uuid`
(no missing index on the live path; the only unindexed predicate = `NeutralPreviewJPEG.hash_style`
in `get_bias_pool`, code with no caller — cf. DB-06); JSON blobs ~1-2 KB/row (negligible
serialization); structurally sound hit-rate: freshness keys aligned get/put per table, a 2nd-pass
Apply only re-decodes the preview (`force_fresh_preview`) — matches the §5 intent.

| ID | file:line | Current cost | Cause | Optimization | Estimated gain | Touches measurements? |
|---|---|---|---|---|---|---|
| P-01 🟠 | `gpu_schedule.py:71-79` | RAW phase (dominant cost of the 1st pass): wall time ≈ T_unpack_CPU + T_GPU instead of max(T_unpack, T_GPU). The docstring (`:9-12`) promises "the CPU unpacks the next wave while the GPU processes" — the code does **not** do this | `bayers = list(ex.map(...))` **blocks** until the whole wave is unpacked, then the GPU processes while the CPU pool sits idle; no prefetch of wave N+1 | Double-buffering: submit the unpack futures for wave N+1 before processing wave N on GPU (the `ThreadPoolExecutor` already exists) | Overlap of min(T_unpack, T_GPU) ≈ **20-40% of the RAW-phase wall time** (minutes on 500 photos); effort M | no |
| P-02 🟠 | `gpu_schedule.py:73` + `:96`, `embedded_jpeg.py:148-168` | Every missing photo opens the `.ARW` **twice** via `rawpy.imread`: step 1 (`unpack_raw` → bayer) then step 2 (`extract_reference` → WB + JPEG bytes). 2 container parses + 2 file reads (~25-50 MB, the 2nd served from the OS cache but the LibRaw parse still paid) | RAW and embedded pipelines designed separately; yet the missing-item lists are aligned (same `raw_signature` key) | Unified unpack: a single rawpy open returns `RawBayer` + as-shot WB + thumb bytes (all 3 are already read within the same `with`), feeding both steps | ~0.1-0.3 s/photo → **~1-2.5 min on 500 photos**; effort M (naturally combined with P-01) | no |
| P-03 🟠 | `gpu_schedule.py:34,43-49` + `:79,121,148` | JPEG waves sized using the **RAW** estimate (`_EST_BYTES_PER_IMG` ≈ 1.19 GB/img) → ~3-5 images/wave for 0.5-3 MP JPEGs (~40-80 MB actual); and `gpu.empty_cache()` **on every wave** = sync + flush of the torch allocator → cudaMalloc repaid on the next wave | A single sizing constant for two workloads that differ by ~15×; systematic `empty_cache` instead of reactive | Per-pipeline estimate (JPEG ≈ 60-80 MB/img → waves of 30-60, nvJPEG finally amortized — the stated goal of `decode_blobs`); `empty_cache` only on OOM or every N waves | ~125-170 sync+realloc cycles avoided over 500 photos + real nvJPEG batching: **×2-4 on the JPEG-decode phase** (estimate); effort S | no |
| P-04 🟡 | `render_metrics_gpu.py:165-177,224-245` | `analyze_rendered_gpu_dual` calls `band_stats`/`neutral_stats` 2× (global+sharp): hue/sat recomputed 2×/image, chroma 4×; above all `diff = hue.unsqueeze(-1) - _BAND_CENTERS` allocates **H×W×8 float32** (≈ 770 MB transient on a 24 MP input on the `gpu_raw:214` side, + `circ` likewise) — it's THIS peak that forces `_EST_BYTES_PER_IMG` to 36 B/px and hence the small waves | No sharing of intermediates between the two scopes; band assignment via 8-center broadcast instead of binning | Hoist hue/sat/chroma/`band_idx` computed **once** in the dual composition and pass them to both scopes; keep `argmin` (bit-exact). Option: `bucketize` on band edges (−8× RAM) to validate bit-exact before adoption | Metrics phase **−30-40%**, band VRAM peak **−50%** (→ larger waves, compounds with P-03); effort M | no (identical values if argmin kept; if switching to bucketize: verify parity, otherwise bump `ANALYSIS_VERSION`) |
| P-05 🟡 | `render_metrics_gpu.py:62-72,115-199`, `gpu_raw.py:185-206,218-219` | ~50-100 GPU→CPU syncs **per image**: every `float(...)`/`_q(...)` forces a sync (band_stats alone: 8 bands × ~6 scalars, ×2 in dual); on top of that `pp[mask_flat]` (N×3 gather) runs 2× (`gpu_raw:218-219`) | Reductions pulled back scalar by scalar throughout the code instead of being grouped | Group them: multi-q quantiles per call (`torch.quantile` accepts a q tensor), stack an image's scalars and do **one** `.cpu()`; cache the `pp[mask_flat]` gather | ~10-40 ms/image of sync overhead → **5-20 s on 500 photos**; effort M (modest gain, do it while working on P-04) | no (same values) |
| P-06 🟡 | `gpu_raw.py:153` | `torch.from_numpy(rb.bayer.astype(np.float32)).to(dev)`: float32 conversion **on the CPU side** (96 MB alloc+copy for 24 MP) then a 96 MB H2D transfer instead of 48 | Conversion done before the transfer instead of after | Transfer the uint16 (48 MB) then `.float()` on GPU; option `pin_memory` + `non_blocking=True` with the streams already exposed (`gpu.streams`, never used) | ~5-10 ms/photo → a few seconds over 500; effort S | no |
| P-07 🟡 | `cache.py:348,361,504,576,608,684` + `autocorrect_worker.py:354-368,405-414,464-468` | `conn.commit()` in **every** `put_*`; the collection loops commit per photo (RAW step: 2 commits/photo → ~1,500 commits over 500 misses). WAL+NORMAL cushions it (no fsync per commit) but each still costs ~0.1-1 ms + a lock/WAL-append | Cache API is autocommit, called in a loop | One transaction per collection step (`BEGIN` … final commit, or `with conn:` around the loop); same remedy as DB-03 (executemany for seeds) | **~0.2-1.5 s per 500-photo run** + less WAL churn; effort S | no |
| P-08 ⚪ | `autocorrect_worker.py:224,255,324-327,384-387` + `cache.py:364-368` | 3-4 one-off SELECTs/photo/run (source_raw, in_camera, preview, `is_seed` per photo) + `raw_signature` recomputed 2×/photo (steps 1 and 2) | Unit lookups inside a loop | Not hot: PK lookups ~20-80 µs → **~50-150 ms total on 500 photos**. Only worthwhile move: reuse `list_seed_uuids` as a set instead of per-photo `is_seed` | negligible; effort S — only worth doing in passing | no |
| P-09 ⚪ | `PollingLoop.lua:28,151-190`, `api.py:52-60`, `job_queue.py:112-139` | Poll 300 ms = 3.3 req/s; per request: uvicorn parsing+FastAPI routing+2 locks ≈ 0.2-0.5 ms CPU → **≈ 0.1-0.2% of one core** server-side, one LrHttp GET + sleep on the plugin side. Pickup latency: 150 ms average/job — invisible next to the 4 s/photo of probes | Polling is an architectural given (plugin = always the client) | **None**: not a hotspot, do not optimize. (If latency ever matters: server-side long-poll — req ÷30, latency ~0 — but the 5 s heartbeat is coupled to the poll cadence, would need rethinking alongside L-05) | – | no |
| P-10 ⚪ | `neutral_preview_worker.py:94-97` | `_probe_chunk` decodes thumbnails **one at a time** (`decode_file` = `decode_blobs([1])`) then analyzes photo by photo — 16 nvJPEG calls per chunk instead of one batch | Loop written following the plugin result | Batch via `gpu_schedule.analyze_render_blobs` (already exists). Real gain negligible: the path is dominated by Lr rendering (4 s/photo budget) | < 1% of the probe wall time; effort S — consistency more than perf | no |

---

## Pass 3 — Consolidated backlog (prioritized)

> Merge of passes 0-2. Sort: descending severity (🔴 > 🟠 > 🟡 > ⚪), then ascending effort
> (S before M before L); at equal severity+effort, CONFIRMED before PLAUSIBLE. No 🔴 at all.
> **Group** column = batches to handle together (see the group list below the table).

| Rank | Source ID | Title | Sev | Effort | Subsystem | Group |
|---|---|---|---|---|---|---|
| 1 | L-03 | Silent probe-restore failure (photo left in neutral state) | 🟠 | S | Lua plugin | G3 |
| 2 | B-01 | `_pending_ids` never compared — Apply replays a stale plan if the selection changes | 🟠 | S | GUI/bridge | – |
| 3 | A-01 | exiftool as argv > Windows limit from ~300 photos on → whole batch left without a profile | 🟠 | S | Analysis | G2 |
| 4 | P-03 | JPEG waves sized to RAW dimensions + `empty_cache` every wave | 🟠 | S | GPU pipeline | G7 |
| 5 | L-01 | `requestJpegThumbnail` return value discarded → callbacks never fire (GC) | 🟠 | S | Lua plugin | G3 |
| 6 | L-02 | Fixed output file + late callbacks → stale pixels measured | 🟠 | S | Lua plugin | G3 |
| 7 | C-01 | Missing `cam_mul[G2]==0` guard (no-op on Sony, breaks other bodies) | 🟠 | S | GPU pipeline | G1 |
| 8 | DB-01 | `hash_style` blind to Color Grading/Texture/ToneCurve → stale neutral anchors served | 🟠 | M | Cache | G1 |
| 9 | P-01 | No CPU/GPU overlap in `process_raw_batch` (wall time = sum, not max) | 🟠 | M | GPU pipeline | G7 |
| 10 | P-02 | Double rawpy open per photo (unpack + extract embedded) | 🟠 | M | GPU pipeline | G7 |
| 11 | B-03 | `_anchor_suspect` swallows every exception → suspect anchor cached anyway | 🟡 | S | GUI/bridge | – |
| 12 | B-04 | SQLite `conn` not closed on the worker's exception paths | 🟡 | S | GUI/bridge | – |
| 13 | L-04 | Partial apply: errors lost when `status='ok'` | 🟡 | S | Lua plugin | G4 |
| 14 | L-05 | Heartbeat written before dispatch → bridge "inactive" during long jobs | 🟡 | S | Lua plugin | G5 |
| 15 | L-06 | Non-JSON 200 body treated as "no job", no log (job lost for 900 s) | 🟡 | S | Lua plugin | G4 |
| 16 | L-07 | Result POST never retried — work done, result lost | 🟡 | S | Lua plugin | G4 |
| 17 | B-02 | Serialization after pop in `/jobs/pending`: non-serializable payload = lost job | 🟡 | S | Server | – |
| 18 | C-02 | Transient CUDA failure memoized for the process's lifetime (`lru_cache` on `_diagnose`) | 🟡 | S | GPU pipeline | – |
| 19 | C-03 | `cv2.imdecode(...)` with no None test in `raw.py` (crashes direct tool calls) | 🟡 | S | GPU pipeline | – |
| 20 | DB-03 | `_apply_seed_flag`: 2 commits/photo on the Qt thread → GUI freeze | 🟡 | S | Cache | G6 |
| 21 | P-07 | Commit per `put_*` → ~1,500 commits per 500-photo run | 🟡 | S | Cache | G6 |
| 22 | DB-02 | `hash_jpeg`/`hash_preview` = file signature, not sha1 — doc wrong, `blob_hash` dead | 🟡 | S | Cache | G8 |
| 23 | A-02 | `text=True` with no encoding → accented-path mojibake, None profiles | 🟡 | S | Analysis | G2 |
| 24 | A-03 | FR slow shutter speeds ("0,4 s") not parsed → `ev100=None` | 🟡 | S | Analysis | – |
| 25 | A-04 | `refine_temp_tint` with no `neutral=None` guard → whole run fails | 🟡 | S | Analysis | – |
| 26 | A-05 | Corrupted disk-cache JSON → `response.load()` fails the analysis | 🟡 | S | Analysis | – |
| 27 | P-06 | float32 conversion on the CPU side before H2D (2× the PCIe traffic) | 🟡 | S | GPU pipeline | G7 |
| 28 | B-05 | Apply = one transaction for the whole selection, fixed 180 s timeout | 🟡 | M | GUI/bridge | G5 |
| 29 | P-04 | Dual: hue/sat/chroma recomputed per scope + band broadcast ≈ 770 MB transient | 🟡 | M | GPU pipeline | G9 |
| 30 | P-05 | ~50-100 GPU→CPU syncs per image (scalars pulled back one at a time) | 🟡 | M | GPU pipeline | G9 |
| 31 | L-09 | Apply with no `findPhotoByUuid` fallback (photos skipped if selection changes) | ⚪ | S | Lua plugin | – |
| 32 | L-08 | `\u` decoding with no surrogate pairs (path almost never taken) | ⚪ | S | Lua plugin | – |
| 33 | B-06 | 204 with body `{}` (RFC; breaks on h11 fallback) | ⚪ | S | Server | – |
| 34 | DB-04 | `"0:0"` fallback not salted with `ANALYSIS_VERSION` (theoretical collision) | ⚪ | S | Cache | – |
| 35 | DB-05 | `ORDER BY cached_at DESC LIMIT 1` on a PK (misleading dead code) | ⚪ | S | Cache | G8 |
| 36 | A-06 | `Tint` never clamped to ±150 | ⚪ | S | Analysis | – |
| 37 | A-07 | k-NN: advertised k=3 only reached at ≥ 6 seeds (document the intent) | ⚪ | S | Analysis | – |
| 38 | P-08 | N+1 PK lookups + per-photo `is_seed` (~100 ms/500 photos — not hot) | ⚪ | S | Cache | – |
| 39 | P-10 | `_probe_chunk` decodes thumbnails one at a time (dominated by Lr rendering) | ⚪ | S | GUI/bridge | – |
| 40 | D-01…D-05 | Doc batch: 44 keys (not 42), `models.py` types omitted, stale dispatch range, stale `analysis`/`seed_match` docstrings | ⚪ | S | Doc | G8 |
| 41 | DB-06 | `get_bias_pool` with no caller (dead or to wire up — architecture decision) | ⚪ | – | Cache | G8 |
| 42 | C-04 | GPU quantiles subsampled > 8 M px — refine ARCHITECTURE §4 | ⚪ | – | Doc | G8 |
| 43 | C-05 | `extract_jpeg_stream` first SOI — comment only | ⚪ | – | Doc | G8 |
| 44 | P-09 | 300 ms poll: ≈ 0.1-0.2% of one core — **do not optimize** | ⚪ | – | Bridge | – |

### Groups (duplicates / related findings)

- **G1 — Shared `ANALYSIS_VERSION` bump**: DB-01 + C-01. Both require a bump (full cache
  rebuild) → ship them **in the same commit** to pay for only one rebuild.
- **G2 — exiftool**: A-01 + A-02. Same function (`exif_profile.py:83-88`), a single patch
  (argfile `-@` + `encoding="utf-8"` + charset).
- **G3 — Thumbnails probe**: L-01 + L-02 + L-03. Same flow (`Thumbnails.lua` fetch/fetchProbe),
  a single hardening pass (retaining the returns, file nonce, restore errors surfaced).
- **G4 — PollingLoop result robustness**: L-04 + L-06 + L-07. Same file, same theme
  (job result silently lost/dropped).
- **G5 — Long jobs vs heartbeat**: L-05 + B-05 (and P-09's long-poll note). Chunking the
  apply (B-05) mechanically shrinks the window where the heartbeat freezes (L-05) — design together.
- **G6 — Batched SQLite writes**: DB-03 + P-07. Same remedy: per-batch transactions
  (executemany / commit per step), and move the writes off the Qt thread.
- **G7 — GPU scheduler overhaul**: P-01 + P-02 + P-03 (+ P-06 while at it). A single
  `gpu_schedule` effort: unified unpack (1 rawpy open), double-buffer prefetch, per-pipeline
  sizing, reactive `empty_cache`. Validate with `tools/validate_gpu_vs_libraw` (unchanged parity).
- **G8 — Doc & dead code**: D-01…D-05 + DB-02 + DB-05 + DB-06 + C-04 + C-05. Documentation batch /
  dead-code removal — feeds PLAN steps 1 and 7.
- **G9 — GPU metrics micro-pass**: P-04 + P-05. Same files (`render_metrics_gpu`,
  `gpu_raw`), do together, bit-exact parity required (otherwise bump `ANALYSIS_VERSION`).

### Proposed PLAN.md / ARCHITECTURE.md update (✅ APPLIED 2026-07-18, Pass 4)

**PLAN.md** — 3 proposed modifications:

1. Step 1 (removing `analysis_worker`), add the Pass 0 nuance:

```diff
 - [ ] **1 — Remove dead code.**
   Remove `app/gui/analysis_worker.py` (`AnalysisWorker` never instantiated or imported — verified).
+  Note (Fable 5 review, Pass 0): `analysis_worker` is the only direct GUI importer of
+  `gpu_raw`/`raw` — their live status hinges on the `gpu_schedule`/`embedded_jpeg` chain.
+  Removing it kills no core module, but makes `gpu_schedule` the sole entry point into `gpu_raw`.
```

2. New backlog section after "Remaining backlog" (🟠 items from the review, grouped):

```diff
+## Fable 5 review backlog (2026-07-18) — 🟠 items (detail: documentation/REVIEW_FABLE5.md, Pass 3)
+
+- [ ] **G3 — Harden `Thumbnails.lua`** (L-01/L-02/L-03): capture `requestJpegThumbnail`'s
+  return values, unique filename per call, surface restore failures.
+- [ ] **B-01 — Check `_pending_ids` before Apply**: re-fetch selection, re-plan if it diverges.
+- [ ] **G2 — exiftool argfile** (A-01/A-02): `-@ argfile` + `encoding="utf-8"` (Windows argv
+  limit hit from ~300 photos on; accented paths).
+- [ ] **G1 — Bump the shared `ANALYSIS_VERSION`** (DB-01/C-01): complete `DEVELOP_KEYS` (Lua) and
+  `_STYLE_KEYS` (Color Grading/Texture/ToneCurve/Parametric), `cam_mul[G2]==0` guard in
+  `gpu_raw` — one bump, one rebuild.
+- [ ] **G7 — GPU scheduler rework** (P-01/P-02/P-03, +P-06): unified unpack (1 rawpy open),
+  CPU/GPU double-buffer prefetch, per-pipeline wave sizing, reactive `empty_cache`.
+  Parity to re-validate (`tools/validate_gpu_vs_libraw`).
```

3. Existing backlog perf line, to correct (Pass 2 contradicts "already covered"):

```diff
-- Perf: 500-1000 series parallelization already covered by GPU + cache; re-profile before any Rust.
+- Perf: GPU + cache in place, but Pass 2 (REVIEW_FABLE5.md) identifies structural
+  losses (no CPU/GPU overlap, double rawpy open, undersized JPEG waves).
+  Profile (`py-spy`/`torch.profiler`) then address G7 before considering Rust.
```

**ARCHITECTURE.md** — Pass 0 corrections (§3 statuses: **zero change**, the map is accurate):

```diff
 §2, plugin table (line ~80):
-| `PhotoData.lua` | Extraction of path/EXIF/develop settings/catalog_path (**42 `DEVELOP_KEYS`**) |
+| `PhotoData.lua` | Extraction of path/EXIF/develop settings/catalog_path (**44 `DEVELOP_KEYS`**) |

 §2, job types (lines ~66-67):
-(~ lines 43-149)
+(~ lines 47-121)

 §3, `server/` (lines ~143-144):
-`models.py` (Pydantic: `Job`, `JobResult`, `PhotoResult`, `ExifData`, `PhotoAdjustment`, enum `JobType`).
+`models.py` (Pydantic: `Job`, `JobResult`, `PhotoResult`, `ThumbnailResult`, `ExifData`,
+`PhotoAdjustment`, enums `JobType` / `JobStatus`).
```

D-04/D-05 (stale docstrings in `core/analysis.py:12` and `core/seed_match.py:1`) = code
patches, not project doc → filed under G8.

---

## Passes journal

| Pass | Date | Reasoning effort | Subagents used | Findings |
|---|---|---|---|---|
| 0 | 2026-07-17 | standard + manual cross-check of import chains | 4 × cavecrew-investigator in parallel (core imports / gui+server imports / Lua require / server+cache fact-check); 3 agent errors corrected via direct grep (gpu_raw, catalog, wb_model wrongly counted as tools-only or via a dead module) | §3 map: 0 status corrections (49 modules audited). 5 doc↔code divergences (D-01 to D-05), all minor; 10 families of claims verified as conforming |
| 1 | 2026-07-17 | high — full read of the 14 Lua files (~1,200 l.) + live server/GUI/core (~4,700 l. Python), Lua APIs checked against `lr15_sdk_api_reference.md`, CPU↔GPU parity verified formula by formula | none (direct reading, no subagent) | 33 findings: 7 🟠 · 16 🟡 · 10 ⚪ · 0 🔴. Top of the list: DB-01 (hash_style blind to Color Grading/Texture → stale neutral anchors), L-03 (silent probe-restore failure), B-01 (`_pending_ids` never checked), A-01 (exiftool argv > Windows limit at 500+ photos), L-01/L-02 (requestJpegThumbnail: retention + file race), C-01 (missing G2=0 guard — no-op on Sony) | 
| 2 | 2026-07-18 | standard — targeted read of the 3 hot zones (gpu_schedule/gpu_raw/gpu_jpeg/render_metrics_gpu/sharpness, cache.py + worker loops, PollingLoop/api/job_queue); **no profiling run**, costs = reasoned estimates marked as such | none (direct reading) | 10 hotspots: 3 🟠 (P-01 no CPU/GPU overlap — the docstring promises the opposite; P-02 double rawpy open/photo; P-03 RAW-sized JPEG waves + systematic empty_cache) · 4 🟡 (P-04 band broadcast ~770 MB transient + dual recomputation; P-05 scalar syncs; P-06 CPU float32 before H2D; P-07 ~1,500 commits/run) · 3 ⚪ including P-09: 300 ms poll **not hot** (≈ 0.1-0.2% core), do not optimize. Cache: no missing index on the live path. No hotspot touches the measurements (bump `ANALYSIS_VERSION` needed only if P-04 switches to a non-bit-exact bucketize) |
| 3 | 2026-07-18 | standard — pure consolidation (no new code analysis), merge of the 49 findings from passes 0-2 | none | Single 44-line backlog (10 🟠 · 20 🟡 · 14 ⚪), sorted by severity then effort; 9 groups of related findings (G1 shared ANALYSIS_VERSION bump DB-01+C-01, G7 GPU scheduler overhaul P-01/02/03…); PLAN.md diffs (3) and ARCHITECTURE.md diffs (3) **proposed, not applied** — awaiting user validation |
| 4 (implementation) | 2026-07-18 | high — executing the overhaul on user validation | none (direct editing) | **Delivered: 40/44 items** — all the 🟠 (G1 bump `v5-style-keys-g2wb` + _STYLE_KEYS fixed (5 non-existent SDK names `ColorGradeShadowHue`… → `SplitToning*`) + `DEVELOP_KEYS` 44→71; G2 argfile; G3 retention/nonce/restore_error; G4 errors_summary/200 log/POST retry; G5 heartbeat during the wait + apply chunked at 50 + timeout ∝ n; B-01 selection re-check; G7 `process_combined_batch` double-buffer/unified unpack/per-pipeline waves/uint16 H2D); 🟡/⚪: B-02/03/04/06, C-02/03, DB-02/03/04/05, A-03/04/05/06/07, L-08/09, P-07, D-01…D-05, C-04/05, DB-06 (dead bias REMOVED: `get_bias_pool`, `_build_bias_by_group`, `compute_profile_bias`, `blob_hash`); PLAN step 1 (analysis_worker removed + `test_no_dead_modules.py`). **Not addressed**: G9 (P-04/P-05, bit-exact parity to profile), P-10, P-08 (not hot). Validation: 78/78 pytest, `validate_gpu_vs_libraw` 3 real ARW (exposure corr 1.000, gray-world 0.996-0.9995), `process_combined_batch` smoke test 4/4 real |
