from __future__ import annotations

from pathlib import Path

import pytest

from beadhive_worktrees import (
    CallbackWorktreeInventory,
    ManagedWorktree,
    WorktreeHandle,
    WorktreeInventoryRequest,
    WorktreeInventoryService,
    WorktreeLifecycleService,
    WorktreeManagerError,
    WorktreeRemoved,
    WorktreeSpec,
    WorktreeStatusRequest,
)


class RecordingManager:
    binds: tuple[str, ...] = ()
    remove_releases_bindings = False

    def __init__(self, calls: list[str], *, succeeds: bool = True, on_attach=None):
        self.calls = calls
        self.succeeds = succeeds
        self.on_attach = on_attach

    def create(self, spec):
        self.calls.append("manager.create")
        if not self.succeeds:
            raise WorktreeManagerError(spec.path, 7)
        return WorktreeHandle.of(spec)

    def attach(self, spec):
        self.calls.append("manager.attach")
        if self.on_attach is not None:
            self.on_attach()
        return WorktreeHandle.of(spec)

    def remove(self, handle, force):
        self.calls.append(f"manager.remove(force={force})")
        if not self.succeeds:
            raise WorktreeManagerError(handle.path, 9)
        return WorktreeRemoved(handle)


class RecordingObserver:
    def __init__(self, calls: list[str]):
        self.calls = calls

    def creating(self, spec):
        assert spec.path.parent.is_dir()  # observers run once the parent exists
        self.calls.append("observer.creating")

    def created(self, handle):
        self.calls.append("observer.created")


class FakeBranches:
    def __init__(self, tips: list[str], *, contains: bool = True):
        self.tips = tips
        self.contained = contains

    def tip(self, main, branch):
        return self.tips.pop(0)

    def contains(self, main, branch, base):
        return self.contained


def _spec(tmp_path: Path, base: str = "main") -> WorktreeSpec:
    return WorktreeSpec(tmp_path / "main", "wt/bead/issue/b", tmp_path / "wts" / "b", base)


def test_create_runs_observers_around_the_one_selected_manager(tmp_path) -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls), observer=RecordingObserver(calls)
    )

    handle = service.create(_spec(tmp_path))

    assert handle.path == tmp_path / "wts" / "b"
    assert calls == ["observer.creating", "manager.create", "observer.created"]


def test_attach_routes_to_attach_never_create(tmp_path) -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls), observer=RecordingObserver(calls)
    )

    service.attach(_spec(tmp_path, base=""))

    assert calls == ["observer.creating", "manager.attach", "observer.created"]


def test_failed_create_stops_before_created_phase_and_never_falls_back(tmp_path) -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls, succeeds=False), observer=RecordingObserver(calls)
    )

    with pytest.raises(WorktreeManagerError) as failed:
        service.create(_spec(tmp_path))

    assert failed.value.returncode == 7
    assert calls == ["observer.creating", "manager.create"]


def test_remove_goes_only_to_the_selected_manager(tmp_path) -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(manager=RecordingManager(calls))
    handle = WorktreeHandle.for_removal(tmp_path / "main", tmp_path / "wts" / "b")

    removed = service.remove(handle, force=True)

    assert removed == WorktreeRemoved(handle)
    assert calls == ["manager.remove(force=True)"]


def test_attach_with_diverged_base_keeps_the_tip_and_warns(tmp_path) -> None:
    """E37: Beadhive pre-checks start-point intent itself; a diverged base is recorded, not
    applied, and the tip read after the attach must equal the tip read before it."""
    calls: list[str] = []
    warnings: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls),
        branches=FakeBranches(["768dd0e", "768dd0e"], contains=False),
        warn=warnings.append,
    )

    handle = service.attach(_spec(tmp_path))

    assert handle.base == "main"
    assert calls == ["manager.attach"]
    assert len(warnings) == 1 and "never moves a branch tip" in warnings[0]


def test_attach_refuses_a_manager_that_moved_the_branch_tip(tmp_path) -> None:
    """E37: never trust the manager's handling of ``base`` on an existing branch."""
    calls: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls),
        branches=FakeBranches(["768dd0e", "3db946e"]),
    )

    with pytest.raises(WorktreeManagerError) as refused:
        service.attach(_spec(tmp_path))

    assert "must never move an existing branch tip" in refused.value.error


def test_inventory_and_status_cross_one_typed_query_port_without_rendering(capsys) -> None:
    seen = []
    status = object()
    adapter = CallbackWorktreeInventory(
        inventory_reader=lambda hive: (
            seen.append(("inventory", hive)) or [("bh", "/managed/bh-1", "wt/bead/issue/bh-1")]
        ),
        status_reader=lambda hive: seen.append(("status", hive)) or [status],
    )
    service = WorktreeInventoryService(adapter)

    inventory = service.inventory(WorktreeInventoryRequest("github/beadhive/beadhive"))
    statuses = service.status(WorktreeStatusRequest("github/beadhive/beadhive"))

    assert inventory.worktrees == (ManagedWorktree("bh", "/managed/bh-1", "wt/bead/issue/bh-1"),)
    assert statuses.rows == (status,)
    assert seen == [
        ("inventory", "github/beadhive/beadhive"),
        ("status", "github/beadhive/beadhive"),
    ]
    assert capsys.readouterr() == ("", "")
