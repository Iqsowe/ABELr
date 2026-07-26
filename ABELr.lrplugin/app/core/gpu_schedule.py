"""VRAM-aware scheduler — feeds the GPU without saturating the 8 GB budget (RAM+VRAM combined).

Policy: pixel decoding **on GPU**, container unpack/I-O on **bounded** CPU. Two
stages, reworked by the Fable 5 review (G7: P-01/P-02/P-03/P-06):

- **Bounded CPU producer** (`ThreadPoolExecutor`, physical cores): `rawpy` releases
  the GIL → true parallelism to unpack bayer/JPEG bytes into **host RAM**. The pool
  unpacks wave N+1 **while** the GPU processes wave N (double-buffer, P-01) —
  at most 2 waves in flight in RAM.
- **Unified unpack** (P-02): a path missing from both the RAW side and the camera
  JPEG side only opens the ARW container **once** (`_unpack_combined`: bayer +
  as-shot WB + thumb bytes in the same `with rawpy.imread`).
- **GPU consumer with waves sized PER PIPELINE** (P-03): full-resolution RAW
  and JPEG (~15x smaller) each get their own VRAM estimate — nvJPEG waves
  go from 3-5 to 30-60 images. `empty_cache` is no longer called on every wave
  (sync + allocator flush per wave): now reactive on OOM plus periodic hygiene.

Pixel computation falls back to CPU automatically if no CUDA GPU is usable
(`gpu.device()` — see `core/gpu.py`, GPU-first + CPU-fallback policy): this
scheduler makes no assumption about the device, it just sizes the waves via
`gpu.vram_budget_bytes()` (real VRAM if GPU, fixed RAM cap if CPU). Measurement
parity is unchanged for the same device (same kernels, only the scheduling changes) —
revalidate with `tools/validate_gpu_vs_libraw` after any change here (that script
stays GPU-only, see its header).
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import torch

from . import embedded_jpeg, gpu, gpu_jpeg, gpu_raw, render_metrics_gpu
from .embedded_jpeg import EmbeddedExtract, RawReference
from .gpu_raw import RawBayer, RawGpuResult
from .pipeline import RenderAnalysisDual

_log = logging.getLogger("abelr.gpu_schedule")

Progress = Optional[Callable[[int, int], None]]

# Transient per-image VRAM estimates, PER PIPELINE (Fable 5 review P-03):
# - Full-resolution RAW (~24-33 MP): demosaic + Lab + band broadcast.
# - Decoded JPEG (preview/camera, ~0.5-3 MP): ~15x smaller. A single
#   RAW-sized estimate gave nvJPEG waves of 3-5 images (batching pointless).
_EST_BYTES_RAW_IMG = 33_000_000 * 36
_EST_BYTES_JPEG_IMG = 80_000_000

# Wave caps (also bound host RAM: 2 waves in flight with the prefetch).
_WAVE_CAP_RAW = 16
_WAVE_CAP_JPEG = 64

# Allocator hygiene: empty_cache only every N waves (P-03 — calling it every
# wave cost a sync + cudaMalloc repaid on the next wave).
_EMPTY_CACHE_EVERY = 8


def _cpu_workers() -> int:
    """CPU unpack pool cap = physical cores (HT heuristic), capped."""
    logical = os.cpu_count() or 4
    return max(1, min(logical // 2, 8))


def _wave_size(est_bytes_per_img: int, cap: int) -> int:
    """GPU wave size = VRAM budget / per-image estimate (at least 1)."""
    try:
        budget = gpu.vram_budget_bytes()
    except Exception:
        return 4
    return max(1, min(cap, budget // est_bytes_per_img))


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _maybe_empty_cache(wave_idx: int) -> None:
    if (wave_idx + 1) % _EMPTY_CACHE_EVERY == 0:
        gpu.empty_cache()


def _with_oom_retry(fn, *args):
    """Runs a GPU step; on OOM, releases cached VRAM and retries ONCE."""
    try:
        return fn(*args)
    except torch.cuda.OutOfMemoryError:
        gpu.empty_cache()
        return fn(*args)


# --------------------------------------------------------------------------- #
# Unified unpack (P-02): ONE rawpy open -> bayer and/or embedded reference
# --------------------------------------------------------------------------- #
@dataclass
class _CombinedUnpack:
    bayer: RawBayer | None
    extract: EmbeddedExtract | None


def _unpack_combined(args: tuple[str, bool, bool]) -> _CombinedUnpack:
    """(path, need_bayer, need_jpeg) -> bayer and/or extract, a single open."""
    path, need_bayer, need_jpeg = args
    import rawpy

    bayer: RawBayer | None = None
    extract: EmbeddedExtract | None = None
    try:
        with rawpy.imread(str(path)) as r:
            if need_bayer:
                bayer = gpu_raw.bayer_from_open(r)
            if need_jpeg:
                extract = embedded_jpeg.extract_from_open(r)
    except Exception:
        pass  # unreadable RAW -> (None, None), same contract as unpack_raw/extract_reference
    return _CombinedUnpack(bayer, extract)


# --------------------------------------------------------------------------- #
# Combined RAW + camera JPEG pass (CPU/GPU double-buffer, P-01)
# --------------------------------------------------------------------------- #
def process_combined_batch(
    raw_paths: list[str],
    embedded_paths: list[str],
    progress: Progress = None,
) -> tuple[dict[str, Optional[RawGpuResult]], dict[str, RawReference]]:
    """Decodes RAW and/or camera JPEG in a single pass.

    A path present in both lists only opens the container once.
    Returns ({path: RawGpuResult|None}, {path: RawReference}) — keys limited
    to the requested lists. `progress` counts one unit per analysis produced
    (len(raw_paths) + len(embedded_paths) total).
    """
    raw_out: dict[str, Optional[RawGpuResult]] = {}
    emb_out: dict[str, RawReference] = {}
    need_raw = set(raw_paths)
    need_emb = set(embedded_paths)
    all_paths = list(dict.fromkeys([*raw_paths, *embedded_paths]))
    if not all_paths:
        return raw_out, emb_out

    total = len(raw_paths) + len(embedded_paths)
    done = 0

    def _tick() -> None:
        nonlocal done
        done += 1
        if progress:
            progress(done, total)

    workers = _cpu_workers()
    # Wave sized for the heaviest workload present; at least the width of
    # the CPU pool so the prefetch keeps all workers busy.
    if need_raw:
        wave = max(workers, _wave_size(_EST_BYTES_RAW_IMG, _WAVE_CAP_RAW))
    else:
        wave = max(workers, _wave_size(_EST_BYTES_JPEG_IMG, _WAVE_CAP_JPEG))
    waves = list(_chunks(all_paths, wave))

    with ThreadPoolExecutor(max_workers=workers) as ex:

        def _submit(wave_paths: list[str]):
            return [
                ex.submit(_unpack_combined, (p, p in need_raw, p in need_emb))
                for p in wave_paths
            ]

        next_futs = _submit(waves[0])
        for wi, wave_paths in enumerate(waves):
            futs = next_futs
            # Double-buffer (P-01): wave N+1 unpacks on the CPU pool
            # while this thread consumes wave N on GPU.
            next_futs = _submit(waves[wi + 1]) if wi + 1 < len(waves) else []

            extracts: list[tuple[str, EmbeddedExtract]] = []
            for path, fut in zip(wave_paths, futs):
                cu = fut.result()
                if path in need_raw:
                    raw_out[path] = (
                        _with_oom_retry(gpu_raw.process_bayer_gpu, cu.bayer)
                        if cu.bayer is not None else None
                    )
                    _tick()
                if path in need_emb:
                    extracts.append(
                        (path, cu.extract or EmbeddedExtract(None, None, None))
                    )

            # Camera JPEG for the wave: BATCHED nvJPEG decode + dual metrics.
            if extracts:
                blobs = [e.jpeg_bytes for _, e in extracts if e.jpeg_bytes]
                pos = [i for i, (_, e) in enumerate(extracts) if e.jpeg_bytes]
                decoded = _with_oom_retry(gpu_jpeg.decode_blobs, blobs) if blobs else []
                dec_by_pos = dict(zip(pos, decoded))
                for i, (path, e) in enumerate(extracts):
                    tone = bands = None
                    sharp = glob = mask_frac = None
                    chw = dec_by_pos.get(i)
                    if chw is not None:
                        dual = _with_oom_retry(
                            render_metrics_gpu.analyze_rendered_gpu_dual, chw
                        )
                        sharp, glob, mask_frac = dual.sharp, dual.glob, dual.mask_sharp_frac
                        tone, bands = sharp.tone, sharp.bands
                    emb_out[path] = RawReference(
                        tone, bands, e.asshot_rg, e.asshot_bg,
                        sharp=sharp, glob=glob, mask_sharp_frac=mask_frac,
                    )
                    _tick()

            _maybe_empty_cache(wi)

    gpu.empty_cache()  # end of batch: release VRAM cached by the allocator
    return raw_out, emb_out


# --------------------------------------------------------------------------- #
# Rendered preview (preview/thumbnail): decode + GPU analysis by wave
# --------------------------------------------------------------------------- #
def analyze_render_blobs(
    items: list[tuple[str, bytes]], progress: Progress = None
) -> dict[str, Optional[RenderAnalysisDual]]:
    """Analyzes a list of (key, rendered JPEG bytes) on GPU. {key: RenderAnalysisDual|None}.

    Returns the global + sharp-zone pair (the caller uses `.sharp` for the current
    state measurement and stores the full pair in the cache)."""
    out: dict[str, Optional[RenderAnalysisDual]] = {}
    if not items:
        return out
    done = 0
    chunk = max(1, _wave_size(_EST_BYTES_JPEG_IMG, _WAVE_CAP_JPEG))
    for wi, group in enumerate(_chunks(items, chunk)):
        decoded = _with_oom_retry(gpu_jpeg.decode_blobs, [blob for _, blob in group])
        for (key, _blob), chw in zip(group, decoded):
            undersized = (
                render_metrics_gpu.reject_if_undersized(width=chw.shape[-1], height=chw.shape[-2])
                if chw is not None else None
            )
            if undersized is not None:
                # PLAN.md R1: measured, never silently accepted below the grid.
                _log.warning("analyze_render_blobs: skipping %s — %s", key, undersized)
                chw = None
            out[key] = (
                _with_oom_retry(render_metrics_gpu.analyze_rendered_gpu_dual, chw)
                if chw is not None else None
            )
            done += 1
            if progress:
                progress(done, len(items))
        _maybe_empty_cache(wi)
    gpu.empty_cache()
    return out
