# Archive — PLAN.md as of 2026-07-25 (pre-refonte)

> Archived in favor of the new `PLAN.md` (refonte 2026-07-25: Neutral Preview repair, measure-grid
> enforcement, exposure calibration, GPU scoping, UI rework, dead-code cleanup). This file is the
> full text of the previous `PLAN.md`, kept verbatim for its live baselines and locked decisions.
> Do not read by default — see `PLAN.md` for current work, `OLD_PLAN.md` for the rework before
> this one (S0/Q/R/W/D/N/T1-T6).

**Period covered**: post-rework backlog (doc resync, calibration coverage, tests, segmentation
gate), closed 2026-07-25. All steps `- [x]` except `SEG1`, carried forward into the new `PLAN.md`.

**Live baseline to keep** (real catalog `Last soirée Abreu`, 610 seeds at the time, LOOCV via
`app/tools/validate_seed_matching.py`): exposure MAE 2.86 L*, WB Temp 104.7 K, Tint 1.44,
Calibration 0.62–2.04, HSL chroma/L*/hue 3.11 / 4.29 / 2.76°.

**Decisions locked, do not reopen without new evidence** (superseded only where the new PLAN.md
says so — the measure-grid enforcement in the new plan **does** invalidate the baseline above,
seeing as it was measured on a mixed grid, see new PLAN.md section R):
- `_CALIB_SPREAD_MAX = 2.0` (real sweep, old-plan D2)
- `MEASURE_LONG_EDGE = 2048` (old-plan R5) — still the target grid; the new plan's `R1` enforces
  it is actually reached, not just requested.
- HSL embedded caps `_MAX_LUM_EMBEDDED_RAW = 10` / `_MAX_HUE_EMBEDDED_RAW = 8` (old-plan W4)
- D3 k-means palette **not built** (D1 composition features not plateaued)
- `match_target_per_band` **built, tested, deliberately unwired** (old-plan N2) — measured worse
  than the global match.
- `ANALYSIS_VERSION = "v7-measure-grid"`, `SCHEMA_VERSION = 5` — both superseded by the new plan's
  `R2` (`ANALYSIS_VERSION` bumped to `"v8-grid-enforced"`; `SCHEMA_VERSION` stays 5).

---

## Full text as of archiving

