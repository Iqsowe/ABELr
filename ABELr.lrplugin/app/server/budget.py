"""Shared time-budget contract for `render_probe` / `get_thumbnails` jobs.

Fixes PLAN.md N section's root cause: two hard-coded per-photo timeout
constants, tuned independently in two languages (Lua's
`THUMB_SECONDS_PER_PHOTO`/`PROBE_SECONDS_PER_PHOTO`, Python's
`_SECONDS_PER_PHOTO` in `neutral_preview_worker.py`/`fresh_render_worker.py`),
that silently drifted apart until the smaller one always lost the timeout
race. The App computes the budget once here; both sides consume it — Python
ships it in the job payload (`timeout_s`, N3b), Lua derives its own wait from
it (`budget * LUA_BUDGET_FRACTION`, N3c).

Constants fit with margin over the **worst** measured probe (7.4s/photo at
2048x2048), not the median (5.7s/photo) — see PLAN.md Origin point 2.
"""

from __future__ import annotations

from typing import Literal

# Per-megapixel cost of a forced-regeneration render (apply -> settle -> Lr
# re-renders the preview -> requestJpegThumbnail). Floored by PROBE_MIN so a
# tiny probe still gets a sane minimum.
PROBE_S_PER_MPX = 2.5
PROBE_MIN = 1.0

# get_thumbnails is read-only (no apply/restore) — flat per-photo cost,
# independent of resolution (dominated by the request/callback round trip,
# not by pixel count).
FETCH_S_PER_PHOTO = 1.0

# Fixed overhead per job (queue submit/poll dispatch, JSON marshalling) added
# on top of the per-photo cost, plus whatever settle the caller passes.
JOB_OVERHEAD = 5.0
MIN_TIMEOUT = 30.0

# Chunk size is derived from a target wall-clock per job rather than tuned as
# an independent constant — see PLAN.md N3a for why (couples chunk size,
# timeout, heartbeat gap and JobQueue._ENTRY_TTL through one knob).
TARGET_JOB_SECONDS = 120.0

# Lua's own internal wait is a fraction of the shipped budget, never equal to
# it: Lua must return a partial result with per-photo errors before Python
# gives up, or a bare Python-side timeout carries zero diagnostic.
LUA_BUDGET_FRACTION = 0.8

JobKind = Literal["probe", "fetch"]


def probe_seconds_per_photo(width: int, height: int) -> float:
    """Per-photo cost of a forced-regeneration render_probe at `width`x`height`."""
    mpx = (width * height) / 1e6
    return max(PROBE_MIN, PROBE_S_PER_MPX * mpx)


def fetch_seconds_per_photo(width: int, height: int) -> float:
    """Per-photo cost of a read-only get_thumbnails fetch. Resolution-independent."""
    del width, height  # part of the shared signature, unused for this kind
    return FETCH_S_PER_PHOTO


def _per_photo(kind: JobKind, width: int, height: int) -> float:
    if kind == "probe":
        return probe_seconds_per_photo(width, height)
    if kind == "fetch":
        return fetch_seconds_per_photo(width, height)
    raise ValueError(f"unknown job kind: {kind!r}")


def job_timeout(
    n: int, width: int, height: int, settle: float, kind: JobKind
) -> float:
    """Wall-clock budget for a job of `n` photos at `width`x`height`.

    `settle` is the extra delay the caller already waits for Lr to regenerate
    a preview (0 for a plain fetch, `DEFAULT_SETTLE`/`_RETRY_SETTLE` for a probe).
    """
    per_photo = _per_photo(kind, width, height)
    return max(MIN_TIMEOUT, JOB_OVERHEAD + settle + n * per_photo)


def chunk_size(width: int, height: int, kind: JobKind, cap: int) -> int:
    """Photos per job, bounded so `chunk_size * per_photo <= TARGET_JOB_SECONDS`.

    `cap` is the caller's own ceiling (e.g. render_probe keeps a small batch to
    bound the window photos sit in a neutral state; get_thumbnails can afford
    a larger one since it's read-only) — always clamped to at least 1.
    """
    per_photo = _per_photo(kind, width, height)
    from_budget = int(TARGET_JOB_SECONDS // per_photo)
    return max(1, min(cap, from_budget))
