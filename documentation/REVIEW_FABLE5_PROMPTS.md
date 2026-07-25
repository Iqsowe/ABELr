# Launch Prompts — Fable 5 Review

> Frozen audit artifact, 2026-07-18. Asserts the retired GPU-strict (no-CPU-fallback) contract
> as current policy — it has since been lifted (GPU-first, CPU fallback; see `CLAUDE.md`). Read
> as history, not current policy.

How to use:
1. `/model` → `claude-fable-5`.
2. **A fresh session per pass** (or `/clear` between each). Never chain two passes
   in the same context: the previous pass pollutes the findings.
3. Copy-paste the pass prompt. Each pass writes its section into
   [`REVIEW_FABLE5.md`](REVIEW_FABLE5.md).
4. Mandatory order: 0 → 1 → 2 → 3. Pass 0 locks down the live/dead scope everything else depends on.

Reminders valid for ALL passes (already in CLAUDE.md, but frequent false positives):
- **GPU-strict = deliberate choice.** The absence of a CPU fallback is not a bug.
- **Lua 5.1**: missing `//`, `goto`, `utf8` stdlib = normal.
- Any recommendation touching the measurement algorithm requires an `ANALYSIS_VERSION` bump (no migration) — state it.
- No finding without `file:line`. Develop parameter names verified against `lr15_sdk_api_reference.md`.

---

## PASS 0 — Ground truth (architecture / doc)

```
ABELr project review, PASS 0 of 4: architecture/doc ground truth. Goal =
lock down the live/dead scope BEFORE any bug hunting. Fix nothing, propose no
code fix: this pass is purely descriptive.

Read first: CLAUDE.md, documentation/ARCHITECTURE.md (especially §3 module map), PLAN.md.

Tasks:
1. For EACH module in app/core/ (24 files), app/server/ (3), app/gui/ (7) and the 14
   Lua files in ABELr.lrplugin/: determine the actual status (live / tool-only / dead)
   by looking for inbound references (imports, require, calls). A module with no importer
   outside app/tests/ and app/tools/ is a candidate for "dead" or "tool-only".
2. Compare this actual status to the status stated in ARCHITECTURE.md §3. List the gaps.
3. Note doc↔code divergences outside the module map: FastAPI endpoints, job types,
   image pipeline, cache schema (number of tables), CLAUDE.md constraints — anything the doc
   asserts that the code contradicts.

To locate things without saturating the context, delegate the "who imports X / who calls Y"
searches to the cavecrew-investigator agent.

Output: fill in ONLY the "Pass 0" sections of documentation/REVIEW_FABLE5.md
(tables 0.1 and 0.2). Every line with file:line evidence. Nothing without evidence. Then fill in
the Pass 0 line of the Journal.
```

---

## PASS 1 — Bugs by subsystem

