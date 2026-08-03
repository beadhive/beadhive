"""What this repo declares about the dependencies it imposes on others (bh-pc2a.21).

The concern behind this file is not today's state but tomorrow's: nothing structurally stopped a
project-scope MCP server being committed, which would hand every contributor and every container a
hard dependency they never chose. The investigation found no such dependency existed — the desired
end state was already the actual state — so these are guards that keep it that way, not fixes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gitignore_patterns() -> list[str]:
    text = (ROOT / ".gitignore").read_text()
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def test_mcp_json_stays_gitignored():
    """`.mcp.json` is Claude Code's PROJECT-scope MCP config: committing one makes its servers a
    hard dependency for everyone who clones, silently. This repo declares none, and the only
    thing keeping it that way is this line in .gitignore."""
    assert ".mcp.json" in _gitignore_patterns(), (
        ".mcp.json must stay gitignored — committing one imposes a project-scope MCP "
        "dependency on every contributor and every container."
    )


def test_no_mcp_json_is_tracked():
    """The gitignore entry does nothing for a file already added with `git add -f`, so assert the
    real property — that git is not tracking one — rather than only the rule that prevents it."""
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", ".mcp.json", "**/.mcp.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert not tracked.stdout.strip(), (
        f"a project-scope MCP config is tracked: {tracked.stdout.strip()!r}. "
        "This repo declares no project-scope MCP servers."
    )


def test_repowise_is_not_a_dependency_only_an_attribution():
    """repowise is AGPL-3.0 and is a user-brought plugin, never something bh requires.

    The distinction this asserts: NAMING a tool in a comment is not depending on it. The four
    attribution comments cite where a code-review finding came from, which is provenance worth
    keeping. What must not reappear is repowise as a *required binary* — it previously showed up
    in test fixtures simulating hitch preflight output, which read as though bh's own profile
    required it.
    """
    hits = subprocess.run(
        ["git", "-C", str(ROOT), "grep", "-riIl", "repowise", "--", "src", "tests"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()

    for rel in hits:
        for i, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            if not re.search(r"repowise", line, re.I):
                continue
            assert not re.search(r"required binary ['\"]?repowise", line, re.I), (
                f"{rel}:{i} presents repowise as a required binary. It is a user-brought "
                f"AGPL plugin — use a neutral placeholder in fixtures.\n  {line.strip()}"
            )
