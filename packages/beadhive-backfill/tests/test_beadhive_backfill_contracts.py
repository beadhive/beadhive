"""beadhive-backfill: contract shapes and the package boundary (backfill platform ADR §2)."""

from __future__ import annotations

import ast
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import beadhive_backfill
import beadlog
import beadlog_sources
from beadhive_backfill import (
    AuditEntry,
    AuditProgress,
    AuditRecovery,
    AuditSink,
    AuditState,
    HivePass,
    StoreLoader,
)

SOURCE = Path(beadhive_backfill.__file__).parent
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
PUBLIC = {"beadlog": set(beadlog.__all__), "beadlog_sources": set(beadlog_sources.__all__)}


class _Pass:
    name = "noop"
    schema_version = "1"
    invalidating_fields = frozenset({"labels"})
    drift_applicable = False

    def propose(
        self,
        baseline: Sequence[beadlog.BeadEvent],
        *,
        extracted_at: str,
        judge: beadlog_sources.JudgmentPort,
    ) -> Iterable[beadlog.BeadEvent]:
        return ()


class _MemoryStore:
    name = "memory"
    version = "0"
    capabilities = frozenset({str(beadlog.LoaderCapability.ERASE)})

    def __init__(self) -> None:
        self.events: list[beadlog.BeadEvent] = []

    def export(self) -> Iterable[beadlog.BeadEvent]:
        return list(self.events)

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run:
            self.events.extend(events)
        return {"losses": [], "mode": "dry-run" if dry_run else "apply"}


class _ListSink:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def test_ports_conform_structurally() -> None:
    assert isinstance(_Pass(), HivePass)
    assert isinstance(_MemoryStore(), StoreLoader)
    sink = _ListSink()
    assert isinstance(sink, AuditSink)
    sink.record(
        AuditEntry(
            pass_name="noop",
            plan_hash="sha256:" + "0" * 64,
            state=AuditState.APPLYING,
            progress=AuditProgress(planned=1, attempted=0, completed=0, next_index=0),
            recovery=AuditRecovery(pre_state_artifact="pre-state.jsonl"),
        )
    )
    assert sink.entries[0].state == "applying"
    assert [str(state) for state in AuditState] == [
        "applying",
        "applied",
        "rolled_back",
        "rollback_failed",
    ]


def test_public_surface_is_declared_and_importable() -> None:
    for module in (beadhive_backfill, beadhive_backfill.contracts):
        assert module.__all__
        for name in module.__all__:
            assert hasattr(module, name), f"{module.__name__} lacks {name}"


def test_imports_only_stdlib_and_the_two_libraries_public_surfaces() -> None:
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
                assert roots <= STDLIB, f"{path.name}: import {sorted(roots - STDLIB)}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".", 1)[0]
                if root in PUBLIC:
                    assert node.module == root, f"{path.name}: {node.module} is not public"
                    names = {alias.name for alias in node.names}
                    assert names <= PUBLIC[root], f"{path.name}: {sorted(names)}"
                else:
                    assert root in STDLIB | {"beadhive_backfill"}, f"{path.name}: {node.module}"
                    assert root != "beadhive"


def test_declares_only_the_two_libraries_and_is_a_library() -> None:
    pyproject = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == ["beadlog", "beadlog-sources"]
    assert "Private :: Do Not Upload" in pyproject["project"]["classifiers"]
    assert not (SOURCE / "plugin.json").exists()
    assert (resources.files(beadhive_backfill) / "py.typed").is_file()
