"""Negative controls for the intake module's checker gaps G5 and G6 (external-intake-module-adr.md
section 2), plus regression controls for the edges the checker already enforced.

Each probe follows ``docs/spikes/artifacts/bh-gj1hb/probe_boundary_rules.py`` and
``tests/test_tracker_import_boundaries.py``: copy the REAL ``modules/intake`` and
``modules/tracker`` packages into a scratch source root, append one forbidden import, and run the
real checker. The baseline copy passes; every probe fails with its exact rule; and the real tree
passes as committed.
Gap G7 (legacy importers of intake internals) is a module-local AST test in
``tests/unit/modules/intake/test_intake_independence.py``.
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
_INTAKE = Path("beadhive/modules/intake")
_TRACKER = Path("beadhive/modules/tracker")
_DOMAIN = _INTAKE / "domain/rules.py"
_CONTRACTS = _INTAKE / "contracts/ports.py"
_APPLICATION = _INTAKE / "application/service.py"
_LAYERS = [
    pytest.param(_DOMAIN, "domain", id="domain"),
    pytest.param(_CONTRACTS, "contracts", id="contracts"),
    pytest.param(_APPLICATION, "application", id="application"),
]
_PORT_LAYERS = [
    pytest.param(_CONTRACTS, "contracts", id="contracts"),
    pytest.param(_APPLICATION, "application", id="application"),
]


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
    """A source root holding the real intake and tracker modules plus stand-in consumers."""
    source_root = tmp_path / "src"
    for module in (_INTAKE, _TRACKER):
        shutil.copytree(
            _REPO_ROOT / "src" / module,
            source_root / module,
            ignore=shutil.ignore_patterns("__pycache__", "BUILD"),
        )
    for package in ("beadhive", "beadhive/modules"):
        _write(source_root, f"{package}/__init__.py")
    _write(source_root, "beadhive/modules/work/contracts/impact.py", "class AttestKey: ...\n")
    _write(source_root, "beadhive/modules/work/application/merge.py", "def merge(): ...\n")
    _write(source_root, "beadhive/modules/planning/contracts/plan.py", "class Plan: ...\n")
    _write(source_root, "beadhive/triage.py", "def accept(): ...\n")
    _write(source_root, "beadhive/adapters/intake/store.py", "class BdReportStore: ...\n")
    return source_root


def _check_with(source_root: Path, relative: Path | str, line: str) -> tuple[str, ...]:
    path = source_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(original + line + "\n", encoding="utf-8")
    return boundaries.check(source_root, _ledger(source_root.parent)).errors


def test_baseline_real_intake_module_passes(scratch: Path) -> None:
    result = boundaries.check(scratch, _ledger(scratch.parent))

    assert result.errors == ()
    assert result.files >= 20  # the copied intake and tracker modules were scanned


# -- G5: intake owns its pager; the tracker's application layer stays unreachable --------------


@pytest.mark.parametrize(
    "line",
    [
        "from beadhive.modules.tracker.application import snapshot_ids",
        "from beadhive.modules.tracker.application.paging import iterate",
        "import beadhive.modules.tracker.application",
    ],
)
@pytest.mark.parametrize(("relative", "kind"), _PORT_LAYERS)
def test_g5_intake_importing_tracker_application_fails(
    scratch: Path, relative: Path, kind: str, line: str
) -> None:
    errors = _check_with(scratch, relative, line)

    assert len(errors) == 1, errors
    assert errors[0].startswith(
        f"src/{relative.as_posix()}: forbidden dependency direction {kind}/intake -> "
        "application/tracker: beadhive.modules.tracker.application"
    )


def test_g5_the_direction_rule_was_not_relaxed() -> None:
    """Intake's own pager imports only tracker CONTRACTS; application stays unreachable."""
    importer = boundaries.PackageRole("application", "intake", True)
    assert not boundaries._direction_allowed(
        importer, boundaries.PackageRole("application", "tracker", True)
    )
    assert boundaries._direction_allowed(
        importer, boundaries.PackageRole("contracts", "tracker", True)
    )


@pytest.mark.parametrize(("relative", "kind"), _PORT_LAYERS)
def test_g5_intake_may_import_the_tracker_contracts(
    scratch: Path, relative: Path, kind: str
) -> None:
    errors = _check_with(
        scratch, relative, "from beadhive.modules.tracker.contracts import TrackerReader"
    )

    assert errors == ()


