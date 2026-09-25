"""Enforce the static import boundary between core and in-repo packages.

Package code may consume only the deliberately public contract and testing surfaces. Core names
first-party package implementations as data and resolves them at bootstrap; a static import in
the other direction would collapse that boundary.
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


def _imports(path: Path) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    edges: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            edges.append((node.lineno, node.module))
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


def _package_modules(root: Path) -> frozenset[str]:
    return frozenset(
        child.name
        for source_root in (root / "packages").glob("*/src")
        for child in source_root.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    )


def check(root: Path = ROOT) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    package_root = root / "packages"
    if package_root.is_dir():
        for path in sorted(package_root.glob("**/*.py")):
            for line, edge in _imports(path):
                if (edge == "beadhive" or edge.startswith("beadhive.")) and not (
                    _is_public_beadhive_surface(edge)
                ):
                    violations.append(
                        Violation(
                            path,
                            line,
                            edge,
                            "packages may use only kernel/module contracts or beadhive.testing",
                        )
                    )

    package_modules = _package_modules(root)
    core_root = root / "src" / "beadhive"
    if core_root.is_dir() and package_modules:
        for path in sorted(core_root.glob("**/*.py")):
            for line, edge in _imports(path):
                if edge.split(".", maxsplit=1)[0] in package_modules:
                    violations.append(
                        Violation(
                            path,
                            line,
                            edge,
                            "src/beadhive must resolve package implementations lazily",
                        )
                    )
    return tuple(sorted(violations))


def main() -> int:
    violations = check()
    if violations:
        print("package-imports: FAILED", file=sys.stderr)
        for violation in violations:
            print(violation.render(ROOT), file=sys.stderr)
        return 1
    print("package-imports: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
