"""Guard against dataclass defaults that Python 3.11 rejects (bh-oyueb).

3.11's ``dataclasses`` refuses a field default whose type has ``__hash__ is None``; ``mappingproxy``
only became hashable in 3.12. So no ``field(default=...)`` may be a ``MappingProxyType`` value.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "src", *sorted((ROOT / "packages").glob("*/src"))]


def _proxy_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and isinstance(value, ast.Call)
            and getattr(value.func, "id", getattr(value.func, "attr", "")) == "MappingProxyType"
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _offenders() -> list[str]:
    bad: list[str] = []
    for src in SOURCES:
        for path in src.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            proxies = _proxy_names(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "field"):
                    continue
                for kw in node.keywords:
                    v = kw.value
                    direct = isinstance(v, ast.Call) and "MappingProxyType" in ast.dump(v.func)
                    named = isinstance(v, ast.Name) and v.id in proxies
                    if kw.arg == "default" and (direct or named):
                        bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return bad


def test_no_mappingproxy_dataclass_field_default() -> None:
    assert _offenders() == [], "use field(default_factory=...) for MappingProxyType defaults"


@pytest.mark.parametrize(
    "module",
    [
        "beadlog._fold",
        "beadlog._manifest",
        "beadlog_sources.reconcile",
        "beadlog_sources.contracts",
        "beadlog_sources.evidence",
        "beadlog_sources.readers",
        "beadhive_backfill.contracts",
    ],
)
def test_modules_import_on_python_311(module: str) -> None:
    py = shutil.which("python3.11")
    if py is None:
        pytest.skip("python3.11 not available")
    paths = ":".join(str(s) for s in SOURCES)
    result = subprocess.run(
        [py, "-c", f"import {module}"],
        env={"PYTHONPATH": paths, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-600:]
