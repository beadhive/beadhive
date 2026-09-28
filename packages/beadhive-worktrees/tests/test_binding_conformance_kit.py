"""The ``workspace.binding`` conformance kit, proven on the package's reference binding.

The Herdr binding (root's ``beadhive.integrations.herdr``) runs the same
:data:`BINDING_CASES`; here the kit proves its own cases are meaningful — the in-memory
reference provider passes, and providers that break one clause of the contract are named by the
exact case they break.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive_worktrees import WorktreeHandle
from beadhive_worktrees.testing import (
    BINDING_CASES,
    BindingHarness,
    ConformanceCase,
    ConformanceFailure,
    InMemoryBeadStateLookup,
    InMemoryPresenter,
    InMemoryWorkspaceBinding,
    assert_binding_conforms,
    run_binding_case,
)


class MemoryHarness:
    def __init__(self, tmp_path: Path, binding: InMemoryWorkspaceBinding | None = None) -> None:
        self._tmp_path = tmp_path
        self.binding = binding or InMemoryWorkspaceBinding()

    def worktree(self, leaf: str) -> WorktreeHandle:
        path = self._tmp_path / "wts" / leaf
        path.mkdir(parents=True, exist_ok=True)
        return WorktreeHandle(leaf, self._tmp_path / "main", path, f"wt/bead/issue/{leaf}")

    def derive(self, path: Path) -> str | None:
        return self.binding.state.derive(path)

    def presentations(self, path: Path) -> int:
        return self.binding.state.presentations(path)

    def is_live(self, reference: str) -> bool:
        return reference in self.binding.state.workspaces


@pytest.mark.parametrize("case", BINDING_CASES, ids=lambda case: case.case_id)
def test_reference_binding_passes_each_conformance_case(
    case: ConformanceCase[BindingHarness], tmp_path
) -> None:
    run_binding_case(case, MemoryHarness(tmp_path))


def test_the_whole_binding_kit_runs_on_a_fresh_harness_per_case(tmp_path) -> None:
    built: list[int] = []

    def factory() -> MemoryHarness:
        built.append(1)
        return MemoryHarness(tmp_path / str(len(built)))

    assert_binding_conforms(factory)
    assert len(built) == len(BINDING_CASES)


class _DuplicatesOnRebind(InMemoryWorkspaceBinding):
    def bind(self, handle):
        self.state.workspaces[f"dup{len(self.state.workspaces)}"] = handle.path
        return super().bind(handle)


class _TrustsTheCachedReference(InMemoryWorkspaceBinding):
    def release(self, handle):
        self.state.workspaces.pop(handle.bindings.get(self.presenter, ""), None)


class _ForgetsToRelease(InMemoryWorkspaceBinding):
    def release(self, handle):
        return None


@pytest.mark.parametrize(
    ("broken", "case_id"),
    [
        (_DuplicatesOnRebind, "bind.idempotent"),
        (_TrustsTheCachedReference, "release.never-closes-foreign"),
        (_TrustsTheCachedReference, "bind.reference-rederivable"),
        (_ForgetsToRelease, "release.before-remove"),
    ],
)
def test_the_kit_names_the_clause_a_broken_binding_breaks(tmp_path, broken, case_id) -> None:
    harness = MemoryHarness(tmp_path, broken(InMemoryPresenter()))
    case = next(case for case in BINDING_CASES if case.case_id == case_id)

    with pytest.raises(ConformanceFailure) as failure:
        run_binding_case(case, harness)

    assert failure.value.case_id == case_id
    assert failure.value.suite == "workspace.binding"


def test_a_provider_that_is_not_a_binding_is_refused_before_any_case(tmp_path) -> None:
    harness = MemoryHarness(tmp_path)
    harness.binding = object()  # type: ignore[assignment]

    with pytest.raises(ConformanceFailure, match="WorkspaceBinding slot"):
        run_binding_case(BINDING_CASES[0], harness)  # type: ignore[arg-type]


def test_in_memory_bead_state_lookup_models_readable_empty_and_unreadable_stores() -> None:
    main = Path("/hive")
    lookup = InMemoryBeadStateLookup({"bh-1": {"status": "closed", "close_reason": "merged"}})
    assert lookup.probe(main) == [{"id": "bh-1", "status": "closed", "close_reason": "merged"}]
    assert lookup.show("bh-1", main) == {"id": "bh-1", "status": "closed", "close_reason": "merged"}
    assert lookup.show("bh-2", main) is None
    assert lookup.all_issues(main) == lookup.probe(main)
    assert lookup.shows == [("bh-1", main), ("bh-2", main)]
    assert InMemoryBeadStateLookup().probe(main) == []
    unreadable = InMemoryBeadStateLookup({"bh-1": {"status": "open"}}, readable=False)
    assert (unreadable.probe(main), unreadable.show("bh-1", main)) == (None, None)
    assert unreadable.all_issues(main) is None
