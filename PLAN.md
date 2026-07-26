# PLAN — Neutral-preview repair, measure-grid enforcement, exposure calibration, GPU scoping, UI rework

Executable roadmap **one step at a time**. Technical context:
[`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md). Working rules:
[`CLAUDE.md`](CLAUDE.md). Previous plan (DOC/CAL/COV, all closed 2026-07-25) archived verbatim
in [`documentation/PLAN_ARCHIVE_2026-07.md`](documentation/PLAN_ARCHIVE_2026-07.md). Rework
before that (S0/Q/R/W/D/N/T1-T6): [`OLD_PLAN.md`](OLD_PLAN.md).

## Origin

Full review of the project (2026-07-25) requested after "Neutral Preview" was reported broken.
Investigation confirmed the break **and** found a worse defect underneath. Everything below is
measured on the live catalog (`Last soirée Abreu`, 708 photos), not estimated.

### Evidence

**1. Neutral Preview is dead — 4 rows out of 708.**
Live cache `C:\photos sony\Catalogues\Last soirée Abreu\ABELr_cache.db`: `LightroomPicture` 708,
`SourceRAW` 708, `InCameraJPEG` 708, `PreviewJPEG` 707, **`NeutralPreviewJPEG` 4**, seeds 690.
The 4 rows are valid, written minutes apart — from single-photo calibration probes, never from
the GUI batch flow.

**2. Root cause — two budgets, both wrong, in two languages.**
`Thumbnails.lua:77` computes `timeout = max(15, n * 0.4)`. `fetchProbe` reuses `fetch` as-is, so
**a 16-photo probe at 2048×2048 gets 15 s**. Python side, `neutral_preview_worker.py:182` waits
`max(30, 4.0*n)` = 64 s. Real throughput, derived from `ABELr.lrplugin/tmp_thumbs/` mtimes
grouped by fetch generation:

| regime | n | s/photo |
|---|---|---|
| `get_thumbnails` 2048, previews already built (gen 63) | 14 | **0.26** |
| 512 px path (gen 2/18/93/94) | 4–19 | 0.47–0.74 |
| **`render_probe` 2048, forced regeneration** (gen 43/44/50/51/64/122/123) | 3–5 | **4.3–7.4 (median 5.7)** |

A 16-photo probe legitimately needs **90–120 s**. Both the 15 s Lua floor and the 64 s Python
budget are under target. Confirming signature: every one of the 7 probe generations contains
3–5 files over a 15–18 s span — the timeout cuts every single one of them.
`THUMB_SECONDS_PER_PHOTO = 0.4` is not a bad value: it matches the passive path (0.26 measured).
The defect is **one constant serving two paths that differ by a factor of 22**.

**3. A worse defect underneath — the "common measurement grid" does not exist.**
Decoding the real JPEG dimensions in `tmp_thumbs/`: `requestJpegThumbnail` **ignores the
requested size** and serves whichever pyramid tier Lr has cached.

| requested | actually rendered |
|---|---|
| 2048 | 3504 (37 files, correct — larger, downsampled back down), but sometimes 876 or 484 |
| 512 | 968 (56 files), sometimes 484 |

`downsample_to_measure_grid` (`render_metrics_gpu.py:71-72`) never upsamples. So **74 of 111
renders on disk (67%) are measured below the 2048 grid** — 484, 876 or 968 px. The entire premise
of `ANALYSIS_VERSION = "v7-measure-grid"` (old-plan R2/R3) is silently defeated. Worse, a single
calibration pass mixes two grids: fetch generation 2 alone has 17 files at 968 px and 2 at
484 px. The WB Jacobians and HSL gains in `app/data/response_cache/*.json` were fit on that
mixture.

**4. The real GPU bottleneck, measured — not what was assumed.**
RTX 2080, torch 2.6.0+cu124, real Sony ARW, bayer 4688×7028:

| stage | ms/photo | batched? |
|---|---|---|
| `rawpy` unpack + bayer + embedded | 1025 (CPU) | ThreadPool ≤8 → ~128 effective |
| **`gpu_raw.process_bayer_gpu`** | **334** | **no — serial loop, `gpu_schedule.py:181`** |
| `gpu_jpeg.decode_blobs` (nvJPEG) | ~92 | yes |
| `analyze_rendered_gpu_dual` | 94 | no — serial |

Inside the 334 ms: full-resolution `_srgb_u8_to_lab` **63.7 ms** (`gpu_raw.py:236`) +
full-resolution `sharp_mask_gpu` **82.1 ms** (`:237`) = **146 ms (44%)**. The same two operations
on the 2048 grid cost **9.4 ms** — 14× cheaper. And they feed only `exposure_sharp` /
`grayworld_*_sharp`, already flagged in the code comment (`gpu_raw.py:228-233`) as
*"write-only in the cache"* — grep-confirmed, no non-test code reads those keys. The k-NN's
`raw_median_l` comes from `sr["tone"].median_l` (`seed_match.py:119`), computed on the 2048
grid. Bonus: `pp[mask_flat]` is computed **twice** (`:253` and `:254`).

**5. Measured and NOT a problem — do not optimize.**
`get_source_raw` ×708 = 47 ms · `get_in_camera_jpeg` ×708 = 81 ms · `is_seed` ×708 = 8 ms ·
`get_picture` ×708 = 32 ms · `build_seed_pool` (690 seeds) = 161 ms · `match_target` =
4.4 ms/photo. The "one query per photo" pattern totals **168 ms against ~236 s of GPU work** =
0.07%. Batching cache reads or memoizing `_feature_scale` would optimize noise.

**6. Exposure calibration does not exist.** Both `response_cache/*.json` have
`exposure: {"ev": [], "lstar": []}`. Nothing calls `response.save` with a populated
`ExposureResponse`. `response.py:68-69` therefore always falls back to
`NOMINAL_DL_DEV = 17.0` L*/EV — on the axis carrying the largest MAE in the baseline (2.86 L*).

### Decisions

- **Grid**: reject any render below 2048 **and** bump `ANALYSIS_VERSION` to start clean (full
  708-photo re-measure).
- **Probe cost**: accept the ~5.7 s/photo, size the budgets for that speed, and **show an ETA**
  before launching. Normal usage = a working selection (30–50 photos ≈ 5 min), not the whole
  catalog (~70 min).
- **Scope**: the four technical workstreams below **plus a GUI rework**.

### Order and why

`A → L → N → G → R → X → U → D`

`L` makes everything else observable. `N` repairs the broken feature. **`G` before `R`**: the
re-measure the grid bump forces is 44% cheaper once `process_bayer_gpu` is lighter. `R` does the
bump and the re-measure. `X` calibrates on measurements that are finally clean. `U` is
independent and can be pulled forward if useful. `D` is hygiene, touches the most files.

## Execution rules (unchanged from the previous plan)

1. One step at a time, in section order.
2. For each step: implement the regression test BEFORE/WITH the change.
3. Validate with `python -m pytest app/tests -q` (must stay **green**) — running
   `python -m app.main` is not required except for steps marked **Lr required**.
4. Check `- [ ]` → `- [x]` only after a green test (or, for **Lr required** steps, after a
   documented manual validation with evidence, numbers included).
5. GPU-strict for the `core/` pipeline (no CPU fallback) — except `gpu.py` itself (GPU-first +
   CPU fallback, cf. CLAUDE.md, unchanged).
6. Any change to the measurement algorithm requires bumping `cache.ANALYSIS_VERSION` (full cache
   rebuild, no row-by-row migration).
7. If a step breaks an existing test with no legitimate reason: stop, don't check it off, flag it.

---

## A — Archiving

- [x] **A1 — Archive the previous `PLAN.md`.** Done 2026-07-25 — moved verbatim to
  `documentation/PLAN_ARCHIVE_2026-07.md` (baselines + locked decisions preserved in a header).
  `OLD_PLAN.md` untouched. `SEG1` (only item left open) and the deferred backlog (G9, P-10,
  `core/regime.py`, `core/image_source.py`, N3's `percentile=75.0`) carried into this file
  (see "Deferred backlog" below). `CLAUDE.md` / `documentation/ARCHITECTURE.md` cross-references
  to `PLAN.md`/`OLD_PLAN.md` still resolve (path unchanged). Verify:
  `python -m app.tools.check_docs`. Lr: no.

---

## L — Logging (do first: ~40 lines, makes `N` diagnosable)

Correction to keep in mind: `logging.lastResort` **is** a stderr handler at WARNING — every
`_log.exception` already prints, just unformatted, to a stderr nobody reads, with nothing
persisted, and INFO is dropped entirely. The fix is "file sink + GUI sink", not "add a handler".

- [x] **L1 — `app/logging_setup.py` (new).** `configure()` attaches a `RotatingFileHandler`
  (`{plugin_root}/abelr_app.log`, 2 MB × 3, INFO) plus a stderr `StreamHandler` (WARNING) to the
  `abelr` logger. Every worker already uses `abelr.*` names (`neutral_preview_worker.py:44`,
  `fresh_render_worker.py:23`, `exif_profile.py:27`, `response.py:219`) so one call covers all of
  them. Called from `app/main.py` before the GUI starts. Add `abelr_app.log` to `.gitignore`.
  Test: `app/tests/test_logging_setup.py` — idempotent (`configure()` called twice does not
  duplicate handlers), file handler present, a child logger propagates to it. Lr: no.

- [x] **L2 — Log → GUI bridge.** `app/gui/log_bridge.py` (new): a `logging.Handler` owning a
  `QObject` with `record = Signal(str)`. Cross-thread emission is auto-queued by Qt, so it's safe
  from a `QThread` worker. Wired into the "Log" panel added by `U2` for WARNING+.
  Test: instantiate the handler with no window, log an ERROR from a `threading.Thread`, assert
  the formatted message arrives on the signal — tests the handler, not `MainWindow` (keeps the
  COV5 decision that GUI workers stay manual-only). Lr: no.

> Logging is the audit trail, **not** the contract: `N2c` surfaces failures through
> `ensure_neutral_previews`'s return value regardless of `L`.

---

## N — Neutral Preview repair

### N1 — Headless harness (before any behavior change, rule 2)

`job_queue` (`app/server/job_queue.py`) is a pure in-process singleton — `submit`/
`next_pending`/`submit_result`/`wait_result` use only `Lock`+`Event`. **No HTTP server is needed**
to test the full flow.

- [x] **N1a — Split transport from behavior in `mock_plugin.py`.** `handle(job) -> dict`
  (`app/tools/mock_plugin.py:104`) is already transport-free. Add a `FakePlugin` class wrapping
  it: `pump()` = `next_pending()` → `handle` → `submit_result`, a `run_in_thread()` context
  manager, and injectable `render_probe` behaviors — `ok`, `partial(n_fail)`, `all_timeout`,
  `error_status`, `no_response`, `restore_failed`, `undersized`. `main()` keeps its HTTP loop
  unchanged.

- [x] **N1b — Extract the summary decision.** `NeutralPreviewWorker.run`
  (`neutral_preview_worker.py:265-303`) decides `failed` vs `finished_result` inline. Move it to
  a module-level `_summarize(outcome, n_photos) -> tuple[bool, str]` — this is what makes `N2`'s
  regression testable without Qt while respecting COV5.

- [x] **N1c — `app/tests/test_neutral_preview_flow.py`.** (undersized/R1 case deferred to R1 —
  grid enforcement doesn't exist yet, per plan order N before R.) Drives `ensure_neutral_previews`
  against `FakePlugin` on an in-memory cache (same fixture pattern as `test_cache_roundtrip.py`),
  monkeypatching `gpu_jpeg.decode_file` and `analyze_rendered_gpu_dual` to return synthetic
  `RenderAnalysisDual` built via `conftest.make_analysis` — the test targets control flow, not
  pixels, staying inside conftest's "no GPU, no RAW" contract. Cases: happy path (N=20, chunk=8)
  · **all-timeout → `_summarize` returns failed=True** (pins the fake-success message) · partial
  (3 of 8 fail: the 5 good ones ARE cached, the summary names the cause) · `status='error'` with
  thumbnails present · plugin never responds → `RuntimeError` · `put_neutral_preview` raises →
  photo reported failed, not counted in `n_refreshed` · `restore_error` → warning emitted **and**
  anchor still cached (current behavior, asserted deliberately) · **undersized render → rejected**
  (`R1`) · circuit breaker: 3 all-failing chunks abort after 2, the 3rd is never submitted.
  Lr: no. Closes the "zero tests touch `render_probe`" gap.

### N2 — Make failure loud on both sides

`JobResult` already carries `status`, `error`, `errors_summary`, `applied`, `total` — **no
`models.py` change**, the `JobType` enum and `dispatch()` stay 14/14 in sync.

- [x] **N2a — Real `status` from `render_probe` and `get_thumbnails`.** `PollingLoop.lua:153-158`
  and `:127-132` both hard-code `status='ok'`. Compute `nOk` = thumbnails with a
  `thumbnail_path`; `status = (nOk > 0 or #thumbs == 0) and 'ok' or 'error'`; attach
  `errors_summary` and `applied`/`total`. Factor the duplicated 5-item truncation
  (`:55-62`, `:167-174`) into one local helper.
  **Expected fallout**: `app/mcp/tools.py:59-62` already raises on `status != 'ok'`. Once the
  plugin tells the truth, `mcp_calibrate.py` and the MCP `render_probe` tool will start failing
  loudly on probes that previously "succeeded". That is the point.
  Test: `app/tests/test_lua_contract.py` (new) — parse `PollingLoop.lua` as text, assert neither
  branch contains a literal `status = 'ok'` outside a computed variable (same spirit as
  `check_docs.py`). Lr: no (behavioral confirmation in `N5`).

- [x] **N2b — `Thumbnails.fetchProbe`: no silent drops.** Two holes: `:200-204` an unresolvable
  `photo_id` is dropped from `targets` with no result row — mirror `Adjustments.lua:75-76`,
  append `{photo_id=id, error='uuid not found'}`. `:209` `LrTasks.pcall(applyDevelopSettings)`
  discards ok/err — capture into `applyErrors[id]`, surface as `results[i].error`. A failed apply
  means the "neutral" anchor is the *current* render, which poisons embedded mode exactly like a
  stale probe, silently.

- [x] **N2c — Python stops discarding the result.** In `neutral_preview_worker.py`:
  `_probe_chunk` returns `(out, failures: dict[photo_id, str])`; check `result.status` (never
  read today, `:96-102`); record `result.error`/`errors_summary`; record `t.error` for every
  thumbnail without a path (`'timeout'`, `'no JPEG returned'`, `'io.open failed'`, already
  produced by `Thumbnails.lua:110/114/137`); record `'jpeg decode failed'` when `decode_file`
  returns None (`:106-108`). `ensure_neutral_previews` returns a `NeutralPreviewOutcome`
  dataclass (`by_id`, `n_refreshed`, `n_requested`, `failures`) instead of a 2-tuple.
  `put_neutral_preview`/`conn.commit()` exceptions (`:236-237`, `:244-248`) go into `failures`
  **in addition to** the log. `_summarize` emits `failed` when
  `n_refreshed == 0 and n_requested > 0`, listing the top 3 distinct reasons with counts.
  **Circuit breaker**: 2 consecutive zero-success chunks → raise with the accumulated reasons,
  instead of grinding through every remaining chunk. Test: `N1c`.

### N3 — One budget, computed once, shipped in the payload

Two hard-coded constants in two languages with no shared test will drift again — that's how this
bug happened. The App computes the budget; Lua derives its wait from it.

- [x] **N3a — `app/server/budget.py` (new).** Next to `models.py` — it's part of the job
  contract, not image code:
  ```
  probe_seconds_per_photo(w, h) = max(PROBE_MIN, PROBE_S_PER_MPX * w*h/1e6)
  fetch_seconds_per_photo(w, h) = FETCH_S_PER_PHOTO
  job_timeout(n, w, h, settle, kind) = max(MIN_TIMEOUT, JOB_OVERHEAD + settle + n*per_photo)
  chunk_size(w, h, kind, cap)       = clamp(1, cap, floor(TARGET_JOB_SECONDS / per_photo))
  LUA_BUDGET_FRACTION = 0.8
  ```
  Constants fit to the measurement above, with margin over the **worst** observed (7.4 s), not
  the median: `PROBE_S_PER_MPX = 2.5`, `PROBE_MIN = 1.0`, `FETCH_S_PER_PHOTO = 1.0`,
  `JOB_OVERHEAD = 5.0`, `MIN_TIMEOUT = 30.0`, `TARGET_JOB_SECONDS = 120`. At 2048×2048:
  10.5 s/photo, chunk 11, ~125 s job budget. At 512×512: 1.0 s/photo, chunk capped at 16, 30 s.
  **Why `TARGET_JOB_SECONDS` replaces a hard-coded chunk size**: chunk size and timeout were
  tuned against each other independently. Deriving the chunk from a target wall-clock per job
  collapses them into one knob bounding (a) the window photos sit in a neutral state, (b) the
  heartbeat gap, (c) the distance to `JobQueue._ENTRY_TTL = 900`. That TTL already claims to sit
  "above the longest legitimate worker timeout" (`job_queue.py:36-39`) — fixing the budget
  invalidates the basis for that claim; this step re-establishes it (125 s → 7× margin).

- [x] **N3b — Ship the budget.** `_probe_chunk` adds `"timeout_s": timeout` to the payload
  (`neutral_preview_worker.py:88-95`); `fetch_thumbnails_chunked` does the same
  (`fresh_render_worker.py:57-60`). Both replace their local constants with `budget.*`.
  `chunk_size` stays overridable as a parameter (tests use it).

- [x] **N3c — Lua consumes the budget.** `PollingLoop.lua` forwards `payload.timeout_s` into
  `Thumbnails.fetch(photos, w, h, budget)` and `fetchProbe(adjustments, w, h, settle, budget)`.
  `fetch` uses `timeout = budget * 0.8` when a budget is given.
  **Why 0.8, not equality**: Lua must return a partial result with per-photo errors before Python
  gives up. Equal budgets produce a bare Python-side timeout with zero diagnostic — the worst of
  both worlds. With the margin, "one photo hung" and "the bridge is dead" become distinguishable.
  `fetchProbe` subtracts time already spent on the apply transaction + settle before delegating.
  Fallback when `timeout_s` is absent (MCP tools, older App): resolution-scaled default,
  `max(FLOOR, n * 0.4 * (w*h)/(512*512))` — also fixes the MCP path, where 0.4 was only ever
  correct because the default was 512.

- [x] **N3d — Anti-drift tests.** `app/tests/test_probe_budget.py`: monotonic in n and
  resolution, floor respected, `chunk_size × per_photo ≤ TARGET_JOB_SECONDS`,
  `job_timeout < JobQueue._ENTRY_TTL` for the largest legal chunk, `LUA_BUDGET_FRACTION < 1`.
  Extend `test_lua_contract.py`: `Thumbnails.lua` reads a budget argument, `PollingLoop.lua`
  forwards `payload.timeout_s` on both branches — guards against Lua reverting to a constant
  while Python keeps shipping a budget. Add the same check to `app/tools/check_docs.py` so
  `test_docs_drift.py` covers it. Lr: no.

### N4 — Robustness on the probe path

- [x] **N4a — Heartbeat during the whole probe.** `HEARTBEAT_TIMEOUT = 5` (`PollingLoop.lua:34`)
  but the heartbeat only refreshes inside `Thumbnails.fetch`'s wait loop (`:126`). Add
  `_G.ABELR_BRIDGE_HEARTBEAT = os.time()` in the apply loop, the as-shot readback loop, and the
  restore loop; slice the `settle` sleep (`:223`) into ≤1 s steps with a heartbeat between —
  mirroring `Adjustments.lua:95-96`. Otherwise a slow probe makes `/bridge` report disconnected
  and blocks the *next* user action (`main_window.py:238-242`).

- [ ] **N4b — `tmp_thumbs` leak AND collision.** `fetchGen`/`staleFiles` are module-locals
  (`Thumbnails.lua:42-45`), reset on every plugin reload. On-disk evidence goes further than a
  leak: **22 files named `*_3.jpg`, written across two different days** — generation numbers get
  reused, so the late-callback guard `gen ~= fetchGen` (`:98`) can be defeated across a reload.
  Not a live correctness bug today (a path is only returned from a successful callback), but it
  shouldn't be the only protection left standing. Persist the generation in
  `_G.ABELR_THUMB_GEN` so it survives a reload, and sweep files older than N hours in
  `thumbsDir()` at startup. Fold the duplicated `thumbsDir()` (`Thumbnails.lua:30-36` vs
  `Utils.lua:37-39`) into `Utils`. Lr: the sweep needs a live reload to confirm.

- [x] **N4c — Defensive guard on `_analysis_from_row`.** Hardening, **not** a contributor to the
  4-row cache: `put_neutral_preview` is only ever called with a non-`None` `sharp` from
  `analyze_rendered_gpu_dual`, which always builds a `ToneStats` (`render_metrics_gpu.py:272-276`)
  — the `None` return of `cache.py:315-316` is unreachable through the current write path. Still,
  2 lines in `get_neutral_preview`: a row whose `sharp` is `None` is treated as a miss.
  Test: extend `test_cache_roundtrip.py`.

### N5 — Live validation (**Lr required**)

- [ ] **N5 — Probe 32 real photos at 2048.** Expect ~3 chunks × ~125 s. Record: wall-clock per
  chunk vs budget, `n_refreshed`, `failures`, count of rejected undersized renders, whether
  `/bridge` ever reports disconnected mid-chunk. Then a full run; confirm
  `NeutralPreviewJPEG` row count reaches the selection size. Record evidence in this file.

---

## G — GPU scoping (before `R`: makes the forced re-measure 44% cheaper)

### G1 — Remove the full-resolution Lab + sharp-mask from `process_bayer_gpu`

- [x] **G1a — Land the parity test FIRST** (rule 2). `app/tests/test_gpu_raw_measure_parity.py`:
  build a deterministic synthetic `RawBayer` (seeded RNG) at two sizes — above the 2048 grid
  (2400×1600) and below it (1200×800) — and assert `process_bayer_gpu(...).tone / .bands /
  .exposure / .grayworld_* / .asshot_*` are **exactly equal** to a legacy re-implementation
  written inside the test file, reproducing today's `gpu_raw.py:234-249` including the
  `hwc_measure is hwc_u8` shortcut.
  **Why a legacy re-implementation, not golden literals**: `gpu.device()` returns CPU without
  CUDA (CLAUDE.md), so hard-coded floats would be device-dependent — either brittle or CUDA-only.
  Comparing two code paths on the same device is device-agnostic and encodes exactly the claim
  being made. Must be green **before** and **after** the change, legacy function unchanged.

- [x] **G1b — The change.** In `gpu_raw.py:234-255`: drop the full-res `lab` (`:236`) and `sharp`
  (`:237`); compute `hwc_measure`/`lab_measure`/`sharp_measure` **unconditionally**, deleting the
  `if hwc_measure is hwc_u8` shortcut (`:243-247`) — provably value-preserving, since when the
  shortcut fired `hwc_measure` *was* `hwc_u8`, so the same deterministic functions on the same
  tensor give the same result; `exposure_sharp = grayworld_rg_sharp = grayworld_bg_sharp = None`;
  `mask_sharp_frac = float(sharp_measure.float().mean())` — free, already computed. The double
  `pp[mask_flat]` gather (`:253`/`:254`) disappears with the mask.
  SQL columns **kept** (dropping = `SCHEMA_VERSION` bump = full DROP+recreate, unjustified for
  dead columns); they become NULL on new rows.
  Expected: 334 → ~188 ms/photo. Lr: no. Re-run `tools/validate_gpu_vs_libraw.py` (GPU-only) as
  secondary evidence.

### G2 — Batching: recommendation is do not batch either loop

**`process_bayer_gpu` — no.** Every kernel already runs on 33 Mpx (4688×7028) — a batch
dimension helps when kernels are too small to fill the SMs; at 33 Mpx the RTX 2080 is already
saturated (demosaic alone = 6 `conv2d` passes over 33 Mpx). `gpu_schedule.py:47` estimates
`_EST_BYTES_RAW_IMG ≈ 1.19 GB`/image — a batch of 4 ≈ 4.8 GB transient on an 8 GB card shared
with everything else.

**`analyze_rendered_gpu_dual` — no to batching, yes to a different fix.** Counting host syncs
per call: `tone_stats` 7, `neutral_stats` 5, `band_stats` 8 bands × 6 = 48 → 60 per scope, ×2
scopes ≈ **120 device→host syncs per photo** (`render_metrics_gpu.py:160-231, 267-277`). At
2.8 Mpx the compute is small; at ~0.2–0.5 ms/round-trip, 24–60 of the measured 94 ms is
plausibly pure sync. Batching across a wave removes none of them — each image still needs its
own scalars. The fix that targets this is the deferred **G9** item: hoist every quantile into a
tensor, `torch.stack`, one `.cpu()` per image — bit-exact by construction (changes *when* a
value crosses to host, not how it's computed).

- [x] **G2 — Confirm the hypothesis before writing code, then implement.** Confirmed via
  `torch.cuda.set_sync_debug_mode('warn')` on a real-shaped synthetic render (1536×1824 CUDA
  u8, RTX 2080): **192 device→host syncs / `analyze_rendered_gpu_dual` call, 80.98 ms/call**
  (measured average over 50 calls after warm-up) — higher than the ~120 inferred from reading
  the source (boolean-index gathers count too, not just `.item()`/`float()`/`int()`). Implemented
  the grouped-sync pass in `tone_stats`/`neutral_stats`/`band_stats`
  (`app/core/render_metrics_gpu.py`): every host-crossing scalar per function (or, for
  `band_stats`, per whole 8-band loop) collected into tensors and pulled in ONE `.tolist()`;
  the four per-band gathers (`hue[m]`/`sat[m]`/`chroma[m]`/`lstar[m]`) merged into one
  `combined_src[m]` gather (same fix shape as `G1`'s double-gather removal). Guarded by a
  bit-exactness test (`app/tests/test_render_metrics_gpu_sync_grouping.py`, built like `G1a`:
  frozen legacy re-implementations vs. the new grouped functions, several mask configurations
  including all-empty bands and an all-false mask) — green both before and after the rewrite.
  **Re-measured after: 100 syncs/call (-48%), 72.62 ms/call (-10%)** — a real, verified win, short
  of the plan's optimistic 94→45ms estimate (most bands in a real/random render are non-empty, so
  the per-band gather count itself doesn't drop, only the post-gather scalar-pull count does).
  Lr: no.

- [x] **G3 — Record the "measured, not a problem" list here** (see Origin point 5) so it is not
  re-proposed. 168 ms of cache reads against ~236 s of GPU work = 0.07%. Memoizing
  `_feature_scale` in particular would introduce a staleness invariant (the pool changes between
  calls) for no measurable gain.

---

## R — Measure-grid invariant + clean re-measure

- [x] **R1 — The invariant: a render below the grid is a failure, not a measurement.** Single
  enforcement point on the App side, after decode and before any measurement: if
  `max(h, w) < MEASURE_LONG_EDGE`, reject with reason
  `f"undersized render {w}×{h} (requested {long_edge})"`. Applies to the three paths that consume
  a Lr-rendered JPEG: `neutral_preview_worker._probe_chunk` (neutral anchor),
  `fresh_render_worker`/`autocorrect_worker._collect_renders` (fresh preview), and the
  calibration tools. Best location: a shared helper next to `downsample_to_measure_grid` (its
  missing counterpart) in `render_metrics_gpu` or `gpu_jpeg` — one place to test, one place not
  to forget. Note: requesting 2048 and getting 3504 back is correct (larger tier, downsampled
  down) — only 484/876/968 are failures.
  Test: extend `app/tests/test_measure_grid.py` — a 484 px render is rejected, a 3504 px render
  is accepted then brought down to 2048, an exactly-2048 render is accepted with no resampling.
  Lr: no.

- [x] **R2 — Bump `ANALYSIS_VERSION`.** `cache.py:61` → `"v8-grid-enforced"`. Salted into
  `raw_signature` and `style_hash` → every measurement starts fresh, no migration.
  `SCHEMA_VERSION` stays 5 (no structural change, per `G1b`). Update the constant's comment with
  the reason (sub-grid renders were being accepted) and `documentation/ARCHITECTURE.md`.

- [ ] **R3 — Full re-measure (Lr required).** 708 photos: RAW re-decode on GPU (~188 ms/photo
  after `G1` ≈ 2.5 min GPU, dominated by the ~1 s/photo CPU unpack ÷ 8 threads ≈ 1.5 min) + Lr
  re-rendering the previews. Launch via "Mark + analyze references" on the catalog. Then re-run
  `app/tools/validate_seed_matching.py` (S0) and **compare against the archived baseline**:
  exposure 2.86 L*, WB Temp 104.7 K, Tint 1.44, Calibration 0.62–2.04, HSL chroma/L*/hue
  3.11 / 4.29 / 2.76°.
  **Interpretation caveat**: any MAE change here is a mixed effect of "grid finally homogeneous"
  — say so as such, don't attribute it to one axis. This is the first measurement on a guaranteed
  homogeneous grid; it becomes the new reference baseline.

---

## X — Exposure response calibration (Lr required)

The axis with the largest MAE (2.86 L*) is the only one whose response is a prior, not a
measurement. If the true slope is 14 or 20 instead of 17.0, every solve is biased 15–20%.

- [ ] **X1 — `calibrate_exposure` in `app/tools/mcp_calibrate.py`.** Reuse the existing
  MCP-client transport and `--photo-id` bypass (both already solve the port-5000 conflict and
  the Lr-selection requirement). Probe `Exposure2012 ∈ {−2, −1, −0.5, +0.5, +1, +2}`, measure
  `sharp.tone.median_l` of each render, fit `ExposureResponse.ev/lstar`, `response.save`.
  **Scope must match the consumer**: `autocorrect` feeds `response.solve_dev(current_l,
  target_l)` with sharp-zone L* — calibrate sharp-zone L*, not global. Getting this wrong biases
  the whole axis silently. Reference-photo selection follows CAL1's evidence-based pattern:
  scan cached `InCameraJPEG` for `tone_sharp.median_l` near 50, rather than guessing. Reject
  probes whose `clipped_hi`/`clipped_lo` exceed a threshold — the curve saturates at the ends
  and a clipped sample poisons `slope_at`. Probe at `MEASURE_LONG_EDGE`, not the 512 default
  (`X3`).
  Test: extend `app/tests/test_response.py` — `slope_at`/`solve_dev` against a synthetic probed
  curve (including a non-monotonic segment and an out-of-range value) plus the clipped-sample
  rejection predicate. The fit logic is unit-testable with no Lr.

- [ ] **X2 — Measure the benefit.** Re-run S0 before/after against the new `R3` baseline. If it
  doesn't move, that's a publishable result too — 17.0 was accidentally right, and the fallback
  can be documented as validated rather than assumed.

- [ ] **X3 — Close the resolution hole on the MCP side.** `app/mcp/server.py:145-168` sends no
  width/height → Lua defaults to 512 (`PollingLoop.lua:138-139`). Add both parameters, defaulting
  to `render_metrics.MEASURE_LONG_EDGE`, on the **App** side (Lua's 512 becomes a legacy
  fallback only), and plumb them through `mcp_calibrate.py`. Route the tool's default timeout
  (`max(30, 5·n)`, `server.py:164`, under-dimensioned at 2048) through `budget.job_timeout`.
  Test: extend `test_mcp_tools.py` — submitted payload carries `width == MEASURE_LONG_EDGE` and a
  budget. Lr: no.

- [ ] **X4 — Re-calibrate WB/HSL at 2048, as an A/B, not a blind redo.** The current fits were
  done on the 968/484 px mixture documented above. Write the 2048 fits to a side-by-side file and
  compare coefficients. **Decision rule set before running**: if |Δslope| falls inside the
  residual spread of the 5 probes, document that the grid doesn't matter here and stop —
  cheaper and more honest than a blanket recalibration.
  Mechanistically the grid should matter most for **WB**: `neutral_stats` medians a*/b* over
  pixels with `chroma < _NEUTRAL_CHROMA`, and area-downsampling averages neighbors → less chroma
  noise → a larger, differently-populated neutral set. Likely why CAL2 lost 2 of 5 Temperature
  probes to `_MIN_NEUTRAL_FRAC`. HSL gains should be more robust (hue is stable under area
  averaging); residual risk there is the `_BAND_MIN_FRAC` gate.

---

## U — GUI rework

The current UI (`app/gui/main_window.py`, 680 lines) is 7 buttons across 3 flat rows with no
hierarchy. Real problems, found reading the code:

- **The usage order lives only in the module docstring** (`:3-17`), invisible to the user.
  Nothing shows that "Mark + analyze references" must precede "Preview", or that "Calibrate
  Neutral Previews" only matters in embedded mode.
- **No state is shown**: catalog photo count, seeds marked vs actually usable, fresh neutral
  anchors available for the selection, whether the response model is calibrated. All of it
  exists in the DB, none of it is surfaced. On this catalog, **690 of 708 photos are marked as
  references** — and `autocorrect.plan:289` does
  `targets = [m for m in measures if not m.is_seed]`, so selecting references produces an empty
  plan, reported only as "No correction needed — photos already match the profile"
  (`main_window.py:561-568`), which **contradicts** the `n_targets: 0` shown just above it.
- **`photo_list` is a dumping ground**: plan lines, diagnostic notes, warnings — and `L2` wants
  to add logs to it too. These need separating.
- **No ETA, no cancel.** At ~5.7 s/photo measured, a neutral run on a large selection is long and
  cannot be interrupted. The `cb_embedded` tooltip (`:118`) even claims "~1-4 s/photo" — wrong,
  needs correcting.
- **The two panels meant for this are 13-line stubs**, never finished (`photo_panel.py`,
  `analysis_panel.py`).

Constraint: stay in PySide6, no new dependency; keep logic in pure, testable functions — the
COV5 decision (GUI workers not unit-tested) stays valid, this reduces what it has to cover
rather than working around it.

- [ ] **U1 — Layout ordered by workflow.** Replace the 3 flat rows with numbered `QGroupBox`es
  reflecting the real order: **1. Catalog** (Analyze Catalog) → **2. References** (Mark +
  analyze / Remove) → **3. Correction** (axes, mode, Preview, Apply). "Calibrate Neutral
  Previews" moves into group 3 as an embedded-mode action, relabeled to say what it does
  ("Prepare neutral anchors"), enabled only when the embedded checkbox is checked. "Test bridge"
  moves to a discreet status bar — it's a diagnostic, not a step.

- [ ] **U2 — Persistent status panel** (what the stubs should have been). A read-only grid,
  refreshed on the existing 1 s timer (`_bridge_timer`) and after every operation: catalog photo
  count · references marked / **usable** (the delta is already computed at
  `main_window.py:544-554`, just buried in a list) · fresh neutral anchors for the current
  selection (`hash_style` already cached — one query) · detected profile and **per-axis
  calibration state** (`WBResponse.is_calibrated()`, whether `ExposureResponse` is populated —
  surfaces the `X` problem at a glance) · bridge status.
  Split `photo_list` into two tabs: **Plan** (the deltas) and **Log** (diagnostics + the `L2`
  stream).
  Logic lives in a pure function `build_status(conn, photos, model) -> StatusSnapshot`, in
  `core/` or `gui/status.py`, **testable without Qt** — it carries the risk, not the rendering.
  Test: `app/tests/test_gui_status.py` on an in-memory cache.

- [ ] **U3 — ETA and cancel.** Before launching a probe-based operation, show the estimate from
  `budget.probe_seconds_per_photo` (`N3a`): "42 photos × ~5.7 s ≈ 4 min". Above a threshold
  (e.g. 5 min), ask for confirmation. Add a **Cancel** button: workers check a cooperative flag
  between chunks (natural granularity, no thread kill needed) — the infrastructure already
  exists, `ensure_neutral_previews` already loops per chunk.

- [ ] **U4 — Missing diagnostics.** (a) **All-references selection**: when
  `n_targets == 0 and len(measures) > 0`, say "N of the N selected photos are references — select
  non-reference photos" instead of "No correction needed". Check first whether
  `_plan_seeds`/`_plan_embedded` already emit a note for this; add one if not. Real here:
  690/708. Test: extend `test_autocorrect_plan_axes.py`. (b) `main_window.py:367-370` — the
  zero-axis guard names Exposure/WB/HSL but `_checked_axes()` also produces `calib`
  (`:234-235`). One word. (c) Fix the "~1-4 s/photo" in the `cb_embedded` tooltip (`:118`) with
  the real measurement.

- [ ] **U5 — Slim down `main_window.py`.** 680 lines including an `_op` state machine
  (`"ref"|"preview"|"apply"|"seed_remove"|"neutral"`) branched in cascade inside
  `_on_selection`. Extract the layout construction and plan formatting (`_format_delta` and the
  summary, `:508-568`) into dedicated modules, replace the `_op` strings with an enum. Target:
  bring the window under ~350 lines with no behavior change. Do this **after** `U1`–`U4` to
  avoid mixing relocation with behavior change.

---

## D — Dead code and hygiene (last: touches the most files, zero functional value)

`test_smoke_import.py:17-22` **globs files present on disk** — deleting a module just removes a
parametrized case, no test breaks. Real risks are a surviving importer and
`check_docs.check_dead_paths` (fails on a doc reference to a non-existent path). Run
`pytest app/tests -q` **and** `python -m app.tools.check_docs` after each step.

- [ ] **D1 — Function-level removals in live modules** (no import-graph change, zero risk):
  `pipeline.analyze_rendered`/`analyze_rendered_dual`/`band_map`;
  `gpu_schedule.process_raw_batch`/`process_embedded_batch`;
  `gpu_jpeg.decode_files`/`analyze_blob`/`analyze_file`; `gpu.streams`; `raw.load_rgb`;
  `exif_profile.exiftool_available`/`read_capture_profile`;
  `analysis.analyze_preview_jpeg` + `PreviewStats`; the unread `camera` parameter of
  `autocorrect.plan` (`:284`).
  **Four traps**: keep `pipeline.RenderAnalysis`/`RenderAnalysisDual` (dataclasses, live
  everywhere) · `previews.PreviewIndex.load_rendered` is a **different symbol** from
  `measure.load_rendered` and is live (`tools/validate_exposure_render.py:71`,
  `tools/validate_hsl.py:73`) — don't grep-and-delete by name · `read_capture_profile` (singular)
  is dead, `read_capture_profiles` (plural) is live · update the `gpu.py:16` and
  `render_metrics_gpu.py:235,257` docstrings that reference removed symbols.

- [ ] **D2 — The dead `embedded_jpeg` chain.** `load_embedded_rgb`, `embedded_tone`,
  `embedded_target_l`, `read_raw_reference`, `read_raw_references`, `extract_reference`,
  `_bounded_workers` (a dead `ProcessPoolExecutor` path). **Trap**: `RawReference`'s first
  *field* is also named `embedded_tone` (`:77`) and is built positionally
  (`gpu_schedule.py:206`) — delete the function, keep the field. Check whether the
  `concurrent.futures` import can go.

- [ ] **D3 — `measure.load_rendered` + `measure.decode_jpeg_file`.** Check whether
  `RenderChannel` (or anything else in `measure.py`) survives; if the module empties out, delete
  it and update ARCHITECTURE §3.

- [ ] **D4 — `gui/photo_panel.py` and `gui/analysis_panel.py`.** Zero importers confirmed.
  Delete both — **after `U2`**, which delivers what they promised. Same commit: update
  ARCHITECTURE §3 (gui table) and §8, and the deferred backlog below. `check_dead_paths` won't
  fire (bare filenames without `/` are skipped, `check_docs.py:131`) — **doc freshness here is
  on the author, not the checker**.

- [ ] **D5 — Explicitly out of scope.** `core/image_source.py` and `core/regime.py` are
  documented tool-only (ARCHITECTURE §3) with real callers in `tools/`
  (`analyze_ground_truth.py:43`, `series_audit.py:37`, `sharp_raw_predict.py:34`,
  `validate_wb_seeds.py:21`). Leave them.

---

## Deferred backlog (carried from the archived plan, not scheduled)

- **SEG1 — Segmentation gate.** Do not build until S0 (re-run after `R3`, on the newly
  re-measured catalog) shows residual error concentrated on content-mixing or cropped-photo
  cases. Blocked until `R3` lands — the gate needs measurements on a guaranteed-homogeneous
  grid, which did not exist before this plan. If the evidence arrives: lightweight semantic
  segmentation (SegFormer-B0/PP-LiteSeg for sky/subject/foliage + a dedicated face-parsing model
  for skin), not MobileSAM (class-agnostic, no usable labels). Constraint: plugin
  self-sufficiency (`bootstrap.ps1`) — model weight download, install-size cost, CPU-only
  inference time must be quantified before any implementation.
- **G9** — superseded by `G2` above (same item, now scoped and gated on a profiling check).
- **P-10** — `_probe_chunk` decodes one photo at a time; batch via `analyze_render_blobs`
  eventually (consistency more than perf, Lr rendering dominates).
- **`core/regime.py`** — no longer consulted by the live k-NN path; revalidate if matching
  proves unstable on small seed pools.
- **N3's `percentile=75.0`** (confidence threshold) — validated as a reasonable default but never
  swept for its actual optimum.

---

## Verification

- `python -m pytest app/tests -q` green at every step (from `ABELr.lrplugin/`). Baseline: 285
  tests. New files expected: `test_logging_setup.py`, `test_neutral_preview_flow.py`,
  `test_lua_contract.py`, `test_probe_budget.py`, `test_gpu_raw_measure_parity.py`,
  `test_gui_status.py`.
- `python -m app.tools.check_docs` green (doc/code drift) after `A1`, `D3`, `D4`.
- `python -m app.tools.mock_plugin`: the full neutral flow must pass **without Lightroom**
  (`N1`) — the criterion that guarantees the next regression will be visible.
- `python -m app.tools.validate_seed_matching "<catalog folder>"` (S0): primary before/after
  evidence for `R3`, `X2`, `X4`.
- **Lr required** for `N5`, `R3`, `X1`, `X4`: documented manual validation with numbers.
- `N`'s acceptance criterion: after a run on a selection of N photos,
  `SELECT COUNT(*) FROM NeutralPreviewJPEG` = N (vs 4 today for 708 photos).

## Risks

- **`R3`'s re-measure is one-way**: the `ANALYSIS_VERSION` bump invalidates all 708 existing
  measurements, no row-by-row migration. Do `G1` first (44% cheaper) and archive the S0 baseline
  **before** bumping, or the comparison point is lost.
- **`N2a` will break things that "worked"**: `mcp/tools.py:59-62` already raises on
  `status != 'ok'`, so partially-failed calibration probes become hard errors. Intended, but
  will surprise on the first run.
- **`X4` may invalidate CAL1/CAL2** (delivered 2026-07-25) if the grid genuinely shifts the
  slopes. The A/B decision rule is set in advance specifically so this is a measured finding,
  not a post-hoc call.
- **`R1` will reject renders that used to pass**: on this catalog, 67% of on-disk renders are
  sub-grid. Expect a non-zero rejection rate on the first run and slower probes (Lr has to
  actually build the preview). Correct behavior, but measure it in `N5` before calling it a
  regression.
- `SEG1` stays blocked on `R3` (see Deferred backlog) — it needs measurements on a grid that is
  finally homogeneous, which did not exist before this plan.
