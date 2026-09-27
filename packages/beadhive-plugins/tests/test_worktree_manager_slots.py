"""The ``worktree.manager`` / ``workspace.binding`` slot declarations (bh-055ot.1)."""

from __future__ import annotations

from beadhive_plugins import (
    WORKSPACE_BINDING,
    WORKTREE_MANAGER,
    CapabilityKey,
    CapabilityRef,
    CapabilitySelection,
    DiscoveryResult,
    ProviderBinding,
    ProviderKey,
    WorkspaceBinding,
    WorktreeManager,
    bind_application_port,
    binding_composes,
)


class _Manager:
    binds: tuple[str, ...] = ()
    remove_releases_bindings = False

    def create(self, spec):
        return spec

    def attach(self, spec):
        return spec

    def remove(self, handle, force):
        return (handle, force)


class _Binding:
    presenter = "herdr"

    def bind(self, handle):
        return handle

    def release(self, handle):
        return None


def test_slots_have_stable_identities() -> None:
    assert WORKTREE_MANAGER == CapabilityRef("worktree.manager", 1)
    assert WORKSPACE_BINDING == CapabilityRef("workspace.binding", 1)


def test_a_structural_manager_binds_through_the_declared_slot() -> None:
    manager = _Manager()
    discovery = DiscoveryResult((), (CapabilitySelection(WORKTREE_MANAGER, "native"),), ())
    providers = (ProviderBinding(ProviderKey("native", WORKTREE_MANAGER), manager),)

    port = bind_application_port(
        CapabilityKey(WORKTREE_MANAGER, WorktreeManager), discovery, providers
    )

    assert port is manager


def test_the_manager_port_requires_capability_flags_and_all_three_methods() -> None:
    class _NoAttach:
        binds: tuple[str, ...] = ()
        remove_releases_bindings = False

        def create(self, spec):
            return spec

        def remove(self, handle, force):
            return handle

    class _NoFlags:
        def create(self, spec):
            return spec

        def attach(self, spec):
            return spec

        def remove(self, handle, force):
            return handle

    assert isinstance(_Manager(), WorktreeManager)
    assert not isinstance(_NoAttach(), WorktreeManager)
    assert not isinstance(_NoFlags(), WorktreeManager)


def test_workspace_binding_port_and_composition_rule() -> None:
    assert isinstance(_Binding(), WorkspaceBinding)
    assert binding_composes((), "herdr") is True
    assert binding_composes(("herdr",), "herdr") is False
