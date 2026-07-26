"""GPU/CPU context — device selection, VRAM budget.

Project policy (updated — user decision): **GPU-first, CPU fallback if no usable
CUDA GPU is present**. Historically this module enforced GPU-strict behavior
(`require_cuda()` raised instead of falling back to CPU); the goal of running the
plugin on machines without an NVIDIA GPU led to lifting that constraint. This
module centralizes the decision:

- `device()` returns `cuda` if available, otherwise `cpu` — **never raises**;
  the rest of the pipeline (`gpu_raw`, `gpu_jpeg`, `render_metrics_gpu`,
  `gpu_schedule`) routes its device through this call, so it switches automatically.
- `require_cuda()` remains available for call sites that explicitly want to
  require CUDA (calibration/GPU-parity tools) — it is no longer the default path.
- `vram_budget_bytes()` exposes the VRAM available to the wave scheduler
  (`gpu_schedule`) on GPU, or a conservative fixed RAM cap on CPU.

The only CPU work that stays **irreducible** even with a GPU present (see
`gpu_raw`) is unpacking/decompressing the ARW container via LibRaw: no GPU codec
exists for Sony ARW.
"""

from __future__ import annotations

import torch


class GpuUnavailable(RuntimeError):
    """Raised by `require_cuda()` when a CUDA GPU is explicitly required but absent."""


# Only SUCCESS is memoized (Fable 5 review C-02): a transient init failure
# (OOM at startup, driver busy) memoized by lru_cache would have condemned the
# process until restart even though the GPU had come back.
_cuda_ok = False

# Conservative host RAM budget used by the scheduler when running on CPU
# (no VRAM to query). Deliberately cautious: CPU is already the slow path,
# no point risking a swap by aiming too high.
_CPU_BUDGET_BYTES = 2_000_000_000


def _diagnose() -> str | None:
    """Returns an error message if CUDA is unusable, otherwise None."""
    global _cuda_ok
    if _cuda_ok:
        return None
    if not torch.cuda.is_available():
        return (
            "torch.cuda.is_available() == False — no NVIDIA driver, or CPU-only torch "
            "build. Install torch CUDA (cu124) to enable the GPU: "
            "pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124"
        )
    try:
        torch.zeros(1, device="cuda")  # force CUDA context init
    except Exception as exc:  # broken context, OOM at startup, etc.
        return f"CUDA context initialization failed: {exc}"
    _cuda_ok = True
    return None


def is_available() -> bool:
    """True if a usable CUDA GPU is present (never raises)."""
    return _diagnose() is None


def require_cuda() -> None:
    """Raises `GpuUnavailable` if no usable GPU is present.

    Reserved for call sites that explicitly want to require CUDA (calibration,
    parity tests) — the normal pipeline uses `device()`, which never raises.
    """
    err = _diagnose()
    if err:
        raise GpuUnavailable(f"CUDA GPU required but unavailable: {err}")


def device() -> torch.device:
    """Compute device: `cuda` if usable, otherwise `cpu` (never raises)."""
    return torch.device("cuda") if is_available() else torch.device("cpu")


def device_name() -> str:
    """Name of the current device — GPU name if CUDA, otherwise an explicit CPU marker."""
    if is_available():
        return torch.cuda.get_device_name(0)
    return "CPU (fallback — no CUDA GPU detected)"


# --------------------------------------------------------------------------- #
# VRAM/RAM budget (for the wave scheduler)
# --------------------------------------------------------------------------- #
def free_total_vram() -> tuple[int, int]:
    """(free, total) in bytes of VRAM on the current device.

    Requires CUDA (no notion of VRAM on CPU) — the scheduler should go through
    `vram_budget_bytes()`, which handles the CPU branch.
    """
    require_cuda()
    free, total = torch.cuda.mem_get_info()
    return int(free), int(total)


def vram_budget_bytes(margin: float = 0.75) -> int:
    """Reasonably usable VRAM = free × margin; fixed RAM cap if CPU.

    The margin (<1) reserves headroom for the CUDA context, fragmentation, and
    decode/demosaic intermediate buffers — on 8 GB the margin avoids a hard OOM.
    On CPU there's no VRAM to measure: the fixed `_CPU_BUDGET_BYTES` caps the
    scheduler's waves to a reasonable size.
    """
    if not is_available():
        return _CPU_BUDGET_BYTES
    free, _ = free_total_vram()
    return int(free * margin)


def empty_cache() -> None:
    """Releases VRAM cached by the torch allocator at the end of a wave."""
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def synchronize() -> None:
    """Waits for all in-flight kernels on the device to finish."""
    if torch.cuda.is_available():
        torch.cuda.synchronize()
