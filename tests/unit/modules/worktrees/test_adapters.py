from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from beadhive.modules.worktrees import (
    CreateWorktreeRequest,
    NativeGitWorktreeProvisioner,
    PluginWorktreeProvisioner,
    ProvisioningResult,
    RemoveWorktreeRequest,
)


def test_native_adapter_builds_new_attach_and_remove_commands(tmp_path) -> None:
    calls = []

    def run_git(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    adapter = NativeGitWorktreeProvisioner(run_git)
    main = tmp_path / "main"
    target = tmp_path / "nested" / "seat"
    request = CreateWorktreeRequest(main, "wt/bead/issue/x", target, True, "main")

    adapter.prepare(request)
    created = adapter.create(request)
    removed = adapter.remove(RemoveWorktreeRequest(main, target, force=True))

    assert target.parent.is_dir()
    assert created.succeeded and not created.delegated
    assert removed.succeeded and not removed.delegated
    assert calls == [
        (
            [
                "git",
                "-C",
                str(main),
                "worktree",
                "add",
                "-b",
                "wt/bead/issue/x",
                str(target),
                "main",
            ],
            {"check": False},
        ),
        (
            ["git", "-C", str(main), "worktree", "remove", str(target), "--force"],
            {"check": False},
        ),
    ]


def test_native_adapter_preserves_attach_prune_order_and_failure_code(tmp_path) -> None:
    calls = []

    def run_git(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=19 if "add" in command else 0)

    adapter = NativeGitWorktreeProvisioner(run_git)
    request = CreateWorktreeRequest(tmp_path / "main", "wt/existing", tmp_path / "seat", False)

    result = adapter.create(request)

    assert result == ProvisioningResult.failure(request.target, 19)
    assert calls[0][-2:] == ["worktree", "prune"]
    assert calls[1][-2:] == [str(request.target), request.branch]


def test_plugin_adapter_preserves_observer_and_delegation_phases() -> None:
    calls = []
    target = Path("/delegated")
    adapter = PluginWorktreeProvisioner(
        preparing=lambda _request: calls.append("prepare"),
        create_delegate=lambda _request: target,
        created_observer=lambda _request, _result: calls.append("created"),
        remove_delegate=lambda _request: True,
        removed_observer=lambda _request, _result: calls.append("removed"),
    )
    create = CreateWorktreeRequest(Path("/main"), "wt/x", Path("/wanted"), True)
    remove = RemoveWorktreeRequest(Path("/main"), target)

    adapter.prepare(create)
    created = adapter.create(create)
    adapter.created(create, created)
    removed = adapter.remove(remove)
    adapter.removed(remove, removed)

    assert created == ProvisioningResult.success(target, delegated=True)
    assert removed == ProvisioningResult.success(target, delegated=True)
    assert calls == ["prepare", "created", "removed"]


def test_plugin_adapter_refuses_attach_delegation_but_warns_when_available() -> None:
    delegated = []
    warnings = []
    adapter = PluginWorktreeProvisioner(
        create_delegate=lambda request: delegated.append(request) or request.target,
        supports_create=lambda: True,
        warn=warnings.append,
    )
    request = CreateWorktreeRequest(Path("/main"), "wt/x", Path("/target"), False)

    result = adapter.create(request)

    assert not result.handled
    assert delegated == []
    assert warnings == ["worktree attach stays native (delegation only covers new-branch create)"]
