"""Pants implementation of the build-system impact backend.

The adapter only asks Pants questions (``peek``); it never invokes a build or test goal.
Pants' changed-target calculation supplies the transitive dependent closure, while a complete
``peek ::`` supplies ownership, attest tags, and the test sources checked against the
sandbox-proof manifest written by ``scripts/check_pants_proven.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from beadhive.modules.work.contracts.impact import BackendImpact, ImpactRequest

PANTS_GLOBAL_INPUTS = (
    "pants.toml",
    "BUILD",
    "**/BUILD",
    "*.lock",
    "**/*.lock",
    "pyproject.toml",
    "uv.lock",
    "justfile",
    ".mise.toml",
    "scripts/hermetic.sh",
)
CHANGE_CATEGORY_PREFIX = "category:"
CHANGE_CATEGORIES = frozenset({"code", "test-only", "build-system", "docs", "config"})
PROVEN_TESTS_MANIFEST = Path(__file__).parent / "data" / "proven_tests.json"

PantsQuery = Callable[[str, Sequence[str], float], list[dict[str, Any]]]
PANTS_PEEK_ATTEMPTS = 2
PANTS_PEEK_RETRY_DELAY_SECONDS = 0.25


def _executable(candidate: str | None) -> str | None:
    if not candidate:
        return None
    resolved = shutil.which(candidate)
    if resolved and Path(resolved).is_file() and os.access(resolved, os.X_OK):
        return resolved
    return None


def _launcher(repo: str) -> str:
    """Resolve the same mise-pinned launcher policy used by ``scripts/pants_launcher.py``."""
    explicit = os.environ.get("PANTS_BIN")
    if explicit:
        resolved = _executable(explicit)
        if resolved:
            return resolved
        raise RuntimeError(f"PANTS_BIN is not executable: {explicit}")
    for name in ("pants", "scie-pants"):
        resolved = _executable(name)
        if resolved:
            return resolved
    mise = _executable("mise")
    if mise:
        result = subprocess.run(
            [mise, "which", "scie-pants"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            if len(lines) == 1 and (resolved := _executable(lines[0].strip())):
                return resolved
    raise RuntimeError("Pants launcher unavailable (install the mise-pinned scie-pants)")


def query_pants(repo: str, args: Sequence[str], timeout_seconds: float) -> list[dict[str, Any]]:
    """Run one bounded Pants ``peek`` query and strictly decode its JSON response."""
    command = [_launcher(repo), "--no-pantsd", *args]
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Pants query exceeded {timeout_seconds:g}s") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no stderr"
        raise RuntimeError(f"Pants peek failed ({result.returncode}): {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Pants peek returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise RuntimeError("Pants peek response must be a JSON array of targets")
    return payload


def _source_paths(target: dict[str, Any]) -> tuple[str, ...]:
    sources = target.get("sources") or ()
    if not isinstance(sources, (list, tuple)) or not all(isinstance(path, str) for path in sources):
        raise RuntimeError(f"Pants target {target.get('address')!r} has invalid sources")
    return tuple(sources)


def _tags(target: dict[str, Any]) -> tuple[str, ...]:
    tags = target.get("tags") or ()
    if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
        raise RuntimeError(f"Pants target {target.get('address')!r} has invalid tags")
    return tuple(tags)


def _address(target: dict[str, Any]) -> str:
    address = target.get("address")
    if not isinstance(address, str) or not address:
        raise RuntimeError("Pants target has no address")
    return address


def _proven_test_sources(repo: Path, manifest: Path) -> frozenset[str]:
    path = manifest if manifest.is_absolute() else repo / manifest
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("tests"), dict):
        raise RuntimeError(f"unsupported Pants proven-test manifest: {path}")
    return frozenset(
        source
        for source, evidence in payload["tests"].items()
        if isinstance(source, str)
        and isinstance(evidence, dict)
        and evidence.get("status") == "proven"
    )


class PantsImpactBackend:
    """Answer ownership, affected-unit, and attest-selector questions using Pants."""

    name = "pants"

    def __init__(
        self,
        repo: str | Path,
        *,
        manifest: str | Path | None = None,
        query: PantsQuery = query_pants,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repo = Path(repo).resolve()
        source_manifest = (
            self._repo / "packages/beadhive-pants/src/beadhive_pants/data/proven_tests.json"
        )
        self._manifest = (
            Path(manifest)
            if manifest is not None
            else source_manifest
            if source_manifest.is_file()
            else PROVEN_TESTS_MANIFEST
        )
        self._query = query
        self._clock = clock
        self._sleeper = sleeper
        config = tomllib.loads((self._repo / "pants.toml").read_text(encoding="utf-8"))
        self.version = str(config["GLOBAL"]["pants_version"])

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        repo = Path(request.repo).resolve()
        if repo != self._repo:
            raise RuntimeError(f"Pants backend is bound to {self._repo}, not {repo}")
        started = self._clock()

        def remaining() -> float:
            left = request.timeout_seconds - (self._clock() - started)
            if left <= 0:
                raise TimeoutError(f"Pants impact analysis exceeded {request.timeout_seconds:g}s")
            return left

        def query_with_retry(args: Sequence[str]) -> list[dict[str, Any]]:
            """Retry one transient Pants process failure within the resolver's deadline."""
            for attempt in range(1, PANTS_PEEK_ATTEMPTS + 1):
                try:
                    return self._query(str(repo), args, remaining())
                except RuntimeError:
                    if attempt == PANTS_PEEK_ATTEMPTS:
                        raise
                    self._sleeper(min(PANTS_PEEK_RETRY_DELAY_SECONDS, remaining()))
            raise AssertionError("unreachable")

        # Pants calculates changes relative to the requested base and the checked-out head.
        current_tree = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if current_tree != request.head_tree:
            raise RuntimeError(
                "Pants checkout tree "
                f"{current_tree} does not match requested head {request.head_tree}"
            )

        graph = query_with_retry(("peek", "::"))
        affected_rows = query_with_retry(
            (
                f"--changed-since={request.base_rev or request.base_tree}",
                "--changed-dependents=transitive",
                "peek",
            ),
        )
        proven_sources = _proven_test_sources(repo, self._manifest)

        owners: dict[str, list[str]] = {change.path: [] for change in request.changed}
        owner_categories: dict[str, tuple[str, ...]] = {}
        key_units: dict[str, list[str]] = {key.name: [] for key in request.keys}
        unit_test_sources: dict[str, tuple[str, ...]] = {}
        selectors = {key.name: key.selector(self.name) for key in request.keys}

        for target in graph:
            address = _address(target)
            sources = _source_paths(target)
            for change in request.changed:
                # Deleted files are absent from the head graph and therefore remain unowned;
                # fail-closed rule 1 invalidates all keys. Existing paths must have an owner.
                if not change.deleted and change.path in sources:
                    owners[change.path].append(address)
            tags = set(_tags(target))
            owner_categories[address] = tuple(
                sorted(
                    tag.removeprefix(CHANGE_CATEGORY_PREFIX)
                    for tag in tags
                    if tag.startswith(CHANGE_CATEGORY_PREFIX)
                )
            )
            for key_name, selector in selectors.items():
                if selector and selector in tags:
                    key_units[key_name].append(address)
                    if target.get("target_type") in {"python_test", "python_tests"}:
                        unit_test_sources[address] = sources

        # A category is a required dimension of an owning graph node, not a fallback path-glob
        # classifier.  Refuse an owner with a missing, unknown, or ambiguous value so the
        # resolver degrades to native-full.  Only changed owners matter: category debt elsewhere
        # cannot make an unrelated, completely classified change pay the full gate.
        invalid_categories: list[str] = []
        build_system_paths: list[str] = []
        for path, addresses in owners.items():
            for address in addresses:
                categories = owner_categories.get(address, ())
                if len(categories) != 1 or categories[0] not in CHANGE_CATEGORIES:
                    rendered = ",".join(categories) if categories else "untagged"
                    invalid_categories.append(f"{path} -> {address} ({rendered})")
                elif categories[0] == "build-system":
                    build_system_paths.append(path)
        if invalid_categories:
            raise RuntimeError(
                "Pants changed-path owner lacks exactly one known category tag: "
                + "; ".join(sorted(invalid_categories))
            )

        affected_units = frozenset(_address(target) for target in affected_rows)
        proven_keys = frozenset(
            key_name
            for key_name, units in key_units.items()
            if units
            and all(
                source in proven_sources
                for unit in units
                for source in unit_test_sources.get(unit, ())
            )
        )
        return BackendImpact(
            backend_version=self.version,
            owners={path: tuple(sorted(set(addresses))) for path, addresses in owners.items()},
            affected_units=affected_units,
            key_units={name: tuple(sorted(set(units))) for name, units in key_units.items()},
            proven_keys=proven_keys,
            # Build-system inputs change the graph oracle itself, so their category always takes
            # the conservative full route.  Exact changed paths extend the legacy static list;
            # no second classifier is introduced.
            global_inputs=(*PANTS_GLOBAL_INPUTS, *sorted(set(build_system_paths))),
        )


__all__ = [
    "PANTS_GLOBAL_INPUTS",
    "CHANGE_CATEGORIES",
    "CHANGE_CATEGORY_PREFIX",
    "PROVEN_TESTS_MANIFEST",
    "PantsImpactBackend",
    "query_pants",
]
