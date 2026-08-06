"""The Brewfile's claim about what `.mise.toml` pins must be TRUE (bh-t2ty).

The Brewfile's header names the tools it deliberately does NOT install, on the grounds that mise
pins them instead. That list said `jq` and `yq` for months and `.mise.toml` contained neither, so
the developer plane shipped without them while `flake.nix` supplied them to the Nix plane. jq is
not optional: `just check` -> `license-check` -> `scripts/osv-license-gate.sh` exits 127 without
it, which is the DEFAULT validate_cmd failing on a freshly bootstrapped Mac.

A comment asserting a fact about another file is a claim, and an unchecked claim rots — this
repo has now been bitten by that three times in one day (the justfile's stale timings, its
"NOT parallel-safe" assertion, and this). These tests make the claim self-verifying, so the next
edit to either file has to keep them honest.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BREWFILE = ROOT / "Brewfile"
MISE_TOML = ROOT / ".mise.toml"

# Scripts that invoke jq, and therefore cannot run without it on PATH. osv-license-gate is the
# one that matters most: `just check` reaches it through `license-check`.
_JQ_DEPENDENT = ("osv-license-gate.sh", "image-cve-gate.sh", "image-drift.sh", "proof-gate.sh")


def _mise_tools() -> set[str]:
    return set(tomllib.loads(MISE_TOML.read_text()).get("tools", {}))


def _brewfile_claims_mise_pins() -> set[str]:
    """The tools the Brewfile's header says are pinned in .mise.toml, read from the header
    itself rather than restated here — restating it would just move the stale claim."""
    match = re.search(r"Everything else \(([^)]*)\) is pinned in", BREWFILE.read_text())
    assert match, "the Brewfile header no longer states which tools mise pins — update this test"
    return {tool.strip() for tool in match.group(1).split(",") if tool.strip()}


def test_every_tool_the_brewfile_defers_to_mise_is_actually_pinned_there():
    """The bug itself: the header listed jq and yq, and .mise.toml pinned neither, so NOTHING on
    the developer plane installed them."""
    claimed = _brewfile_claims_mise_pins()
    pinned = _mise_tools()

    missing = sorted(claimed - pinned)
    assert not missing, (
        f"the Brewfile says .mise.toml pins {missing}, and it does not — either pin them or stop "
        f"claiming it. Pinned today: {sorted(pinned)}"
    )


def test_jq_is_pinned_because_the_default_gate_cannot_run_without_it():
    """Stated separately from the header check so that rewording the Brewfile cannot quietly
    drop the one tool `just check` hard-fails without."""
    assert "jq" in _mise_tools(), (
        "jq is required by scripts/osv-license-gate.sh, which `just check` runs via "
        "`license-check`; without it the gate exits 127 on a freshly bootstrapped machine"
    )


@pytest.mark.parametrize("script", _JQ_DEPENDENT)
def test_the_jq_dependent_scripts_still_exist_and_still_need_it(script):
    """Guards the premise of the test above: if a script stops using jq, this list should shrink
    rather than silently keep justifying a pin nothing needs."""
    path = ROOT / "scripts" / script
    assert path.is_file(), f"{script} moved or was deleted — update _JQ_DEPENDENT"
    assert "jq " in path.read_text(), f"{script} no longer invokes jq — remove it from the list"
