"""bh setup + the in-image component manifest (/etc/beadhive/image-manifest.json).

Inside a Beadhive image the build already knows which component versions shipped together,
so `bh setup check` reads that manifest instead of shelling out to every `--version` on PATH.

Covers:
- manifest present: no subprocess and no PATH lookup happens at all, and the check passes
- manifest present: the cache is tagged with the image ref, and `setup show` displays it
- manifest projected onto the same `tools` dict shape the probe path produces
- REGRESSION: absent / unreadable / malformed manifest leaves today's probe path untouched
- probe_tools() itself still probes even in-image, so `bh doctor` keeps seeing reality
"""

from __future__ import annotations

import json
import subprocess

import pytest

from beadhive import setup as setup_mod

MANIFEST = {
    "schema": 1,
    "image": {
        "tag": "beadhive/agent:dev",
        "target": "agent",
        "build_sha": "c4382b5b19271068fb6c389e1cf7d45ef48a4383",
    },
    "components": [
        {"name": "git", "version": "2.39.5", "source": "apt:debian-bookworm"},
        {"name": "bh", "version": "0.6.0", "source": "pypi:beadhive[otel]"},
        {"name": "bd", "version": "1.1.2", "source": "github:gastownhall/beads"},
        {"name": "dolt", "version": "2.2.3", "source": "github:dolthub/dolt"},
        {"name": "claude", "version": "2.1.220", "source": "npm:@anthropic-ai/claude-code"},
    ],
}


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    """Write an image manifest and point BH_IMAGE_MANIFEST at it."""
    p = tmp_path / "image-manifest.json"
    p.write_text(json.dumps(MANIFEST))
    monkeypatch.setenv("BH_IMAGE_MANIFEST", str(p))
    return p


@pytest.fixture()
def no_probing(monkeypatch):
    """Make any live probe fail loudly, so a test proves the manifest replaced probing."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("probed the host despite an image manifest being present")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(setup_mod.shutil, "which", _boom)


# ---- manifest path ------------------------------------------------------------


def test_check_uses_manifest_without_probing(manifest, no_probing, capsys):
    """In-image, `setup check` succeeds without a single --version subprocess."""
    setup_mod.run_check()
    out = capsys.readouterr().out
    assert "image-manifest.json" in out
    assert "bd" in out
    assert setup_mod.is_setup_complete() is True


def test_check_records_the_image_ref(manifest, no_probing):
    """The cache is tagged with the image that validated this combination."""
    setup_mod.run_check()
    cache = setup_mod.read_cache()
    assert cache["image"] == MANIFEST["image"]
    assert cache["tools"]["dolt"] == {"found": True, "version": "2.2.3"}


def test_show_displays_the_image_ref(manifest, no_probing, capsys):
    """`setup show` names the image, so a cached pass is attributable."""
    setup_mod.run_check()
    capsys.readouterr()
    setup_mod.run_show()
    out = capsys.readouterr().out
    assert "beadhive/agent:dev" in out
    assert "c4382b5b19271068fb6c389e1cf7d45ef48a4383" in out


def test_tools_from_manifest_matches_probe_shape(manifest):
    """The manifest projects onto the same {name: {found, version}} dict probing returns."""
    tools = setup_mod.tools_from_manifest(setup_mod.read_image_manifest())
    assert tools["bh"] == {"found": True, "version": "0.6.0"}
    assert set(tools) == {c["name"] for c in MANIFEST["components"]}


def test_probe_tools_still_probes_in_image(manifest, monkeypatch):
    """probe_tools() is untouched — `bh doctor` must keep reporting what is actually on PATH."""
    monkeypatch.setattr(
        setup_mod, "probe_one", lambda name, wb, vcmd: {"found": True, "version": "probed"}
    )
    tools = setup_mod.probe_tools()
    assert set(tools) == {name for name, _, _ in setup_mod.PROBE_TABLE}
    assert tools["bd"]["version"] == "probed"


# ---- regression: no manifest => today's behavior, unchanged ---------------------


def test_absent_manifest_runs_the_probe_path(monkeypatch, capsys):
    """REGRESSION: with no manifest (every non-container host) `setup check` probes as before
    and writes a cache with no image tag."""
    probed = {n: {"found": True, "version": "1.0"} for n, _, _ in setup_mod.PROBE_TABLE}
    monkeypatch.setattr(setup_mod, "probe_tools", lambda: probed)

    setup_mod.run_check()

    assert "Checking post-ws dependencies" in capsys.readouterr().out
    cache = setup_mod.read_cache()
    assert cache["tools"] == probed
    assert "image" not in cache


@pytest.mark.parametrize(
    "body",
    ["not-json{{{", '{"image": {"tag": "x"}}', '["not", "an", "object"]'],
    ids=["corrupt", "no-components", "not-a-dict"],
)
def test_unusable_manifest_falls_back_to_probing(body, tmp_path, monkeypatch, capsys):
    """A manifest bh cannot trust is the same as no manifest — never a half-report."""
    p = tmp_path / "image-manifest.json"
    p.write_text(body)
    monkeypatch.setenv("BH_IMAGE_MANIFEST", str(p))
    probed = {n: {"found": True, "version": "1.0"} for n, _, _ in setup_mod.PROBE_TABLE}
    monkeypatch.setattr(setup_mod, "probe_tools", lambda: probed)

    assert setup_mod.read_image_manifest() is None
    setup_mod.run_check()
    assert "Checking post-ws dependencies" in capsys.readouterr().out
    assert "image" not in setup_mod.read_cache()
