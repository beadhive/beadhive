#!/usr/bin/env python3
"""Fast pre-check: is every checked-in generated evidence artifact current for this checkout?

Stale generated evidence was the most common red result of the stateful lane (bh-2kodj): a
config dependency ledger, structural-metrics file, module-evidence count, or native impact
owner that a change forgot to regenerate was discovered only by a test 12-15 minutes into the
lane. Every check here is a generator's own ``--check`` mode (or the same invariant its test
asserts), run concurrently, so the whole pre-check costs roughly its slowest generator (~20 s).

It is an EARLY WARNING, not a gate step: the authoritative tests and the architecture recipe
still run in their lanes. On drift it names the generator to re-run, so the fix is mechanical.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = Path("docs/design")
LEDGER = DESIGN / "config-consumer-migration-ledger.json"
METRICS = DESIGN / "config-module-structural-metrics.json"
EVIDENCE = DESIGN / "config-module-evidence.json"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Check:
    """One generated artifact, how to test its currency, and how to regenerate it."""

    name: str
    artifact: str
    regenerate: str
    run: Callable[[Path], Outcome]


def _command(*argv: str) -> Callable[[Path], Outcome]:
    """A generator ``--check`` subprocess; any non-zero exit is drift."""

    def run(root: Path) -> Outcome:
        result = subprocess.run(
            [sys.executable, *argv],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        detail = "\n".join(
            line for line in (result.stdout + result.stderr).splitlines() if line.strip()
        )
        return Outcome(result.returncode == 0, detail)

    return run


def _certification(root: Path) -> Outcome:
    """Derive runtime closure certification into a private temp file and check it.

    Writing to a temp path keeps this pre-check from touching the shared tree-scoped evidence a
    concurrently running lane may be reading.
    """
    with tempfile.TemporaryDirectory(prefix="bh-evidence-precheck-") as tmp:
        output = str(Path(tmp) / "test-closure-certification.json")
        script = "scripts/test_closure_certification.py"
        derived = _command(script, "--output", output)(root)
        if not derived.ok:
            return derived
        return _command(script, "--check-structural", "--output", output)(root)


def module_evidence_errors(evidence: dict, ledger: dict, metrics: dict) -> list[str]:
    """The checked-in count invariants ``test_config_module_evidence_matches_checked_ledgers``
    asserts, phrased as the exact field edit that restores them."""
    current = ledger.get("current") or {}
    pairs = (
        (
            ("fan_in", "production_config_importers_after"),
            current.get("production_config_importer_count"),
        ),
        (
            ("fan_in", "production_config_schema_importers_after"),
            current.get("production_config_schema_importer_count"),
        ),
        (
            ("static_test_blast_radius", "exact_ast_files_after"),
            current.get("test_config_importer_count"),
        ),
    )
    errors = []
    for (section, field), expected in pairs:
        actual = (evidence.get(section) or {}).get(field)
        if actual != expected:
            errors.append(
                f"{EVIDENCE}: {section}.{field} is {actual!r}; the checked ledger says {expected!r}"
            )
    baseline = evidence.get("baseline_revision")
    if (metrics.get("before") or {}).get("revision") != baseline:
        errors.append(f"{METRICS}: before.revision must equal {EVIDENCE} baseline_revision")
    return errors


def _module_evidence(root: Path) -> Outcome:
    try:
        documents = [
            json.loads((root / path).read_text(encoding="utf-8"))
            for path in (EVIDENCE, LEDGER, METRICS)
        ]
    except (OSError, ValueError) as exc:
        return Outcome(False, f"cannot read config evidence: {exc}")
    errors = module_evidence_errors(*documents)
    return Outcome(not errors, "\n".join(errors))


CHECKS: tuple[Check, ...] = (
    Check(
        "config dependency ledger",
        str(LEDGER),
        "uv run python scripts/config_dependency_ledger.py",
        _command("scripts/config_dependency_ledger.py", "--check"),
    ),
    Check(
        "config structural metrics",
        str(METRICS),
        "uv run python scripts/config_module_metrics.py",
        _command("scripts/config_module_metrics.py", "--check"),
    ),
    Check(
        "config module evidence",
        str(EVIDENCE),
        f"regenerate {LEDGER} first, then copy its current counts into {EVIDENCE} by hand",
        _module_evidence,
    ),
    Check(
        "native impact map",
        "work.attest.keys selectors (fleet config)",
        "add an owning paths selector for every listed path (bh config, scope fleet), then "
        "uv run python scripts/check_native_impact_map.py",
        _command("scripts/check_native_impact_map.py"),
    ),
    Check(
        "test-closure certification (runtime evidence)",
        "tests/closures.toml",
        "just validation-evidence-refresh, then fix the reported closure declarations",
        _certification,
    ),
    Check(
        "operation catalog",
        "latest wire release catalog",
        "just wire-publish",
        _command("scripts/render_operation_catalog.py", "--check"),
    ),
    Check(
        "transport inventory",
        "docs/design/transport-projection-inventory-v1.json",
        "uv run python scripts/render_transport_inventory.py",
        _command("scripts/render_transport_inventory.py", "--check"),
    ),
    Check(
        "daemon OpenAPI",
        "checked daemon OpenAPI document",
        "uv run python -m beadhive.daemon_openapi --write",
        _command("-m", "beadhive.daemon_openapi", "--check"),
    ),
    Check(
        "gateway contract",
        "checked gateway contract",
        "uv run python -m beadhive.gateway_contract --write",
        _command("-m", "beadhive.gateway_contract", "--check"),
    ),
)


def _guarded(check: Check, root: Path) -> Outcome:
    try:
        return check.run(root)
    except Exception as exc:  # noqa: BLE001 - a crashed check is reported, never swallowed
        return Outcome(False, f"{type(exc).__name__}: {exc}")


def run_checks(
    checks: Sequence[Check] = CHECKS, root: Path = ROOT, *, workers: int = 4
) -> list[tuple[Check, Outcome]]:
    """Run every check concurrently; results keep the declared order."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = list(pool.map(lambda check: _guarded(check, root), checks))
    return list(zip(checks, outcomes, strict=True))


def report(results: Sequence[tuple[Check, Outcome]], elapsed: float) -> int:
    stale = [(check, outcome) for check, outcome in results if not outcome.ok]
    for check, outcome in results:
        print(f"  {'✓' if outcome.ok else '✗'} {check.name}")
    if not stale:
        print(f"generated-evidence: OK ({len(results)} checks, {elapsed:.1f}s)")
        return 0
    print(f"generated-evidence: STALE ({len(stale)} of {len(results)} checks, {elapsed:.1f}s)")
    for check, outcome in stale:
        print(f"\n✗ {check.name} is not current: {check.artifact}")
        for line in outcome.detail.splitlines()[:20]:
            print(f"    {line}")
        print(f"  → re-run: {check.regenerate}")
    print("\nCommit the regenerated evidence and validate again.")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    started = time.perf_counter()
    results = run_checks()
    return report(results, time.perf_counter() - started)


if __name__ == "__main__":
    raise SystemExit(main())
