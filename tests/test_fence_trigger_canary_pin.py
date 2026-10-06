"""Fast-gate tripwire for the Dolt/bd trigger-semantics canary (bh-p07dv; ADR condition 9).

Pure Python, no ``dolt`` / ``bd`` needed: compares the pins in ``flake.nix`` and
``docker/toolchain-metadata.json`` with :data:`harness.trigger_canary.CANARY_PINS`, the versions
the canary last passed on. A Dolt or bd pin bump that does not also move ``CANARY_PINS`` fails
here, in ``just check``, so the canary is re-run before the bump can land.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness import trigger_canary as tc
from harness import write_guard as wg

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("tool", sorted(tc.CANARY_PINS))
def test_every_pin_source_names_the_version_the_canary_last_passed_on(tool):
    pins = tc.pinned_versions(ROOT)
    for source, versions in pins.items():
        assert versions[tool] == tc.CANARY_PINS[tool], (
            f"{tc.CONDITION}: the {tool} pin moved ({source} pins {versions[tool]!r}, the "
            f"trigger-semantics canary last passed on {tc.CANARY_PINS[tool]!r}). Install the "
            f"new pin, run `just fence-canary` until green, then update CANARY_PINS in "
            f"tests/harness/trigger_canary.py."
        )


def test_the_canary_wiring_is_documented_beside_the_pins():
    flake = (ROOT / "flake.nix").read_text()
    for release in ("beadsRelease = pkgs:", "doltRelease = pkgs:"):
        head = flake[: flake.index(release)]
        comment = head[head.rindex("\n\n") :]
        assert "just fence-canary" in comment and "CANARY_PINS" in comment, (
            f"the comment above `{release}` in flake.nix must name the condition-9 canary"
        )


def test_the_fence_canary_recipe_selects_the_marker():
    justfile = (ROOT / "justfile").read_text()
    match = re.search(r"^fence-canary:\n((?:    .*\n)+)", justfile, re.MULTILINE)
    assert match and "fence_canary" in match.group(1)
    markers = (ROOT / "pyproject.toml").read_text()
    assert '"fence_canary:' in markers


def test_the_generated_metadata_still_names_both_tools():
    names = {
        row["name"] for row in json.loads((ROOT / "docker/toolchain-metadata.json").read_text())
    }
    assert {"dolt", "bd"} <= names


def test_the_static_guard_shape_and_trigger_count_hold():
    """The engine-free half of the canary also runs in the fast gate."""
    assert tc.guard_shape_violations(wg.guard_ddl()) == []
    assert tc.composed_trigger_total() == tc.COMPOSED_TRIGGER_COUNT
