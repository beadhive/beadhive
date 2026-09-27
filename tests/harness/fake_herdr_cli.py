"""A stateful fake of the Herdr 0.9 CLI surface the ``workspace.binding`` uses (bh-cb4jo).

It models exactly the behaviour the bh-mr9tk.1 spike recorded against a real server, so unit
tests exercise the binding against those semantics without a Herdr process:

* ``worktree open --path P`` binds ``P`` idempotently (``already_open`` on repeat, E34) and
  refuses a path that is not a live checkout (``worktree_not_found``);
* ``worktree list --cwd P`` reports ``open_workspace_id`` per checkout (E35);
* ``workspace list`` relabels a bound workspace whose checkout vanished ``"<label> (deleted)"``
  (E31); ``workspace close`` frees the id, and Herdr reuses the lowest free id afterwards;
* ``down()`` makes every call fail with ``server_not_running`` (E33).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace


@dataclass
class _Workspace:
    label: str
    path: str


@dataclass
class FakeHerdrCli:
    workspaces: dict[str, _Workspace] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    running: bool = True

    def down(self) -> None:
        self.running = False

    def up(self) -> None:
        self.running = True

    def __call__(self, *args: str):
        self.calls.append(tuple(args))
        if not self.running:
            return self._error(
                "server_not_running", "no herdr server is running at herdr.sock", args
            )
        verb = tuple(args[:2])
        if verb == ("worktree", "open"):
            return self._open(args)
        if verb == ("worktree", "list"):
            return self._list(self._option(args, "--cwd"))
        if verb == ("workspace", "list"):
            return self._ok(
                {
                    "type": "workspace_list",
                    "workspaces": [
                        {
                            "workspace_id": workspace_id,
                            "label": self._label(workspace),
                            "worktree": {"checkout_path": workspace.path},
                        }
                        for workspace_id, workspace in sorted(self.workspaces.items())
                    ],
                }
            )
        if verb == ("workspace", "close"):
            if self.workspaces.pop(args[2], None) is None:
                return self._error("workspace_not_found", f"workspace {args[2]} not found", args)
            return self._ok({"type": "ok"})
        raise AssertionError(f"unexpected herdr call {args}")

    # ---- helpers -------------------------------------------------------------------------

    def bound(self, path: str | Path) -> str | None:
        return next(
            (wid for wid, ws in self.workspaces.items() if ws.path == str(path)),
            None,
        )

    def _open(self, args):
        path = self._option(args, "--path")
        if not Path(path).is_dir():
            return self._error("worktree_not_found", "worktree path not found", args)
        existing = self.bound(path)
        if existing is not None:
            return self._opened(existing, already_open=True)
        workspace_id = next(f"w{n}" for n in range(1, 1000) if f"w{n}" not in self.workspaces)
        self.workspaces[workspace_id] = _Workspace(
            self._option(args, "--label") or Path(path).name, path
        )
        return self._opened(workspace_id, already_open=False)

    def _opened(self, workspace_id: str, *, already_open: bool):
        workspace = self.workspaces[workspace_id]
        return self._ok(
            {
                "type": "worktree_opened",
                "already_open": already_open,
                "root_pane": {"pane_id": f"{workspace_id}:p1", "workspace_id": workspace_id},
                "workspace": {
                    "workspace_id": workspace_id,
                    "label": workspace.label,
                    "worktree": {"checkout_path": workspace.path},
                },
                "worktree": {"open_workspace_id": workspace_id, "path": workspace.path},
            }
        )

    def _list(self, cwd: str):
        rows = []
        if Path(cwd).is_dir():
            row = {"path": cwd, "branch": "wt/bead/issue/x"}
            if workspace_id := self.bound(cwd):
                row["open_workspace_id"] = workspace_id
            rows.append(row)
        return self._ok({"type": "worktree_list", "worktrees": rows})

    @staticmethod
    def _label(workspace: _Workspace) -> str:
        return workspace.label if Path(workspace.path).is_dir() else f"{workspace.label} (deleted)"

    @staticmethod
    def _option(args, name: str) -> str:
        return args[args.index(name) + 1] if name in args else ""

    @staticmethod
    def _ok(result) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": result}), stderr="")

    @staticmethod
    def _error(code: str, message: str, args) -> SimpleNamespace:
        body = {"id": f"cli:{':'.join(args[:2])}", "error": {"code": code, "message": message}}
        return SimpleNamespace(returncode=1, stdout=json.dumps(body), stderr="")
