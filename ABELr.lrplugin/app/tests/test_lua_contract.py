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
