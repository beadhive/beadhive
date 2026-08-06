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


def test_the_image_bakes_no_agent_harness_at_all():
    """NEITHER harness is shipped, for two DIFFERENT reasons that must both keep holding.

    claude (bh-pc2a.36): `@anthropic-ai/claude-code` declares "SEE LICENSE IN README.md", not an
    SPDX identifier. Baking it makes anyone who publishes the image a redistributor of proprietary
    software under Anthropic's commercial terms.

    codex (bh-lnrn): Apache-2.0 and freely redistributable — it PASSES the licence gate, and was
    baked for exactly that reason. It is excluded by DECISION: the image ships the runtime and the
    means, never the harness. Asserted here beside claude because the invariant is now "no
    harness", not "no proprietary harness"; a rule with an "except the permissive one" clause is
    the rule that lets the next harness in.
    """
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    installed = "\n".join(ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#"))

    assert "@anthropic-ai/claude-code" not in installed, (
        "the Dockerfile installs the proprietary harness — the image must ship the means "
        "(`bh dep install claude`), never the licensed artifact."
    )
    assert "@openai/codex" not in installed, (
        "the Dockerfile installs codex — permissively licensed, but the image ships no harness "
        "at all (bh-lnrn). `bh dep install codex` names the remedy instead."
    )


def test_the_image_ships_no_node_runtime():
    """node had exactly ONE consumer — the baked `npm install -g @openai/codex`.

    bh-hsus.1 had already moved `bh dep install` off npm: claude installs via its own installer,
    and codex's route names brew / a GitHub release / nixpkgs#codex, none of them npm. So removing
    codex left node with no consumer at all, and re-adding a node runtime would be re-adding a
    dependency nothing in this image uses (bh-lnrn)."""
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    installed = "\n".join(ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#"))

    assert "nodejs.org" not in installed, "the Dockerfile fetches a Node runtime nothing consumes."
    assert "npm " not in installed and "NPM_CONFIG_PREFIX" not in installed, (
        "npm plumbing survives in the Dockerfile; nothing in the image installs via npm."
    )


def test_the_manifest_does_not_claim_a_harness_the_image_lacks():
    """In-image `bh setup check` trusts the manifest INSTEAD of probing, so a component listed
    but not shipped is a lie the check cannot catch — precisely because it never probes."""
    manifest_script = (ROOT / "docker" / "write-manifest.sh").read_text()
    emitted = "\n".join(
        ln for ln in manifest_script.splitlines() if not ln.lstrip().startswith("#")
    )

    assert "claude" not in emitted, (
        "write-manifest.sh still records claude as a shipped component; it is no longer baked."
    )
    assert "codex" not in emitted, (
        "write-manifest.sh still records codex as a shipped component; bh-lnrn de-baked it."
    )
    assert "node" not in emitted, (
        "write-manifest.sh still records node; it went with codex, its only consumer."
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
