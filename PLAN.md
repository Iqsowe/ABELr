# PLAN — Rework (measurement scale, embedded JPEG, k-NN)

Executable roadmap **one step at a time**. Technical context:
[`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md). Working rules:
[`CLAUDE.md`](CLAUDE.md). Previous plan (HSL/Calibration hardening, 2026-07-19, all steps
closed) and cleanup history: [`OLD_PLAN.md`](OLD_PLAN.md).

## Origin

2026-07-25 investigation, prompted by the user reporting weak HSL/Calibration corrections and
a non-functional "Ref = embedded JPEG" mode. Code reading + real measurements (a Sony ARW, and
the real `ABELr_cache.db` for the "Last soirée Abreu" catalog — 930 photos / 480 seeds) traced
these symptoms to precise, verifiable causes, detailed in § Evidence below. None require new ML
modelling for the bulk of the gain — see § Mapping to the 4 user goals.

**Addendum (2026-07-25, later same day)**: user-imposed personal workflow constraint — all
computed HSL, Calibration and WB Tint values must land on 5-unit steps, WB Temperature on
250-unit steps (confirmed against Lightroom's own 2000-12000K range, which divides evenly by
250 and not by the initially-stated 150 — see step Q1). Not a bug fix, a new hard requirement
on the output layer. See § Q.

## Execution rules (Sonnet 5)

1. One step at a time, in section order (S0 → Q → R → W → D → N → G).
2. For each step: implement the regression test BEFORE/WITH the change.
3. Validate with `python -m pytest app/tests -q` (must stay **green**, including existing
   tests) — running `python -m app.main` is not required except for steps marked **Lr required**.
4. Check `- [ ]` → `- [x]` only after a green test (or, for **Lr required** steps, after a
   documented manual validation with evidence — same rule as H2/C1 in the previous plan).
5. GPU-strict for the `core/` pipeline (no CPU fallback in those modules) — except where
   already settled elsewhere (`gpu.py` stays GPU-first + CPU fallback, cf. CLAUDE.md, this does
   not change).
6. Any change to the measurement algorithm requires bumping `cache.ANALYSIS_VERSION` (full
   cache rebuild, no row-by-row migration).
7. If a step breaks an existing test with no legitimate reason: stop, don't check it off, flag it.

---

## Evidence (state verified 2026-07-25, basis for this plan)

**Measurement scale mismatch** — the 4 sources compared against each other live at radically
different resolutions:

| Source | Measured resolution | File |
|---|---|---|
| RAW (bayer→ProPhoto) | full sensor resolution, 5120×7168 ≈ 36.7 MP | `gpu_raw.py:158-235` |
| In-camera embedded JPEG | full resolution, 7008×4672 ≈ 32.7 MP (verified on a real ARW) | `embedded_jpeg.py` |
| Lr render / neutral anchor | 512×512 default, never raised on the Python side | `Thumbnails.lua:69-70,181-182`, `main_window.py:393`, `neutral_preview_worker.py:87-89` |

~125-190× pixel-count gap between sources that get differenced against each other.

Real measurement (same photo, repo's own `render_metrics.py`/`sharpness.py`, resampled):
```
edge=  512  globalL*=23.80  sharpL*=33.10  diff=+9.30
edge= 1024  globalL*=23.68  sharpL*=30.09  diff=+6.41
edge= 2048  globalL*=23.64  sharpL*=26.55  diff=+2.92
edge= 4096  globalL*=23.65  sharpL*=23.83  diff=+0.18
edge= 7008  globalL*=23.69  sharpL*=23.56  diff=-0.12
```
Global is scale-invariant (±0.16 L* across a 14× linear scale). Sharp (Laplacian top-25%)
drifts 9.5 L* and **degenerates toward global at native resolution** (diff→0): at 33 MP,
"top 25% |Laplacian|" selects fine noise/grain spread uniformly across the frame, not the
subject. The sharp mask only means something at a given scale, yet the 4 sources are never
measured at the same scale.

**Confirmed correctness bug**: `autocorrect._variant_for` (autocorrect.py:150-152) switches to
"sharp" when crop area < 0.8. In embedded mode on a cropped photo, this differences a full-res
T (embedded JPEG, sharp≈global) against a 512px N (neutral render, genuinely subject-weighted
sharp) → phantom delta up to ~9 L* → bogus exposure correction up to ~0.5 EV.

**`response_cache/` is empty** (verified, 0 files). Consequence: WB in embedded mode is a
**guaranteed no-op** (`WBResponse.is_calibrated()` always False, no nominal fallback by
deliberate design — response.py:104-106; `autocorrect.py:399-404` sends everything to
`n_uncalibrated`, writes nothing). **No WB calibration tool exists** (only
`tools/calibrate_hsl_response.py` exists, for HSL bands — also never run for real).

**Confirmed dead code**: `autocorrect_worker._compute_deltas` (lines 63-100) computes
`delta_luma_median`, `delta_wb_cast_a/b`, `delta_hsl`, written to `InCameraJPEG` — never read
back. `embedded_jpeg.embedded_target_l()` has no caller. **User decision (2026-07-25)**: keep
the raw embedded-JPEG transplant (`ignore_bias=True`) → this code is removed, not revived (see
step W3).

**Current k-NN**: distance over 3 features (`asshot_rg`, `asshot_bg`, `raw_median_l`),
z-scored Euclidean (`seed_match._distance`, line 136-146). Colour composition (per-band
`frac`, already measured and cached) is never used in matching. One single global k-NN match
per photo, reused for every axis. No confidence gate.

**Real catalog data** (`C:/photos sony/Catalogues/Last soirée Abreu/ABELr_cache.db`, read-only):
930 photos analyzed, **480 seeds** (all with full `SourceRAW` + `PreviewJPEG` = direct ground
truth), **395/930 already have a `NeutralPreviewJPEG`** (embedded-mode anchors already computed
for a large share of the catalog), 1 camera (ILCE-7M4), 2 profiles (`IN`, `Neutral`),
**421/480 seeds have non-zero Calibration** with real spread (13-89 distinct values depending
on the field) — plenty for a statistically meaningful validation, and enough to finally settle
`_CALIB_SPREAD_MAX=25` (`seed_match.py:251`, a provisional value never settled, cf. C2/C3 in
the previous plan, archived in `OLD_PLAN.md`).

**Real timings** (RTX 2080, torch 2.6.0+cu124): `sharp_mask+tone_stats+band_stats` (numpy CPU)
48.5 ms at 512px / 206 ms at 1024px; k-means 6 clusters on 20k subsampled Lab pixels ~15 ms
(near resolution-independent). Real dominant cost = Lightroom itself (0.6 s/photo
`get_thumbnails`, ~4 s/photo `render_probe`). Any added compute under ~200 ms is <5% of the Lr
cost — compute is not the limiting factor.

---

## S0 — Validation harness (prerequisite, reused after every following step)

- [x] **S0 — `app/tools/validate_seed_matching.py`.** Same pattern as
  `calibrate_hsl_response.py` but **read-only on `ABELr_cache.db`, no Lr connection needed**.
  - Seeds-mode LOOCV: hold out each seed, re-match against the remaining 479, compare the
    predicted target to ground truth = the held-out seed's `PreviewJPEG` (tone/bands) and real
    `develop_json` (Temp/Tint/7 Calibration fields). Output: MAE per axis/band.
  - Embedded-mode validation: on the 395 photos with a `NeutralPreviewJPEG`, compare the
    predicted T−N delta to the real `develop_json` gap vs. the neutral point.
  - Stratify MAE by distance to the nearest seed (proxy for "same scene block" — known
    limitation: LOOCV over-estimates intra-block generalization, cf. C3 in the previous plan).
  - Test: `python -m pytest app/tests -q` green (new pure, testable module) + the harness
    produces a numeric baseline, recorded in this file once obtained.

  **Done 2026-07-25.** Read-only connection via `sqlite3.connect("file:...?mode=ro", uri=True)`
  — deliberately bypasses `cache.open_cache`/`_ensure_schema`, which would DROP+recreate tables
  on a schema-version mismatch (unacceptable on the user's live, real cache). Added two small
  hash-free accessors to `cache.py` (`get_in_camera_jpeg_latest`, `get_neutral_preview_latest`,
  mirroring the existing `get_source_raw_latest` pattern) and `seed_match.match_target_with_distance`
  (exposes the nearest seed's raw distance alongside the aggregated target — `match_target` now
  a thin wrapper over it). Embedded-mode validation reuses `autocorrect.plan(forced_embedded=True)`
  directly (not a reimplementation) so it tracks the real pipeline logic exactly, including the R1
  crop/divergence bug context.

  **Baseline, real catalog** (`Last soirée Abreu`, 2026-07-25, pre-R/Q — continuous ground truth,
  current scale-mismatch bug still present):

  Seeds-mode LOOCV (509 usable seeds — grown from 480 at evidence-gathering time):
  | Axis | MAE | n | near-half / far-half (nearest-seed distance) |
  |---|---|---|---|
  | Exposure (target L*) | 4.38 | 509 | 4.78 / 3.98 |
  | WB Temperature (K) | 123.62 | 509 | 128.61 / 118.61 |
  | WB Tint | 2.22 | 509 | — |
  | Calibration (7 fields) | 0.99–4.48 (shadow_tint best, green_saturation worst) | 509 each | — |
  | HSL chroma / lightness / hue (8 bands pooled) | 5.99 / 6.81 / 4.98° | 1273 | — |

  Stratification signal is weak (near-half not clearly better than far-half on Exposure/Temp) —
  consistent with the measurement-scale bug (R) dominating the error rather than genuine k-NN
  match quality; worth re-checking after R.

  Embedded-mode validation (396 photos with a cached `NeutralPreviewJPEG`, all resolved):
  - Exposure2012: MAE 1.32 EV (large — expected, confirms the scale-mismatch bug: 345/396 photos
    already flagged "global↔sharp ΔL* diverge >4L*").
  - WB: 0 comparable predictions — `response_cache/` empty confirmed live (206 photos have a
    real manual Custom WB edit with no calibrated response to compare against; diag separately
    flags 3 as measurably deviant-and-uncorrected).
  - HSL (24 keys, embedded mode): Saturation MAE 2.89, Luminance MAE 3.60, Hue MAE 1.89° (n=3168
    each) — the `ignore_bias=True` raw transplant already tracks manual edits moderately well
    even pre-R.

  Read as a **pre-R/Q baseline**: re-run after R (scale fix) and after Q (quantization) to
  measure their real impact, per each step's own test instructions.

---

## Q — Slider grid quantization (user-imposed, forward-looking)

Not derived from a bug — an arbitrary personal workflow rule, confirmed 2026-07-25: HSL
(`SaturationAdjustment*`/`LuminanceAdjustment*`/`HueAdjustment*`) and Calibration
(`ShadowTint`/`RedHue`/`RedSaturation`/`GreenHue`/`GreenSaturation`/`BlueHue`/`BlueSaturation`)
step by 5, WB `Tint` steps by 5, WB `Temperature` steps by **250** (not the initially-stated
150 — none of the user's own example values, 4000/4500/7750K, divide evenly by 150, all three
divide evenly by 250, and Lr's own Temperature range 2000-12000 divides evenly by 250 only —
confirmed with the user). `Exposure2012` is explicitly **not** in scope — stays continuous.

Checked against the real catalog: **none of the 480 seeds' actual slider values are currently
on any of these grids** (verified — e.g. `RedSaturation` has off-grid values like -9, -7, -3;
`Temperature` values like 2649, 2958, 3026 fit no candidate step cleanly). This confirms the
rule applies to future computed corrections only, not to matching against past manual edits —
S0's ground truth (`develop_json`) stays continuous, S0 must therefore be read knowing that
part of its post-Q MAE is expected quantization noise, not prediction error.

- [x] **Q1 — Central snap helper.** New pure function (e.g. `core/quantize.py`, or a small
  addition next to `hsl._clamp`): `snap(value: float, step: float) -> int` =
  `step * round(value / step)`. All 5 write sites below replace their current `round(...)` with
  `snap(..., step)`. Snap is applied **after** clamping to slider bounds, never before — the
  bounds in play (-100/100, -150/150, 2000/12000) are themselves exact multiples of their
  respective steps (5, 5, 250), so snapping post-clamp can never push a value back out of range.
- [x] **Q2 — HSL.** Snap the two points where an *absolute* slider value is assembled and
  written (not the internal per-band delta in `hsl.plan_band`, which stays continuous — only
  the final written value snaps): `autocorrect.py:552` (`_plan_seeds`, `cur + d`) and
  `autocorrect.py:433` (`_plan_embedded`, `d` directly, HSL anchor = 0). Step = 5.
- [x] **Q3 — Calibration.** Snap in `_calib_develop_dict` (`autocorrect.py:247-264`), the
  single function feeding both seeds mode (`autocorrect.py:564`) and embedded mode
  (`autocorrect.py:458`) — one call site to change covers both modes. Step = 5.
- [x] **Q4 — WB Temperature.** Snap at both final `Temperature=round(temp)` write sites:
  `_plan_embedded` (`autocorrect.py:406-410`) and `_plan_seeds` (`autocorrect.py:530-539`,
  after `wb_model.refine_temp_tint` if applied). Step = 250. `wb_model`'s internal ±600K delta
  clamp stays continuous — only the final absolute value snaps.
- [x] **Q5 — WB Tint.** Same two sites as Q4, alongside Temperature (`autocorrect.py:408` and
  `:537`). Step = 5.
- [x] **Q6 — `app/tests/test_quantize.py`.** Pure function tests: nearest-multiple rounding
  (positive/negative), half-step behaviour, boundary interaction (a value clamped to exactly
  -100/100/2000/12000/-150/150 must snap to itself, never drift outside the bound).
- [x] **Q7 — Update existing tests that assert specific numeric outputs.** At minimum
  `test_autocorrect_helpers.py` (`_calib_develop_dict` cases) and any `test_autocorrect_calib.py`
  case asserting a precise non-grid value — expected values change once snapping lands;
  `test_seed_match.py` (operates on `SeedTarget`, upstream of snapping) should be unaffected.
  - Step-level test: `python -m pytest app/tests -q` green with updated expectations; re-run S0
    — document the MAE delta introduced purely by quantization (expected, not a regression) so
    later stages (R/W/D/N) are evaluated against a Q-aware baseline, not a stale continuous one.

  **Done 2026-07-25.** `core/quantize.py` (`snap`) + 5 write sites in `autocorrect.py`, all
  passed a pre-clamped value (seeds-mode WB Temperature had no explicit clamp before — added
  one, matching Q1's own stated invariant). `python -m pytest app/tests -q`: **152 passed**
  (was 129; +23 from `test_quantize.py` + Q6/Q7 additions), no regressions.

  S0 re-run confirms the theory exactly: **seeds-mode LOOCV MAE is byte-for-byte unchanged**
  (it compares `seed_match` output directly against ground truth, upstream of any write-site
  snapping — as predicted). **Embedded-mode HSL MAE** (the only axis in S0 that goes through an
  actual `autocorrect.plan()` write site) picks up the expected quantization noise:
  Saturation 2.89→3.00, Luminance 3.60→3.62, Hue 1.89°→2.07° (n=3168 each). Exposure/Calibration/
  WB unchanged in the embedded run (Exposure stays continuous by design; Calibration/WB embedded
  MAE was already n=0/uncalibrated pre-Q, so quantization had nothing to bite on there yet).

---

## R — Fix the measurement scale (real bug + perf bonus)

- [x] **R1 — Common measurement grid, plugin side.** Send `width`/`height` in the
  `get_thumbnails` (`main_window.py:393`) and `render_probe`
  (`neutral_preview_worker.py:87-89`) payloads. No Lua change needed —
  `Thumbnails.lua:69-70,181-182` and `PollingLoop.lua:106-107,138-139` already accept these
  parameters.
- [x] **R2 — Downsample RAW/embedded JPEG to the same grid.** `cv2.resize` (INTER_AREA) before
  `sharp_mask`/`tone_stats`/`band_stats`/`neutral_stats`, in `gpu_raw.py` (after demosaic) and
  the equivalent `embedded_jpeg`/`gpu_schedule` path. Full-res RAW stays decoded for other
  uses; only the measurement changes scale.
- [x] **R3 — Resolution-proportional pre-Laplacian blur.** Sigma relative to the image
  diagonal (not a fixed sigma like `tools/cluster_sharp_zone.py:20`, which isn't
  scale-invariant) — secondary robustness against the exact resolution chosen.
- [x] **R4 — Bump `ANALYSIS_VERSION`** (`cache.py:61`, `"v6-calib-style-keys"` → new value) +
  full cache rebuild (930 photos).
- [x] **R5 — Validate the target resolution before locking it. Lr required.** Starting
  hypothesis: 1536-2048px long edge (near-zero drift from 2048 in the measurement taken). To
  confirm: (a) re-run the scale→drift curve on 4-5 real photos of varied content (portrait,
  landscape, low-key, high-key) — the current measurement comes from **a single photo**;
  (b) time the real Lr cost live (rendering at 2048 vs 512) before locking the number.
  - Test: new deterministic test (synthetic gradient+hard-edge image) in `app/tests/` asserting
    the sharp/global gap stays bounded at the chosen resolution, as a regression guard. After
    R4, re-run S0 — MAE must not regress; the cropped-photo-in-embedded-mode case (bug above)
    must lose its phantom delta.

  **Done 2026-07-25.** Implementation:
  - **R1**: `render_metrics.MEASURE_LONG_EDGE = 2048` (single source of truth) sent as
    `width`/`height` in both job payloads (`main_window.py`, `neutral_preview_worker.py`).
  - **R2**: `render_metrics_gpu.downsample_to_measure_grid` (torch `F.interpolate(mode="area")`
    — GPU-resident, mathematically equivalent to `cv2.INTER_AREA` for downsampling, avoids a
    CPU round-trip on the GPU-strict path per CLAUDE.md) folded into `_to_hwc_u8`, the single
    choke point shared by `analyze_rendered_gpu`/`analyze_rendered_gpu_dual` — covers
    `PreviewJPEG` (fresh render), `NeutralPreviewJPEG` (neutral anchor), `InCameraJPEG`
    (embedded, via `gpu_schedule`), and `calibrate_hsl_response.py`'s probe measurement in one
    place, for free. `gpu_raw.py` (RAW path, doesn't route through `_to_hwc_u8`) got an explicit
    second call: downsampled Lab/sharp-mask for `tone`/`bands` (the k-NN/comparison-facing
    fields) only — the existing full-res `exposure_sharp`/`grayworld_*_sharp` fields were left
    untouched (confirmed write-only/never-read-back via grep, same class of dead diagnostic data
    as the `delta_*` columns removed in W3 — not part of R2's scope). Never upsamples.
  - **R3**: `sharpness._blur_sigma(h, w) = 0.002 * hypot(h, w)`, applied before the Laplacian in
    both `sharp_mask` (numpy, `scipy.ndimage.gaussian_filter`) and `sharp_mask_gpu` (torch,
    hand-rolled separable conv2d — GPU-strict, no cv2/scipy round-trip). New
    `app/tests/test_sharpness.py` (8 tests, incl. 2 GPU-marked).
  - **R4**: `ANALYSIS_VERSION` bumped to `"v7-measure-grid"`. This does **not** eagerly rebuild
    anything — it only changes the hash salt, so `raw_signature`/`style_hash` mismatch on next
    access and each photo lazily re-measures the next time it goes through Analyze/Preview/Apply
    in the GUI. The literal "full cache rebuild (930 photos)" is therefore a consequence the user
    will see as slower first-touch passes over the catalog, not an eager batch job — no tool
    exists (or was built) to force it atomically, and forcing it wasn't attempted (real Lr
    session, real 930-photo cost, not something to trigger unilaterally).
  - **R5 (Lr required, live evidence gathered)**: bridge was connected during this session.
    (a) Scale→drift curve re-run on 5 real, varied photos (`ILCE-7M4`, mostly underexposed
    night-event frames, one moderate-key) at edges {512, 1024, 1536, 2048, 3072, 4096, native
    7028px} — **with R3's blur active, the sharp/global gap is now stable end-to-end from 512px
    all the way to native resolution** for every photo (e.g. +0.08 to +0.11 L* across the whole
    range for one frame, +4.67 to +4.70 for another) — the degenerate-toward-0 failure mode from
    the original single-photo evidence did not reproduce at any scale once R3 was in place. This
    means R3 alone already neutralizes the specific "sharp mask degenerates at native res"
    mechanism; R2 remains necessary regardless, for matching resolution *across* differently-
    scaled sources (RAW vs. embedded JPEG vs. Lr render), not just for within-source stability.
    (b) Live timing via the MCP `mcp.client` SDK against the running App
    (`http://127.0.0.1:5000/mcp`), 5 real selected photos: **cost is dominated by Lightroom's own
    preview-cache warm/cold state, not cleanly by the requested resolution** — a cold
    (regenerate-needed) request costs ~4s/photo *regardless of whether it was 512 or 2048*, while
    a warm (already-cached-at-that-quality) request costs ~0.1-0.3s/photo. Concretely: first 512
    call 1.0-1.5s/5 photos (warm from Lr's own filmstrip-size caching), first 2048 call 19.4s/5
    photos (~3.9s/photo, cold), but a *later* 512 call also hit 19.6s/5 photos once the cache
    slot had been displaced by the 2048 requests. This matches PLAN's own evidence for
    `render_probe` (~4s/photo) — raising `get_thumbnails` to 2048 means it now falls into that
    same regenerate-cost regime on first touch per photo per session, not a new, distinct
    bottleneck. Stopped further live probing at this point (bridge stayed healthy throughout,
    `get_thumbnails` never mutates develop settings — no risk to the user's catalog — but
    repeated automated Lr requests aren't free to run indefinitely against a live session).
    **Verdict: 2048 confirmed** — flat scale→drift curve at every resolution tested (no reason to
    prefer a smaller edge on drift grounds) and the timing cost is the same class of cost the
    plan already accepted for `render_probe`, not a new prohibitive one.

  `python -m pytest app/tests -q`: **163 passed** (was 152; +11 from `test_sharpness.py` and
  `test_measure_grid.py`), run with **no GPU marker filter** (`-m "not gpu"` dropped) since this
  session has a real CUDA GPU — all GPU-marked tests (parity + new sharp-mask GPU tests) ran for
  real, not skipped.

  S0 re-run is a **sanity check only, not a post-R baseline**: `validate_seed_matching.py` reads
  cached measurements via the hash-free `_latest` accessors (by design, cf. S0), so it serves the
  **old, pre-bump cached rows** untouched — `ANALYSIS_VERSION`'s bump doesn't retroactively
  change bytes already on disk, only what counts as fresh on the *next* access. Confirms the
  harness + `autocorrect.py`/`cache.py` changes stay wired correctly against the real catalog (no
  crash, 163/163 green) and the seed pool grew organically during this session (509→551 seeds,
  the user was actively marking references live) — MAE deltas seen (e.g. expo 4.38→4.30, temp
  123.62→119.80) are from that pool growth, not from R2/R3. **A genuine post-R baseline requires
  the user to re-run Analyze/Preview/Apply over the catalog live** (real 930-photo cost, Lr
  required) — not run here.

---

## W — Make the embedded JPEG's potential actually work

- [x] **W1 — `app/tools/calibrate_wb_response.py`.** Same pattern as
  `calibrate_hsl_response.py`: headless server, probes `render_probe` with known
  `Temperature`/`Tint` deltas, measures `neutral_stats` before/after, regression → `WBResponse`
  (2×2 Jacobian `da_dtemp/db_dtemp/da_dtint/db_dtint`), saved via `response.save`.

  **Done 2026-07-25.** Anchors on the As Shot Temperature/Tint (numeric readback after
  `WhiteBalance='As Shot'`, same mechanism as `gui.neutral_preview_worker` — physically the
  right anchor since `_plan_embedded` applies its correction from that same point), then probes
  `Temperature` and `Tint` independently around it and fits each of the 4 slopes via the
  existing `response.fit_linear_response`. Guards a probe's `neutral_frac` before trusting its
  a*/b* reading (`_MIN_NEUTRAL_FRAC=0.01`) — a probe on a mostly non-neutral crop is skipped and
  logged, never silently folded into the fit. Aborts (does not save) if fewer than 2 usable
  probes per axis. Import-sanity-checked only (see W2 — could not run it end-to-end this
  session).

- [x] **W2 — Run the real calibrations. Lr required.** HSL (already scripted) + WB (new) for
  the catalog's 2 (camera, profile) pairs: `ILCE-7M4|IN`, `ILCE-7M4|Neutral`. **After R** (not
  before — avoid calibrating on the old grid and having to redo it).

  **Done 2026-07-25** (retried after the earlier bridge/App drop — user relaunched Lightroom +
  the App). Since a second `calibrate_hsl_response.py`/`calibrate_wb_response.py` process can't
  bind port 5000 alongside the already-running `app.main`, both calibrations were driven via the
  MCP client SDK against the live App (same pattern as R5's timing script) — **identical math**
  (`response.fit_linear_response`, `response.save`), only the job-submission transport differs.
  `render_probe` does a per-adjustment UUID lookup (`findPhotoByUuid` fallback) independent of
  Lr's GUI selection, so reference photos were picked by `photo_id`/scored for neutral content
  programmatically rather than requiring the user to click through the grid:
  - **`ILCE-7M4|IN`** — `SML03337.ARW`, neutral_frac≈0.057 (good). WB:
    `WBResponse(da_dtemp=+0.523, db_dtemp=+0.992, da_dtint=+0.208, db_dtint=-0.193)`. HSL: only
    **Red/Orange** had genuine content in this single photo (all other bands: 0 reliable probes,
    left on the nominal fallback — expected, one photo can't exercise all 8 hue bands without a
    color-chart target).
  - **`ILCE-7M4|Neutral`** — `SML04237.ARW`, neutral_frac≈0.012 (**marginal** — this catalog is
    almost entirely dark nightclub/event photos; scored across 8 brightness-ranked candidates,
    this was the best available, still barely above the tool's own `_MIN_NEUTRAL_FRAC=0.01`
    guard, and the Tint axis only got 3/5 usable probes). WB:
    `WBResponse(da_dtemp=-0.088, db_dtemp=+0.358, da_dtint=+0.111, db_dtint=-0.138)` — treat with
    more skepticism than the IN result given the weak neutral sample (~7000 px). HSL: Red,
    Orange, Yellow, Aqua calibrated; Green/Blue/Purple/Magenta still nominal.

  **Two real bugs found and fixed along the way** (both would have made this calibration inert):
  1. `gui.autocorrect_worker.py` looked up the response model via
     `current_develop["CameraProfile"]` (Lr's **DCP** color profile — in this catalog, the
     constant string `"Camera FL"` for all 930 photos, verified) instead of `PhotoMeasure.
     profile_capture` (the **in-camera creative profile**, `"IN"`/`"Neutral"` — what
     `calibrate_hsl_response.py`/`calibrate_wb_response.py` actually key their saved files by,
     via `exif_profile.read_capture_profiles`). Two different axes entirely, silently
     mismatched — a calibration would have been produced but **never loaded** by the real
     correction pipeline on any catalog with a uniform DCP profile. Fixed (one-line swap +
     explanatory comment).
  2. `validate_seed_matching.run_embedded_validation` never loaded/passed a `model` into
     `autocorrect.plan()` at all (always `None`) — S0 would have kept reporting "not calibrated"
     forever regardless of real calibration state. Fixed: measures now group by
     `(exif_camera, profile_capture)` and each group loads its own `response.load(...)`, mirroring
     the (now-fixed) production lookup. Also fixed a related ambiguity: `wb_temp_pairs` only
     counted photos where a correction was actually **written**, conflating "no calibration" with
     "calibrated but within the dead zone, nothing to write" — now a calibrated-but-conforming
     photo counts with its implicit zero-delta prediction (consistent with how Exposure/HSL are
     already compared), and `wb_n_uncalibrated` means only "no calibration exists for this
     photo's group." New regression test
     (`test_run_embedded_validation_uses_response_keyed_by_profile_capture`) locks in the fix.

  Real photo counts in the catalog: 26 photos at `IN`, 904 at `Neutral`. `response_cache/` now
  has `ILCE-7M4__IN.json` and `ILCE-7M4__Neutral.json`. `python -m pytest app/tests -q`:
  **195 passed** (was 194). S0 re-run confirms the WB axis genuinely fires now — see W5.

- [x] **W3 — Remove `_compute_deltas`.** Decision made: keep the raw embedded-JPEG transplant
  (`ignore_bias=True`). Remove `autocorrect_worker.py:63-100` and the 4 `delta_*` columns in
  `InCameraJPEG` (`cache.py:205-208`) — confirmed dead code, never read back.

  **Done 2026-07-25.** Removed `_compute_deltas` and its call site (`autocorrect_worker.py`),
  the 4 `delta_*` columns from `InCameraJPEG`'s schema, and the matching kwargs from
  `put_in_camera_jpeg`/`_in_camera_jpeg_dict`/`_delta_bands_to_json` (the last now fully unused,
  removed too). This is a **structural** schema change → `cache.SCHEMA_VERSION` bumped 4→5,
  which per `cache.py`'s own design means the **real, live 930-photo/551-seed cache gets
  DROPPED and recreated on next app launch** (no row-by-row migration, by design) — confirmed
  with the user before doing it (asked explicitly: defer vs. bump now); user chose to bump now.
  Verified on a throwaway cache: `InCameraJPEG` no longer has the 4 columns, `PRAGMA user_version
  == 5`. The real catalog's cache has **not** been touched yet (only wipes lazily, on next
  `open_cache()` call from the GUI/a tool) — the user should expect a full re-analyze next launch.

- [x] **W4 — Revisit `_MAX_LUM_EMBEDDED_RAW`/`_MAX_HUE_EMBEDDED_RAW`** (`hsl.py:45-46`) once W2
  is done — currently a double guard (guessed nominal gain + tight cap) that may over-dampen
  the correction once real gains are available.

  **Done 2026-07-25 — evidence-based decision: caps left unchanged.** The premise behind this
  step was that the nominal gains (`_NOM_DL_DLUM=0.4`, `_NOM_DHUE_DHUE=0.35`) were probably
  *pessimistic* guesses, and once real (presumably stronger) gains landed, the tight
  `_MAX_LUM_EMBEDDED_RAW=10`/`_MAX_HUE_EMBEDDED_RAW=8` caps would become the redundant, over-
  restrictive half of the "double guard." **The real W2 data shows the opposite**: every
  measured Luminance/Hue gain across both profiles is *weaker* than its nominal prior, not
  stronger —
  | Axis | Nominal | Real range (6 calibrated bands) | Ratio |
  |---|---|---|---|
  | `dl_dlum` (Luminance→L*) | 0.40 | 0.023 – 0.076 | **5–17× weaker** |
  | `dhue_dhue` (Hue→°) | 0.35 | 0.037 – 0.159 | **2–9× weaker** |
  | `dchroma_dsat` (Saturation→C*, uncapped-embedded axis, for context) | 0.60 | 0.024 – 0.424 | 1.4–25× weaker |

  A weaker real gain means `plan_band`'s `dl / gain` division now predicts an even *larger*
  slider delta than the nominal fallback did for the same target L*/hue gap — the caps bind
  **more** often post-calibration, not less. HSL Luminance sliders being subtle in practice is a
  well-known Lr behavior, consistent with this. Raising the caps now would mean chasing a
  physically-accurate-but-large predicted delta with a bigger, more visually jarring slider
  swing — exactly what the caps' own stated rationale ("the JPEG has its own color science… we
  don't want to copy it wholesale") argues against. **Conclusion: the caps are doing their job,
  not a stale artifact of an inaccurate nominal prior — left as-is.** (Coverage caveat: only
  Red/Orange/Yellow/Aqua have real numbers at all; Green/Blue/Purple/Magenta still run on the
  nominal fallback on both profiles, cf. W2.)

- [x] **W5 — `test_exposure.py` + `test_wb_model.py`.** Close the test-coverage gap (guards on
  `plan_from_render`/`_headroom_factor`/`_MAX_STEP_EV`, no-op branches of
  `refine_temp_tint`/`calibrate`) — currently absent, already flagged as backlog item 4 in
  `OLD_PLAN.md`.
  - Step-level test: re-run S0 (embedded branch) — the WB axis must now write corrections on
    deviant photos (instead of 100% `n_uncalibrated`), embedded MAE improved vs. the R baseline.

  **Done 2026-07-25.** `test_exposure.py` (14 tests: headroom decay/floor, dead-zone-free
  clamping to `±max_step_ev`, accumulation on `current_exposure`, highlight/shadow headroom
  attenuation direction-specific, calibrated-response usage, multi-sample skip-on-`None`) +
  `test_wb_model.py` (17 tests: `slope_for_camera` known/unknown, `calibrate`'s median-offset/
  robustness/single-seed/empty-seed behavior, `predict_temperature` bounds, `refine_temp_tint`'s
  3 no-op branches + delta/final-value clamping). 31 new tests.

  **S0 criterion now genuinely met** (after W2 landed + the two bugs above got fixed): embedded
  WB axis went from `wb: 0 corrected, ... WB response not calibrated` (100% uncalibrated) to
  `wb: 3 corrected, 393 matching (nothing written)` with `wb_calibrated=True`, and — the part
  that actually matters for judging quality — **WB Temperature MAE is now a real, measured
  number**: 175.67K (n=260, up from n=0/`n/a`), Tint MAE 4.02 (n=260). Worse than seeds-mode WB's
  118.29K, which is expected: embedded-mode WB is anchored on a single marginal-neutral-content
  reference photo (`SML04237`, W2) rather than seeds-mode's 575-seed pool. `python -m pytest
  app/tests -q`: **195 passed** (was 163 pre-W). Seed pool kept growing live during this session
  (575 seeds now, up from 551) — HSL/expo/calib MAE moved slightly from that, not from any code
  change in this step.

---

## D — Colour-composition-aware comparison

- [ ] **D1 — Add composition features to `seed_match._distance`.** Per-band `frac` (already
  cached in `hsl_sharp`/`hsl_global`) — zero new decode.
- [ ] **D2 — Settle `_CALIB_SPREAD_MAX=25`** (`seed_match.py:251`) with the real spread data
  (S0 + real catalog) — replaces the provisional value that was never settled.
- [ ] **D3 — Adaptive k-means palette (conditional).** Only if D1 plateaus the MAE. Prior art:
  `tools/cluster_sharp_zone.py` (never concluded), timed here at ~15 ms/photo — negligible
  next to the Lr cost. Do not build before D1 is proven insufficient.
  - Step-level test: re-run S0 — the composition-aware distance must improve MAE, or the
    report says so honestly and D1 is not kept as-is.

---

## N — Per-axis / per-band k-NN + confidence gate

- [ ] **N1 — Generalize `_distance`** to accept a per-feature weight (`dict[str, float]`)
  instead of the current uniform weighting. Add `k_nearest_weighted(target, seeds, weights,
  k=None)`.
- [ ] **N2 — Separate k-NN per HSL band.** Instead of one global match then averaging whichever
  bands those seeds happen to have (current `_weighted_bands`), run a per-band k-NN adding that
  band's chroma/hue/frac as a distance feature — implements "if a photo renders reds better,
  pick it for reds." `target_from_seeds` moves from a single shared `matches` list to
  `{band_name: matches}` for bands; Temp/Tint/Calibration stay on the global match (consistent
  with the earlier decision: Calibration follows the overall scene, not a single band).
  Skin/subject approximation: heavy weighting on Red/Orange + a dedicated lightness window on
  `neutral_stats`, no segmentation needed (reserved for G1 if proven necessary).
- [ ] **N3 — Confidence gate.** Expose the nearest seed's distance as `SeedTarget.confidence`
  (or `low_confidence: bool` + raw distance), threshold calibrated by percentile of the pool's
  internal distances (relative to the catalog), refined using S0 (which distance correlates
  with high LOOCV error).
- [ ] **N4 — Surface confidence in the GUI.** Distinct visible line in `main_window.py`, not
  buried in the current scrolling notes list.
  - Step-level test: re-run S0 in per-band mode — per-band MAE improved vs. single global
    matching; the confidence flag correlates with the real error measured in LOOCV.

---

## G — AI segmentation (gated)

- [ ] **G1 — Decision gate.** Do not build until S0 (re-run after R/W/D/N) shows residual error
  concentrated on content-mixing or cropped-photo cases. The measurement-scale fix (R) explains
  much of the "sharp zone unreliable" problem — segmentation is speculative until proven
  insufficient.
  - If the evidence arrives: lightweight semantic segmentation (SegFormer-B0/PP-LiteSeg for
    sky/subject/foliage + a dedicated face-parsing model for skin) rather than MobileSAM
    (class-agnostic, no usable labels to target HSL per zone).
  - Constraint: plugin self-sufficiency (`bootstrap.ps1`, CLAUDE.md) — model weight download on
    first launch, install-size cost, and CPU-only inference time must be explicitly quantified
    before any implementation.

---

## Mapping to the 4 user goals — plumbing vs. new modelling

| Goal | Nature of the work | Steps |
|---|---|---|
| 1. Global vs Sharp, is segmentation worth it? | Plumbing (real scale bug) + evidence before modelling | R (fix), G1 (gate) |
| 2. Colour composition per band + overall | Near-free plumbing (data already cached) | D |
| 3. Embedded JPEG's potential | Plumbing + one new tool of the same nature as an existing one | W |
| 4. Mix the best of each seed per axis | Data-structure refactor, not a new ML algorithm | N |

None of the 4 goals require new ML modelling for the bulk of the gain. Only segmentation (G1)
is a genuinely new dependency/model, explicitly gated on evidence from the catalog's own seeds
rather than intuition.

---

## Risks

- Scale→drift curve measured on **a single photo** — R5 confirms it on several before locking.
- `ANALYSIS_VERSION` bump = full cache rebuild (930 photos) — real time cost to budget.
- Raising the thumbnail size requested from Lightroom may raise its render time — to be timed
  live (R5), not assumed.
- WB/HSL calibration needs a connected Lightroom session (W2, like the previous plan's H2,
  never run for real).
- LOOCV over-generalization bias within a scene block — mitigated by S0's stratification, not
  eliminated.
- Doc desync: `ARCHITECTURE.md` §4 (the "sharp = subject" claim) and §8 need updating after R.

## Verification

- `python -m pytest app/tests -q` green at every step.
- `app/tools/validate_seed_matching.py` (S0) is the primary evidence — before/after output at
  every step.
- R5: deterministic synthetic test kept as a regression guard in `app/tests/`.
- After R/W: live smoke test via GUI Preview (or `abelr` MCP tools if the App is running) on a
  small real selection, before triggering the full 930-photo cache rebuild.
