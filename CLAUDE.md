# ABELr — Lightroom Classic Plugin

Lightroom Classic plugin (Lua + Lr SDK) + external Python application for intelligent batch
editing. Core: **exposure / HSL / Calibration / White Balance per photo**, calibrated on
**seeds** (reference photos marked by hand) via k-NN matching on sharp-zone RAW analysis.

**Self-sufficient plugin**: `ABELr.lrplugin/` embeds everything — the Lua code *and* the
complete Python package (`ABELr.lrplugin/app/`), plus `launch.ps1`/`bootstrap.ps1`. Copying
this single folder to another machine is enough to install the plugin. The rest of the repo
(`documentation/`, `PLAN.md`…) is the dev repository, not a runtime dependency of the plugin.

## Where to read what

| File | For |
|---|---|
| [`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md) | How the system works: flow, module map, image pipeline, cache, GPU, communication |
| [`PLAN.md`](PLAN.md) | Roadmap / status: steps in progress, regression tests, backlog |
| [`documentation/lr15_sdk_api_reference.md`](documentation/lr15_sdk_api_reference.md) | All Lua code: imports, SDK APIs, Camera Raw parameters. ⚠️ methods = unverified, confirm before use |
| [`ABELr.lrplugin/app/core/autocorrect.py`](ABELr.lrplugin/app/core/autocorrect.py) | The correction brain — `plan()`, seeds mode vs embedded mode. Central design decision of the project |
| [`ABELr.lrplugin/app/gui/main_window.py`](ABELr.lrplugin/app/gui/main_window.py) | What the user actually clicks — Analyze / Apply-per-axis / calibrate-neutral flow |
| [`ABELr.lrplugin/app/README.md`](ABELr.lrplugin/app/README.md) | Install / launch / `core/` structure |
| [`documentation/project_overview.md`](documentation/project_overview.md) | Historical vision doc — layout it describes predates the current one, do not trust file paths in it |
| [`OLD_PLAN.md`](OLD_PLAN.md) | Archive of a superseded plan — do not read by default |

> Before writing any Lua or looking up a develop parameter name: `lr15_sdk_api_reference.md`.
> Before claiming a module is used: the status map in ARCHITECTURE.md §3 —
> several `core/` modules are tool-only or dead.

---

## Constraints never to violate

**Lua / SDK:**
- Lua 5.1: no `//`, `goto`, or `utf8` stdlib.
- Any catalog/develop write inside `catalog:withWriteAccessDo(...)`.
- Any blocking I/O inside `LrTasks.startAsyncTask`; `LrHttp.post` requires `LrFunctionContext.postAsyncTaskWithContext`.
- Windows paths via `LrPathUtils` — never concatenate `/`.
- SDK modules: `import 'LrXxx'`; plugin modules: `require`.
- No native JSON lib → embedded `Json.lua` (`Json.array(t)` forces a JSON array).
- `Collections.lua`, `Metadata.lua`, `Presets.lua` (Phase 2, required by `PollingLoop.lua`) and
  `PhotoLookup.lua` (required transitively by those three) contain SDK methods marked ⚠️
  unverified in live Lr, in their own header — same rule as `lr15_sdk_api_reference.md`:
  confirm before extending/copying their usage.

**Python App:**
- **GPU-first, CPU fallback** (the plugin must run without an NVIDIA card): `app/core/gpu.py`:
  `device()` returns `cuda` if usable, otherwise `cpu` — **never raises**. The whole pipeline
  (`gpu_raw`, `gpu_jpeg`, `render_metrics_gpu`, `gpu_schedule`) routes its device through this
  call, so it switches automatically. `require_cuda()`/`GpuUnavailable` remain opt-in for call
  sites that explicitly want to require CUDA — grep `gpu.py` for current callers, do not use
  them as a default gate elsewhere.
- **Cache mandatory**: workers consult `cache` (SQLite, `app/core/cache.py`) before decoding
  anything — tables `LightroomPicture`, `SourceRAW`, `InCameraJPEG`, `PreviewJPEG`,
  `NeutralPreviewJPEG` (see `cache.py` for current schema). Two independent version constants,
  both defined in `cache.py`, values not repeated here since they move on every bump:
  - `SCHEMA_VERSION` — table structure. Bump → DROP+recreate, no migration.
  - `ANALYSIS_VERSION` — salted into the freshness hashes. Bump whenever the measurement
    algorithm changes → full re-measure, no migration.
- **`python -m app.main` runs without Lightroom**: the server starts on its own, the bridge
  just stays "disconnected". RAW decoding only requires the `.ARW` on disk, never the catalog
  nor Lr.

**Develop parameters = PV2012**: the real names carry the `2012` suffix (`Exposure2012`,
`Highlights2012`…). `WhiteBalance='Custom'` is required for `Temperature`/`Tint` to take effect.
`WhiteBalance='Custom'` also serves as a historical marker on the App side.

---

## Communication

**Plugin = ALWAYS HTTP client. App = ALWAYS server (`127.0.0.1:5000`).** The App never
pushes: it drops a job into `job_queue`, the plugin picks it up by polling (`GET /jobs/pending`,
300 ms) and returns it via `POST /jobs/{id}/result`.

Job types: source of truth is the `JobType` enum in `app/server/models.py` and `dispatch()` in
`PollingLoop.lua` — one enum member ⇔ one `elseif` branch, keep them in sync on any addition.

```json
{ "job_id": "uuid", "type": "apply_adjustments",
  "payload": { "adjustments": [ { "photo_id": "...", "develop": {
      "WhiteBalance": "Custom", "Temperature": 5650, "Tint": -5, "Exposure2012": 0.35 } } ] } }
```

**Second channel — MCP** (`app/mcp/server.py` + `tools.py`, mounted on `/mcp` in
`app/server/api.py`): re-exposes the job types above as MCP tools for Claude Code itself,
registered in [`.mcp.json`](.mcp.json) (server `abelr`, `http://127.0.0.1:5000/mcp`). Used to
drive live Lr during dev without writing a script. Requires `python -m app.main` running;
tools that depend on the bridge time out cleanly if the Lr plugin isn't connected (no crash).

---

## Commands

Run from `ABELr.lrplugin/` (the plugin is the root of the Python package):

```bash
python -m app.main                          # server + GUI, runs without Lightroom
python -m app.tools.mock_plugin              # fake plugin, no Lr needed
python -m pytest app/tests -q -m "not gpu"   # unit tests, no GPU/RAW required
python -m app.tools.check_docs               # doc/code drift check
```

First launch with no venv: `launch.ps1` chains `bootstrap.ps1` (builds `app/.venv`, GPU/CPU
torch auto-detected). Details, full test invocations, manual install: `app/README.md`.

**Lua:** edit in `ABELr.lrplugin/` → Lr: *File > Plug-in Manager* > Reload → test via
*Library > Plug-in Extras* → logs via `Utils.logf` in *Help > Lua Console*.

---

## Naming conventions

| Context | Convention |
|---|---|
| Lua files | `PascalCase.lua` · functions/locals `camelCase` · constants `UPPER_SNAKE_CASE` |
| Python files | `snake_case.py` · classes `PascalCase` · functions/vars `snake_case` |
| Exchanged JSON keys | `snake_case` |
| Lr SDK parameter names in JSON | `PascalCase` (identical to the SDK) |
