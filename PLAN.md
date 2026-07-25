# PLAN — Post-rework backlog (doc resync, calibration coverage, tests, segmentation gate)

Executable roadmap **one step at a time**. Technical context:
[`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md). Working rules:
[`CLAUDE.md`](CLAUDE.md). Full rework history (S0/Q/R/W/D/N/T1-T6, MAE baselines, decision
rationale): [`OLD_PLAN.md`](OLD_PLAN.md), section "PLAN — Rework (measurement scale, embedded
JPEG, k-NN)".

## Origin

2026-07-25 rework closed (S0, Q1-Q7, R1-R5, W1-W5, D1-D3, N1-N4, T1-T6 all `- [x]`). Carrying
forward only what a future step actually needs to read, not the narrative:

- **Live baseline** (real catalog `Last soirée Abreu`, 610 seeds, post-R/W/D/N, LOOCV via
  `app/tools/validate_seed_matching.py`): exposure MAE 2.86 L*, WB Temp 104.7 K, Tint 1.44,
  Calibration 0.62–2.04, HSL chroma/L*/hue 3.11 / 4.29 / 2.76°. Any new step is measured
  against these numbers.
- **Settled, do not reopen without new evidence**: `_CALIB_SPREAD_MAX=2.0` (real sweep, D2);
  `MEASURE_LONG_EDGE=2048` (R5); HSL embedded caps `_MAX_LUM_EMBEDDED_RAW=10` /
  `_MAX_HUE_EMBEDDED_RAW=8` (W4, real gains weaker than nominal); D3 k-means palette **not
  built** (D1 composition features not plateaued); `match_target_per_band` **built, tested,
  deliberately unwired** (N2) — measured worse than the global match.
- `ANALYSIS_VERSION = "v7-measure-grid"`, `SCHEMA_VERSION = 5`.

## Execution rules (Sonnet 5)

1. One step at a time, in section order (DOC → CAL → COV → SEG).
2. For each step: implement the regression test BEFORE/WITH the change.
3. Validate with `python -m pytest app/tests -q` (must stay **green**, including existing
   tests) — running `python -m app.main` is not required except for steps marked **Lr required**.
4. Check `- [ ]` → `- [x]` only after a green test (or, for **Lr required** steps, after a
   documented manual validation with evidence).
5. GPU-strict for the `core/` pipeline (no CPU fallback) — except `gpu.py` itself
   (GPU-first + CPU fallback, cf. CLAUDE.md, unchanged).
6. Any change to the measurement algorithm requires bumping `cache.ANALYSIS_VERSION` (full
   cache rebuild, no row-by-row migration).
7. If a step breaks an existing test with no legitimate reason: stop, don't check it off, flag it.

---

## DOC — Doc resync (no Lr, do first — cheapest, currently wrong on disk)

- [ ] **DOC1 — Resync `documentation/ARCHITECTURE.md`.** §4 still claims "sharp = subject"
  degrades at native resolution — superseded: R3/R5 measured the sharp/global gap now stable
  512px→native (blur sigma scaled to the diagonal), and R2 puts every source (RAW/embedded
  JPEG/Lr render) on the same 2048px grid before comparison. §8 (module map) predates
  `core/quantize.py`, the `gpu_raw`/`_to_hwc_u8` downsample path, `tools/calibrate_wb_response.py`,
  `gui/fresh_render_worker.py`.
- [ ] **DOC2 — `app/tests/test_docs_consistency.py`.** Old plan's step 3 never landed
  (confirmed absent — `app/tests/` has 20 files, none of them this). Parse `CLAUDE.md` +
  `ARCHITECTURE.md`, assert every cited `core/xxx.py` / `gui/xxx.py` exists on disk, assert no
  removed module is presented as alive. Keeps DOC1 from silently going stale again.
  - Test: the file itself, green.

---

## CAL — Calibration coverage (**Lr required**)

W2 produced real WB/HSL responses but on thin evidence in both cases (see baseline above and
`OLD_PLAN.md` W2 for the original numbers):

- [ ] **CAL1 — Fill the 4 uncalibrated HSL bands.** Green/Blue/Purple/Magenta still run on the
  nominal fallback on both profiles (`ILCE-7M4|IN`, `ILCE-7M4|Neutral`) — one dark event photo
  can't exercise 8 hue bands. Re-run `tools/calibrate_hsl_response.py` against a reference
  frame with real content in those bands, or explicitly accept the nominal fallback and record
  why in the code comment (same class of decision as W4).
- [ ] **CAL2 — Re-fit `ILCE-7M4|Neutral` WB response.** Current fit used `neutral_frac≈0.012`
  (barely above `_MIN_NEUTRAL_FRAC=0.01`, Tint axis only 3/5 usable probes) — this profile
  covers **904 of 930 photos** in the catalog. Find/shoot a better neutral reference, re-run
  `tools/calibrate_wb_response.py`.
- Both re-runs go through the MCP-client transport (a second process can't bind port 5000
  alongside a running `app.main`) — pattern already written and working (see W2 in
  `OLD_PLAN.md`).
  - Step-level test: re-run S0 embedded branch — WB Temp/Tint MAE (175.67 K / 4.02 at last
    measure) must improve, or the step reports honestly that it didn't.

---

## COV — Test coverage (was `T7`, carried verbatim + old plan's step 6)

Explicitly out of scope during T1-T6 (hygiene only, no bulk new tests). Baseline: 45% total
coverage, 208 tests green.

- [ ] **COV1 — `app/server/job_queue.py`** (32%): submit / prune / bridge-connected timing
  paths untested.
- [ ] **COV2 — `app/core/cache.py`** (79%, but only `develop_hash`/`style_hash`/`raw_signature`
  exercised): `put_*`/`get_*` read/write round-trips + `_ensure_schema` version-bump behavior —
  the DROP-and-recreate path that actually wiped the user's real cache at W3, with no direct
  unit test today.
- [ ] **COV3 — `app/core/render_metrics.py`** (61%): `band_stats`/`neutral_stats` core
  measurement functions, only exercised indirectly today via other test files.
- [ ] **COV4 — `app/core/autocorrect.py`** (85%): `_plan_seeds`'s tail (537-571) and
  `_plan_embedded`'s divergence-diagnostic branch (338-341, 354) — the exact code path behind
  the R-section crop/variant bug.
- [ ] **COV5 — GUI workers, decision + closeEvent.** `autocorrect_worker.py` (16%),
  `main_window.py` (10%), `neutral_preview_worker.py` (19%) — Qt/thread-heavy, essentially
  untested; decide unit-test vs. accept-and-document. Folds in old plan's step 6: **confirmed
  still missing** — no `closeEvent` anywhere in `app/gui/`, so workers are still not
  `quit()`+`wait()`'d on close (orphaned QThreads). If accepted as GUI-manual-only, add the
  `closeEvent` fix directly (small, mechanical) even without a Qt unit test — document the
  validation as a smoke import + manual close, same honesty standard as the old plan's step 6.

---

## SEG — Segmentation gate (was `G1`, gate text unchanged)

- [ ] **SEG1 — Decision gate.** Do not build until S0 (re-run after CAL/COV, on a live
  re-analyzed catalog) shows residual error **concentrated on content-mixing or cropped-photo
  cases**. Now checkable for the first time on real, non-pre-R data — the gate was originally
  written before R/W/D/N existed.
  - If the evidence arrives: lightweight semantic segmentation (SegFormer-B0/PP-LiteSeg for
    sky/subject/foliage + a dedicated face-parsing model for skin), not MobileSAM
    (class-agnostic, no usable labels to target HSL per zone).
  - Constraint: plugin self-sufficiency (`bootstrap.ps1`, CLAUDE.md) — model weight download on
    first launch, install-size cost, and CPU-only inference time must be explicitly quantified
    before any implementation.

---

## Deferred backlog (carried, not scheduled)

- **G9** — GPU metrics micro-pass (hoist hue/sat/chroma out of the dual, group scalar syncs).
  Bit-exact parity required (else bump `ANALYSIS_VERSION`) — needs profiling first
  (`torch.profiler`). Detail: `OLD_PLAN.md`.
- **P-10** — `_probe_chunk` decodes one photo at a time; batch via `analyze_render_blobs`
  eventually (consistency more than perf, Lr rendering dominates).
- **`core/regime.py`** — no longer consulted by the live k-NN path; revalidate if matching
  proves unstable on small seed pools.
- **`core/image_source.py`** — tool-only; remove if the `tools/` using it get archived.
- **`photo_panel.py` / `analysis_panel.py`** — GUI stubs (previews, histograms, WB outliers),
  never finished.
- **N3's `percentile=75.0`** — validated as a reasonable working default (r=0.241,
  flagged/unflagged error +44% separation) but never swept for its actual optimum.

## Risks

- CAL needs a live Lr session + a reference frame with real content in it — this catalog is
  almost entirely dark nightclub/event frames, the same constraint that already degraded W2's
  first pass.
- S0 (`app/tools/validate_seed_matching.py`) is read-only on cached measurements — a step that
  changes the measurement algorithm needs the user to re-run Analyze live before S0 means
  anything (this bit twice already: R and D1's early S0 re-runs).
- `cache.SCHEMA_VERSION` bump = full cache DROP+recreate on next launch (already happened once,
  W3) — any future schema change carries the same real cost.

## Verification

- `python -m pytest app/tests -q` green at every step (run from `ABELr.lrplugin/`).
- `app/tools/validate_seed_matching.py` (S0) is the primary evidence — before/after output at
  every step that touches measurement or matching.
- CAL/COV5 (closeEvent): live smoke test via GUI, or `abelr` MCP tools if the App is running.