```
ABELr project review, PASS 1 of 4: bug hunt (correctness). Deal ONLY with
modules marked "live" in Pass 0 (Pass 0 section of documentation/REVIEW_FABLE5.md). Ignore
dead/tool-only code for the bug hunt.

Read first: CLAUDE.md, documentation/ARCHITECTURE.md, the Pass 0 section of REVIEW_FABLE5.md.
For any Lua code or any develop parameter name: documentation/lr15_sdk_api_reference.md.

Sweep subsystem by subsystem, in this order, one at a time:
  (a) Lua plugin — ABELr.lrplugin/ (14 files). Check: catalog/develop writes
      inside catalog:withWriteAccessDo; blocking I/O inside LrTasks.startAsyncTask; LrHttp.post
      inside postAsyncTaskWithContext; paths via LrPathUtils (never "/" concatenation);
      import 'LrXxx' vs require; correct use of Json.array. Flag any missing PV2012
      (Exposure2012…) and WhiteBalance='Custom' required for Temperature/Tint.
  (b) HTTP bridge + server — app/server/api.py, job_queue.py, models.py + HttpClient.lua,
      PollingLoop.lua. Check: polling lifecycle (generation vs shared flag),
      job_id/type/payload contract, network error handling, deserialization, races on the queue.
  (c) Core image/GPU — raw, gpu, gpu_raw, gpu_jpeg, gpu_schedule, pipeline, image_source, color,
      render_metrics, render_metrics_gpu, embedded_jpeg, previews. Check: gpu.require_cuda
      at entry points, color spaces (linear ProPhoto float32, Y luminance,
      use_camera_wb), dtype/normalization, GPU memory release, Windows RAW paths.
  (d) SQLite cache — cache.py + hashing in measure.py, exif_profile.py. Check: the 5 tables,
      ANALYSIS_VERSION salted into ALL relevant hashes, key/insert/lookup consistency,
      invalidation, transactions.
  (e) Analysis/seed-match — analysis, measure, seed_match, wb_model, exposure, hsl, autocorrect,
      sharpness, regime, response, catalog. Check: numerical correctness, divisions by zero,
      HSL bounds (saturation = reduction only), k-NN, WB regression, unit consistency
      (render-space L* for exposure).

Anti-false-positive reminders: GPU-strict with no CPU fallback = deliberate; Lua 5.1 missing
//,goto,utf8 = normal; recommendation touching measurements ⇒ mention the ANALYSIS_VERSION bump.

Output: fill in tables (a)–(e) of the "Pass 1" section of documentation/REVIEW_FABLE5.md.
Columns: file:line, Severity (🔴/🟠/🟡/⚪), Status (CONFIRMED/PLAUSIBLE), Problem, Fix, Effort.
Nothing without file:line. Apply no patch. Then fill in the Pass 1 line of the Journal.
```

---

## PASS 2 — Performance

```
ABELr project review, PASS 2 of 4: performance, hot spots ONLY. No
generic micro-optimization, no cosmetic rewrite.

Read first: CLAUDE.md, documentation/ARCHITECTURE.md (§4 image pipeline, §5 cache), the Pass 0
section of documentation/REVIEW_FABLE5.md.

Allowed targets, in order:
  1. Image / GPU pipeline — RAW decoding, CPU↔GPU transfers, batching, buffer reuse,
     redundant per-photo operations (raw, gpu_raw, gpu_jpeg, pipeline, render_metrics_gpu).
  2. SQLite cache — lookup cost, missing indexes, N+1 queries, blob serialization,
     effective hit-rate (cache.py).
  3. Bridge polling — GET /jobs/pending every 300 ms: server/plugin-side cost,
     unnecessary wakeups, granularity (PollingLoop.lua, api.py, job_queue.py).

For each hotspot: quantify the current cost (or describe why it's hot), the root
cause, the optimization, the estimated gain, and state whether it touches the measurement
algorithm (if so ⇒ ANALYSIS_VERSION bump required). Do not break the GPU-strict contract or the
cache versioning.

Output: fill in the "Pass 2" table of documentation/REVIEW_FABLE5.md. Apply no patch.
Then fill in the Pass 2 line of the Journal.
```

---

## PASS 3 — Consolidation / backlog

```
ABELr project review, PASS 3 of 4: consolidation. No new code analysis —
only synthesis of passes 0 to 2.

Read documentation/REVIEW_FABLE5.md in full (Pass 0, 1, 2 sections).

Tasks:
1. Merge all findings from passes 0-2 into a single backlog. Sort: descending severity
   (🔴 > 🟠 > 🟡 > ⚪), then ascending effort (S before L). Fill in the "Pass 3" table.
2. Detect duplicates / related findings across subsystems and group them.
3. Propose a PLAN.md update: fold 🔴/🟠 items into the project backlog,
   correct ARCHITECTURE.md §3 per Pass 0. Present the proposed diffs, only write to
   PLAN.md / ARCHITECTURE.md after explicit user validation.

Output: "Pass 3" table of REVIEW_FABLE5.md filled in + Pass 3 line of the Journal. The
PLAN.md/ARCHITECTURE.md changes remain proposals until validated.
```
