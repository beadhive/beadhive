"""Keep root composition free of new inline ``bd`` argv routes."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "beadhive"

# These modules own host administration, bootstrap, repair, diagnostics, or a one-shot corpus
# migration.  Their reasons are documented in packages/beadhive-core/README.md; changing this
# allowlist is therefore an architecture decision rather than an incidental test update.
DIRECT_BD_ADMIN = frozenset(
    {
        "complexity_backfill.py",
        "doctor.py",
        "hive_repair.py",
        "hub.py",
        "onboard.py",
        "sync_remote.py",
    }
)

# Narrow exceptions inside otherwise application-facing modules.  The key is the enclosing
# function, so adding a second inline call elsewhere in either file still fails this check.
DIRECT_BD_ADMIN_SCOPES = {
    "backup.py": {
        # Backup administration's timeout-aware bd runner; callers supply backup-only commands.
        "_bd",
    },
    "cli.py": {
        # Explicit operator administration: export the current hive's complete corpus to the
        # configured backup mirror.  ``--all`` is backup policy, not an application route.
        "backup_export",
    },
    "hq.py": {
        # HQ bootstrap/backup administration and its diagnostic version probe.
        "_bd",
        "_bd_version",
    },
    "storage_migrate.py": {
        # One-shot storage administration needs status/config probes plus a deliberately unusual
        # process-cwd-pinned runner while moving a hive between embedded and server mode.
        "_issue_count",
        "_config_get_bool",
        "_bd",
        "verify_migration",
        "_effective_prefix",
    },
}

# Lower-level process boundaries that cannot import the root bd adapter without closing an import
# cycle, or deliberately own a scrubbed environment / long-lived process command.
RAW_BD_INFRASTRUCTURE = frozenset(
    {
        "dolt_health.py",
        "deps.py",
        "engine.py",
        "fleet.py",
        "hub.py",
        "onboard.py",
        "registry.py",
        "safety.py",
        "validate.py",
    }
)

RAW_BD_INFRASTRUCTURE_SCOPES = {
    "fence_data.py": {
        # The in-data fence adapter's ``bd sql`` runner sits below guard/host_adopt in the import
        # graph (the write guard consults it) while ``beadhive.bd`` imports ``beadhive.guard``;
        # routing through the bd adapter would close that cycle. Scoped to the one runner.
        "_bd",
    },
    "cli.py": {
        # Passthrough command-name vocabulary, not a spawned argv; keeping it scoped prevents a
        # new literal ``bd`` process command elsewhere in the CLI from inheriting an exemption.
        "_resolve_hive_routing_mode",
    },
}

# No root composition scope may use CliRoutes.forward/json_forward. Opaque terminal presentation
# is retained only through the named package routes ``presentation_show`` and
# ``presentation_list``, which constrain the operation while deliberately leaving its flags to bd.
GENERIC_FORWARD_SCOPES: dict[str, set[str]] = {}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _bd_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Module and callable names bound to ``beadhive.bd`` routes, including alias chains."""
    modules: set[str] = set()
    callables: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {None, "beadhive"}:
            for imported in node.names:
                if imported.name == "bd":
                    modules.add(imported.asname or imported.name)
        elif isinstance(node, ast.ImportFrom) and node.module in {"bd", "beadhive.bd"}:
            for imported in node.names:
                if imported.name in {"run", "json"}:
                    callables.add(imported.asname or imported.name)
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "beadhive.bd":
                    modules.add(imported.asname or imported.name)

    route_attributes = {f"{module}.{method}" for module in modules for method in ("run", "json")}
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            value = assignment.value
            source = _dotted(value)
            if source not in route_attributes and source not in callables:
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in callables:
                    callables.add(target.id)
                    changed = True
    return modules, callables


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def _direct_bd_calls(source: str, relative: str) -> list[str]:
    tree = ast.parse(source, filename=relative)
    modules, callables = _bd_aliases(tree)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    allowed_scopes = DIRECT_BD_ADMIN_SCOPES.get(relative, set())
    direct = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        module_routes = {f"{module}.{method}" for module in modules for method in ("run", "json")}
        if name not in module_routes and name not in callables:
            continue
        scope = _enclosing_function(node, parents)
        if relative not in DIRECT_BD_ADMIN and scope not in allowed_scopes:
            direct.append(f"{relative}:{node.lineno}:{name}")
    return direct


