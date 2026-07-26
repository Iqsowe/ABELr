"""Embedded camera JPEG — exposure prior (and initial look).

Every ARW contains the JPEG rendered **by the camera body**: it's the exposure
judged correct at capture time + the initial look (Sony Creative Look). sRGB
display-referred. Used as an **exposure-target fallback** when seeds are missing
(user decision: seeds first, camera JPEG second).

`extract_from_open`/`EmbeddedExtract`: CPU-side unpack (as-shot WB + undecoded
JPEG bytes) for `gpu_schedule`'s unified rawpy open — decoding and measuring
the JPEG happens on GPU (`gpu_jpeg`, `render_metrics_gpu`), not here.
`RawReference` is the resulting measurement dataclass, built by
`gpu_schedule.process_combined_batch`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pipeline import RenderAnalysis
from .render_metrics import BandStats, ToneStats


@dataclass
class RawReference:
    """Everything extracted from a photo via ONE RAW open.

    embedded_tone / embedded_bands: sharp-zone measurements of the camera JPEG
                                     (embedded-mode targets; = `sharp.tone`/`sharp.bands`).
    asshot_rg / asshot_bg          : as-shot WB (input to the seeds WB model).
    sharp / glob                   : full analysis (tone+neutral+bands), sharp zone
                                     and global, of the camera JPEG (dual GPU path).
    mask_sharp_frac                : fraction of pixels retained by the sharp mask.
    """

    embedded_tone: ToneStats | None
    embedded_bands: list[BandStats] | None
    asshot_rg: float | None
    asshot_bg: float | None
    sharp: RenderAnalysis | None = None
    glob: RenderAnalysis | None = None
    mask_sharp_frac: float | None = None


@dataclass
class EmbeddedExtract:
    """Output of the embedded CPU unpack — **picklable**, JPEG bytes **not decoded**.

    Decoding the camera JPEG is delegated to the GPU (nvJPEG). This unpack only
    opens the RAW to read the as-shot WB (metadata) and extract the thumbnail bytes.
    """

    asshot_rg: float | None
    asshot_bg: float | None
    jpeg_bytes: bytes | None


def extract_from_open(r) -> EmbeddedExtract:
    """EmbeddedExtract from an ALREADY-open rawpy handle.

    Extracted for the scheduler's unified unpack (Fable 5 review P-02): the same
    rawpy open serves both the bayer (`gpu_raw.bayer_from_open`) AND the camera JPEG.
    """
    import rawpy

    wb = list(r.camera_whitebalance)  # [R, G1, B, G2]
    try:
        thumb = r.extract_thumb()
        jpeg = bytes(thumb.data) if thumb.format == rawpy.ThumbFormat.JPEG else None
    except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
        jpeg = None
    g = wb[1] or 1.0
    return EmbeddedExtract(wb[0] / g, wb[2] / g, jpeg)
