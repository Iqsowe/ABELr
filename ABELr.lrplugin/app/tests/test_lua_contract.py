"""PLAN.md N2a — text-level contract check on `PollingLoop.lua`.

Parses the Lua source as text (same stdlib-only spirit as
`app/tools/check_docs.py` — no Lua interpreter available here). Guards
against reverting to a hardcoded `status = 'ok'` in the two branches that
used to lie about failure: a probe/fetch where every thumbnail failed must
report `status = 'error'`, not blindly 'ok'.
"""

from __future__ import annotations

import re
from pathlib import Path

# ABELr.lrplugin/
ROOT = Path(__file__).resolve().parents[2]
POLLING_LOOP_LUA = ROOT / "PollingLoop.lua"
THUMBNAILS_LUA = ROOT / "Thumbnails.lua"

_HARDCODED_OK = re.compile(r"""status\s*=\s*['"]ok['"]""")


def _branch(text: str, job_type: str) -> str:
    """Slices the `elseif jobType == '<job_type>' then ... end` block."""
    marker = f"jobType == '{job_type}'"
    start = text.index(marker)
    # Block ends at the next `elseif jobType ==` or the closing `end` of dispatch.
    rest = text[start:]
    next_elseif = rest.find("elseif jobType ==", len(marker))
    if next_elseif == -1:
        return rest
    return rest[:next_elseif]


def test_get_thumbnails_status_is_computed_not_hardcoded():
    text = POLLING_LOOP_LUA.read_text(encoding="utf-8")
    block = _branch(text, "get_thumbnails")
    assert not _HARDCODED_OK.search(block), (
        "get_thumbnails must not hardcode status='ok' — a batch where every "
        "thumbnail failed has to report an error status"
    )
    assert "errors_summary" in block
    assert "applied" in block
    assert "total" in block


def test_render_probe_status_is_computed_not_hardcoded():
    text = POLLING_LOOP_LUA.read_text(encoding="utf-8")
    block = _branch(text, "render_probe")
    assert not _HARDCODED_OK.search(block), (
        "render_probe must not hardcode status='ok' — a probe where nothing "
        "rendered has to report an error status (mcp/tools.py raises on it)"
    )
    assert "errors_summary" in block
    assert "applied" in block
    assert "total" in block


def test_summarize_errors_helper_is_shared():
    text = POLLING_LOOP_LUA.read_text(encoding="utf-8")
    # The 5-item truncation logic must exist exactly once (the shared
    # helper), not be duplicated inline across job branches.
    assert text.count("math.min(5, #errors)") == 1
    assert "local function summarizeErrors" in text


def test_fetch_probe_does_not_silently_drop_unresolved_or_apply_failures():
    """PLAN.md N2b — pins the two "no silent drops" fixes in `fetchProbe`."""
    text = THUMBNAILS_LUA.read_text(encoding="utf-8")
    assert "unresolved" in text
    assert "uuid not found" in text
    assert "applyErrors" in text
    assert "APPLY FAILED" in text


def test_thumbnails_fetch_reads_a_budget_argument():
    """PLAN.md N3c — Thumbnails.fetch/fetchProbe must consume a shipped budget,
    not fall back to a single hardcoded per-photo constant unconditionally."""
    text = THUMBNAILS_LUA.read_text(encoding="utf-8")
    assert "function Thumbnails.fetch(photos, width, height, budget)" in text
    assert "function Thumbnails.fetchProbe(adjustments, width, height, settle, budget)" in text
    assert "LUA_BUDGET_FRACTION" in text


def _fetch_probe_body(text: str) -> str:
    start = text.index("function Thumbnails.fetchProbe(")
    end = text.index("\nreturn Thumbnails", start)
    return text[start:end]


def test_fetch_probe_refreshes_heartbeat_in_every_loop_and_slices_settle():
    """PLAN.md N4a — a slow probe (apply/readback/restore/settle) must not let
    the heartbeat go stale, or /bridge reports disconnected mid-probe and
    blocks the NEXT user action, not just this one."""
    body = _fetch_probe_body(THUMBNAILS_LUA.read_text(encoding="utf-8"))
    # apply loop + readback loop + restore loop + the sliced settle wait.
    assert body.count("_G.ABELR_BRIDGE_HEARTBEAT = os.time()") >= 4
    # settle sleep sliced into <=1s steps, not one bare LrTasks.sleep(settle).
    assert "LrTasks.sleep(settle)" not in body
    assert "math.min(1, settleLeft)" in body


def test_thumbnails_gen_persisted_across_reload_and_dir_not_duplicated():
    """PLAN.md N4b — generation counter must survive a plugin reload (a plain
    module-local resets to 0, letting `_<gen>.jpg` filenames collide across
    reloads), and the thumbsDir() path/creation logic must have one owner
    (Utils), not a second private copy inside Thumbnails.lua."""
    text = THUMBNAILS_LUA.read_text(encoding="utf-8")
    assert "_G.ABELR_THUMB_GEN" in text
    assert "local function thumbsDir()" not in text
    assert "Utils.thumbsDir()" in text
    assert "local function sweepOldFiles(" in text


def test_polling_loop_forwards_timeout_s_on_both_branches():
    """PLAN.md N3c/N3d — guards against Lua reverting to a hardcoded constant
    while Python keeps shipping a budget in the payload."""
    text = POLLING_LOOP_LUA.read_text(encoding="utf-8")
    thumbs_block = _branch(text, "get_thumbnails")
    probe_block = _branch(text, "render_probe")
    assert "payload.timeout_s" in thumbs_block
    assert "payload.timeout_s" in probe_block
