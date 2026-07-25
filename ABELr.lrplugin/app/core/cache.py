"""SQLite cache for analyses/computations — avoids re-decoding on every operation.

A `ABELr_cache.db` file is created **in the active catalog's folder**
(next to the `.lrcat`, cf. `catalog.resolve_catalog`). It is fed by all
pixel computations: operations check the cache first and only re-decode (GPU)
items that are missing or whose content has changed.

Five tables, **common key `uuid`** (= the Lr catalog's `id_global`):

| Table                | Decoded source                | Freshness key (`hash_*`)           |
|----------------------|--------------------------------|------------------------------------|
| `LightroomPicture`   | catalog metadata               | `hash_develop` (develop settings)  |
| `SourceRAW`          | RAW pixels (.ARW)               | `hash_raw` (RAW size+mtime)        |
| `InCameraJPEG`       | embedded in-camera JPEG        | `hash_jpeg` = RAW signature (the JPEG lives inside it) |
| `PreviewJPEG`        | Lr-rendered preview            | `hash_preview` = source file signature |
| `NeutralPreviewJPEG` | neutral render (As Shot/Exp0)  | `hash_style` (style subset)         |

`hash_jpeg`/`hash_preview` = `raw_signature` (salted size:mtime), NOT a sha1 of
the bytes (Fable 5 review DB-02 — the old doc described a mechanism that never
existed).

**Unified column naming** (backward compatibility is not a goal):
family as prefix (`luma_`/`wb_`/`tone_`/`neutral_`/`hsl_`/`delta_`/`mask_`/
`exif_`/`profile_`/`hash_`), **scope as suffix** `_global`/`_sharp` (never bare).
Measurements exist in pairs, **global** (whole frame) + **sharp** (sharp zone,
`core.sharpness`) wherever relevant — the global↔sharp delta reveals
backlight / background≠subject color cast, and global is the fallback if the
sharp mask degenerates.

Version control via `PRAGMA user_version`: if the stored schema doesn't match
`SCHEMA_VERSION`, all tables are **dropped and recreated** (no row-by-row
migration — the cache is rebuilt from the RAWs).

Standard read-write SQLite (WAL) — coexists with the `.lrcat` Lightroom has
open (separate file, no lock on the catalog).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .analysis import ExposureStats
from .pipeline import RenderAnalysis
from .render_metrics import BandStats, NeutralStats, ToneStats

CACHE_FILENAME = "ABELr_cache.db"

# **Schema** version (table structure). A structure change
# triggers a DROP+recreate via `PRAGMA user_version`.
SCHEMA_VERSION = 5  # bump: PLAN.md W3 — removed InCameraJPEG's 4 delta_* columns (dead, never read)

# Salted into the freshness hashes (`raw_signature`, `style_hash`):
# a change in the measurement algorithm (new global/sharp pairs, deltas…)
# must invalidate all cached content without migration — bump when the computation changes.
ANALYSIS_VERSION = "v7-measure-grid"  # bump: PLAN.md R2/R3 — common measurement grid
# (RAW/embedded-JPEG downsampled to render_metrics.MEASURE_LONG_EDGE before
# sharp_mask/tone_stats/band_stats) + resolution-proportional pre-Laplacian blur.

# "Style" subset of develop settings = everything that affects the NEUTRAL render
# (`render_probe` probe: WB As Shot + Exposure2012=0 + **HSL 24 zeroed**, everything
# else intact). Freshness key for NeutralPreview (`hash_style`).
#
# - The 24 HSL keys are **excluded**: the probe neutralizes them, so an HSL Apply
#   must NOT invalidate the anchor (otherwise a full re-probe on every cycle).
# - Tone settings (Contrast/Highlights/…/Dehaze/Vibrance/Saturation) and the
#   crop are **included**: they are not neutralized by the probe and do change
#   the neutral render — without them the anchor would go stale silently.
# - Temp/Tint/Exposure stay out of the key (neutralized by the probe).
_STYLE_KEYS = (
    "CameraProfile", "ProcessVersion",
    "Contrast2012", "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
    "Clarity2012", "Dehaze", "Vibrance", "Saturation", "Texture",
    "CropLeft", "CropRight", "CropTop", "CropBottom", "CropAngle",
    # Camera calibration: not WB/Expo/HSL, not neutralized by the probe → changes
    # the neutral render, must invalidate the anchor (the "calib" axis, k-NN transplant).
    "EnableCalibration", "ShadowTint",
    "RedHue", "RedSaturation", "GreenHue", "GreenSaturation", "BlueHue", "BlueSaturation",
    # Color Grading — hybrid SDK names (Fable 5 review DB-01): shadows/HL Hue+Sat
    # = SplitToning*, everything else ColorGrade*. The old ColorGradeShadowHue…
    # names don't exist in the SDK and never matched anything.
    "SplitToningShadowHue", "SplitToningShadowSaturation",
    "SplitToningHighlightHue", "SplitToningHighlightSaturation",
    "SplitToningBalance",
    "ColorGradeShadowLum", "ColorGradeHighlightLum",
    "ColorGradeMidtoneHue", "ColorGradeMidtoneSat", "ColorGradeMidtoneLum",
    "ColorGradeGlobalHue", "ColorGradeGlobalSat", "ColorGradeGlobalLum",
    "ColorGradeBlending",
    # Curves: parametric + point tables (JSON), not neutralized by the probe.
    "ParametricShadows", "ParametricDarks", "ParametricLights", "ParametricHighlights",
    "ParametricShadowSplit", "ParametricMidtoneSplit", "ParametricHighlightSplit",
    "ToneCurveName2012", "ToneCurvePV2012",
    "ToneCurvePV2012Red", "ToneCurvePV2012Green", "ToneCurvePV2012Blue",
)


# --------------------------------------------------------------------------- #
# Location + connection
# --------------------------------------------------------------------------- #
def cache_path_for_catalog(catalog_path: str | Path) -> Path:
    """Path of the cache `.db`: same folder as the `.lrcat`."""
    return Path(catalog_path).parent / CACHE_FILENAME


def open_cache(catalog_path: str | Path) -> sqlite3.Connection:
    """Opens (creates if needed) the cache read-write and ensures the schema.

    WAL + `synchronous=NORMAL`: fast writes, crash-robust, without blocking
    reads. `check_same_thread=False` because the GUI and its workers (QThread)
    may access it; each access stays serialized by SQLite.
    """
    db = cache_path_for_catalog(catalog_path)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


_TABLES = (
    "LightroomPicture", "SourceRAW", "InCameraJPEG", "PreviewJPEG", "NeutralPreviewJPEG",
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Creates the schema; DROP+recreate if `user_version` ≠ `SCHEMA_VERSION`.

    Incremental migration is not used: since the cache is fully rebuilt from
    the RAW/JPEG files, a structure change drops and recreates the tables
    (much simpler and safer than a series of `ALTER`s).
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        for t in _TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        _init_schema(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    else:
        _init_schema(conn)  # CREATE IF NOT EXISTS — no-op if already present


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS LightroomPicture (
            uuid              TEXT PRIMARY KEY,
            path              TEXT,
            catalog_path      TEXT,
            exif_camera       TEXT,
            exif_iso          INTEGER,
            exif_aperture     REAL,
            exif_shutter      TEXT,
            exif_focal_length REAL,
            profile_capture   TEXT,      -- in-camera creative profile (IN/SH/ST/VV2…)
            profile_dcp       TEXT,      -- Lr CameraProfile, extracted flat
            develop_json      TEXT,      -- JSON snapshot of current develop settings
            hash_develop      TEXT,
            hash_style        TEXT,      -- style subset (cf. _STYLE_KEYS)
            is_seed           INTEGER DEFAULT 0,
            cached_at         REAL
        );

        CREATE TABLE IF NOT EXISTS SourceRAW (
            uuid                   TEXT PRIMARY KEY,
            hash_raw               TEXT,
            wb_asshot_rg           REAL,
            wb_asshot_bg           REAL,
            luma_mean_global       REAL,
            luma_median_global     REAL,
            luma_clip_hi_global    REAL,
            luma_clip_lo_global    REAL,
            luma_mean_sharp        REAL,
            luma_median_sharp      REAL,
            luma_clip_hi_sharp     REAL,
            luma_clip_lo_sharp     REAL,
            wb_grayworld_rg_global REAL,
            wb_grayworld_bg_global REAL,
            wb_grayworld_rg_sharp  REAL,
            wb_grayworld_bg_sharp  REAL,
            mask_sharp_frac        REAL,
            ev100                  REAL,
            profile_capture        TEXT,
            tone_sharp             TEXT,  -- JSON ToneStats (sharp zone)
            hsl_sharp              TEXT,  -- JSON list[BandStats] (sharp zone)
            cached_at              REAL
        );

        CREATE TABLE IF NOT EXISTS InCameraJPEG (
            uuid              TEXT PRIMARY KEY,
            hash_jpeg         TEXT,
            tone_sharp        TEXT,
            neutral_sharp     TEXT,
            hsl_sharp         TEXT,
            tone_global       TEXT,
            neutral_global    TEXT,
            hsl_global        TEXT,
            mask_sharp_frac   REAL,
            profile_capture   TEXT,
            cached_at         REAL
        );

        CREATE TABLE IF NOT EXISTS PreviewJPEG (
            uuid            TEXT PRIMARY KEY,
            hash_preview    TEXT,
            tone_sharp      TEXT,
            neutral_sharp   TEXT,
            hsl_sharp       TEXT,
            tone_global     TEXT,
            neutral_global  TEXT,
            hsl_global      TEXT,
            mask_sharp_frac REAL,
            cached_at       REAL
        );

        CREATE TABLE IF NOT EXISTS NeutralPreviewJPEG (
            uuid            TEXT PRIMARY KEY,
            hash_style      TEXT,
            tone_sharp      TEXT,
            neutral_sharp   TEXT,
            hsl_sharp       TEXT,
            tone_global     TEXT,
            neutral_global  TEXT,
            hsl_global      TEXT,
            mask_sharp_frac REAL,
            wb_asshot_temp  REAL,   -- numeric Temperature read after WhiteBalance='As Shot'
            wb_asshot_tint  REAL,   -- numeric Tint likewise (basis for an absolute WB correction)
            cached_at       REAL
        );

        CREATE INDEX IF NOT EXISTS idx_picture_is_seed
            ON LightroomPicture(is_seed);
        """
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Freshness hash — "has the item's content changed?"
# --------------------------------------------------------------------------- #
def raw_signature(path: str | Path) -> str:
    """Quick signature of a RAW file: `size:mtime_ns` (no re-read).

    Enough to detect a rewrite of the `.ARW`; much faster than a sha256 of
    the whole file (~50 MB). Returns `"0:0"` if the file is missing.
    """
    p = Path(path)
    try:
        st = p.stat()
        return f"{st.st_size}:{st.st_mtime_ns}:{ANALYSIS_VERSION}"
    except OSError:
        # The fallback is salted too (Fable 5 review DB-04): never actually
        # written to the database in practice (decoding fails first), but no
        # cross-version collision.
        return f"0:0:{ANALYSIS_VERSION}"


