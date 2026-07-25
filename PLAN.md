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

- [ ] **S0 — `app/tools/validate_seed_matching.py`.** Same pattern as
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

- [ ] **Q1 — Central snap helper.** New pure function (e.g. `core/quantize.py`, or a small
  addition next to `hsl._clamp`): `snap(value: float, step: float) -> int` =
  `step * round(value / step)`. All 5 write sites below replace their current `round(...)` with
  `snap(..., step)`. Snap is applied **after** clamping to slider bounds, never before — the
  bounds in play (-100/100, -150/150, 2000/12000) are themselves exact multiples of their
  respective steps (5, 5, 250), so snapping post-clamp can never push a value back out of range.
- [ ] **Q2 — HSL.** Snap the two points where an *absolute* slider value is assembled and
  written (not the internal per-band delta in `hsl.plan_band`, which stays continuous — only
  the final written value snaps): `autocorrect.py:552` (`_plan_seeds`, `cur + d`) and
  `autocorrect.py:433` (`_plan_embedded`, `d` directly, HSL anchor = 0). Step = 5.
- [ ] **Q3 — Calibration.** Snap in `_calib_develop_dict` (`autocorrect.py:247-264`), the
  single function feeding both seeds mode (`autocorrect.py:564`) and embedded mode
  (`autocorrect.py:458`) — one call site to change covers both modes. Step = 5.
- [ ] **Q4 — WB Temperature.** Snap at both final `Temperature=round(temp)` write sites:
  `_plan_embedded` (`autocorrect.py:406-410`) and `_plan_seeds` (`autocorrect.py:530-539`,
  after `wb_model.refine_temp_tint` if applied). Step = 250. `wb_model`'s internal ±600K delta
  clamp stays continuous — only the final absolute value snaps.
- [ ] **Q5 — WB Tint.** Same two sites as Q4, alongside Temperature (`autocorrect.py:408` and
  `:537`). Step = 5.
- [ ] **Q6 — `app/tests/test_quantize.py`.** Pure function tests: nearest-multiple rounding
  (positive/negative), half-step behaviour, boundary interaction (a value clamped to exactly
  -100/100/2000/12000/-150/150 must snap to itself, never drift outside the bound).
- [ ] **Q7 — Update existing tests that assert specific numeric outputs.** At minimum
  `test_autocorrect_helpers.py` (`_calib_develop_dict` cases) and any `test_autocorrect_calib.py`
  case asserting a precise non-grid value — expected values change once snapping lands;
  `test_seed_match.py` (operates on `SeedTarget`, upstream of snapping) should be unaffected.
  - Step-level test: `python -m pytest app/tests -q` green with updated expectations; re-run S0
    — document the MAE delta introduced purely by quantization (expected, not a regression) so
    later stages (R/W/D/N) are evaluated against a Q-aware baseline, not a stale continuous one.

---

## R — Fix the measurement scale (real bug + perf bonus)

- [ ] **R1 — Common measurement grid, plugin side.** Send `width`/`height` in the
  `get_thumbnails` (`main_window.py:393`) and `render_probe`
  (`neutral_preview_worker.py:87-89`) payloads. No Lua change needed —
  `Thumbnails.lua:69-70,181-182` and `PollingLoop.lua:106-107,138-139` already accept these
  parameters.
- [ ] **R2 — Downsample RAW/embedded JPEG to the same grid.** `cv2.resize` (INTER_AREA) before
  `sharp_mask`/`tone_stats`/`band_stats`/`neutral_stats`, in `gpu_raw.py` (after demosaic) and
  the equivalent `embedded_jpeg`/`gpu_schedule` path. Full-res RAW stays decoded for other
  uses; only the measurement changes scale.
- [ ] **R3 — Resolution-proportional pre-Laplacian blur.** Sigma relative to the image
  diagonal (not a fixed sigma like `tools/cluster_sharp_zone.py:20`, which isn't
  scale-invariant) — secondary robustness against the exact resolution chosen.
- [ ] **R4 — Bump `ANALYSIS_VERSION`** (`cache.py:61`, `"v6-calib-style-keys"` → new value) +
  full cache rebuild (930 photos).
- [ ] **R5 — Validate the target resolution before locking it. Lr required.** Starting
  hypothesis: 1536-2048px long edge (near-zero drift from 2048 in the measurement taken). To
  confirm: (a) re-run the scale→drift curve on 4-5 real photos of varied content (portrait,
  landscape, low-key, high-key) — the current measurement comes from **a single photo**;
  (b) time the real Lr cost live (rendering at 2048 vs 512) before locking the number.
  - Test: new deterministic test (synthetic gradient+hard-edge image) in `app/tests/` asserting
    the sharp/global gap stays bounded at the chosen resolution, as a regression guard. After
    R4, re-run S0 — MAE must not regress; the cropped-photo-in-embedded-mode case (bug above)
    must lose its phantom delta.

---

## W — Make the embedded JPEG's potential actually work

- [ ] **W1 — `app/tools/calibrate_wb_response.py`.** Same pattern as
  `calibrate_hsl_response.py`: headless server, probes `render_probe` with known
  `Temperature`/`Tint` deltas, measures `neutral_stats` before/after, regression → `WBResponse`
  (2×2 Jacobian `da_dtemp/db_dtemp/da_dtint/db_dtint`), saved via `response.save`.
- [ ] **W2 — Run the real calibrations. Lr required.** HSL (already scripted) + WB (new) for
  the catalog's 2 (camera, profile) pairs: `ILCE-7M4|IN`, `ILCE-7M4|Neutral`. **After R** (not
  before — avoid calibrating on the old grid and having to redo it).
- [ ] **W3 — Remove `_compute_deltas`.** Decision made: keep the raw embedded-JPEG transplant
  (`ignore_bias=True`). Remove `autocorrect_worker.py:63-100` and the 4 `delta_*` columns in
  `InCameraJPEG` (`cache.py:205-208`) — confirmed dead code, never read back.
- [ ] **W4 — Revisit `_MAX_LUM_EMBEDDED_RAW`/`_MAX_HUE_EMBEDDED_RAW`** (`hsl.py:45-46`) once W2
  is done — currently a double guard (guessed nominal gain + tight cap) that may over-dampen
  the correction once real gains are available.
- [ ] **W5 — `test_exposure.py` + `test_wb_model.py`.** Close the test-coverage gap (guards on
  `plan_from_render`/`_headroom_factor`/`_MAX_STEP_EV`, no-op branches of
  `refine_temp_tint`/`calibrate`) — currently absent, already flagged as backlog item 4 in
  `OLD_PLAN.md`.
  - Step-level test: re-run S0 (embedded branch) — the WB axis must now write corrections on
    deviant photos (instead of 100% `n_uncalibrated`), embedded MAE improved vs. the R baseline.

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
