"""The advice printed under `✗ missing: …` (bh-pc2a.33).

On a host "install the missing tools" is right. Inside the image it is never right, and for a
container RUNTIME it is actively harmful: bh-pc2a.6 established that a container does not drive
one and the host docker socket is deliberately not mounted, so an operator following generic
advice is led straight to the thing the design forbids.

This is not hypothetical. A stale `beadhive/agent:dev` carrying a bh that predated the manifest
reader fell back to probing, printed `✗ missing: docker`, and gated off every `bh hive` / `bh bd`
verb behind that one line — an inert container whose only clue pointed the wrong way.
"""

from __future__ import annotations

import pytest

from beadhive import setup as setup_mod

MANIFEST = {"schema": 1, "image": {"tag": "beadhive/core:dev"}, "components": []}


@pytest.fixture
def in_image(monkeypatch):
    monkeypatch.setenv("BH_IN_CONTAINER", "1")


@pytest.fixture
def on_host(monkeypatch):
    monkeypatch.delenv("BH_IN_CONTAINER", raising=False)


def test_host_is_told_to_install(on_host):
    remedy = setup_mod._missing_remedy(["jq"], MANIFEST)

    assert "Install the missing tools" in remedy


def test_in_image_never_says_install(in_image):
    """The whole point: inside the image there is nothing for the operator to install."""
    remedy = setup_mod._missing_remedy(["jq"], MANIFEST)

    assert "Install the missing tools" not in remedy
    assert "IMAGE defect" in remedy


@pytest.mark.parametrize("runtime", ["docker", "colima", "podman"])
def test_a_missing_runtime_in_image_warns_against_the_wrong_fix(runtime, in_image):
    """Naming the two wrong fixes explicitly, because both are what a reasonable person tries."""
    remedy = setup_mod._missing_remedy([runtime], MANIFEST)

    assert f"Do NOT install {runtime}" in remedy
    assert "docker socket" in remedy
    assert "Install the missing tools" not in remedy


def test_probe_fallback_in_image_points_at_the_manifest(in_image):
    """manifest=None in-image means bh fell back to probing, which for a Beadhive image means the
    manifest is absent or unreadable — a build problem, fixed by rebaking, not by installing."""
    remedy = setup_mod._missing_remedy(["jq"], None)

    assert "component manifest" in remedy
    assert "just image-local" in remedy


def test_host_runtime_advice_is_unchanged(on_host):
    """A host missing docker really should install docker — do not leak the in-image wording out."""
    remedy = setup_mod._missing_remedy(["docker"], None)

    assert "Install the missing tools" in remedy
    assert "Do NOT install" not in remedy
