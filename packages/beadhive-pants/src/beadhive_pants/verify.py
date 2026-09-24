"""Read-only Pants repository verification exposed through ``build.verify``."""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from pathlib import Path

from beadhive.kernel.plugins.contracts import (
    BUILD_VERIFY,
    DiagnosticCode,
    DiagnosticSeverity,
    PluginDiagnostic,
)

from .impact import _launcher

Query = Callable[[Path, Sequence[str]], tuple[int, str, str]]


def _query(root: Path, args: Sequence[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        [_launcher(str(root)), "--no-pantsd", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def tracked_files(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=root, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"git ls-files failed: {result.stderr.strip() or result.returncode}")
    return {line for line in result.stdout.splitlines() if line}


def pants_owned_files(root: Path, *, query: Query = _query) -> set[str]:
    rc, stdout, stderr = query(root, ("filedeps", "::"))
    if rc:
        raise RuntimeError(f"pants filedeps :: failed: {stderr.strip() or f'exit {rc}'}")
    return {line for line in stdout.splitlines() if line}


def doc_readers(root: Path) -> set[str]:
    readers: set[str] = set()
    for path in (root / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        strings = (
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        if any(
            value == "docs" or value.startswith("docs/") or "/docs/" in value for value in strings
        ):
            readers.add(path.relative_to(root).as_posix())
    return readers


def _diagnostic(
    code: DiagnosticCode, severity: DiagnosticSeverity, detail: str
) -> PluginDiagnostic:
    return PluginDiagnostic(code, severity, detail, plugin_id="pants", capability=BUILD_VERIFY)


def verify_ownership(root: Path, *, query: Query = _query) -> PluginDiagnostic:
    try:
        tracked = tracked_files(root)
        missing = sorted(tracked - pants_owned_files(root, query=query))
    except (OSError, RuntimeError) as exc:
        return _diagnostic(
            DiagnosticCode.BUILD_OWNERSHIP, DiagnosticSeverity.ERROR, f"FAILED: {exc}"
        )
    if missing:
        return _diagnostic(
            DiagnosticCode.BUILD_OWNERSHIP,
            DiagnosticSeverity.ERROR,
            f"FAILED: {len(missing)} unowned tracked file(s): {', '.join(missing)}",
        )
    return _diagnostic(
        DiagnosticCode.BUILD_OWNERSHIP,
        DiagnosticSeverity.INFO,
        f"OK: {len(tracked)} tracked files, all Pants-owned",
    )


def _manifest_path(root: Path) -> Path:
    source = root / "packages/beadhive-pants/src/beadhive_pants/data/proven_tests.json"
    if source.is_file():
        return source
    return Path(str(resources.files("beadhive_pants.data") / "proven_tests.json"))


def verify_proven_manifest(root: Path) -> PluginDiagnostic:
    errors: list[str] = []
    try:
        canonical = _manifest_path(root)
        data = json.loads(canonical.read_text(encoding="utf-8"))
        entries = data["tests"]
        mirror = root / "scripts/pants_proven_tests.json"
        if mirror.is_file() and mirror.read_bytes() != canonical.read_bytes():
            errors.append("compatibility mirror differs from plugin data")
        actual = doc_readers(root)
        if not actual <= set(entries):
            errors.append(f"missing doc readers={sorted(actual - set(entries))!r}")
        for path, record in entries.items():
            status = record.get("status")
            if status not in {"proven", "unproven"}:
                errors.append(f"{path}: invalid status {status!r}")
            if status == "proven" and not record.get("dependencies"):
                errors.append(f"{path}: proven entry has no declared dependencies")
            if record.get("partition") == "pants" and status != "proven":
                errors.append(f"{path}: only proven tests may enter the Pants partition")
            if status == "unproven" and not record.get("reason"):
                errors.append(f"{path}: unproven entry has no reason")
    except (OSError, KeyError, TypeError, ValueError, SyntaxError) as exc:
        errors.append(str(exc))
        entries = {}
    if errors:
        return _diagnostic(
            DiagnosticCode.BUILD_PROVEN_MANIFEST,
            DiagnosticSeverity.ERROR,
            "FAILED: " + "; ".join(errors),
        )
    proven = sum(record["status"] == "proven" for record in entries.values())
    return _diagnostic(
        DiagnosticCode.BUILD_PROVEN_MANIFEST,
        DiagnosticSeverity.INFO,
        f"OK: {len(entries)} inventoried, {proven} proven",
    )


def verify_attest_tags(
    root: Path, selectors: Mapping[str, str], *, query: Query = _query
) -> PluginDiagnostic:
    try:
        rc, stdout, stderr = query(root, ("peek", "::"))
        if rc:
            raise RuntimeError(f"pants peek :: failed: {stderr.strip() or f'exit {rc}'}")
        graph = json.loads(stdout)
        tags = {tag for row in graph for tag in (row.get("tags") or ())}
        missing = sorted(
            f"{key}={selector}" for key, selector in selectors.items() if selector not in tags
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _diagnostic(
            DiagnosticCode.BUILD_ATTEST_TAGS, DiagnosticSeverity.ERROR, f"FAILED: {exc}"
        )
    if missing:
        return _diagnostic(
            DiagnosticCode.BUILD_ATTEST_TAGS,
            DiagnosticSeverity.ERROR,
            "FAILED: configured selectors have no Pants targets: " + ", ".join(missing),
        )
    return _diagnostic(
        DiagnosticCode.BUILD_ATTEST_TAGS,
        DiagnosticSeverity.INFO,
        f"OK: {len(selectors)} configured selector(s) present",
    )


class PantsBuildVerifier:
    """Verify ownership, proof inventory, and configured attest-tag coverage."""

    def __init__(
        self, selectors: Mapping[str, str] | None = None, *, query: Query = _query
    ) -> None:
        self._selectors = dict(selectors or {})
        self._query = query

    def verify(self, repo: str) -> tuple[PluginDiagnostic, ...]:
        root = Path(repo).resolve()
        return (
            verify_ownership(root, query=self._query),
            verify_proven_manifest(root),
            verify_attest_tags(root, self._selectors, query=self._query),
        )


__all__ = [
    "PantsBuildVerifier",
    "doc_readers",
    "pants_owned_files",
    "tracked_files",
    "verify_attest_tags",
    "verify_ownership",
    "verify_proven_manifest",
]
