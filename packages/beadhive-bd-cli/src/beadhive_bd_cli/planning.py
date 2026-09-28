"""The planning cohort's ``bd`` routes (bh-sy36q.2; moved out of the root shell by bh-o3xuf).

* :class:`CliMoleculeFiler` — ``plan.batch-apply.atomic``'s CLI-compatibility route, selected by
  the shell only when no capable Beads session opens and its ``work.beads.route`` allows it.
* :class:`CliPlanningGates` — ``plan.gate.create`` / ``plan.kickoff.update``: v1.3 has no HTTP
  route for either, so they are always over ``bd``. The ONE authoritative code path for the
  kickoff-gate contract: `bh plan file` and `bh plan repair` both call these same methods, so the
  gate description format cannot drift between the two.
"""

from __future__ import annotations

from typing import Any

from beadhive_core import FilingOutcome, MoleculeFilingFailed
from beads_v1_3.types import UNSET

from .transport import BdTransport

__all__ = [
    "KICKOFF_REASON",
    "RELEASE_HOLD_MARKER",
    "CliMoleculeFiler",
    "CliPlanningGates",
]

#: The kickoff-gate contract's authoritative marker (see ``create_kickoff_gate``). Matched by the
#: shell's ``plan._names_kickoff_for`` — the description format must never drift from this literal.
KICKOFF_REASON = "kickoff {epic_id}"
#: The release-hold gate's authoritative marker (mirrored, read-side, in
#: ``beadhive_core.review.Gate.is_release_hold``).
RELEASE_HOLD_MARKER = "release-hold:"


class CliMoleculeFiler:
    """``plan.batch-apply.atomic``'s CLI-compatibility route.

    Walks the SAME compiled item list ``beadhive_core.SessionMoleculeFiler`` would submit in one
    HTTP request, one ``bd create`` / ``bd dep add`` at a time — the compiler decided what the
    molecule lowers to; this only interprets it over a different transport. Selected only when
    the hive's Beads service cannot be reached, never as a retry after a failed API call, so a
    failure here is reported (never silently emulated as atomic): a caller that lands on this
    route and fails partway has genuinely partial state, exactly as the pre-BatchApply
    implementation did.
    """

    route = "cli-compatibility"

    def __init__(self, bd: BdTransport, main: Any) -> None:
        self._bd = bd
        self._main = main

    def apply(self, compiled: Any, *, actor: str) -> FilingOutcome:
        ids: dict[str, str] = {}

        def resolved(ref: Any) -> str:
            # `Unset.__bool__` is False, so a plain truthiness check already excludes an unset
            # `ref.id` exactly like an explicit `is not UNSET` would.
            if ref.id:
                return str(ref.id)
            return ids[ref.key]

        for item in compiled.items:
            if item.kind.value == "create":
                new_id = self._create(item.create, actor)
                if item.create.key:
                    ids[item.create.key] = new_id
            else:
                dep = item.dep_add
                source_id, target_id = resolved(dep.source), resolved(dep.target)
                result = self._bd.run(
                    ["dep", "add", source_id, target_id, "-t", dep.type_], self._main, actor=actor
                )
                if result.returncode != 0:
                    raise MoleculeFilingFailed(
                        f"bd dep add {source_id} {target_id} -t {dep.type_} failed"
                    )
        return FilingOutcome(ids)

    def _create(self, create: Any, actor: str) -> str:
        args = [str(create.title)]
        if create.issue_type:
            args += ["--type", str(create.issue_type)]
        # `priority` may legitimately be 0 (P0/critical) — falsy but SET — so this is the one
        # field that needs the real sentinel rather than a truthiness check.
        if create.priority is not UNSET:
            args += ["-p", str(create.priority)]
        if create.description:
            args += ["-d", str(create.description)]
        if create.design:
            args += ["--design", str(create.design)]
        if create.acceptance_criteria:
            args += ["--acceptance", str(create.acceptance_criteria)]
        if create.external_ref:
            args += ["--external-ref", str(create.external_ref)]
        if create.labels:
            args += ["-l", ",".join(create.labels)]
        result = self._bd.run(["create", *args, "--silent"], self._main, actor=actor, capture=True)
        new_id = (result.stdout or "").strip().splitlines()[-1].strip() if result.stdout else ""
        if result.returncode != 0 or not new_id:
            raise MoleculeFilingFailed(
                f"bd create failed ({(result.stderr or '').strip() or 'no id returned'})"
            )
        return new_id


class CliPlanningGates:
    """``plan.gate.create`` / ``plan.kickoff.update`` over ``bd`` — v1.3 has no HTTP route for
    either. See the module docstring for why this is the one kickoff-gate implementation."""

    def __init__(self, bd: BdTransport, main: Any) -> None:
        self._bd = bd
        self._main = main

    def create_swarm(self, epic_id: str, *, actor: str) -> bool:
        """``bd swarm create <epic>``; True on success."""
        return self._bd.run(["swarm", "create", epic_id], self._main, actor=actor).returncode == 0

    def create_kickoff_gate(self, root_id: str, epic_id: str, *, actor: str) -> None:
        """Open THE kickoff gate for one molecule root: a human gate blocking ``root_id`` whose
        description carries the literal ``kickoff <epic_id>`` marker (the shell's
        ``plan._names_kickoff_for`` matches on exactly this pair)."""
        self._bd.run(
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                root_id,
                "--reason",
                KICKOFF_REASON.format(epic_id=epic_id),
            ],
            self._main,
            actor=actor,
        )

    def set_kickoff_pending(self, epic_id: str, *, actor: str) -> None:
        """Stamp the epic ``kickoff=pending`` — filed/repaired, awaiting `plan approve`."""
        self._bd.run(
            ["set-state", epic_id, "kickoff=pending", "--reason", "awaiting kickoff approval"],
            self._main,
            actor=actor,
        )

    def create_release_hold_gate(self, bead_id: str, epic_id: str, *, actor: str) -> None:
        """Open THE release-hold gate for one ``release:breaking`` bead (bh-k2j8.5,
        ``release.enforce_hold`` on): a human gate blocking ``bead_id`` whose reason carries the
        ``release-hold:`` marker + epic for the selector."""
        self._bd.run(
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                bead_id,
                "--reason",
                f"{RELEASE_HOLD_MARKER} {epic_id} — release:breaking held for release",
            ],
            self._main,
            actor=actor,
        )
