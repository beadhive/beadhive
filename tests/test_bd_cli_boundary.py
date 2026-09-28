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

# Lower-level process boundaries that cannot import the root bd adapter without closing an import
# cycle, or deliberately own a scrubbed environment / long-lived process command.
RAW_BD_INFRASTRUCTURE = frozenset(
    {
        "dolt_health.py",
        "cli.py",
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


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def test_root_bd_argv_is_package_routed_or_an_explicit_admin_boundary() -> None:
    direct: list[str] = []
    raw: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if (name.endswith("bd.run") or name.endswith("bd.json")) and (
                    relative not in DIRECT_BD_ADMIN
                ):
                    direct.append(f"{relative}:{node.lineno}:{name}")
            if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                continue
            first = node.elts[0]
            if (
                isinstance(first, ast.Constant)
                and first.value == "bd"
                and relative not in RAW_BD_INFRASTRUCTURE
            ):
                raw.append(f"{relative}:{node.lineno}")

    assert direct == [], "inline bd adapter calls must move to beadhive-bd-cli:\n" + "\n".join(
        direct
    )
    assert raw == [], (
        "raw bd argv must move to beadhive-bd-cli or be documented infra:\n" + "\n".join(raw)
    )
