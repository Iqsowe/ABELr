# PLAN — Cleanup & hardening (ARCHIVED 2026-07-19)

> Archived in favor of [`PLAN.md`](PLAN.md), which prioritizes the HSL/Calibration fixes
> (2026-07-19 audit). Steps 2-7 and the backlogs below are not abandoned — deferred, see §
> "Cleanup backlog (deferred)" of the new PLAN.md.

Executable roadmap **one step at a time**. Technical context: [`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md).
Working rules: [`CLAUDE.md`](CLAUDE.md).

## Execution rules (Sonnet 5)

1. Do **one step only** at a time, in order.
2. For each step: **implement the regression test BEFORE/WITH the change**.
3. Running `python -m app.main` is not required; validate with: `python -m pytest app/tests -q`
   (must stay **green**, including existing tests).
4. **Check `- [ ]` → `- [x]` only after a green test.** Never check off an unvalidated step.
5. Do not add a CPU fallback (GPU-strict). Do not touch live behavior without a test that covers it.
6. If a step breaks an existing test with no legitimate reason: stop, don't check it off, flag it.

Verified starting state (2026-07-05): 6 test files, ~40 tests, a single `@pytest.mark.gpu`,
**no pytest config** (the `gpu` marker registered only in `conftest.py`).

---

## Steps

- [x] **1 — Remove dead code.** *(done 2026-07-18, Fable 5 review — `test_no_dead_modules.py` green)*
  Remove `app/gui/analysis_worker.py` (`AnalysisWorker` never instantiated nor imported — verified).
  Note (Fable 5 review, Pass 0): `analysis_worker` is the only direct GUI importer of
  `gpu_raw`/`raw` — their live status hangs on the `gpu_schedule`/`embedded_jpeg` chain.
  Removing it kills no core module, but makes `gpu_schedule` the sole entry point into `gpu_raw`.
  - *Prior check*: `grep -rn "analysis_worker\|AnalysisWorker" app` returns only the definition.
  - *Regression test*: create `app/tests/test_no_dead_modules.py` which imports every module in
    `app/core/*` and `app/gui/*` (smoke import, except those requiring a Qt display →
    `importlib` with documented tolerance) and **asserts** that `app.gui.analysis_worker` no
    longer exists.
  - *Validate*: `python -m pytest app/tests -q` green.

- [ ] **2 — Pytest config.**
  Add a `pyproject.toml` at the root with `[tool.pytest.ini_options]`: `testpaths = ["app/tests"]`,
  `markers = ["gpu: GPU/CPU parity, skip if CUDA absent"]`. Keep the `gpu` skip from `conftest.py`.
  - *Test*: `python -m pytest -q` (no path) from the root correctly discovers the tests;
    `python -m pytest -q -m "not gpu"` works. No test broken.

- [ ] **3 — Anti-desync guard for the docs.**
  Create `app/tests/test_docs_consistency.py`: parse `CLAUDE.md` and `documentation/ARCHITECTURE.md`,
  and **assert** that (a) every cited `core/xxx.py` / `gui/xxx.py` actually exists on disk, (b)
  no removed file (`core/seeds.py`, `core/adjustments.py`, `core/prediction.py`,
  `gui/analysis_worker.py`) is presented as alive.
  - *Test = the test itself*: `python -m pytest app/tests/test_docs_consistency.py -q` green.

- [ ] **4 — Cover the untested live modules (pure functions only, no GPU or RAW).**
  Add tests for:
  - `core/exposure.py` — ΔEV from current L* to target L* (bound, sign, monotonicity).
  - `core/hsl.py` — per-band deltas vs. target; **saturation = reduction only** (never an increase).
  - `core/analysis.py` — `ev100()` / `ExposureStats` (known values).
  - `core/autocorrect.py` — `plan()` on synthetic `PhotoMeasure` objects (no disk/GPU access).
  - *Regression test*: new tests green; the ~40 existing tests unchanged.
  - *Note*: if a function isn't pure (depends on GPU/RAW), leave it out of scope and note it
    in the test — do not fabricate a mock that invents behavior.

- [ ] **5 — Logging hygiene.**
  In `app/gui/neutral_preview_worker.py`, replace `except Exception: pass` with a log
  (`logging` module, `warning`/`exception` level). If the anchor logic isn't already pure,
  isolate `_anchor_suspect` into a testable function.
  - *Regression test*: unit test of `_anchor_suspect` on edge cases (sound anchor vs. suspect) —
    numeric inputs, no Qt. `python -m pytest app/tests -q` green.

- [ ] **6 — QThread cleanup.**
  In `app/gui/main_window.py`, call `quit()` + `wait()` on the workers on close
  (`closeEvent`) to avoid orphaned threads.
  - *Test*: **limited, honest coverage** — smoke `python -c "import app.gui.main_window"` with
    no import crash (to be documented as such in the step). Real GUI validation → Appendix.

- [ ] **7 — Resync the final docs.**
  After 1-6, update `documentation/ARCHITECTURE.md` (remove `analysis_worker` from the map,
  §8) and `CLAUDE.md` (add the pytest config to the workflow if relevant).
  - *Test*: `test_docs_consistency` (step 3) stays green after the edit.

---

## Appendix — Lightroom-dependent validation (manual if no MCP)

Not automatable without Lr open (or without an exposed Lr MCP) — **outside the checkable boxes**:

- `requestJpegThumbnail` lock: 2nd Preview convergence ≈ 0; silent `_anchor_suspect` guard.
- Real e2e test: seeds → "Mark + analyze references" (`usable pool: N/N`) → select
  the rest → check axes → "Preview" (`seeds` mode, listed deltas) → "Apply" → re-Preview ≈ 0.
- Finishing the `photo_panel.py` / `analysis_panel.py` GUI stubs (previews, histograms, WB outliers).

## Remaining backlog (out of cleanup scope)

- **Camera calibration ("calib" axis)** — delivered 2026-07-19: k-NN transplant (ShadowTint,
  Red/Green/Blue Hue+Saturation) from the seeds, like Temperature/Tint, active in both
  reference modes (seeds and embedded — no measurable target from a JPEG, so always
  seed-sourced on the embedded side). `ANALYSIS_VERSION` bumped (`v6-calib-style-keys`,
  keys added to `_STYLE_KEYS`) + Lua `DEVELOP_KEYS` (+8). Tests: `test_autocorrect_calib.py`,
  aggregation in `test_seed_match.py`, clamp/dict in `test_autocorrect_helpers.py`.
  **Not validated in real Lightroom** (no Lr in this environment) — to be checked manually
  (appendix below): that `EnableCalibration` applies correctly, that the current seeds don't
  yet have hand-edited Calibration values (transplant will stay empty until a seed has
  been edited in the Camera Calibration panel).
- Artistic regime fallback: `core/regime.py` is no longer consulted by the live (k-NN) path. To
  be revalidated if matching turns out unstable on small seed pools.
- `core/image_source.py`: tool-only — remove it if the `tools/` that use it get archived.
- Perf: GPU + cache are in place, but Pass 2 (REVIEW_FABLE5.md) identifies structural
  losses (no CPU/GPU overlap, double rawpy open, undersized JPEG waves).
  Profile (`py-spy`/`torch.profiler`) then address G7 before considering Rust.

## Fable 5 review backlog (2026-07-18) — 🟠 items (detail: documentation/REVIEW_FABLE5.md, Pass 3)

2026-07-18 implementation: **all 🟠 items + most of the 🟡/⚪ delivered** (see
Pass 4 Journal of REVIEW_FABLE5.md). Tests: 78/78 green, GPU parity revalidated.

- [x] **G3 — Harden `Thumbnails.lua`** (L-01/L-02/L-03): `requestJpegThumbnail` return values
  retained, unique filename per call (generation), restore failures surfaced
  (`restore_error` up to the GUI).
- [x] **B-01 — Verify `_pending_ids` before Apply**: re-fetch selection, re-plan if there's a mismatch.
- [x] **G2 — exiftool argfile** (A-01/A-02): temp UTF-8 `-@ argfile` + `encoding="utf-8"`.
- [x] **G1 — Bump the shared `ANALYSIS_VERSION`** (DB-01/C-01): `DEVELOP_KEYS` 44→71 keys,
  `_STYLE_KEYS` fixed (real SDK names `SplitToning*`) + Texture/ToneCurve/Parametric,
  `cam_mul[G2]==0` guard — single bump `v5-style-keys-g2wb` (cache rebuild on 1st run).
- [x] **G7 — GPU scheduler overhaul** (P-01/P-02/P-03, +P-06): `process_combined_batch`
  (unified unpack, double-buffer, per-pipeline waves, reactive `empty_cache`, uint16 H2D).
  Parity revalidated (`validate_gpu_vs_libraw`: exposure corr 1.000, gray-world 0.996-0.9995).

Remaining (not addressed, scope decision):

- [ ] **G9 — GPU metrics micro-pass** (P-04/P-05): hoist hue/sat/chroma out of the dual,
  group the scalar syncs. **Bit-exact parity required** (otherwise bump `ANALYSIS_VERSION`) —
  to be done backed by profiling (`torch.profiler`).
- [ ] **P-10 — `_probe_chunk` decodes one at a time**: consistency more than perf (dominated by
  Lr rendering); batch via `analyze_render_blobs` at some point.

#################################################################

# PLAN — Hardening HSL & Calibration (ARCHIVED 2026-07-25)

Executable roadmap **one step at a time**. Technical context: [`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md).
Working rules: [`CLAUDE.md`](CLAUDE.md). Cleanup history (steps 1-7, perf/Fable5 backlogs):
[`OLD_PLAN.md`](OLD_PLAN.md).

## Origin

2026-07-19 audit of the HSL axes (`core/hsl.py`) and camera Calibration (`core/seed_match.py` +
`autocorrect._calib_develop_dict`): 2 flaws on HSL (RAW guard never wired, slider gain
never calibrated — always nominal), 3 gaps on Calibration (not validated in real Lr, no
k-NN consistency guard, scene-dependent matching distance not settled). Full detail
in the 2026-07-19 conversation (to be carried over into `documentation/ARCHITECTURE.md`
§ Limitations once fixed).

## Execution rules (Sonnet 5)

1. Do **one step only** at a time, in order.
2. For each step: **implement the regression test BEFORE/WITH the change**.
3. Validate with `python -m pytest app/tests -q` (must stay **green**, including existing tests).
4. **Check `- [ ]` → `- [x]` only after a green test.** Never check off an unvalidated step.
5. Do not add a CPU fallback (GPU-strict). Do not touch live behavior without a test that covers it.
6. If a step breaks an existing test with no legitimate reason: stop, don't check it off, flag it.
7. Steps marked **⚠️ Lr required**: not automatable here (no Lr in this environment) —
   implement + test whatever is testable without Lr, document the manual part in the appendix.

---

## Steps — HSL

- [x] **H1 — Wire up or remove the `raw_oversat` guard.** ✅ Wired (2026-07-19): new
  `hsl.raw_confirms_oversat(raw_band)` (reuses the `_SAT_CLIP_TRIGGER` threshold, no invented
  threshold); `PhotoMeasure.raw_bands` added (sharp-zone RAW, `SourceRAW.hsl_sharp` cache
  already present but never forwarded); wired into `_embedded_band_targets` and
  `_band_targets_from_seed_match` (RAW of the **target** photo, not of the seeds); the worker
  (`autocorrect_worker.py`) feeds `raw_bands=raw_d.get("bands")`. Tests:
  `test_hsl.py` (8 cases plan_band + raw_confirms_oversat) and 5 wiring cases in
  `test_autocorrect_helpers.py`. `python -m pytest app/tests -q`: 103 passed.
  `hsl.BandTarget.raw_oversat` is documented as an anti-overcorrection guard ("only reduce a
  band's saturation if the RAW confirms it is loaded at capture") but no caller
  (`autocorrect._embedded_band_targets`, `_band_targets_from_seed_match`) was setting it —
  `raw_blocks` was always `False`, a dead guard.
  Decision to make first: **wire it up** (measure the band's chroma on the sharp-zone RAW,
  `sharpness`/`analysis`, compare to a threshold) or **remove it** (field + doc + docstring) if
  deemed out of scope. Don't leave a documented safeguard that protects nothing.
  - *Regression test*: if wired, `app/tests/test_hsl.py` case `raw_oversat=False` correctly
    blocks a saturation reduction even with excess measured chroma. If removed: `grep -rn
    "raw_oversat" app` returns nothing outside git history.
  - *Validate*: `python -m pytest app/tests -q` green.

- [x] **H2 — `render_probe` probing to calibrate `ResponseModel.bands`.** ✅ (2026-07-19):
  `core.response.fit_linear_response(deltas, measured)` — pure linear regression (free
  intercept, least squares); falls back to `0.0` (uncalibrated) if <2 samples or deltas aren't
  spread out. Tool `tools/calibrate_hsl_response.py`: starts the App server (headless, no
  GUI — same process as `app.main`, required because `job_queue` is an in-process singleton),
  waits for the bridge, takes the photo selected in Lr, probes each band × axis
  (Saturation/Luminance/Hue) delta by delta via sequential `render_probe` jobs (no
  batching — avoids assuming an unconfirmed result ordering), measures via GPU
  (`render_metrics_gpu.analyze_rendered_gpu`), fits, `response.save()`. Hue is unwrapped
  (circular) before fitting. Tests: `test_response.py` — 5 cases for `fit_linear_response`
  (positive slope, negative slope, <2 samples, non-spread deltas, mismatched lengths) on
  synthetic data. `python -m pytest app/tests -q`: 108 passed.
  - *Regression test*: the pure fitting function is tested on synthetic data (known slider
    delta → expected slope). No Lr test here (⚠️ Lr required for the real probing — not run,
    no Lr environment available here; script ready, to be run manually).
  - *Validate*: `python -m pytest app/tests -q` green.

- [x] **H3 — Guard on the embedded transplant (luminance/hue).** ✅ (2026-07-19):
  `BandTarget.embedded_raw: bool = False` — marks a raw in-camera JPEG transplant target
  (`ignore_bias=True`). `hsl.plan_band` applies a dedicated, stricter cap when
  `embedded_raw=True`: `_MAX_LUM_EMBEDDED_RAW=10` (vs `_MAX_LUM=20`),
  `_MAX_HUE_EMBEDDED_RAW=8` (vs `_MAX_HUE=15`) — saturation already had its reduction-only
  guard, L*/hue had none. Wired only into `_embedded_band_targets(ignore_bias=True)`; the
  historical mode (`ignore_bias=False`, delta vs. bias norm) and
  `_band_targets_from_seed_match` (already-validated k-NN targets, not a raw transplant) keep
  `embedded_raw=False` by default → no regression.
  Tests: `test_hsl.py` (3 cases — strict cap active, nominal cap unchanged by default,
  luminance and hue) and `test_autocorrect_helpers.py` (2 cases — wiring
  `ignore_bias=True/False`, non-wiring for seed-match). `python -m pytest app/tests -q`:
  113 passed.
  - *Regression test*: `test_hsl.py` / `test_autocorrect_helpers.py` — case of a JPEG target
    with strongly shifted L*/hue, verify the cap is applied (no full transplant).
  - *Validate*: `python -m pytest app/tests -q` green.

## Steps — Camera Calibration

- [x] **C1 — Manual validation in real Lightroom.** ✅ (2026-07-19, live Lr + plugin connected
  during the session — real catalog, folder "2- Dernier soir Abreu", camera ILCE-7M4).
  No PySide6 GUI launched: seed_match/`_calib_develop_dict` called directly on real RAWs
  (`gpu_raw.analyze_raw_gpu`) via the `abelr` MCP, see CLAUDE.md § fast validation path.
  Protocol: 3 seeds (SML03779, SML03872, SML04799) each receive 7 distinct, exaggerated
  Calibration values via `apply_adjustments` (durable write, re-verified by reading develop
  back); target SML04057 chosen for being unambiguously closest to a single seed
  (SML04799, z-score distance 1.11 vs 2.44/2.47 for the other two — k=1 on a pool of 3 as
  expected by `seed_match.K_MAX`/`len(pool)//2`). `seed_match.k_nearest` + `target_from_seeds` +
  `autocorrect._calib_develop_dict` reproduced on these real vectors (no mock):
  - `EnableCalibration=True` set.
  - The 7 transplanted values (ShadowTint=-25, RedHue=-18, RedSaturation=-22, GreenHue=14,
    GreenSaturation=-6, BlueHue=-35, BlueSaturation=19) match **exactly** the values of seed
    SML04799 (k=1 → no averaging, raw transplant) — no contamination from the other two
    seeds (distance 2x larger).
  - The returned dict contains only the Calibration keys + `EnableCalibration` — no possible
    overlap with the exposure/wb/hsl keys (`Exposure2012`, `Temperature`/`Tint`,
    `SaturationAdjustment*`…): no structural regression on the other axes.
  - Real-Lr round-trip via `render_probe` (temporary write + render + restore) on the target:
    application accepted with no error, preview rendered (strong cyan cast consistent with
    BlueHue=-35/GreenHue=+14/ShadowTint=-25 — deliberately exaggerated values for an
    unambiguous test), `restore_error` absent, target's develop confirmed back to its
    original state afterward.
  Cleanup: the 3 seeds were rewritten back to their original Calibration values (0,
  `EnableCalibration` already `true` before the session) after the test — real catalog left
  in the state observed at the start of the session. `python -m pytest app/tests -q`: 113
  passed (no code touched, C1 is a pure manual validation).
  - *Limitation*: validation done by calling `core/` directly (real RAWs, no mock), without
    going through the PySide6 GUI (`main_window.py`/`autocorrect_worker.py`) or the `is_seed`
    marking in the SQLite cache — this end-to-end GUI+cache path remains untested by this
    test (only the k-NN mechanics + real Lr write are). To be covered if doubt arises on the
    GUI side.

- [x] **C2 — k-NN consistency guard before transplant.** ✅ (2026-07-19):
  `seed_match._weighted_calib_field` (replaces `_weighted_field`, its only caller — the 7
  `CALIB_FIELDS` fields) — if the values of the matched seeds on a field diverge by more than
  `_CALIB_SPREAD_MAX=25` slider points (-100..100 scale), refuses the weighted average and
  falls back to the exact value of the closest seed by distance (no fabricated field that
  wouldn't match any real seed). Provisional threshold (no real conflicting seed data
  available to settle it — see C3, unresolved), chosen in the same order of magnitude as
  `hsl._MAX_SAT=25`. `temperature`/`tint`/`tone`/`bands` untouched (separate path,
  `_weighted_field` was only used for Calibration). Tests: `test_seed_match.py` — 2 cases
  (spread 50 → fallback to exact closest seed; spread 2 → weighted average unchanged) +
  existing tests (`test_target_from_seeds_aggregates_calibration_weighted`: spread 40 on
  `red_hue` stays `> 19.0` because the fallback = the exact value of the closest seed 20.0 —
  matches the old behavior dominated by the same seed, no regression). `python -m pytest
  app/tests -q`: 115 passed.
  - *Regression test*: `test_seed_match.py` — case of 2 seeds close in distance but diverging
    on `red_hue` (spread 50) → 1-seed fallback instead of blind averaging. Case of consistent
    seeds (spread 2) → transplant unchanged (no regression).
  - *Validate*: `python -m pytest app/tests -q` green.

- [x] **C3 — Settle scene distance vs. camera constant.** ✅ (2026-07-19): **decision = keep
  the scene-dependent k-NN for Calibration, do not replace it with a camera median/mode.** No
  code change (current behavior was already the right one).
  Evidence (real catalog via live Lr, MCP `abelr.get_catalog_photos(include_develop=True)`,
  1057 photos, a single `ILCE-7M4` camera, a single "2- Dernier soir Abreu" folder): 270/1057
  photos have non-zero Calibration fields, grouped into **77 distinct vectors** that change
  in blocks aligned with sequential frame ranges (e.g. `SML03338`→`SML03360`:
  `(0,0,-5,10,5,5,15)` constant, then `SML03361`→`SML03371` shifts to
  `(-25,0,-10,10,-10,40,-10)` — block with `BlueHue=40` clearly out of the norm of neighboring
  blocks (0–15), signature of an isolated mixed-lighting shot — then `SML03372`→`SML03374`
  to `(5,0,-10,-10,-50,5,-5)`, etc.). This block-wise variation by scene/light, on a **single**
  camera and a **single** event, contradicts the "near-constant per camera" hypothesis: this
  user's Calibration setting follows the scene (like exposure/wb/hsl), not just the camera
  body. The scene-based k-NN distance vector (asshot_rg/bg, raw_median_l) therefore remains
  relevant for Calibration — no global median/mode fallback to code. Throwaway analysis
  script (not kept, ad hoc on the catalog's JSON dump).
  - *Regression test*: none (decision = behavioral status quo, existing `test_seed_match.py`
    remains the valid coverage of the Calibration k-NN path, including the C2 guard).
  - *Validate*: `python -m pytest app/tests -q` green (115 passed, unchanged — no code
    touched).

---

## Cleanup backlog (deferred)

Steps 2-7 of the old plan (pytest config, doc-desync guard, pure-module test coverage,
logging hygiene, QThread cleanup, doc resync) + perf backlogs (G9, P-10) + artistic regime
fallback + `core/image_source.py` tool-only: **not abandoned, deferred until after
H1-H3/C1-C3**. Full detail intact in [`OLD_PLAN.md`](OLD_PLAN.md).

## Appendix — Lightroom-dependent validation (manual if no MCP)

- H2: real `render_probe` probing to calibrate `ResponseModel.bands` (result to be noted here
  once done: camera/profile, measured slopes).
- C1: result of the manual Calibration validation (see step C1).
- Deferred from `OLD_PLAN.md`: `requestJpegThumbnail` lock (2nd Preview convergence ≈ 0), real
  e2e seeds→Preview→Apply test, `photo_panel`/`analysis_panel` GUI stubs.
