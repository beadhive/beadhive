"""Spike bh-gj1hb: what do TODAY's boundary checkers say about the proposed layout?

Copies the committed tree (``git archive HEAD``) into a scratch directory, adds the proposed
``modules/tracker`` contracts, a stand-in intake consumer and a stand-in plugin package, then
runs the two real checkers once per probe. Nothing in the checkout is modified.

    usage: python probe_boundary_rules.py <scratch-dir>      (run from the repository root)
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = Path("src/beadhive/modules/tracker")
INTAKE = Path("src/beadhive/modules/intake/application/pull.py")
PKG = Path("packages/beadhive-tracker-github/src/beadhive_tracker_github")
CONTRACTS = "from beadhive.modules.tracker.contracts import TrackerReader\n"

PROBES = [  # (label, file, appended line, checker)
    ("contracts import httpx", MOD / "contracts/ports.py", "import httpx", "boundaries"),
    ("contracts import githubkit", MOD / "contracts/ports.py", "import githubkit", "boundaries"),
    (
        "tracker application imports githubkit",
        MOD / "application/__init__.py",
        "import githubkit",
        "boundaries",
    ),
    (
        "tracker application imports httpx",
        MOD / "application/__init__.py",
        "import httpx",
        "boundaries",
    ),
    (
        "tracker contracts import work contracts (consumer-side module)",
        MOD / "contracts/ports.py",
        "from beadhive.modules.work.contracts import impact",
        "boundaries",
    ),
    (
        "tracker application imports intake application",
        MOD / "application/__init__.py",
        "from beadhive.modules.intake.application import pull",
        "boundaries",
    ),
    (
        "tracker application imports the CLI",
        MOD / "application/__init__.py",
        "import beadhive.cli",
        "boundaries",
    ),
    ("intake application imports httpx", INTAKE, "import httpx", "boundaries"),
    (
        "plugin adapter imports root internals",
        PKG / "adapter.py",
        "from beadhive import github_projection",
        "packages",
    ),
    (
        "plugin adapter imports the plugin kernel contracts (wiring)",
        PKG / "adapter.py",
        "from beadhive.kernel.plugins.contracts import CapabilitySlot",
        "packages",
    ),
    (
        "root imports the plugin package statically",
        INTAKE,
        "import beadhive_tracker_github",
        "packages",
    ),
]
CHECKERS = {
    "boundaries": "scripts/check_import_boundaries.py",
    "packages": "scripts/check_package_imports.py",
}


def run_checker(root: Path, name: str) -> None:
    """Print the checker's verdict lines; a FAILED verdict is data, not an error."""
    result = subprocess.run(
        [sys.executable, "-W", "ignore", CHECKERS[name]],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (result.stdout + result.stderr).strip().splitlines()[-3:]:
        print(f"    {line}")


def write(root: Path, path: Path, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")


def main(scratch: Path) -> int:
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    members = ["src", "scripts", "packages", "docs/design/import-boundary-exceptions.toml"]
    archive = subprocess.run(
        ["git", "archive", "HEAD", *members], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(scratch, filter="data")

    for package in (MOD, MOD / "application", INTAKE.parent.parent, INTAKE.parent):
        write(scratch, package / "__init__.py", "")
    write(scratch, MOD / "contracts/__init__.py", "from .ports import *  # noqa: F403\n")
    shutil.copy(HERE / "tracker_contracts.py", scratch / MOD / "contracts/ports.py")
    write(scratch, INTAKE, CONTRACTS)
    write(scratch, PKG / "plugin.json", "{}\n")
    write(scratch, PKG / "__init__.py", '__all__ = ["GitHubTrackerAdapter"]\n')
    write(scratch, PKG / "adapter.py", CONTRACTS)

    print("== baseline: proposed layout")
    run_checker(scratch, "boundaries")
    run_checker(scratch, "packages")
    for label, path, line, checker in PROBES:
        original = (scratch / path).read_text(encoding="utf-8")
        write(scratch, path, original + line + "\n")
        print(f"== {label}")
        run_checker(scratch, checker)
        write(scratch, path, original)

    print("== package without plugin.json (library class)")
    (scratch / PKG / "plugin.json").unlink()
    run_checker(scratch, "packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
