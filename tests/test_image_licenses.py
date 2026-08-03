"""scripts/image-licenses.sh — the attribution guard on a BUILT image.

Building an image needs a docker daemon, so (per tests/test_image_build.py's convention) the
real inventory runs in bh-pc2a.17's proof gate. What is covered here is the DECISION LOGIC:
given an inventory, does the script pass, fail, and name the right things?

`docker` is stubbed on PATH, so these run anywhere — including CI with no daemon.

The failure this guards is silent: strip .dist-info and every third-party notice vanishes at
once with nothing going red. So the stripping scenario below is DEMONSTRATED by feeding a
gutted inventory, not asserted by inspection.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "image-licenses.sh"


def run_with_inventory(tmp_path, lines, docker_exit=0):
    """Stub `docker` so it emits `lines` as the in-image inventory."""
    payload = "\n".join(lines)
    stub = tmp_path / "docker"
    stub.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{payload}\nEOF\nexit {docker_exit}\n")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return subprocess.run(
        [str(SCRIPT), "beadhive-core:test"],
        capture_output=True,
        text=True,
        env=env,
    )


def healthy(n=80):
    """An inventory resembling the real image: many dists, all carrying licence files."""
    return [f"pkg{i}-1.0.0.dist-info 3" for i in range(n)]


def test_healthy_image_passes(tmp_path):
    result = run_with_inventory(tmp_path, healthy())
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_stripped_dist_info_is_caught(tmp_path):
    """THE failure mode: a slimming change guts dist-info. Demonstrated, not assumed."""
    result = run_with_inventory(tmp_path, healthy(n=3))
    assert result.returncode == 1
    assert "stripped" in result.stderr
    assert "expected >=" in result.stderr


def test_a_dist_losing_its_licence_files_is_caught_and_named(tmp_path):
    inventory = healthy() + ["somepkg-2.0.0.dist-info 0"]
    result = run_with_inventory(tmp_path, inventory)
    assert result.returncode == 1
    assert "somepkg-2.0.0.dist-info" in result.stderr, "must NAME what is missing"


def test_known_exception_does_not_fail_the_build(tmp_path):
    """fastmcp-slim ships no licence file upstream — a recorded exception, not a regression."""
    inventory = healthy() + ["fastmcp_slim-3.4.5.dist-info 0"]
    result = run_with_inventory(tmp_path, inventory)
    assert result.returncode == 0
    assert "fastmcp_slim" in result.stdout


def test_exception_is_matched_on_name_not_substring(tmp_path):
    """A different package must not inherit fastmcp-slim's exemption."""
    inventory = healthy() + ["fastmcp-3.4.2.dist-info 0"]
    result = run_with_inventory(tmp_path, inventory)
    assert result.returncode == 1
    assert "fastmcp-3.4.2.dist-info" in result.stderr


def test_empty_inventory_fails_rather_than_passing_vacuously(tmp_path):
    """No output must never read as 'nothing wrong' — that is the worst failure available."""
    result = run_with_inventory(tmp_path, [])
    assert result.returncode == 1
    assert "could not inventory" in result.stderr


def test_missing_image_ref_is_a_usage_error():
    """No docker stub needed — this must fail before anything is invoked."""
    result = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "usage" in result.stderr
