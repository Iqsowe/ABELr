"""Doc/code drift checker.

Runs stdlib-only (no `app.core` imports) so it works even without the venv's
heavy deps — it parses source files as text, it never executes the app.

Catches the class of error CLAUDE.md/ARCHITECTURE.md kept accumulating: a
hardcoded count or path that was true when written and silently went stale
(see PLAN.md / CLAUDE.md rework, 2026-07-25). Run via:

    python -m app.tools.check_docs

Exit 0 = clean. Exit 1 = failures printed, one per line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ABELr.lrplugin/
ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ROOT.parent

MODELS_PY = ROOT / "app" / "server" / "models.py"
POLLING_LOOP_LUA = ROOT / "PollingLoop.lua"
MCP_SERVER_PY = ROOT / "app" / "mcp" / "server.py"
CACHE_PY = ROOT / "app" / "core" / "cache.py"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ARCHITECTURE_MD = REPO_ROOT / "documentation" / "ARCHITECTURE.md"
PLAN_MD = REPO_ROOT / "PLAN.md"
README_MD = REPO_ROOT / "README.md"
APP_README_MD = ROOT / "app" / "README.md"

DOC_FILES = [CLAUDE_MD, ARCHITECTURE_MD]
# Only the docs this checker's own doc-rework keeps accurate. PLAN.md / README.md /
# app/README.md may carry historical phrasing (e.g. PLAN.md's execution-rules
# section) that is out of scope here — flagging them would fail on day one for
# content nobody asked to rewrite.
RETIRED_PHRASE_FILES = [CLAUDE_MD, ARCHITECTURE_MD]

# Extra tool names the MCP server exposes that are not job types.
MCP_NON_JOB_TOOLS = {"bridge_status", "ping"}

RETIRED_PHRASES = [
    "LrAutomation.lrplugin",
    "launch_app.ps1",
]
# Checked case-insensitively, substring match is enough to flag revival.
RETIRED_PHRASES_CI = [
    "no cpu fallback",
    "gpu-strict",
]


def _fail(failures: list[str], msg: str) -> None:
    failures.append(msg)


def check_job_parity(failures: list[str]) -> None:
    text = MODELS_PY.read_text(encoding="utf-8")
    # Lines like: SET_RATING = "set_rating"   inside class JobType(str, Enum)
    enum_block = text.split("class JobType", 1)[-1].split("\nclass ", 1)[0]
    enum_jobs = set(re.findall(r'=\s*"([a-z_]+)"', enum_block))

    lua_text = POLLING_LOOP_LUA.read_text(encoding="utf-8")
    lua_jobs = set(re.findall(r"jobType\s*==\s*'([a-z_]+)'", lua_text))

    missing_in_lua = enum_jobs - lua_jobs
    missing_in_enum = lua_jobs - enum_jobs
    if missing_in_lua:
        _fail(failures, f"JobType members with no PollingLoop.lua dispatch branch: {sorted(missing_in_lua)}")
    if missing_in_enum:
        _fail(failures, f"PollingLoop.lua branches with no JobType member: {sorted(missing_in_enum)}")
    if not enum_jobs:
        _fail(failures, "check_job_parity: parsed zero JobType members — parser likely broken")


def check_mcp_coverage(failures: list[str]) -> None:
    text = MODELS_PY.read_text(encoding="utf-8")
    enum_block = text.split("class JobType", 1)[-1].split("\nclass ", 1)[0]
    enum_jobs = set(re.findall(r'=\s*"([a-z_]+)"', enum_block))

    mcp_text = MCP_SERVER_PY.read_text(encoding="utf-8")
    tool_names = set(re.findall(r"@mcp\.tool\(\)\s*\n\s*async def (\w+)", mcp_text))

    unexpected = tool_names - enum_jobs - MCP_NON_JOB_TOOLS
    if unexpected:
        _fail(failures, f"MCP tools with no matching JobType and not in MCP_NON_JOB_TOOLS: {sorted(unexpected)}")
    if not tool_names:
        _fail(failures, "check_mcp_coverage: parsed zero @mcp.tool() functions — parser likely broken")


def check_cache_tables(failures: list[str]) -> None:
    cache_text = CACHE_PY.read_text(encoding="utf-8")
    m = re.search(r"_TABLES\s*=\s*\(([^)]*)\)", cache_text, re.DOTALL)
    if not m:
        _fail(failures, "check_cache_tables: could not find _TABLES tuple in cache.py")
        return
    tables = set(re.findall(r'"(\w+)"', m.group(1)))

    for doc in DOC_FILES:
        doc_text = doc.read_text(encoding="utf-8")
        for table in tables:
            if table not in doc_text:
                _fail(failures, f"{doc.name}: cache table `{table}` (from cache.py _TABLES) not mentioned")


_REMOVED_MARKERS = ("removed", "never existed")


def check_dead_paths(failures: list[str]) -> None:
    # Matches `some/path.ext` or [text](some/path.md) style references.
    path_pattern = re.compile(r"`([\w./\\-]+\.\w+)`|\]\(([\w./\\-]+\.\w+)\)")
    # Docs use three conventions for a path: relative to the repo root
    # (`ABELr.lrplugin/...`), relative to ABELr.lrplugin/ (`app/core/x.py`),
    # or module-shorthand relative to ABELr.lrplugin/app/ (`core/x.py`,
    # common inside a section already scoped to `core/`/`gui/`/`server/`).
    bases = [REPO_ROOT, ROOT, ROOT / "app"]
    for doc in DOC_FILES:
        base_dir = doc.parent
        text = doc.read_text(encoding="utf-8")
        lines = text.splitlines()
        for m in path_pattern.finditer(text):
            candidate = m.group(1) or m.group(2)
            if not candidate or "://" in candidate:
                continue
            # Skip things that look like code identifiers, not paths (heuristic:
            # require at least one path separator, i.e. not a bare filename
            # inside a sentence like `pytest.ini`).
            if "/" not in candidate and "\\" not in candidate:
                continue
            line_no = text.count("\n", 0, m.start())
            line_text = lines[line_no] if line_no < len(lines) else ""
            if any(marker in line_text.lower() for marker in _REMOVED_MARKERS):
                continue  # documented as intentionally deleted/never built
            candidate_bases = [base_dir, *bases]
            if not any((b / candidate).resolve().exists() for b in candidate_bases):
                _fail(failures, f"{doc.name}: referenced path does not exist on disk: {candidate}")


def check_retired_phrases(failures: list[str]) -> None:
    for f in RETIRED_PHRASE_FILES:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        for phrase in RETIRED_PHRASES:
            if phrase in text:
                _fail(failures, f"{f.name}: contains retired reference `{phrase}`")
        lower = text.lower()
        for phrase in RETIRED_PHRASES_CI:
            if phrase in lower:
                _fail(failures, f"{f.name}: contains retired phrase (case-insensitive) `{phrase}`")


def check_no_hardcoded_cache_versions(failures: list[str]) -> None:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    if re.search(r'ANALYSIS_VERSION\s*=\s*"', text):
        _fail(failures, "CLAUDE.md: contains a literal ANALYSIS_VERSION value — should point to cache.py instead")
    if re.search(r"SCHEMA_VERSION\s*=\s*\d", text):
        _fail(failures, "CLAUDE.md: contains a literal SCHEMA_VERSION value — should point to cache.py instead")


CHECKS = [
    ("job parity (JobType <-> PollingLoop.lua)", check_job_parity),
    ("MCP tool coverage", check_mcp_coverage),
    ("cache table names in docs", check_cache_tables),
    ("dead paths referenced in docs", check_dead_paths),
    ("retired phrases", check_retired_phrases),
    ("no hardcoded cache versions in CLAUDE.md", check_no_hardcoded_cache_versions),
]


def run() -> list[str]:
    failures: list[str] = []
    for _name, fn in CHECKS:
        fn(failures)
    return failures


def main() -> int:
    failures = run()
    for name, _fn in CHECKS:
        pass
    if failures:
        print(f"check_docs: {len(failures)} failure(s)\n")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"check_docs: OK ({len(CHECKS)} checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
