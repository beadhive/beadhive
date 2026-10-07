"""Fast-gate tripwire for the Dolt/bd trigger-semantics canary (bh-p07dv, bh-vb3yf; ADR cond. 9).

Pure Python, no ``dolt`` / ``bd`` needed: checks that the pins in ``flake.nix`` and
``docker/toolchain-metadata.json`` lie inside the configured ranges
(:func:`harness.trigger_canary.canary_ranges`: defaults ``dolt >=2.3.5,<2.4`` / ``bd >=1.3.0,<1.4``,
overridable by ``BH_FENCE_CANARY_DOLT_RANGE`` / ``BH_FENCE_CANARY_BD_RANGE``). A bump inside a range
needs no test edit (the integration canary still runs on every land and proves it); a pin outside
fails here, in ``just check``, so the range is widened deliberately after ``just fence-canary``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from harness import trigger_canary as tc
from harness import write_guard as wg

ROOT = Path(__file__).resolve().parents[1]


def _outside(pins, ranges):
    return [
        f"{source}: {violation}"
        for source, versions in pins.items()
        for violation in tc.range_violations(versions, ranges)
    ]


def _tripwire_message(outside):
    return (
        f"{tc.CONDITION}: a Dolt/bd pin is outside the trigger-canary range: "
        + "; ".join(outside)
        + ". Install the new pin, run `just fence-canary` until green, then widen the range "
        "deliberately (BH_FENCE_CANARY_*_RANGE / DEFAULT_RANGES in "
        "tests/harness/trigger_canary.py)."
    )


def test_every_pin_source_is_inside_the_configured_ranges():
    outside = _outside(tc.pinned_versions(ROOT), tc.canary_ranges())
    assert not outside, _tripwire_message(outside)


def test_the_default_ranges_are_pinned_and_admit_todays_pins():
    assert tc.DEFAULT_RANGES == {"dolt": ">=2.3.5,<2.4", "bd": ">=1.3.0,<1.4"}
    assert tc.canary_ranges({}) == {k: SpecifierSet(v) for k, v in tc.DEFAULT_RANGES.items()}
    assert not _outside({"today": {"dolt": "2.3.5", "bd": "1.3.0"}}, tc.canary_ranges({}))


def test_every_default_is_configurable_through_its_environment_variable():
    ranges = tc.canary_ranges({"BH_FENCE_CANARY_DOLT_RANGE": ">=3,<4"})
    assert str(ranges["dolt"]) == "<4,>=3"
    assert str(ranges["bd"]) == "<1.4,>=1.3.0"  # untouched default
    assert set(tc.RANGE_ENV) == set(tc.DEFAULT_RANGES)


@pytest.mark.parametrize("tool", sorted(tc.DEFAULT_RANGES))
@pytest.mark.parametrize("bad", ["", "  ", "not-a-range", ">=2.3.5;<2.4", "2.3.x", "~~1"])
def test_a_malformed_range_is_refused_never_ignored_or_clamped(tool, bad):
    with pytest.raises(tc.CanaryRangeError, match=tc.RANGE_ENV[tool]):
        tc.canary_ranges({tc.RANGE_ENV[tool]: bad})


@pytest.mark.parametrize(
    ("pins", "inside"),
    [
        ({"dolt": "2.3.5", "bd": "1.3.0"}, True),
        ({"dolt": "2.3.9", "bd": "1.3.4"}, True),  # an in-range bump needs no test edit
        ({"dolt": "2.4.0", "bd": "1.3.0"}, False),
        ({"dolt": "2.3.4", "bd": "1.3.0"}, False),
        ({"dolt": "2.3.5", "bd": "1.4.0"}, False),
        ({"dolt": None, "bd": "1.3.0"}, False),  # an unreadable pin cannot be vouched for
        ({"dolt": "2.3.5", "bd": "garbage"}, False),
    ],
)
def test_the_tripwire_fires_only_when_a_pin_is_outside_the_range(pins, inside):
    outside = _outside({"flake.nix": pins}, tc.canary_ranges({}))
    assert (not outside) is inside
    if not inside:
        message = _tripwire_message(outside)
        assert "just fence-canary" in message and "widen" in message


def test_the_proof_line_names_the_exact_versions():
    line = tc.proof_line({"dolt": "2.3.5", "bd": "1.3.0"}, tc.canary_ranges({}))
    assert line == "bd 1.3.0 (in <1.4,>=1.3.0), dolt 2.3.5 (in <2.4,>=2.3.5)"


def test_the_canary_wiring_is_documented_beside_the_pins():
    flake = (ROOT / "flake.nix").read_text()
    for release in ("beadsRelease = pkgs:", "doltRelease = pkgs:"):
        head = flake[: flake.index(release)]
        comment = head[head.rindex("\n\n") :]
        assert "just fence-canary" in comment and "BH_FENCE_CANARY" in comment, (
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
