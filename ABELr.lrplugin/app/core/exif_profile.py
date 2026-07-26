"""Sony camera creative profile (Creative Style / Creative Look).

The creative profile chosen in-camera (Standard, IN, SH, FL, VV, VV2, Neutral...)
strongly changes the embedded JPEG's rendering **and** biases exposure habits (RAW
is often underexposed under IN/SH to protect the JPEG's highlights). This profile
is **not** exposed by the Lightroom SDK nor by LibRaw/rawpy — it lives in the Sony
maker note. It is read via **exiftool** (external binary, already required on the
machine), outside Lr, directly from the `.ARW` (like `raw.read_asshot_wb`).

Tag used (proven on real ILCE-7M4 ARW files, several batches): `Sony:CreativeStyle`
(e.g. `Standard`, `SH`, `VV2`). The `SR2DataIFD*:ColorMode` tags are a static
enumeration table (all possible values) — **not** the actual value, do not use.

Batch extraction via `-@ argfile` (UTF-8 temp file): amortizes the process launch
cost over series of 500-1000 AND avoids the Windows argv limit
(CreateProcess ~32,767 characters, exceeded from ~300 paths onward — Fable 5 review A-01).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

_log = logging.getLogger("abelr.exif_profile")

# Sony maker note tag carrying the effective creative profile of the shot.
_TAG = "-Sony:CreativeStyle"

# Only warn once per process about exiftool being missing (otherwise it spams a batch).
_missing_warned = False


def _warn_exiftool_missing() -> None:
    """Warns ONCE that the creative profile will be missing (otherwise silent degradation)."""
    global _missing_warned
    if _missing_warned:
        return
    _missing_warned = True
    _log.warning(
        "exiftool not found on PATH — Sony creative profile (CreativeStyle) "
        "unavailable: embedded matching ignores the profile (degraded quality). "
        "Install exiftool (https://exiftool.org) and add it to PATH."
    )


def read_capture_profiles(paths: list[str]) -> dict[str, str | None]:
    """Reads the creative profile of a **batch** of RAW files in a single exiftool call.

    A single process launch for the whole batch (exiftool spawn cost dominates
    on large series). Returns `{path: profile|None}` — any path without a
    readable tag is mapped to None. Never raises.
    """
    out: dict[str, str | None] = {p: None for p in paths}
    if not paths:
        return out

    # -s3: raw value (no tag name). -j: JSON with SourceFile -> reliable
    # mapping even if exiftool reorders the batch. Paths passed via argfile `-@`
    # (one argument per line, UTF-8): no Windows argv limit, and
    # `-charset filename=UTF8` makes it read the argfile/write SourceFile in UTF-8
    # (accented FR paths — A-01/A-02).
    argfile = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".args", delete=False
        ) as f:
            argfile = f.name
            f.write("-charset\nfilename=UTF8\n-j\n-s3\n")
            f.write(_TAG + "\n")
            for p in paths:
                f.write(p + "\n")
        proc = subprocess.run(
            ["exiftool", "-@", argfile],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(120, len(paths)), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _warn_exiftool_missing()  # binary missing -> warn (once), no crash
        return out
    finally:
        if argfile is not None:
            try:
                os.unlink(argfile)
            except OSError:
                pass

    if proc.returncode not in (0, 1) or not proc.stdout.strip():
        return out

    import json

    try:
        entries = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return out

    for entry in entries:
        src = entry.get("SourceFile")
        style = entry.get("CreativeStyle")
        if src is None:
            continue
        # exiftool returns SourceFile as a normalized path (slashes): re-map to
        # the matching input key (separator-insensitive comparison).
        key = _match_path(src, paths)
        if key is not None and style:
            out[key] = str(style).strip()
    return out


def _match_path(src: str, paths: list[str]) -> str | None:
    """Finds the original path matching an exiftool `SourceFile`."""
    norm_src = src.replace("\\", "/").casefold()
    for p in paths:
        if p.replace("\\", "/").casefold() == norm_src:
            return p
    # Fallback: compare on the filename alone.
    src_name = Path(src).name.casefold()
    for p in paths:
        if Path(p).name.casefold() == src_name:
            return p
    return None
