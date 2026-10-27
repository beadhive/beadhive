"""Negative controls for the tracker port's checker extensions G1, G2 (root half) and G3.

Each probe follows ``docs/spikes/artifacts/bh-gj1hb/probe_boundary_rules.py``: copy the REAL
``modules/tracker`` package into a scratch source root, append one forbidden import, and run the
real checker. The baseline copy passes; every probe fails with its exact rule; and the real tree
passes as committed (tracker-adapter-port-adr.md section 4, "Enforcement").
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import check_import_boundaries as boundaries  # noqa: E402

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_TRACKER = Path("beadhive/modules/tracker")
_CONTRACTS = _TRACKER / "contracts/ports.py"
_APPLICATION = _TRACKER / "application/paging.py"


def _ledger(root: Path) -> Path:
    path = root / "ledger.toml"
    path.write_text(
        "format_version = 1\n\n[cycle_snapshot]\n"
        f'sha256 = "{_EMPTY_SHA256}"\ncomponents = 0\nmodules = 0\nedges = 0\nsymbols = 0\n',
        encoding="utf-8",
    )
    return path


def _write(source_root: Path, relative: Path | str, text: str = "") -> None:
    path = source_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A source root holding the real tracker module plus stand-in consumer modules."""
    source_root = tmp_path / "src"
    shutil.copytree(
        _REPO_ROOT / "src" / _TRACKER,
        source_root / _TRACKER,
        ignore=shutil.ignore_patterns("__pycache__", "BUILD"),
    )
    for package in ("beadhive", "beadhive/modules"):
        _write(source_root, f"{package}/__init__.py")
    _write(source_root, "beadhive/modules/work/contracts/impact.py", "class AttestKey: ...\n")
    _write(source_root, "beadhive/modules/intake/contracts/__init__.py", "PORT = 1\n")
    _write(source_root, "beadhive/modules/intake/application/pull.py", "def pull(): ...\n")
    _write(source_root, "beadhive/kernel/plugins/contracts/__init__.py", "SLOT = 1\n")
    return source_root


def _check_with(source_root: Path, relative: Path | str, line: str) -> tuple[str, ...]:
    path = source_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(original + line + "\n", encoding="utf-8")
    return boundaries.check(source_root, _ledger(source_root.parent)).errors


def test_baseline_real_tracker_module_passes(scratch: Path) -> None:
    result = boundaries.check(scratch, _ledger(scratch.parent))

    assert result.errors == ()
    assert result.files >= 8  # the copied contracts and application modules were scanned


@pytest.mark.parametrize(
    ("relative", "kind", "importer"),
    [
        (_CONTRACTS, "contracts", "beadhive.modules.tracker.contracts.ports"),
        (_APPLICATION, "application", "beadhive.modules.tracker.application.paging"),
    ],
)
def test_g1_tracker_layer_importing_httpx_fails(
    scratch: Path, relative: Path, kind: str, importer: str
) -> None:
    errors = _check_with(scratch, relative, "import httpx")

    assert errors == (
        f"src/{relative.as_posix()}: forbidden {kind} runtime import {importer} -> httpx",
    )


def test_g1_applies_to_every_modules_contracts_kind(scratch: Path) -> None:
    errors = _check_with(scratch, "beadhive/modules/work/contracts/impact.py", "import typer")

    assert errors == (
        "src/beadhive/modules/work/contracts/impact.py: forbidden contracts runtime import "
        "beadhive.modules.work.contracts.impact -> typer",
    )


@pytest.mark.parametrize(
    "line",
    [
        "import githubkit",
        "from githubkit import GitHub",
        "import gidgethub",
        "from github import X",
    ],
)
@pytest.mark.parametrize("relative", [_CONTRACTS, _APPLICATION])
def test_g2_tracker_layer_importing_a_tracker_sdk_fails(
    scratch: Path, relative: Path, line: str
) -> None:
    errors = _check_with(scratch, relative, line)

    assert len(errors) == 1 and errors[0].startswith(f"src/{relative.as_posix()}: forbidden ")
    assert "runtime import" in errors[0]


@pytest.mark.parametrize(
    "relative",
    [
        "beadhive/github_projection.py",  # a legacy root module
        "beadhive/adapters/cli/app.py",
        "beadhive/integrations/herdr/client.py",
        "beadhive/bootstrap/tracker.py",
        "beadhive/kernel/plugins/discovery.py",
    ],
)
def test_g2_root_half_any_layer_importing_githubkit_fails(scratch: Path, relative: str) -> None:
    errors = _check_with(scratch, relative, "from githubkit import GitHub")

    assert len(errors) == 1, errors
    assert errors[0].startswith(f"src/{relative}: forbidden tracker SDK import ")
    assert errors[0].endswith("-> githubkit.GitHub (it belongs in its plugin package)")


def test_g2_matches_whole_name_segments_only(scratch: Path) -> None:
    errors = _check_with(
        scratch, "beadhive/tracker_sync.py", "import githubkit_compat\nfrom . import github_x"
    )

    assert errors == ()


@pytest.mark.parametrize(
    ("relative", "line", "imported"),
    [
        (
            _CONTRACTS,
            "from beadhive.modules.work.contracts import impact",
            "contracts/work: beadhive.modules.work.contracts.impact",
        ),
        (
            _CONTRACTS,
            "from beadhive.modules.intake.contracts import PORT",
            "contracts/intake: beadhive.modules.intake.contracts.PORT",
        ),
        (
            _APPLICATION,
            "from beadhive.modules.intake.contracts import PORT",
            "contracts/intake: beadhive.modules.intake.contracts.PORT",
        ),
        (
            _APPLICATION,
            "from beadhive.modules.intake.application import pull",
            "application/intake: beadhive.modules.intake.application.pull",
        ),
    ],
)
def test_g3_tracker_importing_another_modules_contracts_or_application_fails(
    scratch: Path, relative: Path, line: str, imported: str
) -> None:
    errors = _check_with(scratch, relative, line)

    kind = relative.parts[3]
    assert errors == (
        f"src/{relative.as_posix()}: forbidden leaf-module import {kind}/tracker -> {imported}",
    )


def test_g3_is_scoped_to_leaf_modules(scratch: Path) -> None:
    """A non-leaf module's contracts may still import another module's public contracts.

    The stand-in is a module with no consumer-exclusion entry: ``intake`` itself may not import
    ``work`` (gap G6, ``tests/test_intake_import_boundaries.py``)."""
    errors = _check_with(
        scratch,
        "beadhive/modules/orders/contracts/__init__.py",
        "from beadhive.modules.tracker.contracts import TrackerReader\n"
        "from beadhive.modules.work.contracts.impact import AttestKey",
    )

    assert errors == ()


def test_g3_tracker_may_import_its_own_contracts(scratch: Path) -> None:
    errors = _check_with(
        scratch, _APPLICATION, "from beadhive.modules.tracker.contracts import TrackerReader"
    )

    assert errors == ()


def test_real_tree_passes_the_extended_checker() -> None:
    result = boundaries.check(
        _REPO_ROOT / "src", _REPO_ROOT / "docs/design/import-boundary-exceptions.toml"
    )

    assert result.errors == ()
    assert (_REPO_ROOT / "src" / _CONTRACTS).is_file()
    assert (_REPO_ROOT / "src" / _APPLICATION).is_file()