```markdown
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

- [x] **DOC1 — Resync `documentation/ARCHITECTURE.md`.** Done in `1d6f1ca` (dev CLAUDE and
  ARCHITECTURE improve) — stale "sharp = subject degrades at native resolution" claim gone,
  §3 module map now lists `core/quantize.py`, `gui/fresh_render_worker.py`, `render_metrics_gpu.py`
  (`_to_hwc_u8`), etc. Verified 2026-07-25: content matches code on disk.
- [x] **DOC2 — `app/tests/test_docs_drift.py`.** Landed alongside DOC1 (wraps
  `app/tools/check_docs.py`: job list, MCP tools, cache tables, referenced paths, retired
  phrasing). Confirmed present + green (`python -m pytest app/tests -q`, 209 passed).

---

## CAL — Calibration coverage (**Lr required**)

W2 produced real WB/HSL responses but on thin evidence in both cases (see baseline above and
`OLD_PLAN.md` W2 for the original numbers):

- [x] **CAL1 — Fill the 4 uncalibrated HSL bands.** Done 2026-07-25. Wrote
  `app/tools/mcp_calibrate.py` (MCP-client transport — talks to the already-running
  `app.main` over `/mcp` instead of starting a second FastAPI server, avoids the port-5000
  conflict; same math as `calibrate_hsl_response.py`, `--photo-id` bypasses the Lr-GUI-selection
  requirement since `render_probe` resolves by UUID regardless). Reference photos picked
  programmatically by scanning cached `InCameraJPEG.hsl_global` band `frac` across the live
  catalog (`Last soirée Abreu`, 669 cached photos) instead of guessing — this catalog turned out
  to have real content in all 4 bands, unlike the single dark frame W2 had:
  - **`ILCE-7M4|Neutral`** (904/930 photos): Green (SML04323, frac 0.30, n=5/5 all axes), Blue
    (SML03442, frac 0.92, n=4-5/5), Purple (SML03634, frac 0.65, n=5/5), Magenta (SML03762,
    frac 0.69, n=5/5). All 4 bands now calibrated.
  - **`ILCE-7M4|IN`** (26 photos): Blue/Purple/Magenta from SML03359 (n=5/5 all axes). **Green
    left on the nominal fallback** — confirmed zero photos with any Green-band pixels across
    all 26 cached `IN` photos, this catalog genuinely has no candidate (same class of
    evidence-based decision as W4).
  - `git diff` confirms Red/Orange/Yellow/Aqua (already-calibrated bands) untouched on both
    profiles. `python -m pytest app/tests -q`: 209 passed (data-only change).
- [x] **CAL2 — Re-fit `ILCE-7M4|Neutral` WB response.** Done 2026-07-25, same script/session as
  CAL1. Old fit: reference `SML04237`, `neutral_frac≈0.012`, Tint axis 3/5 usable probes. New
  reference `SML04722` (picked by scanning cached `neutral_global.neutral_frac`, top hit
  0.31 — 25x the old anchor): Temperature n=3/5 (2 probes still dropped below
  `_MIN_NEUTRAL_FRAC` at the largest deltas, expected), **Tint n=5/5** (was 3/5).
  `is_calibrated=True`. S0 embedded-mode WB metric not usable as before/after evidence here —
  only 1 candidate resolves in that branch (`NeutralPreviewJPEG` cache has 4 rows total,
  unrelated to this change), too thin to compare (n=1 either side).
- Both driven via `app/tools/mcp_calibrate.py` (MCP-client transport) — kept as a real tool
  (not a throwaway script) since the port-5000-conflict problem is structural, not one-off;
  reusable for any future re-calibration on a live `app.main`.

---

## COV — Test coverage (was `T7`, carried verbatim + old plan's step 6)

Explicitly out of scope during T1-T6 (hygiene only, no bulk new tests). Baseline: 45% total
coverage, 208 tests green.

- [x] **COV1 — `app/server/job_queue.py`** (32%→99%). Done 2026-07-25:
  `app/tests/test_job_queue.py` (14 tests) — submit/wait_result round-trip, saturation guard,
  TTL pruning (evicts stale + keeps fresh), bridge heartbeat timing, next_pending/submit_result
  lifecycle (ok/error/unknown-job), and a real-thread concurrency smoke test (Lock+Event across
  threads — the class's actual reason to exist). Only line 139 uncovered (defensive skip of a
  non-PENDING entry mid-dequeue). `python -m pytest app/tests -q`: 223 passed (was 209).
- [x] **COV2 — `app/core/cache.py`** (79%→100%). Done 2026-07-25:
  `app/tests/test_cache_roundtrip.py` (26 tests) — put_*/get_* round-trips for all 5 tables
  (LightroomPicture/seeds, SourceRAW, InCameraJPEG, PreviewJPEG, NeutralPreviewJPEG), hash-match
  vs hash-mismatch vs `_latest` (ignores hash), `is_seed` upsert preservation across
  re-analysis, `commit=False` batching, `raw_signature` both branches, and the
  `_ensure_schema` DROP-and-recreate path on a `SCHEMA_VERSION` bump (confirms old rows are
  gone after — the exact W3 real-cache-wipe behavior, now under test). `python -m pytest
  app/tests -q`: 249 passed (was 223).
- [x] **COV3 — `app/core/render_metrics.py`** (61%→100%). Done 2026-07-25:
  `app/tests/test_render_metrics_measure.py` (13 tests) — `tone_stats`/`neutral_stats`/
  `band_stats`/`rgb_u8_to_hsv_hue_sat` on synthetic sRGB patches (solid gray, pure
  red/green/blue, mixed half-red/half-blue), covering both degenerate-population fallback
  branches (`tone_stats` all-highlight-clipped → reuses the unfiltered L* array;
  `neutral_stats` zero neutral pixels on a saturated image → all-zero `NeutralStats`, not
  NaN) plus the `mask` parameter on all three. `python -m pytest app/tests -q`: 262 passed
  (was 249).
- [x] **COV4 — `app/core/autocorrect.py`** (85%→99%). Done 2026-07-25:
  `app/tests/test_autocorrect_plan_axes.py` (23 tests). Confirmed the real gap first
  (`_plan_seeds`'s wb/hsl blocks and `_plan_embedded`'s expo/wb/hsl loops were never exercised
  through `plan()` at all before this — only "expo"/"calib" axes were, via
  `test_autocorrect_confidence.py`/`test_autocorrect_calib.py`/the S0 embedded-validation
  tests). Covers: `_pair_for`'s sharp↔global fallback, the divergence diagnostic
  (338-341/354), wb deviant+calibrated / deviant+uncalibrated / conforming / low-neutral-frac,
  hsl deviant/matching bands in both modes, `_plan_seeds` wb refine-vs-no-model / no-match,
  the usable-photo-count note, and the embedded calib-matched-but-no-calibration branch. Only
  line 445 uncovered — `if d == 0: continue` in the embedded hsl loop, dead in practice since
  `hsl.plan_hsl` already filters zero-delta keys before returning. `python -m pytest
  app/tests -q`: 285 passed (was 262).
- [x] **COV5 — GUI workers, decision + closeEvent.** Done 2026-07-25. Decision: **accept and
  document** — `autocorrect_worker.py`/`main_window.py`/`neutral_preview_worker.py` stay
  GUI-manual-only, not unit-tested (Qt state-machine + signal/slot heavy; a meaningful test
  would need to mock `job_queue` signal emission through 3+ chained worker hops per flow —
  disproportionate to the risk, same call as the old plan's step 6). What *was* fixed: no
  `closeEvent` existed anywhere in `app/gui/` (confirmed absent again before touching it) —
  workers were never `quit()`+`wait()`'d on window close (orphaned QThreads, "Destroyed while
  thread is still running" on exit). Added `MainWindow.closeEvent` (`app/gui/main_window.py`)
  joining all 7 tracked worker attributes with a 5s timeout each. Validated (no Qt unit test,
  same honesty standard as the old plan's step 6): `test_smoke_import.py` already globs
  `app/gui/*.py` so the edit doesn't break import; manually instantiated `MainWindow` under
  `QT_QPA_PLATFORM=offscreen`, closed it with no worker running (no-op path) and with a real
  `JobWorker` started against it (confirmed `isRunning()` False after `close()` — the join
  path actually executes). `python -m pytest app/tests -q`: still 285 passed (no new tests
  from this step, code-only + manual validation).

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
```
