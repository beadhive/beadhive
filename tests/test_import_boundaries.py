from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import check_import_boundaries as boundaries  # noqa: E402

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _write_source(root: Path, relative: str, source: str = "") -> None:
    path = root / "src" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _write_ledger(root: Path, extra: str = "") -> Path:
    path = root / "ledger.toml"
    path.write_text(
        f"""\
format_version = 1

[[successor_owner]]
id = "bh-inqwc"
kind = "live_bead"
scope = "test fixture"
rationale = "The fixture models an exact registered successor."

[cycle_snapshot]
sha256 = "{_EMPTY_SHA256}"
components = 0
modules = 0
edges = 0
symbols = 0

{extra}
""",
        encoding="utf-8",
    )
    return path


def _write_root_manifest(
    root: Path,
    *,
    public_facades: tuple[str, ...] = (),
    composition_boundaries: tuple[str, ...] = (),
    legacy_implementations: tuple[str, ...] = (),
) -> Path:
    classes = [("package_metadata", ("src/beadhive/__init__.py",))]
    classes.extend(
        (role, paths)
        for role, paths in (
            ("public_facade", public_facades),
            ("composition_boundary", composition_boundaries),
            ("legacy_implementation", legacy_implementations),
        )
        if paths
    )
    rows = ["format_version = 1", ""]
    for role, paths in classes:
        rows.extend(
            [
                "[[root_class]]",
                f'role = "{role}"',
                'owner = "fixture owner"',
                'rationale = "fixture classification"',
                "paths = [",
                *(f'  "{path}",' for path in paths),
                "]",
                "",
            ]
        )
    path = root / "root-ownership.toml"
    path.write_text("\n".join(rows), encoding="utf-8")
    return path


def _boundary_exception(*, omit: str = "", **overrides: str) -> str:
    fields = {
        "id": "application-cli-app",
        "status": "active",
        "importer": "beadhive.modules.orders.application.handler",
        "importer_path": "src/beadhive/modules/orders/application/handler.py",
        "imported_module": "beadhive.cli",
        "symbol": "beadhive.cli.app",
        "target_owner_package": "modules/orders",
        "successor": "bh-inqwc",
        "reason": "test fixture exception",
        "executable_test": "tests/test_import_boundaries.py",
        "test_closure": "focused",
        "introduced_commit": "fixture-introduction",
        "last_verified_commit": "fixture-verification",
        "expiry_trigger": "remove the exact fixture import",
    }
    fields.update(overrides)
    lines = ["[[boundary_exception]]"]
    lines.extend(f'{key} = "{value}"' for key, value in fields.items() if key != omit)
    lines.append(
        'consumer_inventory = { production = "fixture", tests = "fixture", '
        'docs = "fixture", external = "none" }'
    )
    return "\n".join(lines)


def _facade(*, facade_path: str) -> str:
    return f"""
[[facade]]
id = "fixture-facade"
status = "active"
facade_path = "{facade_path}"
preserved_symbols = ["public_api"]
target_owner_package = "modules/orders"
successor = "bh-inqwc"
reason = "test fixture facade"
consumer_inventory.production = "fixture"
consumer_inventory.tests = "fixture"
consumer_inventory.docs = "fixture"
consumer_inventory.external = "none"
executable_test = "tests/test_import_boundaries.py"
test_closure = "focused"
introduced_commit = "fixture-introduction"
last_verified_commit = "fixture-verification"
expiry_trigger = "remove after the fixture consumer inventory reaches zero"
"""


def test_allowed_application_imports_its_domain_without_executing_source(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        "from beadhive.modules.orders.domain.model import Order\n"
        "raise RuntimeError('the checker imported production source')\n",
    )
    _write_source(
        tmp_path,
        "beadhive/modules/orders/domain/model.py",
        "class Order: ...\nraise RuntimeError('the checker imported domain source')\n",
    )

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert result.errors == ()


def test_application_import_of_cli_fails_with_exact_path_and_symbol(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        "from beadhive.cli import app\n",
    )
    _write_source(tmp_path, "beadhive/cli.py", "app = object()\n")

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert result.errors == (
        "src/beadhive/modules/orders/application/handler.py: forbidden application runtime "
        "import beadhive.modules.orders.application.handler -> beadhive.cli.app",
    )


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nimportlib.import_module("beadhive.cli")\n',
        'from importlib import import_module as load\nload("beadhive.cli")\n',
    ],
)
def test_literal_importlib_import_of_cli_fails(tmp_path: Path, source: str) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/modules/orders/application/handler.py", source)
    _write_source(tmp_path, "beadhive/cli.py")

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert any(
        "forbidden application runtime import "
        "beadhive.modules.orders.application.handler -> beadhive.cli" in error
        for error in result.errors
    )