def develop_hash(develop: dict[str, Any] | None) -> str:
    """Stable hash of a develop-settings dict (detects a manual edit)."""
    payload = json.dumps(develop or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def style_hash(develop: dict[str, Any] | None) -> str:
    """Hash of the **style** subset of the settings (cf. `_STYLE_KEYS`).

    Freshness key for the neutral render: only changes if the DCP profile / HSL /
    Color Grading changes — insensitive to Temp/Tint/Exposure (which we neutralize).
    """
    dev = develop or {}
    subset = {k: dev[k] for k in _STYLE_KEYS if k in dev}
    payload = json.dumps(subset, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1((payload + ANALYSIS_VERSION).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# dataclass (de)serialization ↔ JSON
# --------------------------------------------------------------------------- #
def _tone_to_json(t: ToneStats | None) -> str | None:
    return json.dumps(asdict(t)) if t is not None else None


def _tone_from_json(s: str | None) -> ToneStats | None:
    return ToneStats(**json.loads(s)) if s else None


def _neutral_to_json(n: NeutralStats | None) -> str | None:
    return json.dumps(asdict(n)) if n is not None else None


def _neutral_from_json(s: str | None) -> NeutralStats | None:
    return NeutralStats(**json.loads(s)) if s else None


def _bands_to_json(bands: list[BandStats] | None) -> str | None:
    return json.dumps([asdict(b) for b in bands]) if bands is not None else None


def _bands_from_json(s: str | None) -> list[BandStats] | None:
    return [BandStats(**d) for d in json.loads(s)] if s else None


def _analysis_from_row(row: sqlite3.Row, scope: str) -> RenderAnalysis | None:
    """Rebuilds a RenderAnalysis from the `tone_<scope>` columns etc."""
    tone = _tone_from_json(row[f"tone_{scope}"])
    neutral = _neutral_from_json(row[f"neutral_{scope}"])
    bands = _bands_from_json(row[f"hsl_{scope}"])
    if tone is None and neutral is None and bands is None:
        return None
    return RenderAnalysis(tone=tone, neutral=neutral, bands=bands)


# --------------------------------------------------------------------------- #
# LightroomPicture (anchor)
# --------------------------------------------------------------------------- #
def put_picture(
    conn: sqlite3.Connection,
    uuid: str,
    *,
    path: str | None,
    catalog_path: str | None,
    exif: dict[str, Any] | None,
    current_develop: dict[str, Any] | None,
    profile_capture: str | None = None,
    commit: bool = True,
) -> None:
    """Inserts/updates the anchor row (metadata + develop snapshot).

    UPSERT (not `INSERT OR REPLACE`): preserves `is_seed`, otherwise every
    re-analysis would overwrite the seed marking with the default value.
    `profile_dcp` and `hash_style` are derived from the develop snapshot.

    `commit=False`: the caller batches several writes into one transaction
    and commits itself (Fable 5 review P-07/DB-03 — one commit per loop
    iteration froze the GUI and cost ~1,500 commits per run).
    """
    exif = exif or {}
    dev = current_develop or {}
    conn.execute(
        """INSERT INTO LightroomPicture
           (uuid, path, catalog_path, exif_camera, exif_iso, exif_aperture,
            exif_shutter, exif_focal_length, profile_capture, profile_dcp,
            develop_json, hash_develop, hash_style, cached_at, is_seed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 0)
           ON CONFLICT(uuid) DO UPDATE SET
               path=excluded.path, catalog_path=excluded.catalog_path,
               exif_camera=excluded.exif_camera, exif_iso=excluded.exif_iso,
               exif_aperture=excluded.exif_aperture, exif_shutter=excluded.exif_shutter,
               exif_focal_length=excluded.exif_focal_length,
               profile_capture=COALESCE(excluded.profile_capture, LightroomPicture.profile_capture),
               profile_dcp=excluded.profile_dcp, develop_json=excluded.develop_json,
               hash_develop=excluded.hash_develop, hash_style=excluded.hash_style,
               cached_at=excluded.cached_at""",
        (
            uuid, path, catalog_path, exif.get("camera"), exif.get("iso"),
            exif.get("aperture"), exif.get("shutter_speed"), exif.get("focal_length"),
            profile_capture, dev.get("CameraProfile"),
            json.dumps(dev), develop_hash(dev), style_hash(dev), time.time(),
        ),
    )
    if commit:
        conn.commit()


# --------------------------------------------------------------------------- #
# Seeds — explicit marking (replaces the WhiteBalance=="Custom" heuristic)
# --------------------------------------------------------------------------- #
def set_seed(conn: sqlite3.Connection, uuid: str, value: bool, commit: bool = True) -> None:
    """Marks/unmarks a photo as a seed. Creates the anchor row if missing."""
    conn.execute(
        "INSERT INTO LightroomPicture (uuid, is_seed, cached_at) VALUES (?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET is_seed=excluded.is_seed",
        (uuid, int(value), time.time()),
    )
    if commit:
        conn.commit()


def is_seed(conn: sqlite3.Connection, uuid: str) -> bool:
    row = conn.execute(
        "SELECT is_seed FROM LightroomPicture WHERE uuid=?", (uuid,)
    ).fetchone()
    return bool(row["is_seed"]) if row is not None else False


def list_seed_uuids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT uuid FROM LightroomPicture WHERE is_seed=1").fetchall()
    return [r["uuid"] for r in rows]


def get_picture(conn: sqlite3.Connection, uuid: str) -> Optional[dict[str, Any]]:
    """`LightroomPicture` row (path, current_develop, is_seed…), no freshness check."""
    row = conn.execute("SELECT * FROM LightroomPicture WHERE uuid=?", (uuid,)).fetchone()
    if row is None:
        return None
    return {
        "uuid": row["uuid"], "path": row["path"], "catalog_path": row["catalog_path"],
        "current_develop": json.loads(row["develop_json"]) if row["develop_json"] else {},
        "profile_capture": row["profile_capture"], "profile_dcp": row["profile_dcp"],
        "hash_style": row["hash_style"], "is_seed": bool(row["is_seed"]),
    }


def get_source_raw_latest(conn: sqlite3.Connection, uuid: str) -> Optional[dict[str, Any]]:
    """Latest known RAW analysis for `uuid`, **without checking the freshness hash**.

    Used for seeds (k-NN vector): a seed's RAW rarely changes after it's
    marked, and requiring a re-analysis on every match would be costly.
    """
    row = conn.execute(
        "SELECT * FROM SourceRAW WHERE uuid=?", (uuid,)
    ).fetchone()
    return _source_raw_dict(row) if row is not None else None


def get_preview_jpeg_latest(conn: sqlite3.Connection, uuid: str) -> Optional[RenderAnalysis]:
    """Latest known rendered preview for `uuid` (sharp zone), without checking the
    freshness hash (a seed's style reference — cf. `get_source_raw_latest`)."""
    row = conn.execute(
        "SELECT * FROM PreviewJPEG WHERE uuid=?", (uuid,)
    ).fetchone()
    return _analysis_from_row(row, "sharp") if row is not None else None


# --------------------------------------------------------------------------- #
# SourceRAW (RAW pixels: exposure + as-shot WB + gray-world, global + sharp zone)
# --------------------------------------------------------------------------- #
def _source_raw_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Maps a SourceRAW row → consumer dict.

    Keys **stable** for existing consumers (`seed_match`, worker):
    `asshot_rg`/`asshot_bg`/`tone`/`bands`/`exposure`/`grayworld_rg`/`grayworld_bg`
    (= global measurements, historical behavior) + new `*_sharp` keys.
    """
    exposure_global = (
        ExposureStats(
            row["luma_mean_global"], row["luma_median_global"],
            row["luma_clip_hi_global"], row["luma_clip_lo_global"],
        )
        if row["luma_mean_global"] is not None
        else None
    )
    exposure_sharp = (
        ExposureStats(
            row["luma_mean_sharp"], row["luma_median_sharp"],
            row["luma_clip_hi_sharp"], row["luma_clip_lo_sharp"],
        )
        if row["luma_mean_sharp"] is not None
        else None
    )
    return {
        "asshot_rg": row["wb_asshot_rg"], "asshot_bg": row["wb_asshot_bg"],
        "exposure": exposure_global, "exposure_sharp": exposure_sharp,
        "grayworld_rg": row["wb_grayworld_rg_global"], "grayworld_bg": row["wb_grayworld_bg_global"],
        "grayworld_rg_sharp": row["wb_grayworld_rg_sharp"],
        "grayworld_bg_sharp": row["wb_grayworld_bg_sharp"],
        "mask_sharp_frac": row["mask_sharp_frac"], "ev100": row["ev100"],
        "profile_capture": row["profile_capture"],
        "tone": _tone_from_json(row["tone_sharp"]),
        "bands": _bands_from_json(row["hsl_sharp"]),
    }


def get_source_raw(
    conn: sqlite3.Connection, uuid: str, hash_raw: str
) -> Optional[dict[str, Any]]:
    """Returns the cached RAW scalars if `hash_raw` matches, otherwise None."""
    row = conn.execute(
        "SELECT * FROM SourceRAW WHERE uuid=? AND hash_raw=?", (uuid, hash_raw)
    ).fetchone()
    return _source_raw_dict(row) if row is not None else None


def put_source_raw(
    conn: sqlite3.Connection,
    uuid: str,
    hash_raw: str,
    *,
    asshot_rg: float | None,
    asshot_bg: float | None,
    exposure_global: ExposureStats | None = None,
    exposure_sharp: ExposureStats | None = None,
    grayworld_global: tuple[float, float] | None = None,
    grayworld_sharp: tuple[float, float] | None = None,
    mask_sharp_frac: float | None = None,
    ev100: float | None = None,
    profile_capture: str | None = None,
    tone: ToneStats | None = None,
    bands: list[BandStats] | None = None,
    commit: bool = True,
) -> None:
    """Writes the SourceRAW row (global + sharp-zone pairs). All measurement
    fields are optional (autocorrect may write only the as-shot WB)."""
    gg = grayworld_global or (None, None)
    gs = grayworld_sharp or (None, None)
    conn.execute(
        """INSERT OR REPLACE INTO SourceRAW
           (uuid, hash_raw, wb_asshot_rg, wb_asshot_bg,
            luma_mean_global, luma_median_global, luma_clip_hi_global, luma_clip_lo_global,
            luma_mean_sharp, luma_median_sharp, luma_clip_hi_sharp, luma_clip_lo_sharp,
            wb_grayworld_rg_global, wb_grayworld_bg_global,
            wb_grayworld_rg_sharp, wb_grayworld_bg_sharp,
            mask_sharp_frac, ev100, profile_capture, tone_sharp, hsl_sharp, cached_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            uuid, hash_raw, asshot_rg, asshot_bg,
            exposure_global.mean_luma if exposure_global else None,
            exposure_global.median_luma if exposure_global else None,
            exposure_global.clipped_highlights if exposure_global else None,
            exposure_global.clipped_shadows if exposure_global else None,
            exposure_sharp.mean_luma if exposure_sharp else None,
            exposure_sharp.median_luma if exposure_sharp else None,
            exposure_sharp.clipped_highlights if exposure_sharp else None,
            exposure_sharp.clipped_shadows if exposure_sharp else None,
            gg[0], gg[1], gs[0], gs[1],
            mask_sharp_frac, ev100, profile_capture,
            _tone_to_json(tone), _bands_to_json(bands), time.time(),
        ),
    )
    if commit:
        conn.commit()


# --------------------------------------------------------------------------- #
# InCameraJPEG (in-camera JPEG: tone/neutral/bands global + sharp)
# --------------------------------------------------------------------------- #
def _in_camera_jpeg_dict(row: sqlite3.Row) -> dict[str, Any]:
    sharp = _analysis_from_row(row, "sharp")
    return {
        "sharp": sharp,
        "global": _analysis_from_row(row, "global"),
        "tone": sharp.tone if sharp else None,
        "bands": sharp.bands if sharp else None,
        "mask_sharp_frac": row["mask_sharp_frac"],
        "profile_capture": row["profile_capture"],
    }


def get_in_camera_jpeg(
    conn: sqlite3.Connection, uuid: str, hash_jpeg: str
) -> Optional[dict[str, Any]]:
    """Cached InCameraJPEG row if `hash_jpeg` matches, otherwise None.

    Returns a dict: `sharp`/`global` (RenderAnalysis), `mask_sharp_frac`,
    `profile_capture`. The `tone`/`bands` key (sharp zone) is also exposed for
    consumers that only use the embedded target.
    """
    row = conn.execute(
        "SELECT * FROM InCameraJPEG WHERE uuid=? AND hash_jpeg=?", (uuid, hash_jpeg)
    ).fetchone()
    return _in_camera_jpeg_dict(row) if row is not None else None


def get_in_camera_jpeg_latest(conn: sqlite3.Connection, uuid: str) -> Optional[dict[str, Any]]:
    """Latest known in-camera JPEG analysis for `uuid`, without checking the
    freshness hash (offline validation on historical measurements — cf.
    `get_source_raw_latest`, same rationale)."""
    row = conn.execute("SELECT * FROM InCameraJPEG WHERE uuid=?", (uuid,)).fetchone()
    return _in_camera_jpeg_dict(row) if row is not None else None


def put_in_camera_jpeg(
    conn: sqlite3.Connection,
    uuid: str,
    hash_jpeg: str,
    *,
    sharp: RenderAnalysis | None,
    glob: RenderAnalysis | None = None,
    mask_sharp_frac: float | None = None,
    profile_capture: str | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO InCameraJPEG
           (uuid, hash_jpeg, tone_sharp, neutral_sharp, hsl_sharp,
            tone_global, neutral_global, hsl_global, mask_sharp_frac, profile_capture, cached_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            uuid, hash_jpeg,
            _tone_to_json(sharp.tone) if sharp else None,
            _neutral_to_json(sharp.neutral) if sharp else None,
            _bands_to_json(sharp.bands) if sharp else None,
            _tone_to_json(glob.tone) if glob else None,
            _neutral_to_json(glob.neutral) if glob else None,
            _bands_to_json(glob.bands) if glob else None,
            mask_sharp_frac, profile_capture, time.time(),
        ),
    )
    if commit:
        conn.commit()


# --------------------------------------------------------------------------- #
# PreviewJPEG / NeutralPreviewJPEG (renders: full global + sharp analysis)
# --------------------------------------------------------------------------- #
def _put_render_dual(
    conn: sqlite3.Connection,
    table: str,
    uuid: str,
    key_col: str,
    key_val: str,
    sharp: RenderAnalysis | None,
    glob: RenderAnalysis | None,
    mask_sharp_frac: float | None,
    commit: bool = True,
) -> None:
    conn.execute(
        f"""INSERT OR REPLACE INTO {table}
            (uuid, {key_col}, tone_sharp, neutral_sharp, hsl_sharp,
             tone_global, neutral_global, hsl_global, mask_sharp_frac, cached_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            uuid, key_val,
            _tone_to_json(sharp.tone) if sharp else None,
            _neutral_to_json(sharp.neutral) if sharp else None,
            _bands_to_json(sharp.bands) if sharp else None,
            _tone_to_json(glob.tone) if glob else None,
            _neutral_to_json(glob.neutral) if glob else None,
            _bands_to_json(glob.bands) if glob else None,
            mask_sharp_frac, time.time(),
        ),
    )
    if commit:
        conn.commit()


def get_preview_jpeg(
    conn: sqlite3.Connection, uuid: str, hash_preview: str
) -> Optional[RenderAnalysis]:
    """Rendered preview (sharp zone) if `hash_preview` matches, otherwise None."""
    row = conn.execute(
        "SELECT * FROM PreviewJPEG WHERE uuid=? AND hash_preview=?", (uuid, hash_preview)
    ).fetchone()
    return _analysis_from_row(row, "sharp") if row is not None else None


def put_preview_jpeg(
    conn: sqlite3.Connection,
    uuid: str,
    hash_preview: str,
    *,
    sharp: RenderAnalysis,
    glob: RenderAnalysis | None = None,
    mask_sharp_frac: float | None = None,
    commit: bool = True,
) -> None:
    _put_render_dual(conn, "PreviewJPEG", uuid, "hash_preview", hash_preview,
                     sharp, glob, mask_sharp_frac, commit)


def _neutral_preview_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sharp": _analysis_from_row(row, "sharp"),
        "glob": _analysis_from_row(row, "global"),
        "asshot_temp": row["wb_asshot_temp"],
        "asshot_tint": row["wb_asshot_tint"],
        "mask_sharp_frac": row["mask_sharp_frac"],
    }


def get_neutral_preview(
    conn: sqlite3.Connection, uuid: str, hash_style: str
) -> Optional[dict[str, Any]]:
    """Neutral render (WB As Shot / Exp 0 / HSL 0, style intact) if `hash_style` matches.

    Returns a dict: `sharp`/`glob` (RenderAnalysis), `asshot_temp`/`asshot_tint`
    (the As Shot's numeric WB, read by the plugin during the probe), `mask_sharp_frac`.
    """
    row = conn.execute(
        "SELECT * FROM NeutralPreviewJPEG WHERE uuid=? AND hash_style=?", (uuid, hash_style)
    ).fetchone()
    return _neutral_preview_dict(row) if row is not None else None


def get_neutral_preview_latest(conn: sqlite3.Connection, uuid: str) -> Optional[dict[str, Any]]:
    """Latest known neutral render for `uuid`, without checking the freshness
    hash (offline validation on historical measurements — cf. `get_source_raw_latest`,
    same rationale)."""
    row = conn.execute("SELECT * FROM NeutralPreviewJPEG WHERE uuid=?", (uuid,)).fetchone()
    return _neutral_preview_dict(row) if row is not None else None


def put_neutral_preview(
    conn: sqlite3.Connection,
    uuid: str,
    hash_style: str,
    *,
    sharp: RenderAnalysis,
    glob: RenderAnalysis | None = None,
    mask_sharp_frac: float | None = None,
    asshot_temp: float | None = None,
    asshot_tint: float | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO NeutralPreviewJPEG
           (uuid, hash_style, tone_sharp, neutral_sharp, hsl_sharp,
            tone_global, neutral_global, hsl_global, mask_sharp_frac,
            wb_asshot_temp, wb_asshot_tint, cached_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            uuid, hash_style,
            _tone_to_json(sharp.tone) if sharp else None,
            _neutral_to_json(sharp.neutral) if sharp else None,
            _bands_to_json(sharp.bands) if sharp else None,
            _tone_to_json(glob.tone) if glob else None,
            _neutral_to_json(glob.neutral) if glob else None,
            _bands_to_json(glob.bands) if glob else None,
            mask_sharp_frac, asshot_temp, asshot_tint, time.time(),
        ),
    )
    if commit:
        conn.commit()
