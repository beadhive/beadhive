"""The Herdr ``workspace.binding`` against a fake Herdr CLI (bh-cb4jo.1, bh-cb4jo.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive.integrations.herdr.workspace_binding import HerdrWorkspaceBinding
from beadhive.modules.worktrees import (
    NativeGitWorktreeManager,
    WorkspaceBindingError,
    WorktreeHandle,
    WorktreeLifecycleService,
    WorktreeRemoved,
)
from harness.fake_herdr_cli import FakeHerdrCli


def _handle(path: Path, **bindings: str) -> WorktreeHandle:
    handle = WorktreeHandle(path.name, path.parent, path, "wt/bead/issue/x")
    for presenter, reference in bindings.items():
        handle = handle.with_binding(presenter, reference)
    return handle


@pytest.fixture
def worktree(tmp_path) -> Path:
    path = tmp_path / "wts" / "a-1"
    path.mkdir(parents=True)
    return path


def test_bind_opens_the_exact_path_and_records_the_workspace_id(worktree) -> None:
    herdr = FakeHerdrCli()
    binding = HerdrWorkspaceBinding(herdr, label="bh:github/acme/widgets")

    bound = binding.bind(_handle(worktree))

    assert bound.bindings == {"herdr": "w1"}
    assert herdr.calls == [
        (
            "worktree",
            "open",
            "--cwd",
            str(worktree.parent),
            "--path",
            str(worktree),
            "--label",
            "bh:github/acme/widgets",
            "--no-focus",
        )
    ]
    assert binding.last_opened is not None
    assert binding.last_opened.root_pane_id == "w1:p1"
    assert not any(call[:2] == ("workspace", "create") for call in herdr.calls)


def test_bind_is_idempotent_and_the_reference_round_trips_through_worktree_list(worktree) -> None:
    """E34 + E35: a repeated bind returns the same reference, and ``worktree list`` re-derives
    it independently of the handle."""
    herdr = FakeHerdrCli()
    binding = HerdrWorkspaceBinding(herdr)

    first = binding.bind(_handle(worktree))
    second = binding.bind(first)

    assert first.bindings == second.bindings == {"herdr": "w1"}
    assert binding.last_opened is not None and binding.last_opened.already_open
    assert binding.derive(worktree) == "w1"
    assert len(herdr.workspaces) == 1


def test_bind_after_create_and_after_attach_are_indistinguishable(tmp_path) -> None:
    """E28/E29: the binding never cares whether the manager created or attached."""
    herdr = FakeHerdrCli()
    binding = HerdrWorkspaceBinding(herdr)
    created, attached = tmp_path / "created", tmp_path / "attached"
    created.mkdir()
    attached.mkdir()

    assert binding.bind(_handle(created)).bindings == {"herdr": "w1"}
    assert binding.bind(_handle(attached)).bindings == {"herdr": "w2"}


def test_bind_with_herdr_down_raises_a_coded_binding_error(worktree) -> None:
    """E33: an unavailable server is a typed presentation failure the service turns into a gap."""
    herdr = FakeHerdrCli()
    herdr.down()

    with pytest.raises(WorkspaceBindingError) as failed:
        HerdrWorkspaceBinding(herdr).bind(_handle(worktree))

    assert failed.value.presenter == "herdr"
    assert failed.value.code == "server_not_running"
    assert worktree.is_dir()


def test_missing_herdr_binary_is_an_unavailable_binding_error(worktree) -> None:
    with pytest.raises(WorkspaceBindingError) as failed:
        HerdrWorkspaceBinding(lambda *_args: None).bind(_handle(worktree))

    assert failed.value.code == "unavailable"


def test_release_closes_the_verified_workspace(worktree) -> None:
    herdr = FakeHerdrCli()
    binding = HerdrWorkspaceBinding(herdr)
    handle = binding.bind(_handle(worktree))

    binding.release(handle)

    assert herdr.workspaces == {}
    assert ("workspace", "close", "w1") in herdr.calls


def test_release_never_closes_a_reused_id_bound_to_another_checkout(tmp_path) -> None:
    """Herdr reuses workspace ids after a close: a stale reference must not close a stranger."""
    herdr = FakeHerdrCli()
    binding = HerdrWorkspaceBinding(herdr)
    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    binding.bind(_handle(other))  # other now owns w1

    binding.release(_handle(mine, herdr="w1"))

    assert herdr.bound(other) == "w1"
    assert not any(call[:2] == ("workspace", "close") for call in herdr.calls)


def test_release_of_a_pending_intent_re_derives_the_workspace(worktree) -> None:
    """A crash after ``open`` but before the id was recorded leaves an empty reference; release
    finds the workspace from Herdr's own inventory rather than leaking it."""
    herdr = FakeHerdrCli()
    binding = HerdrWorkspaceBinding(herdr)
    binding.bind(_handle(worktree))

    binding.release(_handle(worktree, herdr=""))

    assert herdr.workspaces == {}


def test_release_without_any_recorded_binding_never_calls_herdr(worktree) -> None:
    herdr = FakeHerdrCli()

    HerdrWorkspaceBinding(herdr).release(_handle(worktree))

    assert herdr.calls == []


def test_close_of_an_already_closed_workspace_is_idempotent(worktree) -> None:
    herdr = FakeHerdrCli()

    assert HerdrWorkspaceBinding(herdr).close("w9") is False


def test_a_remove_without_release_orphans_the_workspace_and_close_is_its_only_repair(
    tmp_path,
) -> None:
    """E31: skipping release leaves a ``"<label> (deleted)"`` workspace; ``workspace close``
    repairs it (``orphans`` only ever names Beadhive-labelled records)."""
    herdr = FakeHerdrCli()
    binding = HerdrWorkspaceBinding(herdr, label="bh:github/acme/widgets")
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    ours.mkdir()
    theirs.mkdir()
    binding.bind(_handle(ours))
    HerdrWorkspaceBinding(herdr, label="scratch").bind(_handle(theirs))
    ours.rmdir()  # the native remove ran with no release first
    theirs.rmdir()

    orphans = binding.orphans()

    assert [(item.workspace_id, item.label) for item in orphans] == [
        ("w1", "bh:github/acme/widgets (deleted)")
    ]
    assert binding.close("w1") is True
    assert binding.orphans() == ()
    assert herdr.bound(theirs) == "w2"  # a foreign orphan is never touched


def test_the_native_lifecycle_service_releases_herdr_before_git_removes(tmp_path) -> None:
    """E30 end to end over the real composition: the native manager binds nothing, so the Herdr
    binding is composed, and its close lands before ``git worktree remove``."""
    order: list[str] = []
    herdr = FakeHerdrCli()

    def run_git(args, **_kwargs):
        order.append("git " + " ".join(args[3:5]))
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    def herdr_cli(*args):
        order.append("herdr " + " ".join(args[:2]))
        return herdr(*args)

    binding = HerdrWorkspaceBinding(herdr_cli)
    service = WorktreeLifecycleService(
        manager=NativeGitWorktreeManager(run_git), bindings=(binding,)
    )
    worktree = tmp_path / "a-1"
    worktree.mkdir()
    handle = service.bind(_handle(worktree)).handle
    order.clear()

    removed = service.remove(handle, force=True)

    assert removed == WorktreeRemoved(handle)
    assert order == ["herdr workspace list", "herdr workspace close", "git worktree remove"]
