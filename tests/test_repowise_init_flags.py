"""bh-rcroq: guard the documented `repowise init` example against upstream CLI drift.

The invocation itself lives in operator config (worktrees.init / worktree_init), not in bh's
own source (see test_dependency_policy.py::test_repowise_is_not_a_dependency_only_an_attribution)
— bh never requires or installs repowise. This test only guards the ONE example bh ships, in
templates/config.example.yaml, so a future repowise release that renames/removes a flag fails a
local test run (when repowise happens to be installed) instead of silently no-op'ing on every
worktree provision, the way `--no-mcp-json` did.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from beadhive import repowise_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_EXAMPLE = REPO_ROOT / "src" / "beadhive" / "templates" / "config.example.yaml"

_EXAMPLE_RE = re.compile(r'run:\s*"(env REPOWISE_SKIP_EDITOR_SETUP=1 repowise init [^"]+)"')


def _documented_repowise_cmd() -> str:
    text = CONFIG_EXAMPLE.read_text()
    m = _EXAMPLE_RE.search(text)
    assert m, "config.example.yaml no longer documents a repowise init example"
    return m.group(1)


@pytest.mark.skipif(shutil.which("repowise") is None, reason="repowise not installed")
def test_documented_repowise_flags_are_accepted():
    cmd = _documented_repowise_cmd()
    tokens = shlex.split(cmd)
    flags = {t for t in tokens if t.startswith("--")}

    help_text = subprocess.run(
        ["repowise", "init", "--help"], capture_output=True, text=True, check=True
    ).stdout
    accepted = set(re.findall(r"--[\w-]+", help_text))

    unknown = flags - accepted
    assert not unknown, (
        f"config.example.yaml's repowise init example uses flag(s) {sorted(unknown)} the "
        f"installed repowise no longer accepts (bh-rcroq) — update the example (and any live "
        f"worktrees.init / worktree_init rule copied from it)."
    )


def test_documented_repowise_command_is_noninteractive_and_editor_isolated():
    tokens = shlex.split(_documented_repowise_cmd())

    assert tokens[:3] == ["env", "REPOWISE_SKIP_EDITOR_SETUP=1", "repowise"]
    assert "-y" in tokens
    assert repowise_plugin._REQUIRED_INIT_FLAGS - {"--all", "--yes"} <= set(tokens)


@pytest.mark.skipif(shutil.which("repowise") is None, reason="repowise not installed")
def test_installed_repowise_satisfies_the_plugin_init_contract():
    repowise_plugin.capabilities.cache_clear()
    assert repowise_plugin.capability_error() is None