def _raw_bd_argv(source: str, relative: str) -> list[str]:
    tree = ast.parse(source, filename=relative)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    allowed_scopes = RAW_BD_INFRASTRUCTURE_SCOPES.get(relative, set())
    raw = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
            continue
        first = node.elts[0]
        if not isinstance(first, ast.Constant) or first.value != "bd":
            continue
        scope = _enclosing_function(node, parents)
        if relative not in RAW_BD_INFRASTRUCTURE and scope not in allowed_scopes:
            raw.append(f"{relative}:{node.lineno}")
    return raw


def _generic_forward_calls(source: str, relative: str) -> list[str]:
    """Generic package forwarding would let arbitrary bd argv bypass named-route ownership."""
    tree = ast.parse(source, filename=relative)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    allowed_scopes = GENERIC_FORWARD_SCOPES.get(relative, set())
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"forward", "json_forward"}:
            continue
        scope = _enclosing_function(node, parents)
        if scope not in allowed_scopes:
            calls.append(f"{relative}:{node.lineno}:{node.func.attr}")
    return calls


def test_bd_module_import_aliases_cannot_hide_inline_routes() -> None:
    source = (
        "from . import bd as bd_mod\n"
        "import beadhive.bd\n\n"
        "def application():\n"
        "    bd_mod.run([\"ready\"], '.')\n"
        "    beadhive.bd.json([\"list\"], '.')\n"
    )
    assert _direct_bd_calls(source, "application.py") == [
        "application.py:5:bd_mod.run",
        "application.py:6:beadhive.bd.json",
    ]


def test_bd_callable_and_assignment_aliases_cannot_hide_inline_routes() -> None:
    source = (
        "from .bd import run as invoke\n"
        "from . import bd as bd_mod\n"
        "assigned = bd_mod.json\n"
        "chained = assigned\n\n"
        "def application():\n"
        "    invoke([\"ready\"], '.')\n"
        "    chained([\"list\"], '.')\n"
    )
    assert _direct_bd_calls(source, "application.py") == [
        "application.py:7:invoke",
        "application.py:8:chained",
    ]


def test_cli_raw_bd_literal_exception_is_function_scoped() -> None:
    source = 'def application():\n    command = ["bd", "ready"]\n'
    assert _raw_bd_argv(source, "cli.py") == ["cli.py:2"]
    vocabulary = 'def _resolve_hive_routing_mode():\n    names = ("bd", "git")\n'
    assert _raw_bd_argv(vocabulary, "cli.py") == []


def test_generic_package_forwarders_cannot_hide_arbitrary_routes() -> None:
    source = (
        "def application(routes, args):\n"
        "    routes.forward(args)\n"
        "    routes.json_forward(['ready'])\n"
    )
    assert _generic_forward_calls(source, "application.py") == [
        "application.py:2:forward",
        "application.py:3:json_forward",
    ]


def test_root_bd_argv_is_package_routed_or_an_explicit_admin_boundary() -> None:
    direct: list[str] = []
    raw: list[str] = []
    generic: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        source = path.read_text()
        direct.extend(_direct_bd_calls(source, relative))
        raw.extend(_raw_bd_argv(source, relative))
        generic.extend(_generic_forward_calls(source, relative))

    assert direct == [], "inline bd adapter calls must move to beadhive-bd-cli:\n" + "\n".join(
        direct
    )
    assert raw == [], (
        "raw bd argv must move to beadhive-bd-cli or be documented infra:\n" + "\n".join(raw)
    )
    assert generic == [], (
        "generic bd forwarding must become a named beadhive-bd-cli route:\n" + "\n".join(generic)
    )
