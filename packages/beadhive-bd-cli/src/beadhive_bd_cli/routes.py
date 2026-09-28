"""Named ``bd`` argv routes used by the root compatibility shell.

The root application owns policy and composition.  This module owns the remaining mechanical
translation from semantic operations to ``bd`` argv, keeping that translation independently
testable and preventing new inline command builders from accumulating in ``src/beadhive``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .transport import BdTransport

__all__ = ["CliRoutes", "public_snapshot_argv"]


def public_snapshot_argv(output: Path) -> list[str]:
    """The deliberately narrow one-hive public snapshot route."""
    return ["export", "-o", str(output)]


class CliRoutes:
    """The shell's named compatibility routes over a caller supplied transport."""

    def __init__(self, bd: BdTransport, cwd: Any) -> None:
        self._bd = bd
        self._cwd = cwd

    def issue_list(
        self,
        *,
        label: str = "",
        status: str = "",
        all_: bool = False,
        include_infra: bool = False,
        limit: int | None = None,
        strict: bool = False,
        label_first: bool = False,
    ) -> Any:
        args = ["list"]
        if all_:
            args.append("--all")
        if include_infra:
            args.append("--include-infra")
        if limit is not None:
            args += ["--limit", str(limit)]
        filters = []
        if label_first and label:
            filters += ["--label", label]
        if status:
            filters += ["--status", status]
        if not label_first and label:
            filters += ["--label", label]
        args += filters
        if strict:
            return self._bd.json(args, self._cwd, strict=True)
        return self._bd.json(args, self._cwd)

    def issue_show_raw(self, bead: str) -> Any:
        """The un-normalized ``bd show --json`` shape (object or one-element list)."""
        return self._bd.json(["show", bead], self._cwd)

    def forward(self, args: Iterable[str], *, capture: bool = True) -> Any:
        """A named byte-forward route for shell commands whose flags are intentionally opaque."""
        return self._bd.run(list(args), self._cwd, capture=capture)

    def json_forward(self, args: Iterable[str]) -> Any:
        """A named JSON-forward route for compatibility reads with caller-owned flag policy."""
        return self._bd.run([*args, "--json"], self._cwd, capture=True)

    def gate_list(self, *, include_resolved: bool = False, strict: bool = False) -> Any:
        args = ["gate", "list", "--limit", "0"]
        if include_resolved:
            args.append("--all")
        if strict:
            return self._bd.json(args, self._cwd, strict=True)
        return self._bd.json(args, self._cwd)

    def gate_list_raw(self, *, include_resolved: bool = False) -> Any:
        """Captured JSON form, preserving the distinction between ``null`` and command failure."""
        args = ["gate", "list", "--limit", "0"]
        if include_resolved:
            args.append("--all")
        return self._bd.run([*args, "--json"], self._cwd, capture=True)

    def gate_create(
        self,
        bead: str,
        gate_type: str,
        reason: str = "",
        *,
        actor: str = "",
        capture: bool = False,
    ) -> Any:
        return self._bd.run(
            ["gate", "create", "--blocks", bead, "--type", gate_type, "--reason", reason],
            self._cwd,
            actor=actor,
            capture=capture,
        )

    def gate_resolve(self, gate_id: str, *, reason: str = "", actor: str = "") -> Any:
        args = ["gate", "resolve", gate_id]
        if reason:
            args += ["--reason", reason]
        return self._bd.run(args, self._cwd, actor=actor)

    def issue_claim(self, bead: str, *, actor: str = "") -> Any:
        return self._bd.run(["update", bead, "--claim"], self._cwd, actor=actor)

    def issue_update_fields(
        self,
        bead: str,
        *,
        issue_type: str = "",
        priority: str = "",
        actor: str = "",
        capture: bool = False,
    ) -> Any:
        args = ["update", bead]
        if issue_type:
            args += ["--type", issue_type]
        if priority:
            args += ["--priority", priority]
        return self._bd.run(args, self._cwd, actor=actor, capture=capture)

    def issue_assign(
        self, bead: str, assignee: str, *, actor: str = "", capture: bool = False
    ) -> Any:
        return self._bd.run(["assign", bead, assignee], self._cwd, actor=actor, capture=capture)

    def issue_close(
        self,
        beads: str | Iterable[str],
        *,
        reason: str,
        actor: str = "",
        force: bool = False,
        capture: bool = False,
    ) -> Any:
        ids = [beads] if isinstance(beads, str) else list(beads)
        args = ["close", *ids]
        if reason:
            args += ["--reason", reason]
        if force:
            args.append("--force")
        return self._bd.run(args, self._cwd, actor=actor, capture=capture)

    def issue_reopen(self, bead: str) -> Any:
        return self._bd.run(["reopen", bead], self._cwd)

    def issue_note(self, bead: str, note: str) -> Any:
        return self._bd.run(["note", bead, note], self._cwd)

    def issue_set_state(
        self,
        bead: str,
        state: str,
        *,
        reason: str,
        actor: str = "",
        capture: bool = False,
    ) -> Any:
        return self._bd.run(
            ["set-state", bead, state, "--reason", reason],
            self._cwd,
            actor=actor,
            capture=capture,
        )

    def issue_add_label(self, bead: str, label: str, *, actor: str = "") -> Any:
        return self._bd.run(["label", "add", bead, label], self._cwd, actor=actor)

    def issue_remove_label(self, bead: str, label: str, *, actor: str = "") -> Any:
        return self._bd.run(["label", "remove", bead, label], self._cwd, actor=actor)

    def issue_set_external_ref(
        self, bead: str, external_ref: str, *, actor: str = "", capture: bool = False
    ) -> Any:
        return self._bd.run(
            ["update", bead, "--external-ref", external_ref],
            self._cwd,
            actor=actor,
            capture=capture,
        )

    def issue_set_metadata(self, bead: str, value: str, *, capture: bool = False) -> Any:
        return self._bd.run(["update", bead, "--set-metadata", value], self._cwd, capture=capture)

    def issue_update_metadata(self, bead: str, payload: str, *, capture: bool = False) -> Any:
        return self._bd.run(["update", bead, "--metadata", payload], self._cwd, capture=capture)

    def dependency_add(self, source: str, target: str, type_: str) -> Any:
        return self._bd.run(["dep", "add", source, target, "-t", type_], self._cwd)

    def dependency_remove(self, source: str, target: str) -> Any:
        return self._bd.run(["dep", "remove", source, target], self._cwd)

    def dependency_list(self, bead: str, *, direction: str, type_: str) -> Any:
        return self._bd.json(
            ["dep", "list", bead, "--direction", direction, "--type", type_], self._cwd
        )

    def swarm_list(self, *, strict: bool = False) -> Any:
        if strict:
            return self._bd.json(["swarm", "list"], self._cwd, strict=True)
        return self._bd.json(["swarm", "list"], self._cwd)

    def swarm_status(self, epic: str, *, strict: bool = False) -> Any:
        if strict:
            return self._bd.json(["swarm", "status", epic], self._cwd, strict=True)
        return self._bd.json(["swarm", "status", epic], self._cwd)

    def comments(self, bead: str) -> Any:
        return self._bd.run(["comments", bead], self._cwd)

    def comment_add(self, bead: str, body: str, *, actor: str = "") -> Any:
        return self._bd.run(["comments", "add", bead, body], self._cwd, actor=actor)

    def import_records(self, payload: str, *, actor: str = "") -> Any:
        return self._bd.run(
            ["import", "-", "--json"],
            self._cwd,
            actor=actor,
            capture=True,
            text_input=payload,
        )

    def github_push_issue(self, bead: str, *, actor: str = "") -> Any:
        return self._bd.run(
            ["github", "push", "--issues", bead], self._cwd, actor=actor, capture=True
        )

    def create_report(
        self,
        title: str,
        issue_type: str,
        labels: str,
        *,
        description: str = "",
        actor: str = "",
    ) -> Any:
        args = ["--json", "create", title, "--type", issue_type, "-l", labels]
        if description:
            args += ["-d", description]
        return self._bd.run(args, self._cwd, actor=actor, capture=True)

    def find_duplicates(self, *, threshold: float, method: str) -> Any:
        return self._bd.json(
            ["find-duplicates", "--threshold", str(threshold), "--method", method], self._cwd
        )

    def public_snapshot_export(self, output: Path) -> Any:
        return self._bd.run(public_snapshot_argv(output), self._cwd)

    def merge_slot_create(self) -> Any:
        return self._bd.run(["merge-slot", "create"], self._cwd)

    def merge_slot_check(self) -> Any:
        return self._bd.run(["merge-slot", "check", "--json"], self._cwd, capture=True)

    def merge_slot_acquire(self, holder: str) -> Any:
        return self._bd.run(["merge-slot", "acquire", "--holder", holder], self._cwd)

    def merge_slot_release(self) -> Any:
        return self._bd.run(["merge-slot", "release"], self._cwd)
