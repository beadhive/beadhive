from __future__ import annotations

from pathlib import Path

import pytest

from beadhive_worktrees import (
    BindingGap,
    BoundWorktree,
    CallbackWorktreeInventory,
    ManagedWorktree,
    WorkspaceBindingError,
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


class RecordingBinding:
    """A fake ``workspace.binding`` recording every call into the shared ordering log."""

    def __init__(self, calls: list[str], presenter: str = "herdr", *, fails: str = ""):
        self.calls = calls
        self.presenter = presenter
        self.fails = fails

    def bind(self, handle):
        self.calls.append(f"{self.presenter}.bind")
        if self.fails == "bind":
            raise WorkspaceBindingError(self.presenter, "server down", code="server_not_running")
        return handle.with_binding(self.presenter, handle.bindings.get(self.presenter) or "w4")

    def release(self, handle):
        self.calls.append(f"{self.presenter}.release")
        if self.fails == "release":
            raise WorkspaceBindingError(self.presenter, "server down", code="server_not_running")


class HerdrBindingManager(RecordingManager):
    """A manager that binds Herdr itself as a side effect (the deferred Option B shape)."""

    binds: tuple[str, ...] = ("herdr",)
    remove_releases_bindings = True


def test_binding_is_composed_for_a_manager_that_does_not_bind_the_presenter(tmp_path) -> None:
    calls: list[str] = []
    binding = RecordingBinding(calls)
    service = WorktreeLifecycleService(manager=RecordingManager(calls), bindings=(binding,))

    bound = service.bind(WorktreeHandle.of(_spec(tmp_path)))

    assert service.bindings == (binding,)
    assert service.binding("herdr") is binding
    assert bound.handle.bindings == {"herdr": "w4"}
    assert bound.gaps == ()
    assert calls == ["herdr.bind"]


def test_no_separate_binding_is_composed_when_the_manager_already_binds_it(tmp_path) -> None:
    """The ADR composition rule: ``binds: ["herdr"]`` means no second, redundant binding."""
    calls: list[str] = []
    service = WorktreeLifecycleService(
        manager=HerdrBindingManager(calls), bindings=(RecordingBinding(calls),)
    )
    handle = WorktreeHandle.of(_spec(tmp_path))

    assert service.bindings == ()
    assert service.binding("herdr") is None
    assert service.bind(handle) == BoundWorktree(handle)
    service.remove(handle, force=False)
    assert calls == ["manager.remove(force=False)"]


def test_bind_is_idempotent_and_returns_the_same_reference(tmp_path) -> None:
    """E34: a repeated bind on an already-bound handle returns the same reference."""
    service = WorktreeLifecycleService(
        manager=RecordingManager([]), bindings=(RecordingBinding([]),)
    )

    first = service.bind(WorktreeHandle.of(_spec(tmp_path))).handle
    second = service.bind(first).handle

    assert first.bindings == second.bindings == {"herdr": "w4"}


def test_a_failed_bind_is_a_reported_gap_that_never_touches_the_worktree(tmp_path) -> None:
    """E33: the presenter being down leaves the worktree unbound, reported, never rolled back."""
    calls: list[str] = []
    warnings: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls),
        bindings=(RecordingBinding(calls, fails="bind"),),
        warn=warnings.append,
    )
    handle = service.create(_spec(tmp_path))

    bound = service.bind(handle)

    assert bound.handle == handle
    assert bound.gaps == (BindingGap("herdr", "bind", "server down", "server_not_running"),)
    assert calls == ["manager.create", "herdr.bind"]
    assert len(warnings) == 1 and "binding gap" in warnings[0]


def test_remove_releases_every_composed_binding_before_the_manager_removes(tmp_path) -> None:
    """E30: release, then remove — two ordered effects, never a remove without a release."""
    calls: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls),
        bindings=(RecordingBinding(calls), RecordingBinding(calls, "other")),
    )
    handle = WorktreeHandle.for_removal(tmp_path / "main", tmp_path / "wts" / "b")

    service.remove(handle.with_binding("herdr", "w4"), force=True)

    assert calls == ["herdr.release", "other.release", "manager.remove(force=True)"]


def test_an_unreleasable_binding_is_reported_and_the_remove_still_runs(tmp_path) -> None:
    calls: list[str] = []
    warnings: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls),
        bindings=(RecordingBinding(calls, fails="release"),),
        warn=warnings.append,
    )
    handle = WorktreeHandle.for_removal(tmp_path / "main", tmp_path / "wts" / "b")

    gaps = service.release(handle)
    removed = service.remove(handle, force=False)

    assert gaps == (BindingGap("herdr", "release", "server down", "server_not_running"),)
    assert removed.gaps == gaps and removed.handle == handle
    assert calls == ["herdr.release", "herdr.release", "manager.remove(force=False)"]
    assert len(warnings) == 2


def test_the_release_still_precedes_a_remove_the_manager_refuses(tmp_path) -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        manager=RecordingManager(calls, succeeds=False), bindings=(RecordingBinding(calls),)
    )

    with pytest.raises(WorktreeManagerError):
        service.remove(WorktreeHandle.for_removal(tmp_path, tmp_path / "b"), force=False)

    assert calls == ["herdr.release", "manager.remove(force=False)"]


def test_handle_binding_references_can_be_added_and_dropped(tmp_path) -> None:
    handle = WorktreeHandle.of(_spec(tmp_path)).with_binding("herdr", "w4")

    assert handle.bindings == {"herdr": "w4"}
    assert handle.without_binding("herdr").bindings == {}
    assert handle.bindings == {"herdr": "w4"}
