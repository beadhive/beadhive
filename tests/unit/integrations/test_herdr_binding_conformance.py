"""The Herdr ``workspace.binding`` (ADR bh-mr9tk.2 Option A) passes the binding conformance kit.

Runs every case of ``beadhive_worktrees.testing.BINDING_CASES`` — bind idempotence, the
re-derivable binding reference, release-before-remove, and the stale-reference guard — against
:class:`HerdrWorkspaceBinding` over the stateful fake Herdr CLI. The oracles read the fake
server's own workspace table, never the handle's cached reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive.integrations.herdr.workspace_binding import HerdrWorkspaceBinding
from beadhive.modules.worktrees import WorktreeHandle
from beadhive_worktrees.testing import (
    BINDING_CASES,
    BindingHarness,
    ConformanceCase,
    assert_binding_conforms,
    run_binding_case,
)
from harness.fake_herdr_cli import FakeHerdrCli


class HerdrHarness:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self.herdr = FakeHerdrCli()
        self.binding = HerdrWorkspaceBinding(self.herdr, label="bh:github/acme/widgets")

    def worktree(self, leaf: str) -> WorktreeHandle:
        path = self._tmp_path / "wts" / leaf
        path.mkdir(parents=True, exist_ok=True)
        return WorktreeHandle(leaf, self._tmp_path / "main", path, f"wt/bead/issue/{leaf}")

    def derive(self, path: Path) -> str | None:
        return self.binding.derive(path)

    def presentations(self, path: Path) -> int:
        return sum(1 for workspace in self.herdr.workspaces.values() if workspace.path == str(path))

    def is_live(self, reference: str) -> bool:
        return reference in self.herdr.workspaces


@pytest.mark.parametrize("case", BINDING_CASES, ids=lambda case: case.case_id)
def test_herdr_binding_passes_each_conformance_case(
    case: ConformanceCase[BindingHarness], tmp_path
) -> None:
    run_binding_case(case, HerdrHarness(tmp_path))


def test_herdr_binding_passes_the_whole_kit_on_fresh_servers(tmp_path) -> None:
    servers: list[FakeHerdrCli] = []

    def factory() -> HerdrHarness:
        harness = HerdrHarness(tmp_path / f"case-{len(servers)}")
        servers.append(harness.herdr)
        return harness

    assert_binding_conforms(factory)
    assert len(servers) == len(BINDING_CASES)
    # No case ever fell back to a plain `workspace create --cwd` for a managed worktree (E48).
    assert not any(call[:2] == ("workspace", "create") for s in servers for call in s.calls)