def test_literal_builtin_import_allows_inward_dependency(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        '__import__("beadhive.modules.orders.domain.model", fromlist=["Order"])\n',
    )
    _write_source(tmp_path, "beadhive/modules/orders/domain/model.py", "class Order: ...\n")

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert result.errors == ()


@pytest.mark.parametrize(
    "source, function",
    [
        (
            "import importlib\ntarget = 'beadhive.cli'\nimportlib.import_module(target)\n",
            "importlib.import_module",
        ),
        ("target = 'beadhive.cli'\n__import__(target)\n", "__import__"),
    ],
)
def test_nonliteral_dynamic_import_fails_closed(tmp_path: Path, source: str, function: str) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/modules/orders/application/handler.py", source)

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert (
        "src/beadhive/modules/orders/application/handler.py: nonliteral dynamic import cannot "
        f"be verified in application/orders: {function}"
    ) in result.errors


def test_exact_boundary_exception_permits_only_its_recorded_edge(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        "from beadhive.cli import app\n",
    )
    _write_source(tmp_path, "beadhive/cli.py", "app = object()\n")

    result = boundaries.check(source_root, _write_ledger(tmp_path, _boundary_exception()))

    assert result.errors == ()


@pytest.mark.parametrize("missing_field", ["importer_path", "imported_module"])
def test_boundary_exception_missing_exact_edge_field_cannot_bypass_rule(
    tmp_path: Path, missing_field: str
) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        "from beadhive.cli import app\n",
    )
    _write_source(tmp_path, "beadhive/cli.py", "app = object()\n")

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path, _boundary_exception(omit=missing_field)),
    )

    assert any(f"missing {missing_field}" in error for error in result.errors)
    assert any("forbidden application runtime import" in error for error in result.errors)


@pytest.mark.parametrize(
    "field, wrong_value",
    [
        ("importer_path", "src/beadhive/modules/orders/application/other.py"),
        ("imported_module", "beadhive.mcp"),
    ],
)
def test_boundary_exception_must_match_exact_path_and_module(
    tmp_path: Path, field: str, wrong_value: str
) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        "from beadhive.cli import app\n",
    )
    _write_source(tmp_path, "beadhive/cli.py", "app = object()\n")

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path, _boundary_exception(**{field: wrong_value})),
    )

    assert any("forbidden application runtime import" in error for error in result.errors)
    assert any("stale boundary exception" in error for error in result.errors)


@pytest.mark.parametrize(
    "field",
    ["target_owner_package", "successor", "expiry_trigger"],
)
def test_empty_ownership_or_expiry_metadata_is_rejected(tmp_path: Path, field: str) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        "from beadhive.cli import app\n",
    )
    _write_source(tmp_path, "beadhive/cli.py", "app = object()\n")

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path, _boundary_exception(**{field: ""})),
    )

    assert any(f"{field} must be a non-empty string" in error for error in result.errors)


def test_active_record_rejects_an_unregistered_successor(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/application/handler.py",
        "from beadhive.cli import app\n",
    )
    _write_source(tmp_path, "beadhive/cli.py", "app = object()\n")

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path, _boundary_exception(successor="bh-closed")),
    )

    assert "active successor is not registered" in "\n".join(result.errors)


def test_successor_owner_and_overlap_disposition_reject_wildcards(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    extra = """
[[successor_owner]]
id = "retained-owner:*"
kind = "retained_owner"
scope = "fixture"
rationale = "bad wildcard fixture"

[[overlap_disposition]]
id = "bh-*"
disposition = "exclude"
scope = "fixture"
rationale = "bad wildcard fixture"
"""

    result = boundaries.check(source_root, _write_ledger(tmp_path, extra))

    assert "ledger successor_owner retained-owner:*: wildcards are forbidden" in result.errors
    assert "ledger overlap_disposition bh-*: wildcards are forbidden" in result.errors


def test_registered_root_facade_is_allowed_without_importing_source(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/__init__.py")
    _write_source(
        tmp_path,
        "beadhive/orders.py",
        "public_api = object()\nraise RuntimeError('the root guard imported production source')\n",
    )
    facade_path = "src/beadhive/orders.py"

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path, _facade(facade_path=facade_path)),
        _write_root_manifest(tmp_path, public_facades=(facade_path,)),
    )

    assert result.errors == ()