def test_g5_the_trackers_leaf_rule_still_forbids_the_reverse_edge(scratch: Path) -> None:
    errors = _check_with(
        scratch,
        _TRACKER / "contracts/ports.py",
        "from beadhive.modules.intake.contracts import IntakePort",
    )

    assert errors == (
        "src/beadhive/modules/tracker/contracts/ports.py: forbidden leaf-module import "
        "contracts/tracker -> contracts/intake: beadhive.modules.intake.contracts.IntakePort",
    )


# -- G6: intake may not import its consumers (work, planning) -----------------------------------


@pytest.mark.parametrize(
    ("line", "imported"),
    [
        (
            "from beadhive.modules.work.contracts import impact",
            "contracts/work: beadhive.modules.work.contracts.impact",
        ),
        (
            "from beadhive.modules.work.application import merge",
            "application/work: beadhive.modules.work.application.merge",
        ),
        (
            "from beadhive.modules.planning.contracts import plan",
            "contracts/planning: beadhive.modules.planning.contracts.plan",
        ),
    ],
)
@pytest.mark.parametrize(("relative", "kind"), _LAYERS)
def test_g6_intake_importing_a_consumer_module_fails(
    scratch: Path, relative: Path, kind: str, line: str, imported: str
) -> None:
    errors = _check_with(scratch, relative, line)

    assert errors == (
        f"src/{relative.as_posix()}: forbidden consumer-module import {kind}/intake -> {imported}",
    )


def test_g6_the_consumers_may_import_intakes_public_contracts(scratch: Path) -> None:
    """The exclusion is one-way: work reaches intake through the port, never the reverse."""
    errors = _check_with(
        scratch,
        "beadhive/modules/work/application/merge.py",
        "from beadhive.modules.intake.contracts import IntakePort",
    )

    assert errors == ()


def test_g6_is_scoped_to_the_declared_owners(scratch: Path) -> None:
    """Other modules' contracts may still import work contracts: the table has one owner."""
    errors = _check_with(
        scratch,
        "beadhive/modules/planning/contracts/plan.py",
        "from beadhive.modules.work.contracts import impact",
    )

    assert errors == ()


# -- regression controls: the edges the checker already enforced for intake --------------------


@pytest.mark.parametrize(("relative", "kind"), _LAYERS)
@pytest.mark.parametrize(
    ("line", "imported"),
    [
        ("from beadhive import triage", "legacy/legacy: beadhive.triage"),
        (
            "from beadhive.adapters.intake import store",
            "adapter/intake: beadhive.adapters.intake.store",
        ),
    ],
)
def test_intake_layers_importing_legacy_or_adapters_fail(
    scratch: Path, relative: Path, kind: str, line: str, imported: str
) -> None:
    errors = _check_with(scratch, relative, line)

    assert errors == (
        f"src/{relative.as_posix()}: forbidden dependency direction {kind}/intake -> {imported}",
    )


def test_the_domain_may_not_import_the_tracker_contracts(scratch: Path) -> None:
    errors = _check_with(
        scratch, _DOMAIN, "from beadhive.modules.tracker.contracts import IssueKey"
    )

    assert errors == (
        f"src/{_DOMAIN.as_posix()}: forbidden dependency direction domain/intake -> "
        "contracts/tracker: beadhive.modules.tracker.contracts.IssueKey",
    )


@pytest.mark.parametrize("runtime", ["typer", "httpx", "fastmcp", "githubkit"])
@pytest.mark.parametrize(("relative", "kind"), _LAYERS)
def test_intake_layers_importing_a_transport_or_sdk_fail(
    scratch: Path, relative: Path, kind: str, runtime: str
) -> None:
    errors = _check_with(scratch, relative, f"import {runtime}")

    assert errors and all(
        e.startswith(f"src/{relative.as_posix()}: forbidden ") and runtime in e for e in errors
    ), errors


def test_real_tree_passes_the_extended_checker() -> None:
    result = boundaries.check(
        _REPO_ROOT / "src", _REPO_ROOT / "docs/design/import-boundary-exceptions.toml"
    )

    assert result.errors == ()
    assert (_REPO_ROOT / "src" / _CONTRACTS).is_file()
    assert (_REPO_ROOT / "src" / _APPLICATION).is_file()
