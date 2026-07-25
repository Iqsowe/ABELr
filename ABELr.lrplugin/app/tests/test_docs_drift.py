"""Doc/code drift guard: CLAUDE.md and ARCHITECTURE.md must stay in sync with
the code they describe (job list, MCP tools, cache tables, referenced paths,
retired phrasing). See `app/tools/check_docs.py` for the individual checks —
this is a thin pytest wrapper so drift fails CI like any other regression.
"""

from __future__ import annotations

from app.tools.check_docs import run


def test_docs_drift():
    failures = run()
    assert not failures, "doc/code drift detected:\n" + "\n".join(f"  - {f}" for f in failures)