def test_documented_root_composition_boundary_is_allowed(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/__init__.py")
    _write_source(
        tmp_path,
        "beadhive/process_root.py",
        "def main() -> int:\n    return 0\n",
    )

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path),
        _write_root_manifest(
            tmp_path,
            composition_boundaries=("src/beadhive/process_root.py",),
        ),
    )

    assert result.errors == ()


def test_unowned_root_implementation_is_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/__init__.py")
    _write_source(tmp_path, "beadhive/unowned.py", "def behavior() -> None: ...\n")

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path),
        _write_root_manifest(tmp_path),
    )

    assert result.errors == ("unowned package-root implementation: src/beadhive/unowned.py",)


def test_root_facade_classification_requires_active_ledger_record(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/__init__.py")
    _write_source(tmp_path, "beadhive/unregistered_facade.py", "public_api = object()\n")
    facade_path = "src/beadhive/unregistered_facade.py"

    result = boundaries.check(
        source_root,
        _write_ledger(tmp_path),
        _write_root_manifest(tmp_path, public_facades=(facade_path,)),
    )

    assert result.errors == (
        f"root public facade is not active in exception ledger: {facade_path}",
    )


def test_domain_import_of_opentelemetry_sdk_fails(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/domain/model.py",
        "from opentelemetry.sdk.trace import TracerProvider\n",
    )

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert result.errors == (
        "src/beadhive/modules/orders/domain/model.py: forbidden domain runtime import "
        "beadhive.modules.orders.domain.model -> opentelemetry.sdk.trace.TracerProvider",
    )


def test_real_opentelemetry_imports_are_confined_to_adapter_and_composition_owners() -> None:
    _modules, edges, _dynamic = boundaries.collect_imports(_REPO_ROOT / "src")
    opentelemetry_edges = tuple(
        edge for edge in edges if edge.imported_module.startswith("opentelemetry")
    )
    sdk_edges = tuple(
        edge
        for edge in opentelemetry_edges
        if any(symbol.startswith("opentelemetry.sdk") for symbol in edge.symbols)
    )

    assert sdk_edges
    assert {edge.importer for edge in sdk_edges} == {"beadhive.otel"}
    assert {edge.importer for edge in opentelemetry_edges} <= {
        "beadhive.otel",
        "beadhive.log",
        "beadhive.daemon_telemetry",
    }
    assert not [
        edge
        for edge in opentelemetry_edges
        if edge.importer.startswith("beadhive.kernel.")
        or ".application." in edge.importer
        or ".domain." in edge.importer
    ]


def test_cross_capability_domain_import_fails_direction_rule(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(
        tmp_path,
        "beadhive/modules/orders/domain/model.py",
        "from beadhive.modules.billing.domain.invoice import Invoice\n",
    )
    _write_source(tmp_path, "beadhive/modules/billing/domain/invoice.py", "class Invoice: ...\n")

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert any(
        "forbidden dependency direction domain/orders -> domain/billing" in error
        for error in result.errors
    )


def test_new_cycle_fails_without_an_exact_owned_exception(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/first.py", "from beadhive import second\n")
    _write_source(tmp_path, "beadhive/second.py", "from beadhive import first\n")

    result = boundaries.check(source_root, _write_ledger(tmp_path))

    assert any(error.startswith("cycle snapshot changed:") for error in result.errors)
    assert "unowned import cycle: beadhive.first -> beadhive.second" in result.errors


def test_wildcard_exception_is_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    _write_source(tmp_path, "beadhive/first.py")
    wildcard = """
[[cycle_exception]]
id = "bad-wildcard"
status = "active"
importer = "beadhive.*"
importer_path = "src/beadhive/*.py"
imported_module = "beadhive.other"
symbols = ["beadhive.other.*"]
target_owner_package = "src/beadhive"
successor = "bh-inqwc"
reason = "bad fixture"
consumer_inventory = { production = "fixture", tests = "fixture", docs = "fixture" }
executable_test = "tests/test_import_boundaries.py"
test_closure = "focused"
introduced_commit = "fixture"
last_verified_commit = "fixture"
expiry_trigger = "remove fixture edge"
"""

    result = boundaries.check(source_root, _write_ledger(tmp_path, wildcard))

    assert "ledger cycle_exception bad-wildcard: wildcards are forbidden" in result.errors
