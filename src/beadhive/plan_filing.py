"""Top-level adapter selecting the ``beadhive_core`` molecule-filing handler (bh-sy36q.2).

The one composition seam for `bh plan file`'s (and `plan_file`'s MCP tool) mutating half: compile
the validated molecule spec ONCE (:func:`beadhive_core.planning.compile_molecule`) and submit it
through whichever route is selected BEFORE the first Beads operation, never as a retry after one
fails — the same rule :mod:`beadhive.work_queue` (bh-l5sxi.2) and :mod:`beadhive.work_lifecycle`
(bh-sy36q.1) already use:

* **``plan.batch-apply.atomic`` (api-ready)** when the hive's one supervised Beads v1.3 service
  (``bh host beads``) can be reached: :class:`SessionMoleculeFiler` submits the compiled request
  and resolves its key-to-id map in ONE HTTP call.
* **cli-compatibility** otherwise (an embedded-Dolt hive Beads 1.3 cannot serve, no service
  running, a missing capability): :class:`CliMoleculeFiler` walks the identical compiled item
  list one ``bd create`` / ``bd dep add`` at a time. It is a thin interpreter of the SAME
  :class:`~beadhive_core.planning.CompiledMolecule`, not a second implementation of the molecule
  contract — the compiler is the one place that decides what a spec lowers to, on both routes.

The molecule's gate/kickoff/swarm conventions (``plan.gate.create`` / ``plan.kickoff.update``,
always cli-compatibility — v1.3 has no HTTP route for either) are :class:`CliPlanningGates`, the
SAME ``bd`` calls `bh plan repair` shares (``_create_swarm`` / ``_create_kickoff_gate`` /
``_set_kickoff_pending`` moved here from :mod:`beadhive.plan` so both callers share one
implementation of the kickoff-gate contract, never two that could drift — see this module's
``create_kickoff_gate`` docstring).

``beadhive_core`` is resolved lazily by name; ``src/beadhive`` never imports a workspace package
statically (``scripts/check_package_imports.py``).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import adopt, bd, beads_routing, config, log, molecule, registry
from .identity import workspace_identity
from .modules.planning import PlanningError

_CORE_MODULE = "beadhive_core"


def _unset() -> Any:
    """The ``beads_v1_3.types.UNSET`` sentinel, imported lazily: ``src/beadhive`` must import
    without ``beadhive-beads-client`` installed (``tests/test_demo_live_ingress_matrix.py``'s
    no-venv parity check), and this module's own top-level import chain (``beadhive.cli`` ->
    ``beadhive.plan`` -> here) must not force that dependency just to define the CLI-
    compatibility molecule-filing fallback, which only runs when filing actually happens."""
    return importlib.import_module("beads_v1_3.types").UNSET


#: The kickoff-gate contract's authoritative marker (see ``create_kickoff_gate``). Matched by
#: ``plan._names_kickoff_for`` — the description format must never drift from this literal.
_KICKOFF_REASON = "kickoff {epic_id}"
#: The release-hold gate's authoritative marker (mirrored, read-side, in
#: ``beadhive_core.review.Gate.is_release_hold``).
_RELEASE_HOLD_MARKER = "release-hold:"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


class TelemetryRoutingObserver:
    def selected(self, name: str, kind: str) -> None:
        log.get_logger("beadhive.plan").info("route_selected", operation=name, route=kind)


# ---- named CLI-compatibility routes ------------------------------------------------------------


class CliMoleculeFiler:
    """``plan.batch-apply.atomic``'s CLI-compatibility fallback.

    Walks the SAME compiled item list :class:`SessionMoleculeFiler` would submit in one HTTP
    request, one ``bd create`` / ``bd dep add`` at a time — the compiler decided what the
    molecule lowers to; this only interprets it over a different transport. Selected only when
    the hive's Beads service cannot be reached, never as a retry after a failed API call, so a
    failure here is reported (never silently emulated as atomic): a caller that lands on this
    route and fails partway has genuinely partial state, exactly as the pre-BatchApply
    implementation did.
    """

    route = "cli-compatibility"

    def __init__(self, main: Path) -> None:
        self._main = main

    def apply(self, compiled: Any, *, actor: str) -> Any:
        core = _core()
        ids: dict[str, str] = {}

        def resolved(ref: Any) -> str:
            # `Unset.__bool__` is False, so a plain truthiness check already excludes an unset
            # `ref.id` exactly like an explicit `is not UNSET` would — no import needed here.
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
                result = bd.run(
                    ["dep", "add", source_id, target_id, "-t", dep.type_], self._main, actor=actor
                )
                if result.returncode != 0:
                    raise core.MoleculeFilingFailed(
                        f"bd dep add {source_id} {target_id} -t {dep.type_} failed"
                    )
        return core.FilingOutcome(ids)

    def _create(self, create: Any, actor: str) -> str:
        core = _core()
        args = [str(create.title)]
        if create.issue_type:
            args += ["--type", str(create.issue_type)]
        # `priority` may legitimately be 0 (P0/critical) — falsy but SET — so this is the one
        # field that needs the real sentinel rather than a truthiness check.
        if create.priority is not _unset():
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
        result = bd.run(["create", *args, "--silent"], self._main, actor=actor, capture=True)
        new_id = (result.stdout or "").strip().splitlines()[-1].strip() if result.stdout else ""
        if result.returncode != 0 or not new_id:
            raise core.MoleculeFilingFailed(
                f"bd create failed ({(result.stderr or '').strip() or 'no id returned'})"
            )
        return new_id


class CliPlanningGates:
    """``plan.gate.create`` / ``plan.kickoff.update`` over ``bd`` — v1.3 has no HTTP route for
    either. The ONE authoritative code path for the kickoff-gate contract: `bh plan file` and
    `bh plan repair` (:mod:`beadhive.plan`'s ``_repair_epic``) both call these same methods, so
    the gate description format cannot drift between the two — see ``create_kickoff_gate``."""

    def __init__(self, main: Path) -> None:
        self._main = main

    def create_swarm(self, epic_id: str, *, actor: str) -> bool:
        """``bd swarm create <epic>``; True on success."""
        return bd.run(["swarm", "create", epic_id], self._main, actor=actor).returncode == 0

    def create_kickoff_gate(self, root_id: str, epic_id: str, *, actor: str) -> None:
        """Open THE kickoff gate for one molecule root: a human gate blocking ``root_id`` whose
        description carries the literal ``kickoff <epic_id>`` marker
        (:mod:`beadhive.plan`'s ``_names_kickoff_for`` matches on exactly this pair)."""
        bd.run(
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                root_id,
                "--reason",
                _KICKOFF_REASON.format(epic_id=epic_id),
            ],
            self._main,
            actor=actor,
        )

    def set_kickoff_pending(self, epic_id: str, *, actor: str) -> None:
        """Stamp the epic ``kickoff=pending`` — filed/repaired, awaiting `plan approve`."""
        bd.run(
            ["set-state", epic_id, "kickoff=pending", "--reason", "awaiting kickoff approval"],
            self._main,
            actor=actor,
        )

    def create_release_hold_gate(self, bead_id: str, epic_id: str, *, actor: str) -> None:
        """Open THE release-hold gate for one ``release:breaking`` bead (bh-k2j8.5,
        ``release.enforce_hold`` on): a human gate blocking ``bead_id`` whose reason carries the
        ``release-hold:`` marker + epic for the selector."""
        bd.run(
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                bead_id,
                "--reason",
                f"{_RELEASE_HOLD_MARKER} {epic_id} — release:breaking held for release",
            ],
            self._main,
            actor=actor,
        )


