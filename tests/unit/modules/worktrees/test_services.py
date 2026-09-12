from __future__ import annotations

from pathlib import Path

from beadhive.modules.worktrees import (
    CallbackWorktreeInventory,
    CreateWorktreeRequest,
    ManagedWorktree,
    ProvisioningResult,
    RemoveWorktreeRequest,
    WorktreeInventoryRequest,
    WorktreeInventoryService,
    WorktreeLifecycleService,
    WorktreeStatusRequest,
)


class RecordingProvisioner:
    def __init__(self, name: str, calls: list[str], *, handles: bool, succeeds: bool = True):
        self.name = name
        self.calls = calls
        self.handles = handles
        self.succeeds = succeeds

    def prepare(self, request):
        self.calls.append(f"{self.name}.prepare")

    def create(self, request):
        self.calls.append(f"{self.name}.create")
        if not self.handles:
            return ProvisioningResult.unhandled(request.target)
        if self.succeeds:
            return ProvisioningResult.success(request.target, delegated=self.name == "plugin")
        return ProvisioningResult.failure(request.target, 7)

    def created(self, request, result):
        self.calls.append(f"{self.name}.created")

    def remove(self, request):
        self.calls.append(f"{self.name}.remove")
        if not self.handles:
            return ProvisioningResult.unhandled(request.target)
        if self.succeeds:
            return ProvisioningResult.success(request.target, delegated=self.name == "plugin")
        return ProvisioningResult.failure(request.target, 9)

    def removed(self, request, result):
        self.calls.append(f"{self.name}.removed")


def _create() -> CreateWorktreeRequest:
    return CreateWorktreeRequest(
        Path("/main"), Path("branch").as_posix(), Path("/target"), True, "main"
    )


def _remove() -> RemoveWorktreeRequest:
    return RemoveWorktreeRequest(Path("/main"), Path("/target"), force=True)


def test_create_orders_prepare_delegate_fallback_and_created_phases() -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        native=RecordingProvisioner("native", calls, handles=True),
        plugin=RecordingProvisioner("plugin", calls, handles=False),
    )

    result = service.create(_create())

    assert result.succeeded and not result.delegated
    assert calls == [
        "native.prepare",
        "plugin.prepare",
        "plugin.create",
        "native.create",
        "plugin.created",
        "native.created",
    ]


def test_delegated_create_prevents_native_create() -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        native=RecordingProvisioner("native", calls, handles=True),
        plugin=RecordingProvisioner("plugin", calls, handles=True),
    )

    result = service.create(_create())

    assert result.succeeded and result.delegated
    assert "native.create" not in calls
    assert calls[-2:] == ["plugin.created", "native.created"]


def test_failed_native_create_stops_before_created_phase() -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        native=RecordingProvisioner("native", calls, handles=True, succeeds=False),
        plugin=RecordingProvisioner("plugin", calls, handles=False),
    )

    result = service.create(_create())

    assert result.returncode == 7 and not result.succeeded
    assert calls[-1] == "native.create"


def test_remove_is_plugin_first_and_runs_removed_only_after_success() -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        native=RecordingProvisioner("native", calls, handles=True),
        plugin=RecordingProvisioner("plugin", calls, handles=False),
    )

    result = service.remove(_remove())

    assert result.succeeded and not result.delegated
    assert calls == [
        "plugin.remove",
        "native.remove",
        "plugin.removed",
        "native.removed",
    ]


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


def test_failed_plugin_create_does_not_fall_back_or_render(capsys) -> None:
    calls: list[str] = []
    service = WorktreeLifecycleService(
        native=RecordingProvisioner("native", calls, handles=True),
        plugin=RecordingProvisioner("plugin", calls, handles=True, succeeds=False),
    )

    result = service.create(_create())

    assert result.returncode == 7
    assert "native.create" not in calls
    assert not any(call.endswith("created") for call in calls)
    assert capsys.readouterr() == ("", "")
