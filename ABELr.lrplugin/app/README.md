# External App — ABELr

HTTP server (FastAPI, localhost:5000) + GUI (PySide6). The Lr plugin is the client:
it polls the App.

This folder (`app/`) lives **inside** `ABELr.lrplugin/` — the plugin is self-sufficient, it
embeds all the Python code. All the commands below are run from
`ABELr.lrplugin/` (the plugin folder), not from the repo root.

## Install

**Automatic (recommended)**: the plugin builds its own venv on first launch
(`launch.ps1` → `bootstrap.ps1`, triggered by the Lr *Start/connect the application* menu).
Detects an NVIDIA GPU (`nvidia-smi`) and installs torch CUDA (cu124) if present, otherwise torch CPU.
Requires Python 3.11+ on the PATH + internet (first install only).

**Manual** (dev):
```bash
cd ABELr.lrplugin
python -m venv app\.venv
app\.venv\Scripts\activate        # Windows
pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124  # GPU
# or: pip install torch==2.6.0 torchvision==0.21.0                                                 # CPU
pip install -r app\requirements.txt
```

## Launch

```bash
python -m app.main            # from ABELr.lrplugin/
```

Starts the FastAPI server (daemon thread) + the GUI window. The device (GPU if CUDA
is usable, otherwise CPU) is decided automatically by `core/gpu.py` — no setting required.

## Test without Lightroom

With the App running, simulate the plugin in another terminal:

```bash
python -m app.tools.mock_plugin
```

Click "Analyze selection" in the GUI → the mock returns fake photos.

## Check the server alone

```bash
curl http://localhost:5000/health
curl http://localhost:5000/status
```

## Tests

```bash
python -m pytest app/tests -q                                                # all (from ABELr.lrplugin/)
python -m pytest app/tests -q -m "not gpu"                                   # excludes GPU parity
python -m pytest app/tests -q --cov=app --cov-report=term-missing:skip-covered  # + coverage report
```

Config lives in `ABELr.lrplugin/pytest.ini` (`testpaths`, the `gpu` marker, `filterwarnings`).

## Structure

| Folder | Role |
|---|---|
| `server/` | FastAPI (`api.py`), thread-safe job queue (`job_queue.py`), Pydantic models (`models.py`) |
| `gui/` | PySide6 window (`main_window.py`), non-blocking Qt workers (`job_worker.py` = waits on the plugin, `autocorrect_worker.py` = measure+plan, `neutral_preview_worker.py` = neutral anchors) |
| `core/` | Image and analysis pipeline (see below) |
| `tools/` | Mock plugin for dev without Lr |

### `core/` — image pipeline

| File | Role |
|---|---|
| `color.py` | Analysis color spaces: linear ProPhoto, Y (XYZ) luminance, conversion → sRGB for display |
| `raw.py` | Sony ARW RAW decoding via rawpy: `load_linear` (linear ProPhoto, analysis) / `load_rgb` (sRGB uint8, GUI) |
| `image_source.py` | Analysis pixel source: **RAW → linear ProPhoto** (`LoadedImage`) |
| `analysis.py` | Exposure metrics (Y luminance) + white balance (gray-world), in linear space |
| `catalog.py` | Locates the `.lrcat` + `.lrdata` bundles; opens the SQLite files read-only (coexists with Lr open) |
| `previews.py` | Resolves `id_global` → preview files; rendered preview (result check). **Smart Preview = inspection only** |
| `adjustments.py` / `prediction.py` | Correction / series smoothing computation — in progress |

> **Why RAW and not the Smart Preview?** Calibration (`tools/calibrate_sp_vs_raw.py`)
> showed that the Smart Preview is **camera-native raw** (before WB and the color
> matrix), which LibRaw doesn't decode and which a hand-tuned develop doesn't map
> faithfully back to the RAW. RAW via rawpy is the only accurate, consistent source.
> Analysis format: **float32 linear ProPhoto** (wide gamut = unbiased WB),
> luminance via XYZ's Y; sRGB reserved for display.

## Job flow

```
GUI submit() -> JobQueue.pending
plugin GET /jobs/pending      -> retrieves the job
plugin POST /jobs/{id}/result -> JobQueue.submit_result() unblocks the GUI worker
```
