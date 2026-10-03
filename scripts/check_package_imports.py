"""Enforce the static import boundary between root and in-repo `packages/*` distributions.

Every `packages/*` distribution is classified into exactly one of two classes by a single
signal — whether it carries a `plugin.json` manifest under `src/<import_name>/` — and the
matching rule set is enforced. See
`docs/design/package-class-library-vs-plugin-adr.md` for the decision this encodes.

- **Plugin package** (carries `plugin.json`): may import root only through the allowlisted
  `beadhive.kernel.*.contracts` / `beadhive.modules.*.contracts` / `beadhive.testing` surfaces,
  and may import a library package through that library's public `__all__` surface. It may not
  import root internals or any other package statically resolve it. Root only ever resolves a
  plugin package lazily, by name, after manifest selection.
- **Library package** (no manifest): may not import anything from the `beadhive` distribution,
  under any path. It may import another library package only through that package's public
  `__all__` surface. Root, and any other package, may import a library package statically
  through its public `__all__` surface.

- **Root tests** (`tests/**`, bh-qdezo.9): may import a library package only through its public
  `__all__` surface, exactly like `src/beadhive`, and may never patch a package-private name —
  a `monkeypatch.setattr` / `delattr` / `mock.patch` / `patch.object` whose target resolves into
  a library package with any `_`-prefixed segment. Higher-layer tests substitute a package's
  ports at the consumer boundary instead of reaching into its internals. (Root tests may import
  plugin packages directly; they are tests, not composition.)

"Public `__all__` surface" is checked structurally: an import that resolves to a package's own
top-level `__init__.py` must name only members of that module's `__all__`; an import that
resolves to a specific submodule must land on a submodule that itself declares an `__all__`
(a submodule with no `__all__` at all has no public surface to import). Imports within the same
`packages/*` distribution (including a distribution that ships more than one top-level import
name, such as `beadhive-beads-client`'s `beads_v1_3` and `beadhive_beads_client`) are not
restricted by this rule — the boundary is between distributions, not between files.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, order=True)
class Violation:
    path: Path
    line: int
    edge: str
    reason: str

    def render(self, root: Path) -> str:
        location = f"{self.path.relative_to(root)}:{self.line}"
        return f"{location}: forbidden import {self.edge}: {self.reason}"


@dataclass(frozen=True)
class Edge:
    line: int
    module: str
    # None for a plain `import module` statement (the whole module is bound); a tuple of the
    # imported names for `from module import name, ...`.
    names: tuple[str, ...] | None


def _edges(path: Path) -> tuple[Edge, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    edges: list[Edge] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.extend(Edge(node.lineno, alias.name, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = tuple(alias.name for alias in node.names)
            edges.append(Edge(node.lineno, node.module, names))
    return tuple(edges)


def _is_public_beadhive_surface(module: str) -> bool:
    if module == "beadhive.testing" or module.startswith("beadhive.testing."):
        return True
    parts = module.split(".")
    return (
        len(parts) >= 4
        and parts[0] == "beadhive"
        and parts[1] in {"kernel", "modules"}
        and parts[3] == "contracts"
    )


@dataclass(frozen=True)
class PackageInfo:
    name: str
    path: Path
    import_names: frozenset[str]
    is_plugin: bool


def _discover_packages(root: Path) -> tuple[PackageInfo, ...]:
    packages_root = root / "packages"
    if not packages_root.is_dir():
        return ()
    packages: list[PackageInfo] = []
    for pkg_dir in sorted(p for p in packages_root.iterdir() if p.is_dir()):
        src = pkg_dir / "src"
        if not src.is_dir():
            continue
        import_names = frozenset(
            child.name
            for child in src.iterdir()
            if child.is_dir() and (child / "__init__.py").is_file()
        )
        if not import_names:
            continue
        is_plugin = any((src / name / "plugin.json").is_file() for name in import_names)
        packages.append(PackageInfo(pkg_dir.name, pkg_dir, import_names, is_plugin))
    return tuple(packages)


def _owner_map(packages: tuple[PackageInfo, ...]) -> dict[str, PackageInfo]:
    return {name: pkg for pkg in packages for name in pkg.import_names}


def _resolve_owner(owner_map: dict[str, PackageInfo], dotted: str) -> PackageInfo | None:
    return owner_map.get(dotted.split(".", maxsplit=1)[0])


def _module_file(owner: PackageInfo, dotted: str) -> Path | None:
    # Path.is_file() accepts differently cased names on APFS: the exported class Ref
    # would otherwise resolve to private ref.py. Python module names remain case-sensitive.
    base = owner.path / "src"
    parts = dotted.split(".")
    try:
        for part in parts[:-1]:
            directories = {child.name: child for child in base.iterdir() if child.is_dir()}
            if part not in directories:
                return None
            base = directories[part]
        children = {child.name: child for child in base.iterdir()}
        package = children.get(parts[-1])
        if package is not None and package.is_dir():
            files = {child.name: child for child in package.iterdir()}
            package_init = files.get("__init__.py")
            if package_init is not None and package_init.is_file():
                return package_init
        module = children.get(f"{parts[-1]}.py")
        return module if module is not None and module.is_file() else None
    except OSError:
        return None


def _module_all(path: Path) -> frozenset[str] | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets_all = any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        )
        if not targets_all:
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            return frozenset(
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            )
    return None


def _public_surface_violation(owner_map: dict[str, PackageInfo], edge: Edge) -> str | None:
    """Reason the edge is not a public `__all__`-surface access, or ``None`` if it is one."""
    owner = _resolve_owner(owner_map, edge.module)
    if owner is None:
        return None

    if edge.names is None:
        target = _module_file(owner, edge.module)
        if target is None:
            return f"{edge.module} has no exact-case public module"
        if _module_all(target) is None:
            return f"{edge.module} has no public __all__ surface"
        return None

    for name in edge.names:
        deeper = _module_file(owner, f"{edge.module}.{name}")
        if deeper is not None:
            if _module_all(deeper) is None:
                return f"{edge.module}.{name} has no public __all__ surface"
            continue
        target = _module_file(owner, edge.module)
        if target is None:
            return f"{edge.module} has no exact-case public module"
        all_names = _module_all(target)
        if all_names is None:
            return f"{edge.module} has no public __all__ surface"
        if name not in all_names:
            return f"{name!r} is not in {edge.module}'s public __all__ surface"
    return None


def _package_of(
    package_root: Path, packages: tuple[PackageInfo, ...], path: Path
) -> PackageInfo | None:
    try:
        top = path.relative_to(package_root).parts[0]
    except ValueError:
        return None
    return next((pkg for pkg in packages if pkg.name == top), None)


def check(root: Path = ROOT) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    packages = _discover_packages(root)
    owner_map = _owner_map(packages)
    package_root = root / "packages"

    if package_root.is_dir():
        for path in sorted(package_root.glob("**/*.py")):
            source_pkg = _package_of(package_root, packages, path)
            if source_pkg is None:
                continue
            for edge in _edges(path):
                module = edge.module
                if module == "beadhive" or module.startswith("beadhive."):
                    if source_pkg.is_plugin:
                        if not _is_public_beadhive_surface(module):
                            violations.append(
                                Violation(
                                    path,
                                    edge.line,
                                    module,
                                    "plugin packages may use only kernel/module contracts or "
                                    "beadhive.testing",
                                )
                            )
                    else:
                        violations.append(
                            Violation(
                                path,
                                edge.line,
                                module,
                                "library packages may not import beadhive under any path",
                            )
                        )
                    continue

                target_pkg = _resolve_owner(owner_map, module)
                if target_pkg is None or target_pkg.name == source_pkg.name:
                    # Not a packages/* target, or an intra-distribution access (including a
                    # distribution shipping more than one top-level import name) — not a
                    # package-boundary concern.
                    continue

                if target_pkg.is_plugin:
                    violations.append(
                        Violation(
                            path,
                            edge.line,
                            module,
                            "plugin packages are leaves; only root may resolve them, and only "
                            "lazily after manifest selection",
                        )
                    )
                    continue

                reason = _public_surface_violation(owner_map, edge)
                if reason:
                    violations.append(
                        Violation(
                            path,
                            edge.line,
                            module,
                            f"packages may import another package only through its public "
                            f"__all__ surface ({reason})",
                        )
                    )

    core_root = root / "src" / "beadhive"
    if core_root.is_dir() and owner_map:
        for path in sorted(core_root.glob("**/*.py")):
            for edge in _edges(path):
                target_pkg = _resolve_owner(owner_map, edge.module)
                if target_pkg is None:
                    continue
                if target_pkg.is_plugin:
                    violations.append(
                        Violation(
                            path,
                            edge.line,
                            edge.module,
                            "src/beadhive must resolve a plugin package's implementation lazily",
                        )
                    )
                    continue
                reason = _public_surface_violation(owner_map, edge)
                if reason:
                    violations.append(
                        Violation(
                            path,
                            edge.line,
                            edge.module,
                            f"src/beadhive may import a library package only through its "
                            f"public __all__ surface ({reason})",
                        )
                    )
    return tuple(sorted(violations))


_PATCH_CALLS = frozenset({"setattr", "delattr"})


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_patch_call(func: ast.AST) -> bool:
    dotted = _dotted(func) or ""
    last = dotted.rsplit(".", 1)[-1]
    if last in _PATCH_CALLS:
        return True
    return dotted in {"patch", "mock.patch", "unittest.mock.patch"} or dotted.endswith(
        ("patch.object", "patch.dict")
    )


def _library_aliases(tree: ast.AST, owner_map: dict[str, PackageInfo]) -> dict[str, str]:
    """Local names bound to a library-package module (or member) → their dotted path."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                owner = _resolve_owner(owner_map, alias.name)
                if owner is not None and not owner.is_plugin:
                    local = alias.asname or alias.name.split(".", 1)[0]
                    aliases[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            owner = _resolve_owner(owner_map, node.module)
            if owner is not None and not owner.is_plugin:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _private_patch_target(
    call: ast.Call, aliases: dict[str, str], owner_map: dict[str, PackageInfo]
) -> str | None:
    """The dotted package target a patch call reaches, when it names a private segment."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        target = first.value
    else:
        dotted = _dotted(first)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        if head not in aliases:
            return None
        target = aliases[head] + (f".{rest}" if rest else "")
        if len(call.args) > 1:
            name = call.args[1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                target = f"{target}.{name.value}"
    owner = _resolve_owner(owner_map, target)
    if owner is None or owner.is_plugin:
        return None
    segments = target.split(".")[1:]
    if any(seg.startswith("_") and not seg.startswith("__") for seg in segments):
        return target
    return None


def check_root_tests(root: Path = ROOT) -> tuple[Violation, ...]:
    """Root tests reach a library package only through its public surface and never patch it."""
    owner_map = _owner_map(_discover_packages(root))
    tests_root = root / "tests"
    if not owner_map or not tests_root.is_dir():
        return ()
    violations: list[Violation] = []
    for path in sorted(tests_root.glob("**/*.py")):
        for edge in _edges(path):
            target_pkg = _resolve_owner(owner_map, edge.module)
            if target_pkg is None or target_pkg.is_plugin:
                continue
            reason = _public_surface_violation(owner_map, edge)
            if reason:
                violations.append(
                    Violation(
                        path,
                        edge.line,
                        edge.module,
                        f"root tests may import a library package only through its public "
                        f"__all__ surface ({reason})",
                    )
                )
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _library_aliases(tree, owner_map)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_patch_call(node.func):
                target = _private_patch_target(node, aliases, owner_map)
                if target:
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            target,
                            "root tests may not patch a package-private name; substitute the "
                            "package's port at the consumer boundary instead",
                        )
                    )
    return tuple(sorted(violations))


def main() -> int:
    violations = tuple(sorted((*check(), *check_root_tests())))
    if violations:
        print("package-imports: FAILED", file=sys.stderr)
        for violation in violations:
            print(violation.render(ROOT), file=sys.stderr)
        return 1
    print("package-imports: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
