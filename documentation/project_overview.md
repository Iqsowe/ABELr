# ABELr — Project overview

> Historical / vision doc. The file layout and module names described below (e.g. `plugin/lib/`,
> `app/` at repo root, `prediction.py`, `rust_ext/`) predate the self-sufficient
> `ABELr.lrplugin/` rework and no longer match what's on disk — see `ARCHITECTURE.md` for the
> current layout.

## Description

ABELr is an intelligent photo-editing system for Adobe Lightroom Classic.
It couples a Lightroom plugin (Lua) with an external Python application that has a graphical interface.

The application analyzes Sony RAW files (ARW format), determines the optimal adjustments,
and applies them automatically in Lightroom — without manual photo-by-photo intervention.

---

## Problem solved

Manually editing a series of 500 to 1000 photos is slow and inconsistent.
Exposure, white balance, and color calibration vary from one shot to the next
depending on lighting conditions, and correcting them by hand produces uneven results.

ABELr analyzes the whole series, builds a prediction map of the adjustments,
and applies consistent, precise corrections across all the photos in batch.

---

## Overall architecture

```
┌─────────────────────────────────┐      HTTP JSON       ┌──────────────────────────────────────┐
│       Lightroom Plugin          │ ◄──── polling ──────► │         Python Application            │
│         (Lua, Lr SDK)           │      localhost:5000   │   FastAPI + PySide6 (Qt6) GUI        │
│                                 │                       │                                      │
│  • Reads Lr catalog data        │                       │  • Graphical user interface          │
│  • Applies the adjustments      │                       │  • Sony RAW decoding (rawpy/LibRaw)  │
│  • 300ms polling loop           │                       │  • Image analysis (numpy, OpenCV)    │
└─────────────────────────────────┘                       │  • Adjustment computation             │
              │                                           │  • Prediction map (scikit-learn)     │
              │ Lr SDK                                    └──────────────────────────────────────┘
              ▼
┌─────────────────────────────────┐
│      Lightroom Classic 12+      │
│                                 │
│  • Catalog photos               │
│  • Develop settings             │
│  • Metadata / EXIF              │
└─────────────────────────────────┘
```

### Communication principle

The Lua plugin is always the **HTTP client**. The Python App is always the **HTTP server**.

The plugin runs a polling loop every 300 ms (`LrTasks`).
When the App needs Lightroom data, it creates a *job* in its internal queue.
The plugin picks up this job, executes it via the Lr SDK, and returns the result to the App.
Adjustments computed by the App are likewise passed to the plugin via a job.

---

## Features

### Batch exposure balancing
Analyzes the luminance histograms of each photo.
Computes an exposure delta to bring each image back to a consistent target brightness.
Takes EXIF settings (ISO, aperture, shutter speed) into account to weight the correction.

### Batch white balance balancing
Analyzes the neutral areas and the effective color temperature of each RAW.
Computes the Lr Temperature and Tint values to make the series uniform.

### Color calibration harmonization
Analyzes dominant hues, saturation, and lightness per channel (HSL).
Harmonizes the calibration (Color Grading) across the whole series.

### Prediction map (500-1000 photo series)
On a large series, builds a model of the lighting conditions' variation.
Predicts the adjustments needed for the in-between photos.
Enables a progressive, natural correction across an entire shooting session.

---

## Tech stack

| Component | Technology | Role |
|---|---|---|
| Lr Plugin | Lua 5.1 + Lr Classic SDK 12+ | Bridge to Lightroom |
| App server | Python 3.11+ + FastAPI | Localhost HTTP API |
| GUI | PySide6 (Qt6) | User interface |
| RAW decoding | rawpy (LibRaw) | Reading Sony ARW files |
| Image analysis | numpy + OpenCV | Histograms, color analysis |
| Adjustment computation | scipy | Numerical optimization |
| Prediction map | scikit-learn | Model over photo series |
| Acceleration | Rust via PyO3 (optional) | If a custom algo bottleneck is identified |

---

## File structure

```
ABELr/
├── CLAUDE.md                      # Technical reference for development
├── documentation/
│   └── project_overview.md        # This file
│
├── plugin/                        # Lightroom plugin (Lua)
│   ├── Info.lua                   # Plugin manifest (required)
│   ├── Menu.lua                   # Lightroom menu entries
│   └── lib/
│       ├── PollingLoop.lua        # 300ms LrTasks loop
│       ├── HttpClient.lua         # HTTP requests (LrHttp)
│       ├── Adjustments.lua        # SDK adjustment application
│       ├── PhotoData.lua          # Photo data reading
│       └── Utils.lua              # Helpers, JSON
│
└── app/                           # Python application
    ├── main.py                    # Entry point (GUI + server)
    ├── server/
    │   ├── api.py                 # FastAPI routes
    │   └── job_queue.py           # Thread-safe job queue
    ├── gui/
    │   ├── main_window.py         # Main window
    │   ├── photo_panel.py         # Photo panel
    │   └── analysis_panel.py      # Analysis visualization
    ├── core/
    │   ├── raw.py                 # Sony RAW decoding
    │   ├── analysis.py            # Exposure / WB / color analysis
    │   ├── prediction.py          # Prediction map model
    │   └── adjustments.py         # Final correction computation
    ├── rust_ext/                  # (optional) Rust/PyO3 extensions
    └── requirements.txt
```

---

## Typical usage flow

```
1. User opens Lightroom, selects a series of photos
2. User launches the ABELr App (python app/main.py)
3. Plugin detects the App (polling /health)
4. User clicks "Analyze selection" in the App
5. App creates a "get_selected_photos" job
6. Plugin picks up the job → reads paths + EXIF + develop settings via the Lr SDK
7. Plugin returns the data to the App
8. App decodes each ARW (rawpy), analyzes histograms and colors
9. App computes adjustments and generates the prediction map
10. App displays a preview of the corrections in the GUI
11. User confirms
12. App creates an "apply_adjustments" job with all the corrections
13. Plugin picks up the job → applies the batch in Lr (withWriteAccessDo)
14. Photos corrected in Lightroom
```

---

## Architecture decisions

| Decision | Choice made | Reason |
|---|---|---|
| Plugin ↔ App communication | HTTP JSON polling | The plugin cannot easily expose a server; LrHttp is available as a client |
| App language | Python (not native Rust) | Mature image ecosystem (rawpy, OpenCV, scikit-learn); Rust has no equivalent |
| Rust | Optional PyO3, deferred | rawpy/OpenCV/numpy are already C/C++; profile before optimizing |
| GUI | PySide6 (Qt6) | Rich UI not possible with native Lr dialogs |
| App server | FastAPI | Sufficient for localhost; native async compatible with PySide6 |
| Lr version | 12+ (2023+) | Stable LrHttp, mature SDK, no backward-compatibility constraint |

---

## Requirements

- Adobe Lightroom Classic 12+
- Python 3.11+
- Python dependencies: see `app/requirements.txt`
- RAW files in Sony ARW format (ILCE-7M4 and LibRaw-compatible)