# ---- route selection ---------------------------------------------------------------------------


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session for this cohort's ``PLANNING_CAPABILITIES`` — see
    :func:`beadhive.beads_routing.hive_session` (the one composition decision, including the
    ``BH_BEADS_ROUTE=cli`` rollback)."""
    return beads_routing.hive_session(main, entry, _core().PLANNING_CAPABILITIES)


#: The session seam: ``(main, entry)`` -> an unopened ``BeadsSession``. Tests substitute a
#: transport-fixture session here; production resolves the hive's supervised service.
session_factory = hive_session


def _unavailable_errors() -> tuple[type[BaseException], ...]:
    return (*beads_routing.unavailable_errors(), OSError, ValueError)


@contextmanager
def _filer(main: Path, entry: Any) -> Iterator[Any]:
    """The ``MoleculeFiler`` port, its route selected before the first Beads operation.

    Only OPENING the session is inside the fallback's try/except (mirrors
    ``work_lifecycle.SelectedIssues``): once a route is selected, using it is outside any
    exception mapping, so a genuine failure from the caller's own filing call is never
    misdiagnosed as "service unavailable" and silently rerouted mid-command.
    """
    core = _core()
    try:
        session = session_factory(main, entry)
        session.open()
    except _unavailable_errors() as exc:
        log.get_logger("beadhive.plan").info(
            "planning_route_fallback", operation=core.BATCH_APPLY_ROUTE, detail=str(exc)
        )
        yield CliMoleculeFiler(main)
        return
    try:
        yield core.SessionMoleculeFiler(session, observer=TelemetryRoutingObserver())
    finally:
        session.close()


# ---- composition --------------------------------------------------------------------------------


def identity_labels(cwd: Path) -> tuple[str, ...]:
    """The provider/org/repo identity labels for ``cwd``, as INDIVIDUAL entries (BatchApply's
    ``labels`` is the complete authoritative list, unlike ``bd create``'s comma-joined ``-l``
    flag — no comma-splitting hack is needed here)."""
    ident = workspace_identity(cwd)
    return (
        ()
        if ident is None
        else tuple(f"{k}:{v}" for k, v in zip(("provider", "org", "repo"), ident, strict=True))
    )


def _dimension_labels(item: dict, dimension_fields: tuple[str, ...]) -> list[str]:
    return [
        f"{field}:{item[field]}" for field in dimension_fields if item.get(field) not in (None, "")
    ]


def import_epic(epic: dict, dimension_fields: tuple[str, ...], cwd: Path, actor: str) -> str:
    """Birth an epic carrying native ``source_system`` provenance via ``bd import`` — the
    sanctioned path for a field settable only at bead creation (no ``bd create``/``update`` flag
    exists). ``ApplyCreateItem`` has no member for it either, so this is the one piece of filing
    BatchApply genuinely cannot express — every other molecule primitive (child issues, the
    parent-child and declared-dependency edges, any adopted-report link) still compiles into the
    ONE BatchApply request that follows, addressing this epic by the id returned here rather than
    a request-local key. Only called when ``adopt.provenance_of(epic)`` names a ``source_system``.
    """
    labels = _dimension_labels(epic, dimension_fields) + list(identity_labels(cwd))
    record = adopt.epic_import_record(epic, labels)
    result = bd.run(
        ["import", "-", "--json"], cwd, actor=actor, capture=True, text_input=json.dumps(record)
    )
    data = json.loads(result.stdout or "null") if result.returncode == 0 and result.stdout else None
    ids = data.get("ids") if isinstance(data, dict) else None
    if result.returncode != 0 or not ids:
        raise PlanningError(
            f"bd import failed ({(result.stderr or '').strip() or 'no id returned'})"
        )
    return str(ids[0])


def file(spec: dict, cwd: Path, actor: str, cfg: Any) -> Any:
    """File one validated molecule spec: compile once, submit once, then open the gate/kickoff/
    swarm conventions the compiled request could not carry. Returns a ``beadhive_core.FileOutcome``.

    Raises :class:`~beadhive.modules.planning.PlanningError` (``plan.py``'s ``PlanError``) on any
    refusal — the compiled request over the 100-item cap, a refused/indeterminate BatchApply
    submission, or a failed gate/kickoff/swarm convention — so the CLI and MCP callers' existing
    ``except PlanError`` handling covers this path unchanged.
    """
    core = _core()
    entry = registry.entry_for_dir(cfg, cwd)
    epic = spec["epic"]
    issues = spec.get("issues") or ()
    dimension_fields = molecule.DIMENSION_FIELDS
    source_system, external_ref = adopt.provenance_of(epic)
    epic_id = import_epic(epic, dimension_fields, cwd, actor) if source_system else None
    release_breaking_handles = (
        tuple(
            str(issue["handle"])
            for issue in issues
            if str(issue.get("release") or "") == "breaking"
        )
        if config.release_enforce_hold(cfg, entry)
        else ()
    )
    try:
        with _filer(cwd, entry) as filer:
            commands = core.PlanningCommands(filer, CliPlanningGates(cwd))
            return commands.file(
                spec,
                actor=actor,
                dimension_fields=dimension_fields,
                identity_labels=identity_labels(cwd),
                epic_id=epic_id,
                epic_external_ref="" if epic_id else external_ref,
                adopted_reports=tuple(adopt.adopts_of(epic)),
                release_breaking_handles=release_breaking_handles,
            )
    except (core.PlanningError, core.MoleculeFilingFailed, core.GateCreateFailed) as exc:
        raise PlanningError(str(exc)) from exc
