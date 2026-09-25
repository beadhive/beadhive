"""Installed artifacts retain the lazily resolved first-party Pants backend."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from scripts.pants_launcher import launcher

ROOT = Path(__file__).parents[1]
PANTS = launcher()
RESOLVE = (
    "from beadhive.bootstrap.impact import collect_impact_backends; "
    "backend = collect_impact_backends('.').backends['pants']; "
    "assert type(backend).__module__ == 'beadhive_pants.impact'"
)


def _pants_repo(path: Path) -> None:
    (path / "pants.toml").write_text('[GLOBAL]\npants_version = "2.32.1"\n')


def test_installed_root_and_plugin_wheels_resolve_the_backend(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for package in ("beadhive", "beadhive-pants"):
        subprocess.run(
            [
                "uv",
                "build",
                "--offline",
                "--no-build-isolation",
                "--wheel",
                "--package",
                package,
                "--out-dir",
                wheels,
            ],
            cwd=ROOT,
            check=True,
        )
    installed = tmp_path / "installed"
    installed.mkdir()
    for wheel in wheels.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(installed)

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _pants_repo(checkout)
    env = {**os.environ, "PYTHONPATH": str(installed)}

    subprocess.run([sys.executable, "-c", RESOLVE], cwd=checkout, env=env, check=True)


def test_bh_pex_declares_the_plugin_in_its_build_closure() -> None:
    build = (ROOT / "src" / "beadhive" / "BUILD").read_text()
    pex_target = build.split("pex_binary(", 1)[1]

    assert 'name="bh"' in pex_target
    assert '"//packages/beadhive-pants/src:lib"' in pex_target


@pytest.mark.pants_profile
@pytest.mark.skipif(
    shutil.which("python3.11") is None,
    reason="recursive Pants artifact proof needs the repository's Python 3.11 toolchain",
)
def test_bh_pex_contains_and_resolves_the_backend(tmp_path: Path) -> None:
    python_311 = shutil.which("python3.11")
    assert python_311 is not None
    pants_env = {
        **os.environ,
        # The shared Pants cache carries the repository's pinned 3.11 artifact closure. Keep
        # this recursive artifact proof on that interpreter instead of inheriting whichever
        # Python the native test partition selected for its outer hermetic environment.
        "PANTS_PYTHON_INTERPRETER_CONSTRAINTS": '["CPython==3.11.*"]',
        "PANTS_PYTHON_BOOTSTRAP_SEARCH_PATH": f'["{Path(python_311).resolve().parent}"]',
    }
    subprocess.run(
        [
            sys.executable,
            "scripts/pants_cache.py",
            "run",
            "--",
            PANTS,
            "--no-pantsd",
            "package",
            "src/beadhive:bh",
        ],
        cwd=ROOT,
        env=pants_env,
        check=True,
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _pants_repo(checkout)
    env = {**os.environ, "PEX_INTERPRETER": "1"}

    subprocess.run([ROOT / "dist" / "bh.pex", "-c", RESOLVE], cwd=checkout, env=env, check=True)
